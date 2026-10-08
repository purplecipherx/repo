#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import time
import uuid
from typing import Any

import uvicorn
from mcp.server import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.applications import Starlette
from starlette.routing import Mount, Route

mcp = MCPServer(
    "mom-relay",
    instructions=(
        "Relay MOM/M0J1M0J1 control calls to the authorized Windows MOM agent. "
        "No arbitrary shell execution is available."
    ),
)

PENDING: dict[str, asyncio.Future] = {}
QUEUE: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
AGENT_LAST_SEEN: float = 0.0
AGENT_NAME: str | None = None

def now() -> float:
    return time.time()

def agent_online(max_age: float = 35.0) -> bool:
    return AGENT_LAST_SEEN > 0 and (now() - AGENT_LAST_SEEN) <= max_age

def bearer(request: Request) -> str:
    value = request.headers.get("authorization", "")
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return ""

def require_agent(request: Request) -> Response | None:
    expected = os.environ.get("MOM_RELAY_AGENT_TOKEN", "")
    if not expected:
        return JSONResponse({"error": "relay agent token not configured"}, status_code=503)
    if bearer(request) != expected:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None

async def dispatch(tool: str, arguments: dict[str, Any], timeout: float = 45.0) -> dict[str, Any]:
    if not agent_online():
        return {
            "ok": False,
            "error": "MOM Windows agent is offline",
            "agent": AGENT_NAME,
            "last_seen_age_s": None if AGENT_LAST_SEEN <= 0 else round(now() - AGENT_LAST_SEEN, 1),
        }

    request_id = uuid.uuid4().hex
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    PENDING[request_id] = fut
    await QUEUE.put({
        "request_id": request_id,
        "tool": tool,
        "arguments": arguments,
        "created_at": now(),
    })
    try:
        result = await asyncio.wait_for(fut, timeout=timeout)
        return result
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "error": f"Timed out waiting for MOM agent after {timeout:.0f}s",
            "agent": AGENT_NAME,
        }
    finally:
        PENDING.pop(request_id, None)

async def agent_poll(request: Request) -> Response:
    global AGENT_LAST_SEEN, AGENT_NAME
    denied = require_agent(request)
    if denied:
        return denied
    try:
        body = await request.json()
    except Exception:
        body = {}
    AGENT_NAME = str(body.get("device") or "mom-windows-agent")
    AGENT_LAST_SEEN = now()

    try:
        item = await asyncio.wait_for(QUEUE.get(), timeout=20.0)
    except asyncio.TimeoutError:
        return Response(status_code=204)

    return JSONResponse(item)

async def agent_respond(request: Request) -> Response:
    global AGENT_LAST_SEEN
    denied = require_agent(request)
    if denied:
        return denied
    AGENT_LAST_SEEN = now()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    request_id = str(body.get("request_id") or "")
    fut = PENDING.get(request_id)
    if fut is None:
        return JSONResponse({"accepted": False, "reason": "unknown_or_expired_request"}, status_code=404)
    if not fut.done():
        fut.set_result({
            "ok": bool(body.get("ok", False)),
            "result": body.get("result"),
            "error": body.get("error"),
            "agent": AGENT_NAME,
        })
    return JSONResponse({"accepted": True})

async def health(_: Request) -> Response:
    return JSONResponse({
        "status": "ok",
        "agent_online": agent_online(),
        "agent": AGENT_NAME,
        "agent_last_seen_age_s": None if AGENT_LAST_SEEN <= 0 else round(now() - AGENT_LAST_SEEN, 1),
        "pending_requests": len(PENDING),
        "queued_requests": QUEUE.qsize(),
    })

@mcp.tool()
async def mom_status(log_lines: int = 60) -> dict[str, Any]:
    """Return current MOM supervisor, GPU, git, queue, and current log state through the authorized Windows agent."""
    out = await dispatch("mom_status", {"log_lines": int(log_lines)})
    out["relay"] = {"agent_online": agent_online(), "agent": AGENT_NAME}
    return out

@mcp.tool()
async def mom_queue() -> dict[str, Any]:
    """Return MOM GitHub-plan jobs, local queue, and completion/failure state."""
    return await dispatch("mom_queue", {})

@mcp.tool()
async def mom_result(experiment: int, log_lines: int = 160) -> dict[str, Any]:
    """Return structured result and bounded log tail for one MOM native experiment."""
    return await dispatch("mom_result", {"experiment": int(experiment), "log_lines": int(log_lines)})

@mcp.tool()
async def mom_logs(experiment: int | None = None, lines: int = 120) -> dict[str, Any]:
    """Return a bounded experiment/current/supervisor log tail."""
    args: dict[str, Any] = {"lines": int(lines)}
    if experiment is not None:
        args["experiment"] = int(experiment)
    return await dispatch("mom_logs", args)

@mcp.tool()
async def mom_enqueue(experiment: int, revision: int = 1, max_attempts: int = 1) -> dict[str, Any]:
    """Queue one allow-listed numbered MOM experiment through the Windows agent."""
    return await dispatch(
        "mom_enqueue",
        {
            "experiment": int(experiment),
            "revision": int(revision),
            "max_attempts": int(max_attempts),
        },
    )

@mcp.tool()
async def mom_cancel(experiment: int, revision: int | None = None) -> dict[str, Any]:
    """Cancel one specific active or queued numbered MOM experiment, including its process tree."""
    arguments: dict[str, Any] = {"experiment": int(experiment)}
    if revision is not None:
        arguments["revision"] = int(revision)
    return await dispatch("mom_cancel", arguments)

@mcp.tool()
async def mom_sync() -> dict[str, Any]:
    """Fast-forward the MOM worktree when the supervisor is idle."""
    return await dispatch("mom_sync", {})

class PluginBearerGate:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path", "").startswith("/mcp"):
            expected = os.environ.get("MOM_RELAY_PLUGIN_TOKEN", "")
            if not expected:
                response = JSONResponse({"error": "relay plugin token not configured"}, status_code=503)
                await response(scope, receive, send)
                return
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            supplied = headers.get("authorization", "")
            if supplied != f"Bearer {expected}":
                response = JSONResponse({"error": "unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)

def make_app():
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        host="0.0.0.0",
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette):
        async with mcp.session_manager.run():
            yield

    app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/agent/poll", agent_poll, methods=["POST"]),
            Route("/agent/respond", agent_respond, methods=["POST"]),
            Mount("/", app=mcp_app),
        ],
        lifespan=lifespan,
    )
    return PluginBearerGate(app)

app = make_app()

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8877")))
    args = ap.parse_args()
    if not os.environ.get("MOM_RELAY_AGENT_TOKEN"):
        raise SystemExit("MOM_RELAY_AGENT_TOKEN is required")
    if not os.environ.get("MOM_RELAY_PLUGIN_TOKEN"):
        raise SystemExit("MOM_RELAY_PLUGIN_TOKEN is required")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
