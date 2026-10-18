"""FastAPI adapter. It delegates all safety decisions to the shared service."""
from __future__ import annotations

import json
import ipaddress
import os
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .graphdb import GraphUnavailable, connect
from .service import (
    ParachuteError, apply_fixes, audit_backups, audit_history,
    explain, preflight, replay_verdict, restore_test, run_command,
    world_summary,
)

MAX_BODY = 64 * 1024
RATE_LIMIT = 60
RATE_WINDOW = 60.0
_windows: dict[str, tuple[float, int]] = {}

app = FastAPI(
    title="Parachute API",
    version="0.2.0",
    description="FalkorDB-backed command safety and recovery verification.",
)


class BodyLimitMiddleware:
    """Bound the complete request body, including requests without Content-Length."""
    def __init__(self, app, limit=MAX_BODY):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            body = message.get("body", b"")
            size += len(body)
            if size > self.limit:
                payload = json.dumps({"detail": "Request body too large"}).encode()
                await send({
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(payload)).encode())],
                })
                await send({"type": "http.response.body", "body": payload})
                return
            chunks.append(body)
            if not message.get("more_body", False):
                break

        body = b"".join(chunks)
        delivered = False

        async def replay_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)


app.add_middleware(BodyLimitMiddleware)


class CommandRequest(BaseModel):
    root: str = Field(min_length=1, max_length=4096)
    command: str = Field(min_length=1, max_length=4096)
    cwd: str | None = Field(default=None, max_length=4096)
    env: dict[str, str] = Field(default_factory=dict)
    timeout: int = Field(default=60, ge=1, le=300)


class RootRequest(BaseModel):
    root: str = Field(min_length=1, max_length=4096)


class VerdictRequest(RootRequest):
    verdict_id: str = Field(min_length=1, max_length=128)


class BackupAuditRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except GraphUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="FalkorDB unavailable; safety operation failed closed",
        ) from exc
    except (ValueError, ParachuteError, OSError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    if request.url.path not in ("/health", "/docs", "/openapi.json", "/redoc"):
        key = request.client.host if request.client else "unknown"
        now = time.monotonic()
        start, count = _windows.get(key, (now, 0))
        if now - start >= RATE_WINDOW:
            start, count = now, 0
        if count >= RATE_LIMIT:
            return JSONResponse(
                {"detail": "Rate limit exceeded"}, status_code=429
            )
        _windows[key] = (start, count + 1)
    return await call_next(request)


@app.get("/health")
def health():
    return {"status": "ok", "service": "parachute"}


@app.get("/ready")
def ready():
    _call(connect)
    return {"status": "ready", "dependency": "falkordb"}


@app.post("/world-summary")
def world_summary_endpoint(request: RootRequest):
    return _call(world_summary, request.root)


@app.post("/preflight")
def preflight_endpoint(request: CommandRequest):
    return _call(preflight, request.root, request.command, request.cwd, request.env)


@app.post("/explain")
def explain_endpoint(request: VerdictRequest):
    return _call(explain, request.root, request.verdict_id)


@app.post("/apply-fixes")
def apply_fixes_endpoint(request: VerdictRequest):
    return _call(apply_fixes, request.root, request.verdict_id)


@app.post("/restore-test")
def restore_test_endpoint(request: VerdictRequest):
    return _call(restore_test, request.root, request.verdict_id)


@app.post("/audit-backups")
def audit_backups_endpoint(request: BackupAuditRequest):
    return _call(audit_backups, request.path)


@app.post("/audit")
def audit_endpoint(request: RootRequest):
    return {"verdicts": _call(audit_history, request.root)}


@app.post("/replay")
def replay_endpoint(request: VerdictRequest):
    return _call(replay_verdict, request.root, request.verdict_id)


@app.post("/run")
def run_endpoint(request: CommandRequest, http_request: Request):
    # Never allow arbitrary execution in public/read-only mode. Local execution
    # requires an explicit opt-in; the server also refuses remote binding by default.
    if os.environ.get("PARACHUTE_PUBLIC_READ_ONLY") == "1":
        raise HTTPException(status_code=403, detail="Execution is disabled in read-only mode")
    if os.environ.get("PARACHUTE_ALLOW_EXECUTION") != "1":
        raise HTTPException(
            status_code=403,
            detail="Execution disabled. Set PARACHUTE_ALLOW_EXECUTION=1 for a local sandbox demo.",
        )
    client_host = http_request.client.host if http_request.client else ""
    try:
        is_loopback = ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        is_loopback = client_host == "localhost"
    if not is_loopback:
        raise HTTPException(status_code=403, detail="Remote command execution is disabled")
    return _call(
        run_command, request.root, request.command, request.cwd,
        request.env, request.timeout,
    )


def main():
    import uvicorn

    host = os.environ.get("PARACHUTE_API_HOST", "127.0.0.1")
    port = int(os.environ.get("PARACHUTE_API_PORT", "8000"))
    if host not in ("127.0.0.1", "::1", "localhost") and os.environ.get(
        "PARACHUTE_ALLOW_REMOTE_API"
    ) != "1":
        raise SystemExit(
            "Refusing non-local bind. Configure authentication and network controls first."
        )
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
