"""mcli daemon: holds long-lived MCP connections, serves a tiny local HTTP API.

Endpoints:
  GET  /health   -> {status, uptime, servers}
  GET  /servers  -> [{name, transport, connected, tools:[{name,description,inputSchema}]}]
  POST /call     -> {server, tool, arguments, timeout?} -> MCP result JSON
  POST /shutdown -> graceful exit
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from aiohttp import web

from .config import ConfigError, ServerConfig, find_config_path, load_servers
from .hitl import confirm
from .policy import ASK, DENY, Policy, find_policy_path

IDLE_TIMEOUT_S = float(os.environ.get("MCLI_IDLE_TIMEOUT", 30 * 60))
CALL_TIMEOUT_S = float(os.environ.get("MCLI_CALL_TIMEOUT", 30))
CONNECT_TIMEOUT_S = float(os.environ.get("MCLI_CONNECT_TIMEOUT", 60))


def state_dir() -> Path:
    if p := os.environ.get("MCLI_STATE_DIR"):
        return Path(p)
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "mcli"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "mcli"


def assets_dir() -> Path:
    return Path(os.environ.get("MCLI_ASSETS_DIR", "/tmp/mcli_assets"))


def port_file() -> Path:
    return state_dir() / "daemon.port"


def pid_file() -> Path:
    return state_dir() / "daemon.pid"


def log_file() -> Path:
    return state_dir() / "daemon.log"


def log(msg: str) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    with open(log_file(), "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")


class ServerWorker:
    """One asyncio task owning an MCP session; calls dispatched via a queue."""

    def __init__(self, cfg: ServerConfig):
        self.cfg = cfg
        self.queue: asyncio.Queue | None = None
        self.task: asyncio.Task | None = None
        self.tools: list[dict] = []
        self.error: str | None = None

    @property
    def connected(self) -> bool:
        return self.task is not None and not self.task.done() and self.queue is not None

    async def ensure_started(self) -> None:
        if self.connected:
            return
        self.queue = asyncio.Queue()
        self.task = asyncio.create_task(self._run(), name=f"mcp-{self.cfg.name}")
        ready = asyncio.get_event_loop().create_future()
        await self.queue.put(("__ready__", ready))
        try:
            await asyncio.wait_for(ready, CONNECT_TIMEOUT_S)
        except (asyncio.TimeoutError, Exception):
            self.error = self.error or "connect timeout"
            raise

    async def _connect(self, stack: AsyncExitStack):
        cfg = self.cfg
        if cfg.transport == "stdio":
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            params = StdioServerParameters(
                command=cfg.command, args=cfg.args,
                env={**os.environ, **cfg.env} if cfg.env else None,
                cwd=cfg.cwd,
            )
            read, write = await stack.enter_async_context(stdio_client(params))
        elif cfg.transport == "sse":
            from mcp import ClientSession
            from mcp.client.sse import sse_client

            read, write = await stack.enter_async_context(
                sse_client(cfg.url, headers=cfg.headers or None))
        else:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client

            read, write, _ = await stack.enter_async_context(
                streamablehttp_client(cfg.url, headers=cfg.headers or None))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    async def _run(self) -> None:
        try:
            async with AsyncExitStack() as stack:
                try:
                    session = await self._connect(stack)
                    res = await session.list_tools()
                    self.tools = [
                        {
                            "name": t.name,
                            "description": t.description or "",
                            "inputSchema": getattr(t, "input_schema", None)
                            or getattr(t, "inputSchema", None) or {},
                            "annotations": (
                                t.annotations.model_dump(by_alias=True, exclude_none=True)
                                if getattr(t, "annotations", None) else {}),
                        }
                        for t in res.tools
                    ]
                    self.error = None
                except Exception as e:
                    self.error = f"{type(e).__name__}: {e}"
                    log(f"[{self.cfg.name}] connect failed: {self.error}")
                while True:
                    item = await self.queue.get()
                    if item is None:
                        return
                    op, fut = item
                    if op == "__ready__":
                        if not fut.done():
                            if self.error:
                                fut.set_exception(RuntimeError(self.error))
                            else:
                                fut.set_result(True)
                        continue
                    if self.error:
                        if not fut.done():
                            fut.set_exception(RuntimeError(self.error))
                        continue
                    tool, arguments, timeout = op
                    try:
                        result = await asyncio.wait_for(
                            session.call_tool(tool, arguments), timeout)
                        if not fut.done():
                            fut.set_result(result)
                    except Exception as e:
                        if not fut.done():
                            fut.set_exception(e)
        finally:
            self.error = self.error or "worker stopped"

    async def call(self, tool: str, arguments: dict, timeout: float) -> Any:
        await self.ensure_started()
        fut = asyncio.get_event_loop().create_future()
        await self.queue.put(((tool, arguments, timeout), fut))
        return await fut

    async def stop(self) -> None:
        if self.queue is not None:
            await self.queue.put(None)
        if self.task is not None:
            try:
                await asyncio.wait_for(self.task, 10)
            except (asyncio.TimeoutError, Exception):
                self.task.cancel()


class Pool:
    def __init__(self, servers: dict[str, ServerConfig]):
        self.workers = {name: ServerWorker(cfg) for name, cfg in servers.items()}

    def info(self) -> list[dict]:
        out = []
        for name, w in self.workers.items():
            out.append({
                "name": name,
                "transport": w.cfg.transport,
                "connected": w.connected,
                "error": w.error if not w.connected else None,
                "tools": w.tools,
            })
        return out

    async def ensure_tools(self, name: str) -> list[dict]:
        w = self.workers[name]
        if not w.connected:
            await w.ensure_started()
        if w.error:
            raise RuntimeError(f"{name}: {w.error}")
        return w.tools

    async def call(self, name: str, tool: str, arguments: dict, timeout: float):
        w = self.workers.get(name)
        if w is None:
            raise KeyError(name)
        return await w.call(tool, arguments, timeout)

    async def stop_all(self) -> None:
        await asyncio.gather(*(w.stop() for w in self.workers.values()),
                             return_exceptions=True)


def make_app(pool: Pool, started_at: float, policy: Policy) -> web.Application:
    last_request = {"t": time.monotonic()}

    @web.middleware
    async def touch(request, handler):
        last_request["t"] = time.monotonic()
        return await handler(request)

    async def health(request):
        return web.json_response({
            "status": "ok",
            "uptime": round(time.monotonic() - started_at, 1),
            "servers": [{"name": i["name"], "connected": i["connected"]}
                        for i in pool.info()],
        })

    async def servers(request):
        # lazily connect to all servers so ls reflects real status
        await asyncio.gather(
            *(w.ensure_started() for w in pool.workers.values()),
            return_exceptions=True,
        )
        return web.json_response({"servers": pool.info()})

    async def tools_of(request):
        name = request.match_info["server"]
        if name not in pool.workers:
            return web.json_response({"error": f"unknown server '{name}'"}, status=404)
        try:
            tools = await pool.ensure_tools(name)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=502)
        return web.json_response({"tools": tools})

    async def call(request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "body must be JSON"}, status=400)
        name, tool = body.get("server"), body.get("tool")
        arguments = body.get("arguments") or {}
        timeout = float(body.get("timeout") or CALL_TIMEOUT_S)
        if name not in pool.workers:
            return web.json_response({"error": f"unknown server '{name}'"}, status=404)

        # policy gate: annotations come from the cached tools/list
        try:
            tools = await pool.ensure_tools(name)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=502)
        tmeta = next((t for t in tools if t["name"] == tool), None)
        if tmeta is None:
            names = ", ".join(t["name"] for t in tools)
            return web.json_response(
                {"error": f"unknown tool '{tool}' on '{name}'; available: {names}"},
                status=404)
        decision = policy.evaluate(name, tool, arguments, tmeta.get("annotations"))
        if decision == DENY:
            return web.json_response({
                "code": "PERMISSION_DENIED",
                "error": f"tool '{name}.{tool}' denied by security policy",
            }, status=403)
        if decision == ASK:
            ok = await confirm(name, tool, arguments)
            if not ok:
                return web.json_response({
                    "code": "PERMISSION_DENIED",
                    "error": f"tool '{name}.{tool}' rejected by user or timed out",
                }, status=403)

        try:
            result = await pool.call(name, tool, arguments, timeout)
        except asyncio.TimeoutError:
            return web.json_response(
                {"error": f"tool call timed out after {timeout}s"}, status=504)
        except Exception as e:
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=502)
        return web.json_response(
            {"result": result.model_dump(mode="json", by_alias=True)})

    async def shutdown(request):
        asyncio.get_event_loop().call_later(0.2, request.app["stop_event"].set)
        return web.json_response({"status": "stopping"})

    app = web.Application(middlewares=[touch])
    app["stop_event"] = asyncio.Event()
    app.router.add_get("/health", health)
    app.router.add_get("/servers", servers)
    app.router.add_get("/servers/{server}/tools", tools_of)
    app.router.add_post("/call", call)
    app.router.add_post("/shutdown", shutdown)

    async def idle_watchdog():
        while True:
            await asyncio.sleep(30)
            if time.monotonic() - last_request["t"] > IDLE_TIMEOUT_S:
                log("idle timeout reached, shutting down")
                app["stop_event"].set()
                return

    async def start_idle_watchdog(app):
        app["_idle"] = asyncio.create_task(idle_watchdog())

    app.on_startup.append(start_idle_watchdog)
    return app


async def amain() -> int:
    try:
        cfg_path = find_config_path()
        servers = load_servers(cfg_path)
    except ConfigError as e:
        log(f"config error: {e}")
        print(f"mcli-daemon: {e}", file=sys.stderr)
        return 2

    policy = Policy.load(find_policy_path(cfg_path))
    pool = Pool(servers)
    app = make_app(pool, time.monotonic(), policy)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1] if runner.addresses else None
    if port is None:
        log("failed to determine bound port")
        return 2

    state_dir().mkdir(parents=True, exist_ok=True)
    port_file().write_text(str(port))
    pid_file().write_text(str(os.getpid()))
    log(f"daemon up on 127.0.0.1:{port} pid={os.getpid()}")

    loop = asyncio.get_event_loop()
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is not None:
            try:
                loop.add_signal_handler(sig, app["stop_event"].set)
            except (NotImplementedError, RuntimeError):
                pass

    await app["stop_event"].wait()
    log("shutting down")
    await pool.stop_all()
    await runner.cleanup()
    for f in (port_file(), pid_file()):
        try:
            f.unlink()
        except OSError:
            pass
    return 0


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
