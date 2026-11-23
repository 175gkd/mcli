import json

import pytest

from mcli.config import ConfigError, load_servers


def _write(tmp_path, obj):
    p = tmp_path / "mcp_servers.json"
    p.write_text(json.dumps(obj))
    return p


def test_load_stdio(tmp_path):
    p = _write(tmp_path, {"mcpServers": {
        "fs": {"command": "npx", "args": ["-y", "srv"], "env": {"A": "b"}}}})
    s = load_servers(p)["fs"]
    assert s.transport == "stdio" and s.command == "npx" and s.args == ["-y", "srv"]


def test_load_http_transports(tmp_path):
    p = _write(tmp_path, {"mcpServers": {
        "a": {"url": "http://x/sse"},
        "b": {"url": "http://x/mcp"},
        "c": {"url": "http://x/api", "type": "sse"},
    }})
    s = load_servers(p)
    assert s["a"].transport == "sse"
    assert s["b"].transport == "streamable-http"
    assert s["c"].transport == "sse"


def test_invalid_entries(tmp_path):
    p = _write(tmp_path, {"mcpServers": {"bad": {"foo": 1}}})
    with pytest.raises(ConfigError, match="neither 'command'"):
        load_servers(p)
    p2 = _write(tmp_path, {"mcpServers": {}})
    with pytest.raises(ConfigError):
        load_servers(p2)
