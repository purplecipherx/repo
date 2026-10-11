#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import os
import time
import uuid
from typing import Any

import httpx
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
        "Owner-authorized Windows desktop operations and bounded command execution are available through mom_desktop."
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

GITHUB_TOKEN_CACHE: dict[str, float] = {}

async def agent_authorized(request: Request) -> bool:
    supplied = bearer(request)
    if not supplied:
        return False

    static_expected = os.environ.get("MOM_RELAY_AGENT_TOKEN", "")
    if static_expected and supplied == static_expected:
        return True

    expected_login = os.environ.get("MOM_RELAY_GITHUB_LOGIN", "").strip()
    if not expected_login:
        return False

    digest = hashlib.sha256(supplied.encode("utf-8")).hexdigest()
    if GITHUB_TOKEN_CACHE.get(digest, 0.0) > now():
        return True

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": f"Bearer {supplied}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "mom-control-relay",
                },
            )
        if response.status_code != 200:
            return False
        login = str((response.json() or {}).get("login") or "")
        if login.casefold() != expected_login.casefold():
            return False
        GITHUB_TOKEN_CACHE[digest] = now() + 600.0
        if len(GITHUB_TOKEN_CACHE) > 64:
            cutoff = now()
            for key, expiry in list(GITHUB_TOKEN_CACHE.items()):
                if expiry <= cutoff:
                    GITHUB_TOKEN_CACHE.pop(key, None)
        return True
    except Exception:
        return False

async def require_agent(request: Request) -> Response | None:
    if not await agent_authorized(request):
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
    denied = await require_agent(request)
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
    denied = await require_agent(request)
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
async def mom_sync() -> dict[str, Any]:
    """Recover only stale N9915 MCP restart if necessary, then safely fast-forward MOM."""
    state = await dispatch("mom_status", {"log_lines": 0})
    active = (((state.get("result") or {}).get("suite") or {}).get("current_job") or {})
    recovered = None
    if active.get("key") == "N9915_MCP@1":
        cancel = await dispatch("mom_cancel", {"experiment": 9915, "revision": 1}, timeout=50.0)
        recovered = {"target": "N9915_MCP@1", "cancel_result": cancel}
        for _ in range(9):
            await asyncio.sleep(2.0)
            probe = await dispatch("mom_status", {"log_lines": 0})
            running = (((probe.get("result") or {}).get("suite") or {}).get("current_job") or {})
            if not running:
                break
        else:
            return {"ok": False, "reason": "N9915 cancellation did not release supervisor; no sync attempted", "recovery": recovered}
    synced = await dispatch("mom_sync", {}, timeout=195.0)
    return {"ok": synced.get("ok"), "result": synced.get("result"), "recovery": recovered}


# Expose the complete owner-authorized Windows control surface. These are
# pass-through calls; Windows-side validation and auditing remain authoritative.
@mcp.tool()
async def mom_desktop(action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Owner Windows desktop: info/list/read/write/mkdir/search/run/start/job/cancel_job/
    screenshot/windows/focus/move_window/adb. For run/start pass params.argv as a
    string array and optionally params.cwd; for adb pass params.operation.
    """
    timeout = 195.0 if action == "run" else 60.0
    return await dispatch("mom_desktop", {"action": action, "params": params or {}}, timeout=timeout)

@mcp.tool()
async def mom_workspace_list(path: str, limit: int = 100) -> dict[str, Any]:
    """List files and folders in the MOM/Entropy and approved Windows workspaces."""
    return await dispatch("mom_workspace_list", {"path": path, "limit": limit})

@mcp.tool()
async def mom_workspace_read(path: str, start_line: int = 1, end_line: int = 250) -> dict[str, Any]:
    """Read an approved workspace text file and its SHA256 for safe editing."""
    return await dispatch("mom_workspace_read", {"path": path, "start_line": start_line, "end_line": end_line})

@mcp.tool()
async def mom_workspace_write(path: str, content: str, expected_sha256: str = "") -> dict[str, Any]:
    """Write a workspace text file; replacing files requires expected SHA256."""
    return await dispatch("mom_workspace_write", {"path": path, "content": content, "expected_sha256": expected_sha256})

@mcp.tool()
async def mom_workspace_patch(path: str, old_string: str, new_string: str, expected_replacements: int = 1,
                              expected_sha256: str = "") -> dict[str, Any]:
    """Safely patch a workspace file, enforcing exact replacements and optional SHA256."""
    return await dispatch("mom_workspace_patch", {"path": path, "old_string": old_string,
                           "new_string": new_string, "expected_replacements": expected_replacements,
                           "expected_sha256": expected_sha256})

@mcp.tool()
async def mom_workspace_run(path: str, args: list[str] | None = None, timeout_seconds: int = 60) -> dict[str, Any]:
    """Run an approved local Python/PowerShell script with bounded runtime and captured logs."""
    return await dispatch("mom_workspace_run", {"path": path, "args": args or [],
                          "timeout_seconds": timeout_seconds}, timeout=150.0)

@mcp.tool()
async def mom_read_file(path: str, start_line: int = 1, end_line: int = 400) -> dict[str, Any]:
    """Read a selected MOM source-file section for debugging or maintenance."""
    return await dispatch("mom_read_file", {"path":path,"start_line":start_line,"end_line":end_line})

@mcp.tool()
async def mom_patch_file(path: str, old_string: str, new_string: str, expected_replacements: int = 1) -> dict[str, Any]:
    """Patch a selected MOM source file with exact replacement matching."""
    return await dispatch("mom_patch_file", {"path":path,"old_string":old_string,
                          "new_string":new_string,"expected_replacements":expected_replacements})

@mcp.tool()
async def mom_write_file(path: str, content: str, overwrite: bool = False) -> dict[str, Any]:
    """Write an allowed MOM source file."""
    return await dispatch("mom_write_file", {"path":path,"content":content,"overwrite":overwrite})

@mcp.tool()
async def mom_validate(paths: list[str], mode: str = "py_compile") -> dict[str, Any]:
    """Validate selected MOM source files without starting an experiment."""
    return await dispatch("mom_validate", {"paths":paths,"mode":mode}, timeout=150.0)

@mcp.tool()
async def mom_git_commit(message: str, paths: list[str]) -> dict[str, Any]:
    """Commit selected MOM source changes on the authorized Windows worktree."""
    return await dispatch("mom_git_commit", {"message":message,"paths":paths}, timeout=90.0)

@mcp.tool()
async def mom_git_push() -> dict[str, Any]:
    """Push the clean authorized MOM experiment branch while the supervisor is idle."""
    return await dispatch("mom_git_push", {}, timeout=190.0)

@mcp.tool()
async def mom_cancel(experiment: int, revision: int | None = None) -> dict[str, Any]:
    """Cancel a targeted active or queued MOM experiment."""
    return await dispatch("mom_cancel", {"experiment":experiment,"revision":revision})

@mcp.tool()
async def mom_recover_supervisor(stale_after_seconds: int = 120) -> dict[str, Any]:
    """Recover MOM research supervisor only when heartbeat is stale and no job is active."""
    return await dispatch("mom_recover_supervisor", {"stale_after_seconds":stale_after_seconds}, timeout=100.0)


# Human-friendly desktop tools. All operations delegate to the audited MOM
# desktop controller; its path restrictions and SHA checks remain authoritative.
async def _desktop_call(action: str, params: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
    return await dispatch("mom_desktop", {"action": action, "params": params}, timeout=timeout)

@mcp.tool()
async def mom_desktop_directory(path: str, offset: int = 0, limit: int = 100) -> dict[str, Any]:
    """List files and folders on the authorized Windows computer."""
    return await _desktop_call("list", {"path":path, "offset":offset, "limit":limit})

@mcp.tool()
async def mom_desktop_file_info(path: str) -> dict[str, Any]:
    """Get Windows file metadata, SHA256, and spreadsheet sheet names when available."""
    return await _desktop_call("file_info", {"path":path})

@mcp.tool()
async def mom_desktop_read_text(path: str, start: int = 1, count: int = 250) -> dict[str, Any]:
    """Read a bounded section of a Windows text file."""
    return await _desktop_call("read", {"path":path, "start":start, "count":count})

@mcp.tool()
async def mom_desktop_read_many(paths: list[str], lines: int = 80) -> dict[str, Any]:
    """Read up to fifteen files, each with a bounded excerpt."""
    return await _desktop_call("read_multiple", {"paths":paths, "lines":lines})

@mcp.tool()
async def mom_desktop_write_text(path: str, content: str, expected_sha256: str = "") -> dict[str, Any]:
    """Create a Windows text file; overwrites require the matching file SHA256."""
    return await _desktop_call("write", {"path":path, "content":content, "expected_sha256":expected_sha256})

@mcp.tool()
async def mom_desktop_edit_text(path: str, old_string: str, new_string: str, expected_replacements: int = 1,
                                expected_sha256: str = "") -> dict[str, Any]:
    """Apply precise replacements to a Windows text file and report its new SHA."""
    return await _desktop_call("edit_block", {"path":path, "old_string":old_string,
                               "new_string":new_string, "expected_replacements":expected_replacements,
                               "expected_sha256":expected_sha256})

@mcp.tool()
async def mom_desktop_copy_file(source: str, destination: str,
                                expected_destination_sha256: str = "") -> dict[str, Any]:
    """Copy a file to another approved workspace; reject unchecked overwrites."""
    return await _desktop_call("copy_file", {"source":source, "destination":destination,
                               "expected_destination_sha256":expected_destination_sha256})

@mcp.tool()
async def mom_desktop_move_file(source: str, destination: str,
                                expected_destination_sha256: str = "") -> dict[str, Any]:
    """Move or rename a file between approved workspaces."""
    return await _desktop_call("move_file", {"source":source, "destination":destination,
                               "expected_destination_sha256":expected_destination_sha256})

@mcp.tool()
async def mom_desktop_search(path: str, term: str, names_only: bool = False,
                             max_files: int = 1000) -> dict[str, Any]:
    """Search bounded Windows directory trees by filename or text contents."""
    return await _desktop_call("search", {"path":path, "term":term,
                               "names_only":names_only, "max_files":max_files})

@mcp.tool()
async def mom_desktop_terminal(argv: list[str], cwd: str = "", timeout_seconds: int = 60) -> dict[str, Any]:
    """Execute an allow-listed Windows program with explicit argv and a bounded timeout."""
    params: dict[str,Any] = {"argv":argv,"timeout":timeout_seconds}
    if cwd: params["cwd"]=cwd
    return await _desktop_call("run", params, timeout=195.0)

@mcp.tool()
async def mom_desktop_process_start(argv: list[str], cwd: str = "") -> dict[str, Any]:
    """Launch an audited detached Windows process and return a job ID."""
    params: dict[str, Any] = {"argv":argv}
    if cwd: params["cwd"]=cwd
    return await _desktop_call("start", params)

@mcp.tool()
async def mom_desktop_process_status(id: str) -> dict[str, Any]:
    """Check a detached MOM desktop process by job ID."""
    return await _desktop_call("job", {"id":id})

@mcp.tool()
async def mom_desktop_excel_read(path: str, sheet: str = "", start_row: int = 1,
                                 row_count: int = 30, column_count: int = 16) -> dict[str, Any]:
    """Read a bounded range from an Excel workbook without launching Excel."""
    params={"path":path,"start_row":start_row,"row_count":row_count,"column_count":column_count}
    if sheet:params["sheet"]=sheet
    return await _desktop_call("spreadsheet_read",params)

@mcp.tool()
async def mom_desktop_excel_edit(path: str, start_cell: str, values: list[list[Any]],
                                 expected_sha256: str, sheet: str = "") -> dict[str, Any]:
    """Edit an Excel cell matrix after verifying the workbook SHA256."""
    params={"path":path,"start_cell":start_cell,"values":values,"expected_sha256":expected_sha256}
    if sheet:params["sheet"]=sheet
    return await _desktop_call("spreadsheet_edit",params)

@mcp.tool()
async def mom_desktop_pdf_read(path: str, start_page: int = 1,
                               page_count: int = 3) -> dict[str, Any]:
    """Extract text from a bounded page range of a local PDF."""
    return await _desktop_call("pdf_read",{"path":path,"start_page":start_page,"page_count":page_count})

@mcp.tool()
async def mom_desktop_image_info(path: str) -> dict[str, Any]:
    """Inspect a local image's dimensions, transparency and format."""
    return await _desktop_call("image_info",{"path":path})

@mcp.tool()
async def mom_desktop_capture(max_width: int = 1200, all_screens: bool = False) -> dict[str, Any]:
    """Capture a resized Windows screenshot through the authorized MOM agent."""
    return await _desktop_call("screenshot",{"max_width":max_width,"all_screens":all_screens})

@mcp.tool()
async def mom_desktop_android(operation: str = "devices", serial: str = "emulator-5554",
                              x: int = 0, y: int = 0, x2: int = 0, y2: int = 0,
                              duration_ms: int = 350, key: int = 0, package: str = "",
                              lines: int = 100) -> dict[str, Any]:
    """Android emulator control: devices, screenshot, tap, swipe, keyevent, launch, logcat."""
    return await _desktop_call("adb", {"operation":operation,"serial":serial,"x":x,"y":y,
                              "x2":x2,"y2":y2,"duration_ms":duration_ms,
                              "key":key,"package":package,"lines":lines})


@mcp.tool()
async def mom_desktop_tree(path: str, depth: int = 2, limit: int = 200) -> dict[str, Any]:
    """Browse an owner-approved Windows folder recursively, without visiting sensitive directories."""
    return await dispatch("mom_desktop", {"action":"tree","params":{"path":path,"depth":depth,"limit":limit}})

@mcp.tool()
async def mom_desktop_session_start(argv: list[str], cwd: str = "", max_runtime_s: int = 600) -> dict[str, Any]:
    """Start a bounded interactive terminal process on Windows, using an argv array and approved cwd."""
    params: dict[str, Any] = {"argv":argv,"max_runtime_s":max_runtime_s}
    if cwd: params["cwd"] = cwd
    return await dispatch("mom_desktop",{"action":"session_start","params":params})

@mcp.tool()
async def mom_desktop_session_output(id: str, stream: str = "stdout", offset: int = 0,
                                     max_bytes: int = 12000) -> dict[str, Any]:
    """Read paged stdout/stderr from an interactive MOM Windows terminal session."""
    return await dispatch("mom_desktop",{"action":"session_output",
                          "params":{"id":id,"stream":stream,"offset":offset,"max_bytes":max_bytes}})

@mcp.tool()
async def mom_desktop_session_input(id: str, text: str) -> dict[str, Any]:
    """Send text to the stdin of a running interactive MOM Windows terminal session."""
    return await dispatch("mom_desktop",{"action":"session_input","params":{"id":id,"text":text}})

@mcp.tool()
async def mom_desktop_session_stop(id: str) -> dict[str, Any]:
    """Terminate one explicitly identified MOM interactive terminal session."""
    return await dispatch("mom_desktop",{"action":"session_stop","params":{"id":id}})

@mcp.tool()
async def mom_desktop_session_list() -> dict[str, Any]:
    """List running and recent MOM interactive terminal sessions."""
    return await dispatch("mom_desktop",{"action":"session_list","params":{}})

def mcp_path() -> str:
    value = os.environ.get("MOM_RELAY_MCP_PATH", "/mcp").strip() or "/mcp"
    if not value.startswith("/"):
        value = "/" + value
    return value.rstrip("/") or "/mcp"

def make_app():
    mcp_app = mcp.streamable_http_app(
        streamable_http_path=mcp_path(),
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
    return app

app = make_app()

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8877")))
    args = ap.parse_args()
    if not os.environ.get("MOM_RELAY_AGENT_TOKEN") and not os.environ.get("MOM_RELAY_GITHUB_LOGIN"):
        raise SystemExit("Configure MOM_RELAY_AGENT_TOKEN or MOM_RELAY_GITHUB_LOGIN")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
