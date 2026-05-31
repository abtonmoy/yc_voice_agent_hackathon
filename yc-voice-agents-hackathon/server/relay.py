"""Standalone FastAPI event relay (lld-backend.md §9).

In-memory, single process. The bot pushes events here (``POST /events``) and the
dashboard subscribes over SSE (``GET /stream``). A ring buffer replays recent
events to late joiners, and every event is also appended to an ndjson file for
backup/replay.

Run with: ``uvicorn relay:app --port 8080``
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import deque

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

DEFAULT_TOKEN = "yc-hack-relay-7f3a9c2e"

app = FastAPI(title="yc-triage event relay")

# The dashboard subscribes to /stream over SSE from whatever origin it's served
# from (the relay itself on :8080, a separate static server, a tunnel, or file://).
# Without CORS the browser silently blocks the cross-origin EventSource and the
# dashboard never updates even though events are arriving. Allow all origins —
# this is a read-only, single-tenant demo relay.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Ring buffer of recent events (replayed to late SSE joiners).
buffer: deque[dict] = deque(maxlen=500)
# Live SSE subscriber queues.
subscribers: set[asyncio.Queue] = set()


def _token() -> str:
    return os.environ.get("RELAY_TOKEN", DEFAULT_TOKEN)


def _ndjson_path() -> str:
    # Resolved at each write so tests can repoint RELAY_NDJSON at a temp file.
    return os.environ.get("RELAY_NDJSON", "events.ndjson")


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


@app.post("/events")
async def ingest(request: Request, x_relay_token: str | None = Header(default=None)):
    """Ingest one event envelope. Auth via ``X-Relay-Token`` header."""
    if x_relay_token != _token():
        raise HTTPException(status_code=401, detail="bad relay token")

    event = await request.json()

    buffer.append(event)
    try:
        with open(_ndjson_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except Exception:
        # Persistence is best-effort; never fail the ingest on disk errors.
        pass

    for q in list(subscribers):
        try:
            q.put_nowait(event)
        except Exception:
            pass

    return JSONResponse({"ok": True}, status_code=202)


@app.get("/stream")
async def stream(once: bool = False):
    """Server-Sent Events: replay the buffer, then stream new events.

    ``once=1`` replays the current backlog and then closes the stream instead of
    blocking on new events. Production dashboards use the default (infinite)
    stream; ``once`` exists so a sync client (e.g. tests) can read the backlog
    without deadlocking on the forever-open live tail.
    """

    async def gen():
        # Replay current backlog to late joiners first.
        for e in list(buffer):
            yield _sse(e)

        if once:
            return

        q: asyncio.Queue = asyncio.Queue()
        subscribers.add(q)
        try:
            while True:
                event = await q.get()
                yield _sse(event)
        finally:
            subscribers.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/reset")
async def reset():
    """Clear the buffer and truncate the ndjson file (between demo runs)."""
    buffer.clear()
    try:
        with open(_ndjson_path(), "w", encoding="utf-8") as f:
            f.truncate(0)
    except Exception:
        pass
    return {"ok": True}


@app.get("/replay")
async def replay():
    """Stream the ndjson file as SSE, preserving rough original timing.

    Gaps between events are derived from their ``ts`` deltas and capped at ~2s
    so a demo replay never stalls. The first event is emitted with no delay.
    """
    path = _ndjson_path()

    async def gen():
        prev_ts = None
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue

            ts = event.get("ts")
            if prev_ts is not None and isinstance(ts, (int, float)):
                gap = (ts - prev_ts) / 1000.0
                gap = max(0.0, min(gap, 2.0))  # cap at ~2s, never negative
                if gap > 0:
                    await asyncio.sleep(gap)
            if isinstance(ts, (int, float)):
                prev_ts = ts

            yield _sse(event)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# Optionally serve the dashboard if DASHBOARD_DIR is set and exists.
_dashboard_dir = os.environ.get("DASHBOARD_DIR")
if _dashboard_dir:
    try:
        if os.path.isdir(_dashboard_dir):
            from fastapi.staticfiles import StaticFiles

            app.mount("/", StaticFiles(directory=_dashboard_dir, html=True), name="dashboard")
    except Exception:
        # A missing/bad dir must never crash relay startup.
        pass
