"""Event system for the incident-triage voice agent.

``EventBus`` is the in-bot event producer: tools call ``emit(type, payload)``
and the bus stamps an envelope (see lld-backend.md §3) and forwards it to an
optional ``sink``. Sinks are pluggable so tests can collect events in a list
(``ListSink``) while production fires them at the relay over HTTP
(``make_http_relay_sink``).

NO fastapi import here, and ``httpx`` is imported LAZILY only inside the HTTP
sink — so this module imports cleanly even when httpx isn't installed.
"""

from __future__ import annotations

import time
from typing import Callable


def _default_clock() -> int:
    """Epoch milliseconds as an int."""
    return int(time.time() * 1000)


class EventBus:
    """Per-session event producer.

    One ``EventBus`` is created per session in the bot and closed over by the
    tools (which call ``emit`` synchronously). The bus assigns a monotonic
    ``seq`` (starting at 0), stamps a timestamp via ``clock``, records every
    envelope in ``self.events`` for inspection/tests, and forwards it to the
    optional ``sink``.
    """

    def __init__(
        self,
        session_id: str,
        sink: Callable[[dict], None] | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.session_id = session_id
        self.sink = sink
        self.clock = clock or _default_clock
        self.seq = 0
        self.events: list[dict] = []

    def emit(self, type: str, payload: dict) -> dict:
        """Build the envelope, record it, and fire it at the sink.

        SYNC by design — the tools that call this are sync, and the HTTP sink
        is fire-and-forget so it never blocks the voice path.
        """
        envelope = {
            "id": f"evt_{self.seq:06d}",
            "seq": self.seq,
            "ts": self.clock(),
            "session_id": self.session_id,
            "type": type,
            "payload": payload,
        }
        self.seq += 1
        self.events.append(envelope)
        if self.sink is not None:
            self.sink(envelope)
        return envelope


class ListSink:
    """Trivial callable sink that collects envelopes into ``.events`` (tests)."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def __call__(self, envelope: dict) -> None:
        self.events.append(envelope)


def make_http_relay_sink(relay_url: str, token: str) -> Callable[[dict], None]:
    """Return a fire-and-forget sink that POSTs envelopes to the relay.

    Posts to ``{relay_url}/events`` with header ``X-Relay-Token: token``.
    ``httpx`` is imported lazily so this module stays importable without it.

    If an asyncio loop is running we schedule the POST as a background task on
    an ``httpx.AsyncClient`` (never blocking the voice path); otherwise we do a
    best-effort synchronous POST. ALL exceptions are swallowed — a relay outage
    must never affect the caller.
    """

    url = relay_url.rstrip("/") + "/events"
    headers = {"X-Relay-Token": token}

    def sink(envelope: dict) -> None:
        try:
            import asyncio

            import httpx  # lazy: keep events.py importable without httpx

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop is not None:
                async def _post() -> None:
                    try:
                        async with httpx.AsyncClient(timeout=2.0) as client:
                            await client.post(url, json=envelope, headers=headers)
                    except Exception:
                        pass

                loop.create_task(_post())
            else:
                try:
                    httpx.post(url, json=envelope, headers=headers, timeout=2.0)
                except Exception:
                    pass
        except Exception:
            # Swallow EVERYTHING (including a missing httpx) — never affect the caller.
            pass

    return sink
