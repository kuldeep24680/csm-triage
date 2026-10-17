"""API request audit: endpoint, headers, status, latency for every /api call.

* The middleware never blocks a request on the database: it puts a record on
  an in-memory queue, and a background thread writes them in batches.
* Bodies are never stored. Sensitive headers are redacted before they leave
  the process. Authorization keeps only its scheme and a short fingerprint,
  so one user's calls can be correlated without storing the token.
* Every response gets an ``X-Request-ID`` (echoed if the caller sent one) and
  an ``X-Response-Time-ms`` header, so a user's bug report can be matched to
  the exact audit row and server log line.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import logging
import queue
import threading
import time
import uuid
from datetime import datetime, timezone

import jwt
from fastapi import Request

from ..config import settings

log = logging.getLogger("triage.api")

REDACT = {"authorization", "cookie", "set-cookie", "x-api-key", "proxy-authorization"}
_q: "queue.Queue[dict]" = queue.Queue(maxsize=10_000)
_worker: threading.Thread | None = None
_stop = threading.Event()


def redact_headers(headers) -> dict:
    out = {}
    for k, v in headers.items():
        k = k.lower()
        if k == "authorization" and " " in v:
            scheme, token = v.split(" ", 1)
            out[k] = f"{scheme} <redacted:{hashlib.sha256(token.encode()).hexdigest()[:8]}>"
        elif k in REDACT:
            out[k] = "<redacted>"
        else:
            out[k] = v[:500]
    return out


def _identity(request: Request) -> dict:
    """Best-effort caller identity from the bearer token (never raises)."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return {}
    try:
        c = jwt.decode(auth[7:], settings.jwt_secret, algorithms=["HS256"])
        return {"tenant_id": c.get("tenant"), "user_id": c.get("sub"), "user_email": c.get("email"), "role": c.get("role")}
    except jwt.PyJWTError:
        return {}


async def audit_middleware(request: Request, call_next):
    if not request.url.path.startswith("/api"):
        return await call_next(request)
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    t0 = time.perf_counter()
    status, resp, error = 500, None, None
    try:
        resp = await call_next(request)
        status = resp.status_code
        return resp
    except Exception as e:  # unhandled -> still audited as 500, then re-raised
        error = f"{type(e).__name__}: {e}"[:500]
        raise
    finally:
        ms = round((time.perf_counter() - t0) * 1000, 1)
        route = getattr(request.scope.get("route"), "path", None)
        if resp is not None:
            resp.headers["X-Request-ID"] = request_id
            resp.headers["X-Response-Time-ms"] = str(ms)
        rec = {
            "request_id": request_id, "at": datetime.now(timezone.utc), **{
                "tenant_id": None, "user_id": None, "user_email": None, "role": None, **_identity(request)},
            "method": request.method, "path": request.url.path, "route": route,
            "query": request.url.query or None, "status": status, "latency_ms": ms,
            "response_bytes": int(resp.headers.get("content-length", 0) or 0) if resp is not None else None,
            "client_ip": request.client.host if request.client else None,
            "user_agent": request.headers.get("user-agent", "")[:300] or None,
            "request_headers": json.dumps(redact_headers(request.headers)),
            "response_headers": json.dumps(redact_headers(resp.headers) if resp is not None else {}),
            "error": error,
        }
        log.info("%s %s %s %.0fms req=%s tenant=%s", request.method, request.url.path, status, ms,
                 request_id[:8], rec["tenant_id"])
        try:
            _q.put_nowait(rec)
        except queue.Full:  # never let auditing take the API down
            log.warning("audit queue full; dropping record %s", request_id)


COLS = ["request_id", "at", "tenant_id", "user_id", "user_email", "role", "method", "path", "route", "query",
        "status", "latency_ms", "response_bytes", "client_ip", "user_agent", "request_headers",
        "response_headers", "error"]


def flush(max_rows: int = 500) -> int:
    batch = []
    while len(batch) < max_rows:
        try:
            batch.append(_q.get_nowait())
        except queue.Empty:
            break
    if not batch:
        return 0
    from ..db import system_tx
    try:
        with system_tx() as conn:
            # COPY is not allowed into a table with row-level security, so batch INSERTs
            conn.cursor().executemany(
                f"INSERT INTO api_requests ({', '.join(COLS)}) VALUES ({', '.join(['%s'] * len(COLS))})",
                [[r[c] for c in COLS] for r in batch])
    except Exception:
        log.exception("failed to write %d audit records", len(batch))
    return len(batch)


def _run():
    while not _stop.is_set():
        _stop.wait(1.0)
        while flush():
            pass


def start_writer():
    global _worker
    if _worker is None or not _worker.is_alive():
        _stop.clear()
        _worker = threading.Thread(target=_run, name="audit-writer", daemon=True)
        _worker.start()


def stop_writer():
    _stop.set()
    while flush():
        pass


atexit.register(stop_writer)
