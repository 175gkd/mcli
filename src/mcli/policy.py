"""Tool-level read/write authorization policy engine for mcli.

Decision precedence (highest first):
  1. param_guards deny_regex against serialized arguments -> deny
  2. explicit rules: deny -> ask -> allow (fnmatch on tool name)
  3. MCP annotations fallback: readOnlyHint -> allow, destructiveHint -> ask
  4. default_action (default: ask)
"""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path
from typing import Any

ALLOW, ASK, DENY = "allow", "ask", "deny"

TEMPLATE = {
    "default_action": "ask",
    "rules": {
        "example-server": {
            "allow": ["list_*", "get_*", "search_*", "read_*"],
            "ask": ["create_*", "update_*", "add_*"],
            "deny": ["delete_*", "merge_*"],
            "param_guards": {
                "query": {"deny_regex": "(?i)\\b(DROP|TRUNCATE|DELETE|ALTER)\\b"}
            },
        }
    },
}


class Policy:
    def __init__(self, data: dict):
        self.default_action = data.get("default_action") or ASK
        if self.default_action not in (ALLOW, ASK, DENY):
            self.default_action = ASK
        self.rules: dict[str, dict] = data.get("rules") or {}

    @classmethod
    def load(cls, path: Path) -> "Policy":
        """Load policy; if missing, write a template and return it."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            try:
                with path.open("x", encoding="utf-8") as template:
                    json.dump(TEMPLATE, template, indent=2, ensure_ascii=False)
            except FileExistsError:
                return cls.load(path)
            return cls({})
        except ValueError as exc:
            raise ValueError(f"{path}: invalid policy JSON: {exc}") from exc
        try:
            cls._validate(data)
        except ValueError as exc:
            raise ValueError(f"{path}: {exc}") from exc
        return cls(data)

    @staticmethod
    def _validate(data: dict) -> None:
        if not isinstance(data, dict):
            raise ValueError("policy must be an object")
        if set(data) - {"default_action", "rules"}:
            raise ValueError("policy accepts only default_action and rules")
        if data.get("default_action", ASK) not in (ALLOW, ASK, DENY):
            raise ValueError("default_action must be allow, ask or deny")
        rules = data.get("rules", {})
        if not isinstance(rules, dict):
            raise ValueError("rules must be an object")
        for server, rule in rules.items():
            prefix = f"rules.{server}"
            if not isinstance(rule, dict):
                raise ValueError(f"{prefix} must be an object")
            if set(rule) - {ALLOW, ASK, DENY, "param_guards"}:
                raise ValueError(f"{prefix} has an unknown rule field")
            for action in (ALLOW, ASK, DENY):
                patterns = rule.get(action, [])
                if not isinstance(patterns, list) or not all(
                    isinstance(pattern, str) for pattern in patterns
                ):
                    raise ValueError(f"{prefix}.{action} must be an array of strings")
            guards = rule.get("param_guards", {})
            if not isinstance(guards, dict):
                raise ValueError(f"{prefix}.param_guards must be an object")
            for tool, guard in guards.items():
                field = f"{prefix}.param_guards.{tool}.deny_regex"
                if not isinstance(guard, dict) or set(guard) != {"deny_regex"}:
                    raise ValueError(f"{field} is required")
                pattern = guard["deny_regex"]
                if not isinstance(pattern, str) or not pattern:
                    raise ValueError(f"{field} must be a non-empty string")
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"{field}: {exc}") from exc

    def _server_rules(self, server: str) -> dict:
        return self.rules.get(server) or {}

    @staticmethod
    def _match_any(name: str, patterns) -> bool:
        return any(fnmatch.fnmatchcase(name, p) for p in patterns or [])

    def evaluate(
        self,
        server: str,
        tool: str,
        arguments: dict | None = None,
        annotations: dict | None = None,
    ) -> str:
        rules = self._server_rules(server)

        # 1. param-level regex guards
        guards = rules.get("param_guards") or {}
        args_str = json.dumps(arguments or {}, sort_keys=True, default=str)
        for pattern, guard in guards.items():
            if not fnmatch.fnmatchcase(tool, pattern):
                continue
            deny_re = (guard or {}).get("deny_regex")
            if deny_re and re.search(deny_re, args_str):
                return DENY

        # 2. explicit rules: deny -> ask -> allow
        if self._match_any(tool, rules.get("deny")):
            return DENY
        if self._match_any(tool, rules.get("ask")):
            return ASK
        if self._match_any(tool, rules.get("allow")):
            return ALLOW

        # 3. MCP annotations fallback
        if annotations:
            if annotations.get("destructiveHint"):
                return ASK
            if annotations.get("readOnlyHint"):
                return ALLOW

        # 4. global default
        return self.default_action


def find_policy_path(config_path: Path) -> Path:
    return config_path.parent / "mcli_policy.json"
