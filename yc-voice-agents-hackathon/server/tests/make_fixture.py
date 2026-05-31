"""Fixture generator for the canonical E1 incident-triage investigation.

Pipecat-free. Replays the headline E1 scenario (inc-1, paged engineer = priya)
end-to-end through the REAL tool logic + EventBus + ListSink, producing a
fully-ordered, realistic event stream. The resulting envelopes are written as
ndjson to the dashboard fixtures dir for replay, and exposed via
``build_events()`` so the integration test can import them.

Determinism:
  * ``DEMO_NOW`` is frozen to 2026-05-30T03:00:00-07:00 (03:00 PT) so
    follow-the-sun routing deterministically chooses **priya** (London, 11:00
    local, on-shift, owns payments) over Diego (SF, asleep) and Sam (NY,
    pre-shift).
  * The EventBus clock is a fixed-step counter (seq*1000) so ``ts`` values are
    reproducible across runs.

Run::

    uv run --no-project --with pytest --with tzdata --with fastapi \
        python tests/make_fixture.py
"""

from __future__ import annotations

import json
import os
import sys

# --- Freeze the clock BEFORE importing anything that reads DEMO_NOW. ---------
os.environ["DEMO_NOW"] = "2026-05-30T03:00:00-07:00"

# Put server/ on sys.path so ``tools`` / ``mock_backend`` import cleanly
# regardless of the invocation cwd.
_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from events import EventBus, ListSink  # noqa: E402
from tools import (  # noqa: E402
    InvestigationState,
    find_on_call_engineer,
    get_alerts,
    get_deploy_history,
    get_logs,
    get_metrics,
)

SESSION_ID = "sess_e1_demo"

# Canonical fixture location (the dashboard replays this).
DASHBOARD_FIXTURE = os.path.normpath(
    os.path.join(
        _SERVER_DIR, "..", "dashboard", "fixtures", "events.ndjson"
    )
)


def _step_clock():
    """Deterministic, monotonic, fixed-step clock (seq*1000 epoch-ms)."""
    counter = {"n": 0}

    def clock() -> int:
        ts = counter["n"] * 1000
        counter["n"] += 1
        return ts

    return clock


def build_events() -> list[dict]:
    """Drive the full E1 investigation and return the ordered event envelopes.

    Interleaves hand-authored ``transcript`` events with REAL tool calls so the
    stream reads like a genuine phone call. Returns ``bus.events`` (the full
    envelopes, in emission order).
    """
    sink = ListSink()
    bus = EventBus(session_id=SESSION_ID, sink=sink, clock=_step_clock())
    emit = bus.emit

    state = InvestigationState(active_incident_id="inc-1")
    remediator = state.remediator

    def transcript(role: str, text: str, final: bool = True) -> None:
        emit("transcript", {"role": role, "text": text, "final": final})

    # 1. session_start
    emit("session_start", {"session_id": SESSION_ID, "backend": "claude"})

    # 2. user opener
    transcript(
        "user",
        "I'm getting paged — payments API is throwing a ton of 500s, "
        "started a few minutes ago.",
        final=True,
    )

    # 3. agent acknowledges
    transcript(
        "agent",
        "On it — let me check what's firing and any recent deploys.",
    )

    # 4. get_alerts -> tool_call / alert / tool_result
    get_alerts(state, emit=emit)

    # 5. get_deploy_history -> finds abc123
    get_deploy_history(state, emit=emit, service="payments-api")

    # 6. agent notes the 14:30 deploy
    transcript(
        "agent",
        "There's a deploy abc123 to payments-api at 14:30, right before the "
        "errors started at 14:32. Let me pull the logs and metrics.",
    )

    # 7. get_logs then get_metrics
    get_logs(state, emit=emit, service="payments-api")
    get_metrics(state, emit=emit, name="payments-db active_connections")

    # 8. agent synthesizes
    transcript(
        "agent",
        "The logs show payments-db is out of connection slots and the pool is "
        "pegged at 100/100 — deploy abc123 exhausted the payments-db pool.",
    )

    # 9. rca (the bot's report_root_cause path — emit directly)
    incident = state.incident
    emit(
        "rca",
        {
            "root_cause": incident["ground_truth_root_cause"],
            "evidence": (
                "deploy abc123 @14:30 immediately precedes 5xx onset @14:32; "
                "logs: FATAL remaining connection slots reserved → payments-db; "
                "metrics: payments-db active_connections 100/100 @14:31"
            ),
            "remediation": incident["proposed_remediation"],
            "code_fixable": True,
        },
    )

    # 10. agent: page the owner who is awake
    transcript(
        "agent",
        "This is a database problem — let me page whoever owns it and is awake.",
    )

    # 11. find_on_call_engineer -> routing_decision (chooses priya)
    routing = find_on_call_engineer("payments", emit=emit)
    chosen = routing["chosen"]
    engineer_id = chosen["id"]
    engineer_name = chosen["name"]

    # 12. outbound_call ringing -> answered
    emit(
        "outbound_call",
        {
            "engineer_id": engineer_id,
            "engineer": engineer_name,
            "phone_masked": "+1*****6634",
            "status": "ringing",
        },
    )
    emit(
        "outbound_call",
        {
            "engineer_id": engineer_id,
            "engineer": engineer_name,
            "phone_masked": "+1*****6634",
            "status": "answered",
        },
    )

    # 13. agent: paging the on-call engineer
    transcript(
        "agent",
        f"Paging {engineer_name} — on-shift, owns payments. Calling now.",
    )

    # 14. read_repo_file db.py
    remediator.read_repo_file("app/db.py", emit=emit)

    # 15. propose_code_fix -> fix_proposed
    remediator.propose_code_fix("inc-1", emit=emit)

    # 16. agent describes the fix and asks to apply
    transcript(
        "agent",
        "I found it in db.py — the pool was created with no cap in abc123. "
        "I can apply a one-line fix. Want me to?",
    )

    # 17. engineer approves
    transcript("user", "Yes, do it.")

    # 18. apply_code_fix(True) -> fix_decision(approved) + code_fix(applied)
    remediator.apply_code_fix(True, emit=emit)

    # 19. agent confirms
    transcript(
        "agent",
        "Done — capped the pool at 20. Errors should drain. Anything else?",
    )

    # 20. user wraps up
    transcript("user", "No, thanks.")

    # 21. agent sign-off
    transcript("agent", "Thanks — take care.")

    # 22. session_end
    emit("session_end", {"reason": "end_call"})

    return bus.events


def _write_ndjson(path: str, events: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def main() -> int:
    events = build_events()
    _write_ndjson(DASHBOARD_FIXTURE, events)
    print(f"wrote {len(events)} events -> {DASHBOARD_FIXTURE}")
    types = [e["type"] for e in events]
    print("event types:", " ".join(types))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
