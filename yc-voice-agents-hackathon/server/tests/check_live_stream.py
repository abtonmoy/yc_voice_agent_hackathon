"""Integration check: the relay's LIVE SSE fan-out (production path).

Drives the REAL /stream async generator in-process: registers a subscriber via
the generator, then ingests a new event and asserts the generator yields it.
This avoids httpx ASGITransport (which can't stream an infinite response
incrementally) while still exercising the actual relay handlers.

Run: uv run --no-project --with fastapi python tests/check_live_stream.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # server/
os.environ["RELAY_TOKEN"] = "tkn"
os.environ["RELAY_NDJSON"] = os.path.join(os.path.dirname(__file__), "_live_check.ndjson")

import relay
from fastapi import HTTPException


class _Req:
    def __init__(self, d):
        self._d = d

    async def json(self):
        return self._d


def envelope(seq, typ):
    return {"id": f"evt_{seq:06d}", "seq": seq, "ts": 1000 + seq,
            "session_id": "s", "type": typ, "payload": {"n": seq}}


def _parse(chunk):
    assert chunk.startswith("data:"), chunk
    return json.loads(chunk[5:].strip())


async def main():
    relay.buffer.clear()
    relay.subscribers.clear()

    # token auth: wrong token rejected
    try:
        await relay.ingest(_Req(envelope(99, "x")), x_relay_token="wrong")
        print("FAIL: bad token accepted"); raise SystemExit(1)
    except HTTPException as e:
        assert e.status_code == 401

    # backlog event present before any subscriber connects
    relay.buffer.append(envelope(0, "alert"))

    # drive the real /stream generator
    gen = (await relay.stream(once=False)).body_iterator
    backlog = _parse(await gen.__anext__())          # yields backlog (seq 0)

    live_task = asyncio.create_task(gen.__anext__())  # registers subscriber, then awaits
    await asyncio.sleep(0)                             # let it register
    assert len(relay.subscribers) == 1, "subscriber not registered"

    # ingest a LIVE event after the subscriber connected
    await relay.ingest(_Req(envelope(1, "code_fix")), x_relay_token="tkn")
    live = _parse(await asyncio.wait_for(live_task, timeout=5))

    assert backlog["type"] == "alert" and backlog["seq"] == 0, backlog
    assert live["type"] == "code_fix" and live["seq"] == 1, live

    # ndjson persisted the live event
    with open(os.environ["RELAY_NDJSON"], encoding="utf-8") as f:
        lines = [json.loads(x) for x in f if x.strip()]
    assert any(e["seq"] == 1 for e in lines), "live event not persisted"

    print("PASS: 401 auth + backlog replay + live fan-out + ndjson ->",
          [backlog["type"], live["type"]])


asyncio.run(main())
