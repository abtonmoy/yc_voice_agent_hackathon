"""Tests for the event system: EventBus envelope/sink behavior and the relay
(auth, buffer replay, ndjson persistence, reset).

We point ``RELAY_NDJSON`` at a temp file BEFORE importing the relay module so
all writes land in tmp. SSE ``/stream`` reads are made non-blocking by reading
only the backlog and breaking after N data lines.
"""

import importlib
import json
import sys

import pytest
from fastapi.testclient import TestClient


# --------------------------------------------------------------------------- #
# EventBus
# --------------------------------------------------------------------------- #
def test_eventbus_envelope_and_sink():
    import events as events_mod

    sink = events_mod.ListSink()
    # Fixed clock so we can assert ts deterministically.
    bus = events_mod.EventBus("sess_ab12", sink=sink, clock=lambda: 1748600000000)

    e0 = bus.emit("alert", {"service": "payments-api"})
    e1 = bus.emit("tool_call", {"tool": "get_alerts"})
    e2 = bus.emit("rca", {"root_cause": "pool"})

    # id format + seq increments 0,1,2
    assert [e["id"] for e in (e0, e1, e2)] == ["evt_000000", "evt_000001", "evt_000002"]
    assert [e["seq"] for e in (e0, e1, e2)] == [0, 1, 2]

    # ts is an int, session_id / type / payload preserved
    assert isinstance(e0["ts"], int) and e0["ts"] == 1748600000000
    assert e0["session_id"] == "sess_ab12"
    assert e0["type"] == "alert"
    assert e0["payload"] == {"service": "payments-api"}

    # self.events collects, and the ListSink received each envelope
    assert bus.events == [e0, e1, e2]
    assert sink.events == [e0, e1, e2]


def test_eventbus_default_clock_is_int():
    import events as events_mod

    bus = events_mod.EventBus("sess_x")
    evt = bus.emit("transcript", {"text": "hi"})
    assert isinstance(evt["ts"], int)
    assert evt["ts"] > 0


# --------------------------------------------------------------------------- #
# Relay fixture
# --------------------------------------------------------------------------- #
@pytest.fixture()
def relay_ctx(monkeypatch, tmp_path):
    """Fresh relay module wired to a temp ndjson file + known token."""
    ndjson = tmp_path / "events.ndjson"
    monkeypatch.setenv("RELAY_NDJSON", str(ndjson))
    monkeypatch.setenv("RELAY_TOKEN", "test-token")

    # Reload so module-level state (buffer, subscribers) starts clean and the
    # DASHBOARD_DIR mount logic re-runs under this env.
    sys.modules.pop("relay", None)
    relay = importlib.import_module("relay")
    relay = importlib.reload(relay)

    client = TestClient(relay.app)
    return relay, client, ndjson


def _post_event(client, seq, type="tool_call", token="test-token"):
    evt = {
        "id": f"evt_{seq:06d}",
        "seq": seq,
        "ts": 1748600000000 + seq,
        "session_id": "sess_test",
        "type": type,
        "payload": {"n": seq},
    }
    headers = {"X-Relay-Token": token} if token is not None else {}
    return evt, client.post("/events", json=evt, headers=headers)


# --------------------------------------------------------------------------- #
# Relay auth
# --------------------------------------------------------------------------- #
def test_relay_auth_ok(relay_ctx):
    _, client, _ = relay_ctx
    _, resp = _post_event(client, 0)
    assert resp.status_code in (200, 202)
    assert resp.json() == {"ok": True}


def test_relay_auth_wrong_token(relay_ctx):
    _, client, _ = relay_ctx
    _, resp = _post_event(client, 0, token="nope")
    assert resp.status_code == 401


def test_relay_auth_missing_token(relay_ctx):
    _, client, _ = relay_ctx
    _, resp = _post_event(client, 0, token=None)
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# Buffer replay over SSE (non-blocking: read backlog then break)
# --------------------------------------------------------------------------- #
def test_stream_replays_buffer(relay_ctx):
    _, client, _ = relay_ctx

    posted = []
    for i in range(3):
        evt, resp = _post_event(client, i)
        assert resp.status_code in (200, 202)
        posted.append(evt)

    got = []
    # Read ONLY the backlog: ``once=1`` closes the stream after replaying the
    # buffer, and we also break after 3 data lines, so the test never hangs on
    # the (otherwise forever-open) live tail.
    with client.stream("GET", "/stream?once=1") as r:
        for line in r.iter_lines():
            if not line:
                continue
            text = line if isinstance(line, str) else line.decode("utf-8")
            if text.startswith("data:"):
                got.append(json.loads(text[len("data:"):].strip()))
                if len(got) == 3:
                    break

    assert got == posted


# --------------------------------------------------------------------------- #
# ndjson persistence
# --------------------------------------------------------------------------- #
def test_ndjson_persistence(relay_ctx):
    _, client, ndjson = relay_ctx

    posted = []
    for i in range(3):
        evt, resp = _post_event(client, i)
        assert resp.status_code in (200, 202)
        posted.append(evt)

    lines = ndjson.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    parsed = [json.loads(ln) for ln in lines]  # each line valid JSON
    assert parsed == posted


# --------------------------------------------------------------------------- #
# Reset
# --------------------------------------------------------------------------- #
def test_reset_clears_buffer_and_truncates(relay_ctx):
    relay, client, ndjson = relay_ctx

    for i in range(3):
        _, resp = _post_event(client, i)
        assert resp.status_code in (200, 202)

    assert len(relay.buffer) == 3

    resp = client.post("/reset")
    assert resp.status_code in (200, 202)

    # Buffer empty -> a fresh /stream yields no backlog.
    assert len(relay.buffer) == 0
    # ndjson truncated.
    assert ndjson.read_text(encoding="utf-8") == ""

    # And a fresh /stream yields no backlog: ``once=1`` replays the (now empty)
    # buffer and closes immediately, so we read ZERO data lines without hanging.
    data_lines = []
    with client.stream("GET", "/stream?once=1") as r:
        for line in r.iter_lines():
            if not line:
                continue
            text = line if isinstance(line, str) else line.decode("utf-8")
            if text.startswith("data:"):
                data_lines.append(text)
    assert data_lines == []


def test_healthz(relay_ctx):
    _, client, _ = relay_ctx
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
