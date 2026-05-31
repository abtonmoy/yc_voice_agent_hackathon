"""Backend integration test for the E1 incident-triage investigation.

Two layers:

1. **Tool-logic + event-bus integration** — imports ``build_events`` from
   ``make_fixture`` (which drives the REAL tools through an ``EventBus`` +
   ``ListSink``) and asserts the event envelopes + the E1 milestone ordering,
   the routing choice (priya), the single approved fix_decision / applied
   code_fix, and the envelope contract (keys + strictly-increasing seq from 0).

2. **Relay replay integration** — loads the fixture events into the REAL
   ``relay.buffer`` and drives the REAL ``relay.stream(once=True)`` async
   generator (the production SSE backlog-replay path), asserting the subscriber
   receives ALL fixture events in the same order. This proves the full
   tools -> events -> relay pipeline end-to-end.

Pure python — no pipecat import (relay needs fastapi only).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

# server/ on sys.path (conftest also does this, belt-and-suspenders for direct runs).
_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from make_fixture import build_events  # noqa: E402


# ---------------------------------------------------------------------------
# Layer 1: tool-logic + event-bus integration
# ---------------------------------------------------------------------------


def test_envelope_contract():
    """Every event has the full envelope and seq is strictly increasing from 0."""
    events = build_events()
    assert events, "no events produced"
    expected_keys = {"id", "seq", "ts", "session_id", "type", "payload"}
    for i, ev in enumerate(events):
        assert set(ev.keys()) == expected_keys, ev
        assert ev["seq"] == i, f"seq not strictly increasing at index {i}: {ev['seq']}"
        assert ev["session_id"], ev
        assert isinstance(ev["payload"], dict), ev
    assert events[0]["seq"] == 0


def _types(events):
    return [e["type"] for e in events]


def _assert_in_order(seq, milestones):
    """Assert each milestone (predicate) matches a later event than the prior."""
    idx = -1
    for label, pred in milestones:
        found = None
        for i in range(idx + 1, len(seq)):
            if pred(seq[i]):
                found = i
                break
        assert found is not None, (
            f"milestone '{label}' not found after index {idx}; "
            f"types so far: {[e['type'] for e in seq]}"
        )
        idx = found


def test_e1_milestone_sequence():
    """Key E1 milestones appear in the expected order."""
    events = build_events()

    milestones = [
        ("session_start", lambda e: e["type"] == "session_start"),
        ("alert", lambda e: e["type"] == "alert"),
        (
            "get_deploy_history tool_result mentioning abc123",
            lambda e: (
                e["type"] == "tool_result"
                and e["payload"].get("tool") == "get_deploy_history"
                and "abc123" in e["payload"].get("summary", "")
            ),
        ),
        (
            "rca code_fixable True",
            lambda e: e["type"] == "rca" and e["payload"].get("code_fixable") is True,
        ),
        ("routing_decision", lambda e: e["type"] == "routing_decision"),
        (
            "outbound_call answered",
            lambda e: e["type"] == "outbound_call"
            and e["payload"].get("status") == "answered",
        ),
        ("fix_proposed", lambda e: e["type"] == "fix_proposed"),
        (
            "fix_decision approved True",
            lambda e: e["type"] == "fix_decision"
            and e["payload"].get("approved") is True,
        ),
        (
            "code_fix applied True",
            lambda e: e["type"] == "code_fix" and e["payload"].get("applied") is True,
        ),
        ("session_end", lambda e: e["type"] == "session_end"),
    ]
    _assert_in_order(events, milestones)


def test_routing_choice_is_fardin():
    events = build_events()
    routing = [e for e in events if e["type"] == "routing_decision"]
    assert len(routing) == 1, f"expected exactly one routing_decision, got {len(routing)}"
    chosen = routing[0]["payload"]["chosen"]
    assert chosen is not None, "routing produced no chosen engineer"
    assert chosen["id"] == "fardin", f"expected fardin, got {chosen['id']}"


def test_exactly_one_approval_and_applied_fix():
    events = build_events()

    approved = [
        e
        for e in events
        if e["type"] == "fix_decision" and e["payload"].get("approved") is True
    ]
    assert len(approved) == 1, f"expected one approved fix_decision, got {len(approved)}"

    applied = [
        e
        for e in events
        if e["type"] == "code_fix" and e["payload"].get("applied") is True
    ]
    assert len(applied) == 1, f"expected one applied code_fix, got {len(applied)}"


# ---------------------------------------------------------------------------
# Layer 2: relay replay integration (tools -> events -> relay end-to-end)
# ---------------------------------------------------------------------------


def _parse_sse(chunk: str) -> dict:
    assert chunk.startswith("data:"), chunk
    return json.loads(chunk[len("data:"):].strip())


async def _drive_relay_replay():
    """Load the fixture into relay.buffer and drain the REAL stream(once=True).

    ``once=True`` returns after the backlog so this never hangs on the live
    tail. Returns ``(received, events, lingering_subscribers)``.
    """
    import relay

    events = build_events()

    relay.buffer.clear()
    relay.subscribers.clear()
    for ev in events:
        relay.buffer.append(ev)

    response = await relay.stream(once=True)
    gen = response.body_iterator

    received: list[dict] = []
    async for chunk in gen:
        received.append(_parse_sse(chunk))

    return received, events, len(relay.subscribers)


def test_relay_replays_all_fixture_events_in_order():
    """Subscriber receives every fixture event, in order, via the real relay.

    Proves tools -> events -> relay end-to-end. Drives the async generator with
    ``asyncio.run`` so the test stays dependency-free (no pytest-asyncio).
    """
    received, events, lingering = asyncio.run(_drive_relay_replay())

    assert len(received) == len(events), (
        f"relay yielded {len(received)} events, expected {len(events)}"
    )
    # Same order, same identity (seq + type) across the full stream.
    for got, want in zip(received, events):
        assert got["seq"] == want["seq"], (got, want)
        assert got["type"] == want["type"], (got, want)

    # No live subscriber should linger after a once=True backlog drain.
    assert lingering == 0
