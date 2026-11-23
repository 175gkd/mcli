"""mcli: thin CLI client. Talks to mcli-daemon over local HTTP; auto-spawns it."""

from __future__ import annotations

import base64
import http.client
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .schema_parser import (
    CliError,
    parse_tool_args,
    render_server_help,
    render_tool_help,
)

CLIENT_TIMEOUT_S = float(os.environ.get("MCLI_CLIENT_TIMEOUT", 120))
SPAWN_WAIT_S = float(os.environ.get("MCLI_SPAWN_WAIT", 20))


def state_dir() -> Path:
    if p := os.environ.get("MCLI_STATE_DIR"):
        return Path(p)
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "mcli"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "mcli"


def port_file() -> Path:
    return state_dir() / "daemon.port"


def pid_file() -> Path:
    return state_dir() / "daemon.pid"


def log_file() -> Path:
    return state_dir() / "daemon.log"


def assets_dir() -> Path:
    if p := os.environ.get("MCLI_ASSETS_DIR"):
        return Path(p)
    return Path(tempfile.gettempdir()) / "mcli_assets"


class PermissionDenied(Exception):
    """Daemon rejected the call via security policy or user denial."""


def eprint(msg: str) -> None:
    print(msg, file=sys.stderr)


def fail(msg: str, code: int = 1) -> "SystemExit":
    eprint(f"mcli: error: {msg}")
    return SystemExit(code)


class DaemonClient:
    def __init__(self, port: int):
        self.port = port

    def request(self, method: str, path: str, body=None, timeout=CLIENT_TIMEOUT_S) -> dict:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.request(method, path,
                         body=json.dumps(body) if body is not None else None,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            data = resp.read()
        except (OSError, http.client.HTTPException) as e:
            raise CliError(f"daemon request failed: {e}")
        finally:
            conn.close()
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            raise CliError(f"daemon returned non-JSON response (status {resp.status})")
        if resp.status >= 400:
            if payload.get("code") == "PERMISSION_DENIED":
                raise PermissionDenied(
                    payload.get("error", "permission denied"))
            raise CliError(payload.get("error", f"daemon error (status {resp.status})"))
        return payload


def _read_port() -> int | None:
    try:
        return int(port_file().read_text().strip())
    except (OSError, ValueError):
        return None


def _daemon_alive() -> int | None:
    port = _read_port()
    if port is None:
        return None
    try:
        DaemonClient(port).request("GET", "/health", timeout=2)
        return port
    except CliError:
        return None


def _spawn_daemon() -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    logf = open(log_file(), "ab")
    kwargs: dict = dict(stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, "-m", "mcli.daemon"], **kwargs)
    logf.close()


def ensure_daemon() -> DaemonClient:
    port = _daemon_alive()
    if port is not None:
        return DaemonClient(port)
    _spawn_daemon()
    deadline = time.monotonic() + SPAWN_WAIT_S
    while time.monotonic() < deadline:
        port = _daemon_alive()
        if port is not None:
            return DaemonClient(port)
        time.sleep(0.25)
    raise CliError(
        f"daemon did not become ready in {SPAWN_WAIT_S}s; check {log_file()}")


def _daemon_pid() -> int | None:
    try:
        return int(pid_file().read_text().strip())
    except (OSError, ValueError):
        return None


def usage() -> str:
    return """usage: mcli <command> [args]

commands:
  mcli ls                          list MCP servers and their tools status
  mcli <server> --help             list tools on a server
  mcli <server> <tool> --help      show tool options (from its inputSchema)
  mcli <server> <tool> [OPTIONS]   call a tool (--key value | --json | --stdin)
  mcli daemon start|stop|status|restart   manage the daemon
"""


def cmd_daemon(action: str) -> int:
    if action == "start":
        if _daemon_alive():
            eprint("mcli-daemon already running")
            return 0
        _spawn_daemon()
        for _ in range(int(SPAWN_WAIT_S / 0.25)):
            if _daemon_alive():
                print("mcli-daemon started")
                return 0
            time.sleep(0.25)
        raise CliError(f"daemon failed to start; check {log_file()}")
    if action == "stop":
        port = _daemon_alive()
        if port is None:
            print("mcli-daemon is not running")
            return 0
        try:
            DaemonClient(port).request("POST", "/shutdown", body={}, timeout=5)
        except CliError:
            pass
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _daemon_alive():
            time.sleep(0.2)
        if _daemon_alive():
            pid = _daemon_pid()
            if pid:
                try:
                    os.kill(pid, 15)
                except OSError:
                    pass
        print("mcli-daemon stopped")
        return 0
    if action == "status":
        port = _daemon_alive()
        if port is None:
            print("mcli-daemon is not running")
            return 1
        info = DaemonClient(port).request("GET", "/health")
        print(f"mcli-daemon running on 127.0.0.1:{port} (uptime {info['uptime']}s)")
        for s in info["servers"]:
            print(f"  {s['name']}: {'connected' if s['connected'] else 'not connected'}")
        return 0
    if action == "restart":
        cmd_daemon("stop")
        return cmd_daemon("start")
    raise CliError(f"unknown daemon action '{action}'; expected start|stop|status|restart")


def cmd_ls(client: DaemonClient) -> int:
    data = client.request("GET", "/servers")
    for s in data["servers"]:
        status = "connected" if s["connected"] else (f"error: {s['error']}" if s["error"] else "down")
        tools = f"{len(s['tools'])} tools" if s["connected"] else "-"
        print(f"{s['name']}\t{s['transport']}\t{status}\t{tools}")
    return 0


def _get_tools(client: DaemonClient, server: str) -> list[dict]:
    return client.request("GET", f"/servers/{server}/tools")["tools"]


def _emit_result(result: dict) -> int:
    content = result.get("content") or []
    is_error = result.get("isError", False)
    out_lines, err_lines = [], []
    for item in content:
        t = item.get("type")
        if t == "text":
            (err_lines if is_error else out_lines).append(item.get("text", ""))
        elif t == "image":
            path = _save_asset(item.get("data", ""), _ext_for(item.get("mimeType", "")))
            out_lines.append(path)
        elif t == "resource":
            res = item.get("resource") or {}
            if res.get("blob"):
                path = _save_asset(res["blob"], _ext_for(res.get("mimeType", "")))
                out_lines.append(path)
            else:
                out_lines.append(res.get("text", json.dumps(item)))
        else:
            out_lines.append(json.dumps(item))
    if is_error:
        for line in err_lines or out_lines:
            eprint(line)
        return 1
    for line in out_lines:
        print(line)
    return 0


def _ext_for(mime: str) -> str:
    return {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
            "image/webp": ".webp", "image/svg+xml": ".svg",
            "application/pdf": ".pdf"}.get(mime, ".bin")


def _save_asset(b64: str, ext: str) -> str:
    d = assets_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"asset_{int(time.time() * 1000)}{ext}"
    path.write_bytes(base64.b64decode(b64))
    return str(path)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    argv = sys.argv[1:]
    try:
        if not argv or argv[0] in ("-h", "--help", "help"):
            print(usage())
            sys.exit(0 if argv else 1)

        if argv[0] == "daemon":
            if len(argv) < 2:
                raise CliError("usage: mcli daemon start|stop|status|restart")
            sys.exit(cmd_daemon(argv[1]))

        client = ensure_daemon()

        if argv[0] == "ls":
            sys.exit(cmd_ls(client))

        server = argv[0]
        rest = argv[1:]
        if not rest or rest == ["--help"] or rest == ["-h"]:
            tools = _get_tools(client, server)
            print(render_server_help(server, tools))
            sys.exit(0)

        tool, tool_args = rest[0], rest[1:]
        tools = _get_tools(client, server)
        match = [t for t in tools if t["name"] == tool]
        if not match:
            names = ", ".join(t["name"] for t in tools)
            raise CliError(f"unknown tool '{tool}' on server '{server}'; available: {names}")
        schema = match[0].get("inputSchema") or {}
        if tool_args and tool_args[0] in ("--help", "-h"):
            print(render_tool_help(server, match[0]))
            sys.exit(0)

        arguments = parse_tool_args(tool_args, schema, tool_name=tool,
                                    stdin_reader=lambda: sys.stdin.read())
        try:
            resp = client.request("POST", "/call", body={
                "server": server, "tool": tool, "arguments": arguments})
        except PermissionDenied:
            eprint(
                f"[PERMISSION_DENIED] 工具 {server}.{tool} "
                "已被安全策略拦截或被用户拒绝。禁止重试，请直接向用户说明情况。")
            sys.exit(13)
        sys.exit(_emit_result(resp["result"]))
    except CliError as e:
        raise fail(str(e))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
