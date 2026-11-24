import json

import pytest

from mcli.policy import ALLOW, ASK, DENY, Policy


def make_policy(**overrides):
    data = {
        "default_action": "ask",
        "rules": {
            "github": {
                "allow": ["list_*", "get_*", "search_*"],
                "ask": ["create_*", "update_*"],
                "deny": ["delete_*", "merge_*"],
            },
            "postgres": {
                "allow": ["list_tables"],
                "ask": ["query"],
                "param_guards": {
                    "query": {
                        "deny_regex": r"(?i)\b(DROP|TRUNCATE|DELETE|ALTER)\b"
                    }
                },
            },
        },
    }
    data.update(overrides)
    return Policy(data)


def test_explicit_allow():
    p = make_policy()
    assert p.evaluate("github", "list_issues") == ALLOW
    assert p.evaluate("github", "get_repo") == ALLOW


def test_deny_beats_allow_and_ask():
    p = make_policy()
    assert p.evaluate("github", "delete_repo") == DENY
    assert p.evaluate("github", "merge_pr") == DENY
    # deny has priority even if a broader allow pattern would match
    p2 = Policy({"rules": {"s": {"allow": ["*"], "deny": ["nuke_*"]}}})
    assert p2.evaluate("s", "nuke_all") == DENY
    assert p2.evaluate("s", "read_x") == ALLOW


def test_ask_rule():
    p = make_policy()
    assert p.evaluate("github", "create_issue") == ASK


def test_param_guard_regex_deny():
    p = make_policy()
    assert p.evaluate("postgres", "query", {"sql": "SELECT 1"}) == ASK
    assert p.evaluate("postgres", "query", {"sql": "DROP TABLE users"}) == DENY
    assert p.evaluate("postgres", "query",
                      {"sql": "delete from t where id=1"}) == DENY
    # guard must not leak to other tools/servers
    assert p.evaluate("github", "create_issue",
                      {"title": "DROP TABLE"}) == ASK


def test_annotations_fallback():
    p = make_policy()
    # no explicit rule match for 'mystery' -> annotation decides
    assert p.evaluate("github", "mystery",
                      annotations={"readOnlyHint": True}) == ALLOW
    assert p.evaluate("github", "mystery",
                      annotations={"destructiveHint": True}) == ASK


def test_default_action():
    p = make_policy()
    assert p.evaluate("github", "mystery") == ASK
    p2 = make_policy(default_action="allow")
    assert p2.evaluate("github", "mystery") == ALLOW
    # invalid default falls back to ask
    p3 = make_policy(default_action="bogus")
    assert p3.evaluate("github", "mystery") == ASK


def test_load_creates_template(tmp_path):
    path = tmp_path / "mcli_policy.json"
    p = Policy.load(path)
    assert path.is_file()
    data = json.loads(path.read_text())
    assert "default_action" in data and "rules" in data
    assert p.default_action == "ask"


def test_load_broken_file_rejected(tmp_path):
    path = tmp_path / "mcli_policy.json"
    path.write_text("{not json")
    with pytest.raises(ValueError, match="invalid policy JSON"):
        Policy.load(path)
