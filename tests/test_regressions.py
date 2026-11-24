import asyncio
import io
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from mcli.cli import DaemonClient, _emit_result, state_dir
from mcli.config import ServerConfig
from mcli.daemon import ServerWorker, make_app
from mcli.policy import Policy
from mcli.schema_parser import CliError, parse_tool_args


def test_json_validates_nested_types():
    schema = {"type": "object", "properties": {
        "items": {"type": "array", "items": {"type": "integer"}}
    }}
    with pytest.raises(CliError, match="items"):
        parse_tool_args(["--stdin"], schema, stdin_reader=io.StringIO('{"items":["x"]}').read)


def test_mcp_error_is_one_line(capsys):
    assert _emit_result({"isError": True, "content": [
        {"type": "text", "text": "bad\ninput"},
        {"type": "text", "text": "try again"},
    ]}) == 1
    output = capsys.readouterr()
    assert not output.out
    assert output.err.count("\n") == 1


def test_policy_rejects_invalid_regex(tmp_path):
    path = tmp_path / "mcli_policy.json"
    path.write_text('{"rules":{"mock":{"param_guards":{"add":{"deny_regex":"["}}}}}')
    with pytest.raises(ValueError, match="deny_regex"):
        Policy.load(path)


def test_distinct_configs_have_distinct_state(tmp_path, monkeypatch):
    monkeypatch.setenv("MCLI_STATE_DIR", str(tmp_path / "state"))
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    for path in (first, second):
        path.write_text("{}")
    monkeypatch.setenv("MCLI_CONFIG", str(first))
    left = state_dir()
    monkeypatch.setenv("MCLI_CONFIG", str(second))
    assert state_dir() != left


def test_failed_start_can_reconnect(monkeypatch):
    async def run():
        worker = ServerWorker(ServerConfig("mock", "stdio"))
        attempts = 0

        async def connect(stack):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("offline")

            async def list_tools():
                return SimpleNamespace(tools=[])

            return SimpleNamespace(list_tools=list_tools)

        monkeypatch.setattr(worker, "_connect", connect)
        monkeypatch.setattr("mcli.daemon.log", lambda message: None)
        try:
            with pytest.raises(RuntimeError, match="offline"):
                await worker.ensure_started()
            assert not worker.connected
            await worker.ensure_started()
            assert worker.connected
        finally:
            await worker.stop()

    asyncio.run(run())


def test_transport_failure_does_not_replay_call(monkeypatch):
    async def run():
        worker = ServerWorker(ServerConfig("mock", "stdio"))
        calls = []
        connections = 0

        async def connect(stack):
            nonlocal connections
            connections += 1

            async def list_tools():
                return SimpleNamespace(tools=[])

            async def call_tool(tool, arguments):
                calls.append(arguments)
                if connections == 1:
                    raise OSError("connection lost")
                return "ok"

            return SimpleNamespace(list_tools=list_tools, call_tool=call_tool)

        monkeypatch.setattr(worker, "_connect", connect)
        try:
            await worker.ensure_started()
            with pytest.raises(OSError, match="connection lost"):
                await worker.call("change", {"id": 1}, 1, worker.tools)
            assert connections == 1
            await worker.ensure_started()
            assert await worker.call("change", {"id": 2}, 1, worker.tools) == "ok"
            assert connections == 2
            assert calls == [{"id": 1}, {"id": 2}]
        finally:
            await worker.stop()

    asyncio.run(run())


def test_cli_and_local_daemon(tmp_path):
    mock = Path(__file__).with_name("mock_server.py").resolve()
    config = {"mcpServers": {
        "mock": {"command": sys.executable, "args": [str(mock)]},
        "offline": {"command": str(tmp_path / "missing-command")},
    }}
    config_path = tmp_path / "mcp_servers.json"
    config_path.write_text(json.dumps(config))
    (tmp_path / "mcli_policy.json").write_text(json.dumps({
        "rules": {"mock": {"allow": ["add", "fail"], "deny": ["echo"]}},
    }))
    env = {**os.environ, "MCLI_CONFIG": str(config_path),
           "MCLI_STATE_DIR": str(tmp_path / "state")}

    def cli(*args, input_text=None):
        return subprocess.run(
            [sys.executable, "-m", "mcli.cli", *args],
            input=input_text, env=env, capture_output=True, timeout=30,
            encoding="utf-8",
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            listings = list(executor.map(lambda _: cli("ls"), range(2)))
        for listing in listings:
            assert listing.returncode == 0, listing.stderr
            assert "mock\tstdio\tconnected" in listing.stdout
            assert "offline\tstdio\terror:" in listing.stdout
        metadata_path, = (tmp_path / "state").glob("*/daemon.port")
        metadata = json.loads(metadata_path.read_text())
        with pytest.raises(CliError, match="authentication required"):
            DaemonClient(metadata["port"], "wrong-token").request("POST", "/shutdown", {})
        result = cli("mock", "add", "--stdin", input_text='{"a": 2, "b": 3}')
        assert (result.returncode, result.stdout, result.stderr) == (0, "5\n", "")
        denied = cli("mock", "echo", "--text", "test")
        assert (denied.returncode, denied.stdout) == (13, "")
        assert denied.stderr.startswith("[PERMISSION_DENIED]")
        failure = cli("mock", "fail", "--msg", "first\nsecond")
        assert (failure.returncode, failure.stdout) == (1, "")
        assert failure.stderr.count("\n") == 1
        assert "Error executing tool fail" in failure.stderr
        restarted = cli("daemon", "restart")
        assert restarted.returncode == 0, restarted.stderr
        assert cli("mock", "add", "--a", "7").stdout == "7\n"
    finally:
        stopped = cli("daemon", "stop")
        assert stopped.returncode == 0, stopped.stderr


@pytest.mark.parametrize("approved", [True, False])
def test_ask_reaches_mcp_only_with_approval(monkeypatch, approved):
    async def run():
        invocations = []
        tools = [{"name": "change"}]

        async def confirm(server, tool, arguments):
            invocations.append(("confirm", server, tool, arguments))
            return approved

        async def ensure_tools(server):
            return tools

        async def call(server, tool, arguments, timeout, tool_metadata):
            invocations.append(("call", server, tool, arguments))
            assert tool_metadata is tools
            return SimpleNamespace(model_dump=lambda **_: {"content": []})

        monkeypatch.setattr("mcli.daemon.confirm", confirm)
        pool = SimpleNamespace(workers={"mock": object()}, ensure_tools=ensure_tools, call=call)
        app = make_app(pool, 0, Policy({}), "test-token")
        arguments = {"value": "x"}
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/call", headers={"Authorization": "Bearer test-token"},
                json={"server": "mock", "tool": "change", "arguments": arguments},
            )
            assert response.status == (200 if approved else 403)
            if not approved:
                assert (await response.json())["code"] == "PERMISSION_DENIED"
        expected = [("confirm", "mock", "change", arguments)]
        if approved:
            expected.append(("call", "mock", "change", arguments))
        assert invocations == expected

    asyncio.run(run())
