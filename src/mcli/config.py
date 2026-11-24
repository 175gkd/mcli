"""Loading and validation of mcp_servers.json (Claude Desktop compatible)."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    """Raised when the MCP server config is missing or invalid."""


@dataclass
class ServerConfig:
    name: str
    transport: str  # "stdio" | "sse" | "streamable-http"
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None


def default_config_candidates() -> list[Path]:
    candidates = []
    if env_path := os.environ.get("MCLI_CONFIG"):
        candidates.append(Path(env_path))
    candidates.append(Path.cwd() / "mcp_servers.json")
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    candidates.append(base / "mcli" / "mcp_servers.json")
    return candidates


def find_config_path() -> Path:
    if explicit := os.environ.get("MCLI_CONFIG"):
        path = Path(explicit)
        if not path.is_file():
            raise ConfigError(f"MCLI_CONFIG does not point to a file: {path}")
        return path
    for p in default_config_candidates():
        if p.is_file():
            return p
    searched = "\n  ".join(str(p) for p in default_config_candidates())
    raise ConfigError(
        f"no mcp_servers.json found; searched:\n  {searched}\n"
        "Create one or set MCLI_CONFIG to its path."
    )


def _infer_transport(name: str, entry: dict) -> ServerConfig:
    if "command" in entry:
        return ServerConfig(
            name=name,
            transport="stdio",
            command=entry["command"],
            args=[str(a) for a in entry.get("args", [])],
            env={str(k): str(v) for k, v in entry.get("env", {}).items()},
            cwd=entry.get("cwd"),
        )
    if "url" in entry:
        url = entry["url"]
        t = entry.get("type") or entry.get("transport")
        if t in ("sse", "streamable-http"):
            transport = t
        else:
            transport = "sse" if url.rstrip("/").endswith("/sse") else "streamable-http"
        return ServerConfig(
            name=name,
            transport=transport,
            url=url,
            headers={str(k): str(v) for k, v in entry.get("headers", {}).items()},
        )
    raise ConfigError(
        f"server '{name}' has neither 'command' (stdio) nor 'url' (sse/streamable-http)"
    )


def load_servers(path: Path | None = None) -> dict[str, ServerConfig]:
    path = path or find_config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path}: invalid JSON: {e}") from e
    servers_raw = raw.get("mcpServers")
    if not isinstance(servers_raw, dict) or not servers_raw:
        raise ConfigError(f"{path}: expected non-empty 'mcpServers' object")
    return {name: _infer_transport(name, entry) for name, entry in servers_raw.items()}
