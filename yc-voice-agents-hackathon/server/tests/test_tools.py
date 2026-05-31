"""Extensive unit tests for the pure incident-triage tool logic.

Capturing emit pattern: ``events=[]; emit=lambda t,p: events.append((t,p))``.
No pipecat / fastapi here — pure logic tests.
"""

from datetime import datetime

import importlib

import pytest

import mock_backend
from mock_backend import INCIDENTS, REPO_FILES
from tools import (
    InvestigationState,
    Remediator,
    find_on_call_engineer,
    get_alerts,
    get_deploy_history,
    get_logs,
    get_metrics,
)


# Frozen routing clock from lld-backend.md §10 / §11.
NOW_DEMO = datetime.fromisoformat("2026-05-30T03:00:00-07:00")
# Follow-the-sun second clock (see test docstring for the derivation).
NOW_FTS = datetime.fromisoformat("2026-05-30T04:00:00+00:00")


def make_emit():
    events = []
    return events, (lambda t, p: events.append((t, p)))


def state_for(incident_id):
    s = InvestigationState()
    s.set_incident(incident_id)
    return s


@pytest.fixture(autouse=True)
def _restore_repo_files():
    """Each test gets a pristine REPO_FILES (apply_code_fix mutates it)."""
    snapshot = dict(REPO_FILES)
    yield
    REPO_FILES.clear()
    REPO_FILES.update(snapshot)


# ---------------------------------------------------------------------------
# Diagnostic
# ---------------------------------------------------------------------------
def test_get_alerts_returns_alert_and_emits():
    events, emit = make_emit()
    res = get_alerts(state_for("inc-1"), emit=emit)
    assert "payments-api 5xx >20%" in res["summary"]
    types = [t for t, _ in events]
    assert types == ["tool_call", "alert", "tool_result"]
    alert_payload = dict(events)["alert"]
    assert alert_payload == {
        "title": "payments-api 5xx >20%",
        "service": "payments-api",
        "severity": "P1",
        "started_at": "14:32",
    }
    tr = dict(events)["tool_result"]
    assert tr["tool"] == "get_alerts" and tr["summary"]


def test_get_deploy_history_inc1_mentions_commit():
    events, emit = make_emit()
    res = get_deploy_history(state_for("inc-1"), emit=emit)
    assert "abc123" in res["summary"]
    assert [t for t, _ in events] == ["tool_call", "tool_result"]


def test_get_deploy_history_inc2_no_deploys():
    events, emit = make_emit()
    res = get_deploy_history(state_for("inc-2"), emit=emit)
    s = res["summary"].lower()
    assert "no deploys" in s and "24h" in s
    assert [t for t, _ in events] == ["tool_call", "tool_result"]


def test_get_logs_summary_and_events():
    events, emit = make_emit()
    res = get_logs(state_for("inc-1"), emit=emit, service="payments-api")
    assert res["summary"].strip()
    assert "connection slots" in res["summary"]
    assert [t for t, _ in events] == ["tool_call", "tool_result"]


def test_get_metrics_summary_and_events():
    events, emit = make_emit()
    res = get_metrics(state_for("inc-1"), emit=emit, name="active_connections")
    assert res["summary"].strip()
    assert "100/100" in res["summary"]
    assert [t for t, _ in events] == ["tool_call", "tool_result"]


def test_diagnostic_default_emit_is_noop():
    # No emit passed -> must not raise.
    assert get_alerts(state_for("inc-4"))["summary"]
    assert get_deploy_history(state_for("inc-4"))["summary"]


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------
def test_routing_payments_chooses_fardin():
    events, emit = make_emit()
    res = find_on_call_engineer("payments", emit=emit, now=NOW_DEMO)
    # Fardin is always on-call and owns payments; sorts first -> the agent pages him.
    assert res["chosen"]["id"] == "fardin"
    # diego is off-shift / asleep at 03:00 SF.
    by_name = {c["name"]: c for c in res["candidates"]}
    assert by_name["Diego"]["excluded_reason"] in ("asleep", "off-shift")
    assert by_name["Diego"]["on_shift"] is False
    assert [t for t, _ in events] == ["routing_decision"]


def test_routing_networking_chooses_lena_expertise_beats_availability():
    res = find_on_call_engineer("networking", now=NOW_DEMO)
    # priya & raj are awake but wrong team -> must NOT be chosen.
    assert res["chosen"]["id"] == "lena"
    by_name = {c["name"]: c for c in res["candidates"]}
    assert by_name["Priya"]["on_shift"] is True
    assert by_name["Priya"]["excluded_reason"] == "wrong team"
    assert by_name["Raj"]["excluded_reason"] == "wrong team"


def test_routing_chosen_is_always_on_shift_no_false_wake():
    for area in ("payments", "networking", "database", "infra"):
        res = find_on_call_engineer(area, now=NOW_DEMO)
        if res["chosen"] is None:
            continue
        cname = res["chosen"]["name"]
        cand = next(c for c in res["candidates"] if c["name"] == cname)
        assert cand["on_shift"] is True, f"woke an off-shift engineer for {area}"


def test_routing_follow_the_sun_changes_choice():
    """At NOW_DEMO (2026-05-30 03:00 -07:00 = 10:00 UTC) the on-shift owners of
    'database' are priya (London 11:00) and raj (Bangalore 15:30); sorted-id
    tie-break picks priya.

    At NOW_FTS (2026-05-30 04:00 UTC) priya is off-shift (London 05:00) while
    raj is on-shift (Bangalore 09:30) -> raj becomes the unique on-shift owner,
    so the choice flips. Proves follow-the-sun routing.
    """
    first = find_on_call_engineer("database", now=NOW_DEMO)
    second = find_on_call_engineer("database", now=NOW_FTS)
    assert first["chosen"]["id"] == "priya"
    assert second["chosen"]["id"] == "raj"
    assert first["chosen"]["id"] != second["chosen"]["id"]


def test_routing_emits_routing_decision_payload_shape():
    events, emit = make_emit()
    find_on_call_engineer("payments", emit=emit, now=NOW_DEMO)
    assert len(events) == 1
    typ, payload = events[0]
    assert typ == "routing_decision"
    assert set(payload) == {"chosen", "backup", "reason", "candidates"}
    for c in payload["candidates"]:
        assert set(c) == {"name", "local_time", "on_shift", "teams", "excluded_reason"}


def test_routing_no_oncall_available():
    # 'web' is owned only by diego, who is off-shift at NOW_DEMO -> no on-shift
    # owner. Fallback surfaces diego (same team) rather than None.
    res = find_on_call_engineer("web", now=NOW_DEMO)
    if res["chosen"] is not None:
        assert res["chosen"]["id"] == "diego"
    # A truly unknown area has nobody -> chosen None, explicit reason.
    res2 = find_on_call_engineer("nonexistent-area", now=NOW_DEMO)
    assert res2["chosen"] is None
    assert res2["reason"] == "no on-call available"


# ---------------------------------------------------------------------------
# Remediation — the SAFETY-CRITICAL two-step gate
# ---------------------------------------------------------------------------
def test_propose_sets_pending_and_emits_fix_proposed():
    events, emit = make_emit()
    r = Remediator()
    res = r.propose_code_fix("inc-1", emit=emit)
    assert res["needs_approval"] is True
    after = INCIDENTS["inc-1"]["fix"]["after"]
    assert after in res["diff"]
    assert [t for t, _ in events] == ["fix_proposed"]
    payload = events[0][1]
    assert payload["incident_id"] == "inc-1"
    assert payload["file"] == "app/db.py"
    assert after in payload["diff"]
    assert r._pending is not None


def test_apply_false_after_propose_is_refused_pending_kept():
    events, emit = make_emit()
    r = Remediator()
    r.propose_code_fix("inc-1", emit=emit)
    events.clear()
    res = r.apply_code_fix(False, emit=emit)
    assert res["applied"] is False
    assert "refusing" in res["reason"]
    types = [t for t, _ in events]
    assert "code_fix" not in types
    assert types == ["fix_decision"]
    assert events[0][1] == {"approved": False}
    # pending must still be set (refusal does not clear it).
    assert r._pending is not None


def test_apply_true_without_propose_is_refused():
    events, emit = make_emit()
    r = Remediator()  # fresh, no pending
    res = r.apply_code_fix(True, emit=emit)
    assert res["applied"] is False
    types = [t for t, _ in events]
    assert "code_fix" not in types
    assert types == ["fix_decision"]
    assert events[0][1] == {"approved": False}


def test_propose_then_apply_true_patches_session_not_global():
    events, emit = make_emit()
    r = Remediator()
    before = INCIDENTS["inc-1"]["fix"]["before"]
    after = INCIDENTS["inc-1"]["fix"]["after"]
    file = INCIDENTS["inc-1"]["fix"]["file"]

    assert before in REPO_FILES[file]
    r.propose_code_fix("inc-1", emit=emit)
    events.clear()

    res = r.apply_code_fix(True, emit=emit)
    assert res == {"applied": True, "file": file}
    types = [t for t, _ in events]
    assert types == ["fix_decision", "code_fix"]
    assert events[0][1] == {"approved": True}
    cf = events[1][1]
    assert cf["applied"] is True and cf["file"] == file

    # GLOBAL repo is NOT mutated -> a later demo run still works.
    assert before in REPO_FILES[file]
    assert after not in REPO_FILES[file]
    # The per-session view IS patched.
    view = r.read_repo_file(file)
    assert view["found"] is True
    assert after in view["content"] and before not in view["content"]

    # pending cleared -> a SECOND apply is refused.
    events.clear()
    res2 = r.apply_code_fix(True, emit=emit)
    assert res2["applied"] is False
    assert "code_fix" not in [t for t, _ in events]


def test_read_repo_file_pristine_then_missing():
    r = Remediator()
    file = INCIDENTS["inc-1"]["fix"]["file"]
    before = INCIDENTS["inc-1"]["fix"]["before"]
    events, emit = make_emit()
    pristine = r.read_repo_file(file, emit=emit)
    assert pristine["found"] is True and before in pristine["content"]
    assert [t for t, _ in events] == ["tool_call", "tool_result"]
    assert r.read_repo_file("app/nope.py")["found"] is False


def test_apply_does_not_break_across_independent_sessions():
    """Two fresh Remediators must each apply inc-1 — i.e. apply must not mutate
    global REPO_FILES (regression for the cross-run crash)."""
    file = INCIDENTS["inc-1"]["fix"]["file"]
    after = INCIDENTS["inc-1"]["fix"]["after"]
    for _ in range(2):
        r = Remediator()
        r.propose_code_fix("inc-1")
        assert r.apply_code_fix(True)["applied"] is True
        assert after in r.read_repo_file(file)["content"]


def test_propose_non_code_incident_returns_advice_no_pending():
    events, emit = make_emit()
    r = Remediator()
    res = r.propose_code_fix("inc-3", emit=emit)  # cert rotation, fix=None
    assert res["code_fixable"] is False
    assert res["advice"] == INCIDENTS["inc-3"]["proposed_remediation"]
    assert events == []  # nothing destructive emitted
    assert r._pending is None

    # A following apply(True) must be refused (no pending).
    res2 = r.apply_code_fix(True, emit=emit)
    assert res2["applied"] is False
    assert "code_fix" not in [t for t, _ in events]


def test_investigation_state_owns_remediator():
    s = InvestigationState()
    assert isinstance(s.remediator, Remediator)
    s.set_incident("inc-1")
    assert s.incident is INCIDENTS["inc-1"]


def test_no_pipecat_import_in_tools():
    import tools
    mod = importlib.import_module("tools")
    # smoke: importing the package pulled in submodules with zero pipecat.
    import sys
    assert not any(
        name == "pipecat" or name.startswith("pipecat.")
        for name in sys.modules
    )
