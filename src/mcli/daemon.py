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
import logging
import math
import os
import signal
import secrets
from hmac import compare_digest
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from aiohttp import web

from .config import ConfigError, ServerConfig, find_config_path, load_servers
from .hitl import confirm
from .ipc import daemon_lock, make_state_dir, state_dir
from .policy import ASK, DENY, Policy, find_policy_path

IDLE_TIMEOUT_S = float(os.environ.get("MCLI_IDLE_TIMEOUT", 30 * 60))
CALL_TIMEOUT_S = float(os.environ.get("MCLI_CALL_TIMEOUT", 30))
CONNECT_TIMEOUT_S = float(os.environ.get("MCLI_CONNECT_TIMEOUT", 60))
STOP_EVENT = web.AppKey("stop_event", asyncio.Event)


def port_file() -> Path:
    return state_dir() / "daemon.port"


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
        self.ready: asyncio.Future | None = None
        self.stopping = False

    @property
    def connected(self) -> bool:
        return (
            self.task is not None and not self.task.done()
            and self.ready is not None and self.ready.done()
            and self.error is None and not self.stopping
        )

    async def ensure_started(self) -> None:
        if self.stopping:
            raise RuntimeError("worker is stopping")
        if self.task is not None and self.error is not None:
            await asyncio.shield(self.task)
        if self.task is None or self.task.done():
            self.queue = asyncio.Queue()
            self.ready = asyncio.get_running_loop().create_future()
            self.error = None
            self.tools = []
            self.task = asyncio.create_task(self._run(), name=f"mcp-{self.cfg.name}")
        error = await asyncio.shield(self.ready)
        if error:
            raise RuntimeError(error)

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
                async with asyncio.timeout(CONNECT_TIMEOUT_S):
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
                self.ready.set_result(None)
                while (item := await self.queue.get()) is not None:
                    tool, arguments, timeout, future = item
                    if future.done():
                        continue
                    try:
                        result = await asyncio.wait_for(session.call_tool(tool, arguments), timeout)
                    except Exception as exc:
                        # The server may have executed the operation. Retire this
                        # connection and let a subsequent request reconnect.
                        self.error = str(exc) or type(exc).__name__
                        if not future.done():
                            future.set_exception(exc)
                        return
                    except asyncio.CancelledError:
                        if not future.done():
                            future.set_exception(RuntimeError("worker stopped during call"))
                        raise
                    if not future.done():
                        future.set_result(result)
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
            log(f"[{self.cfg.name}] worker failed: {self.error}")
        finally:
            self.error = self.error or "worker stopped"
            if not self.ready.done():
                self.ready.set_result(self.error)
            while not self.queue.empty():
                pending = self.queue.get_nowait()
                if pending is not None and not pending[-1].done():
                    pending[-1].set_exception(RuntimeError(self.error))

    async def call(self, tool: str, arguments: dict, timeout: float, tools: list[dict]) -> Any:
        if not self.connected or self.tools is not tools:
            raise RuntimeError("MCP connection changed; start a new request")
        future = asyncio.get_running_loop().create_future()
        self.queue.put_nowait((tool, arguments, timeout, future))
        return await future

    async def stop(self) -> None:
        self.stopping = True
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


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

    async def call(self, name: str, tool: str, arguments: dict, timeout: float, tools: list[dict]):
        w = self.workers.get(name)
        if w is None:
            raise KeyError(name)
        return await w.call(tool, arguments, timeout, tools)

    async def stop_all(self) -> None:
        await asyncio.gather(*(w.stop() for w in self.workers.values()),
                             return_exceptions=True)


def make_app(pool: Pool, started_at: float, policy: Policy, token: str) -> web.Application:
    last_request = time.monotonic()
    active_requests = 0

    @web.middleware
    async def authenticate(request, handler):
        supplied = request.headers.get("Authorization", "").encode("utf-8")
        if not compare_digest(supplied, f"Bearer {token}".encode()):
            return web.json_response({"error": "daemon authentication required"}, status=401)
        return await handler(request)

    @web.middleware
    async def touch(request, handler):
        nonlocal active_requests, last_request
        active_requests += 1
        try:
            return await handler(request)
        finally:
            active_requests -= 1
            last_request = time.monotonic()

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
        if not isinstance(body, dict):
            return web.json_response({"error": "body must be a JSON object"}, status=400)
        name, tool = body.get("server"), body.get("tool")
        arguments = body.get("arguments", {})
        timeout = body.get("timeout", CALL_TIMEOUT_S)
        if not isinstance(name, str) or not isinstance(tool, str) or not isinstance(arguments, dict):
            return web.json_response({"error": "expected server, tool and an arguments object"}, status=400)
        if type(timeout) not in (int, float) or not 0 < timeout < math.inf:
            return web.json_response({"error": "timeout must be a positive finite number"}, status=400)
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
            result = await pool.call(name, tool, arguments, timeout, tools)
        except asyncio.TimeoutError:
            return web.json_response(
                {"error": f"tool call timed out after {timeout}s"}, status=504)
        except Exception as e:
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=502)
        return web.json_response(
            {"result": result.model_dump(mode="json", by_alias=True)})

    async def shutdown(request):
        asyncio.get_event_loop().call_later(0.2, request.app[STOP_EVENT].set)
        return web.json_response({"status": "stopping"})

    app = web.Application(middlewares=[authenticate, touch])
    app[STOP_EVENT] = asyncio.Event()
    app.router.add_get("/health", health)
    app.router.add_get("/servers", servers)
    app.router.add_get("/servers/{server}/tools", tools_of)
    app.router.add_post("/call", call)
    app.router.add_post("/shutdown", shutdown)

    async def idle_watchdog():
        while True:
            await asyncio.sleep(30)
            if not active_requests and time.monotonic() - last_request > IDLE_TIMEOUT_S:
                log("idle timeout reached, shutting down")
                app[STOP_EVENT].set()
                return

    async def manage_idle_watchdog(app):
        task = asyncio.create_task(idle_watchdog())
        yield
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    app.cleanup_ctx.append(manage_idle_watchdog)
    return app


async def amain() -> int:
    try:
        cfg_path = find_config_path()
        servers = load_servers(cfg_path)
    except ConfigError as e:
        log(f"config error: {e}")
        print(f"mcli-daemon: {e}", file=sys.stderr)
        return 2

    try:
        policy = Policy.load(find_policy_path(cfg_path))
    except (OSError, ValueError) as exc:
        log(f"policy error: {exc}")
        return 2
    token = secrets.token_urlsafe(32)
    pool = Pool(servers)
    app = make_app(pool, time.monotonic(), policy, token)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1] if runner.addresses else None
    if port is None:
        log("failed to determine bound port")
        return 2

    state_dir().mkdir(parents=True, exist_ok=True)
    metadata = port_file().with_suffix(".tmp")
    metadata.write_text(json.dumps({"port": port, "token": token}), encoding="utf-8")
    metadata.replace(port_file())
    log(f"daemon up on 127.0.0.1:{port} pid={os.getpid()}")

    loop = asyncio.get_event_loop()
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is not None:
            try:
                loop.add_signal_handler(sig, app[STOP_EVENT].set)
            except (NotImplementedError, RuntimeError):
                pass

    try:
        await app[STOP_EVENT].wait()
    finally:
        log("shutting down")
        await site.stop()
        await pool.stop_all()
        await runner.cleanup()
        port_file().unlink(missing_ok=True)
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    try:
        with daemon_lock(make_state_dir() / "daemon.lock") as acquired:
            if acquired:
                sys.exit(asyncio.run(amain()))
    except (ConfigError, OSError) as exc:
        sys.exit(f"mcli-daemon: {exc}")


if __name__ == "__main__":
    main()
