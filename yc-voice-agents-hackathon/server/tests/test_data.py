"""Tests for the incident-triage data layer (mock_backend.py).

Pure standard-library + pytest. Validates the shape and ground-truth invariants
of ENGINEERS / INCIDENTS / REPO_FILES, plus the deterministic follow-the-sun
routing outcome at the pinned DEMO_NOW used by the Cekura eval plan.

On Windows, zoneinfo needs the `tzdata` package; the documented test command
provides it via `--with tzdata`.
"""

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

# Import target lives in server/ (the parent of this tests/ dir).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mock_backend import ENGINEERS, INCIDENTS, REPO_FILES  # noqa: E402

REQUIRED_INCIDENT_KEYS = {
    "alert",
    "deploys",
    "logs",
    "metrics",
    "affected_team",
    "ground_truth_root_cause",
    "proposed_remediation",
    "fix",
}

CODE_FIXABLE = {"inc-1", "inc-2", "inc-4"}
OPS_ONLY = {"inc-3"}


# --- 1. Incident shape -----------------------------------------------------


def test_incident_ids():
    assert set(INCIDENTS) == {"inc-1", "inc-2", "inc-3", "inc-4"}


@pytest.mark.parametrize("inc_id", sorted(INCIDENTS))
def test_incident_required_keys(inc_id):
    inc = INCIDENTS[inc_id]
    assert REQUIRED_INCIDENT_KEYS <= set(inc), (
        f"{inc_id} missing keys: {REQUIRED_INCIDENT_KEYS - set(inc)}"
    )
    assert isinstance(inc["ground_truth_root_cause"], str)
    assert inc["ground_truth_root_cause"].strip(), f"{inc_id} empty root cause"
    assert isinstance(inc["affected_team"], str)
    assert inc["affected_team"].strip(), f"{inc_id} empty affected_team"


# --- 2. Fixes & repo files -------------------------------------------------


@pytest.mark.parametrize("inc_id", sorted(CODE_FIXABLE))
def test_code_fixable_incidents(inc_id):
    fix = INCIDENTS[inc_id]["fix"]
    assert fix is not None, f"{inc_id} should be code-fixable"
    assert fix["code_fixable"] is True
    assert fix["file"], f"{inc_id} fix has no file"
    assert fix["before"], f"{inc_id} fix has empty 'before'"
    assert fix["after"], f"{inc_id} fix has empty 'after'"
    assert fix["file"] in REPO_FILES, f"{inc_id} fix.file not in REPO_FILES"
    # Patch must be applicable by a plain string replace.
    assert fix["before"] in REPO_FILES[fix["file"]], (
        f"{inc_id} fix.before not found verbatim in REPO_FILES[{fix['file']!r}]"
    )
    assert fix["before"] != fix["after"], f"{inc_id} fix is a no-op"


@pytest.mark.parametrize("inc_id", sorted(OPS_ONLY))
def test_ops_only_incidents_have_no_fix(inc_id):
    assert INCIDENTS[inc_id]["fix"] is None, f"{inc_id} should not be code-fixable"


# --- 3. Engineer directory shape -------------------------------------------


@pytest.mark.parametrize("eid", sorted(ENGINEERS))
def test_engineer_shape(eid):
    e = ENGINEERS[eid]
    # timezone must be a real IANA zone resolvable by zoneinfo.
    ZoneInfo(e["timezone"])

    wh = e["working_hours"]
    assert isinstance(wh, tuple) and len(wh) == 2, f"{eid} working_hours not a 2-tuple"
    start, end = wh
    assert isinstance(start, int) and isinstance(end, int)
    assert 0 <= start <= 24 and 0 <= end <= 24, f"{eid} working_hours out of range"

    assert e["teams"], f"{eid} has no teams"
    assert e["phone"].startswith("+"), f"{eid} phone not E.164"


# --- 4. Every incident has an owner ----------------------------------------


@pytest.mark.parametrize("inc_id", sorted(INCIDENTS))
def test_incident_has_team_owner(inc_id):
    team = INCIDENTS[inc_id]["affected_team"]
    owners = [eid for eid, e in ENGINEERS.items() if team in e["teams"]]
    assert owners, f"{inc_id}: no engineer owns team {team!r}"


# --- 5. Determinism: follow-the-sun routing at the pinned DEMO_NOW ----------


def _local_hour(engineer, now):
    """Engineer's local clock hour at `now` (timezone-aware)."""
    return now.astimezone(ZoneInfo(engineer["timezone"])).hour


def _on_shift(engineer, now):
    start, end = engineer["working_hours"]
    return start <= _local_hour(engineer, now) < end


def test_determinism_on_shift_set_and_unique_owners():
    # The pinned routing clock from cekura-eval-plan.md (03:00 America/LA).
    now = datetime.fromisoformat("2026-05-30T03:00:00-07:00")

    on_shift = {eid for eid in ENGINEERS if _on_shift(ENGINEERS[eid], now)}

    # priya (London 11:00), lena (Berlin 12:00), raj (Bangalore 15:30) are on.
    assert {"priya", "lena", "raj"} <= on_shift, f"missing on-shift: {on_shift}"
    # diego (SF 03:00) and sam (NY 06:00) are off.
    assert "diego" not in on_shift
    assert "sam" not in on_shift

    # On-shift owners of inc-1's team (payments): Fardin (always on-call) + Priya.
    # Routing tie-breaks by sorted id, so the agent pages Fardin.
    team1 = INCIDENTS["inc-1"]["affected_team"]
    owners1 = {
        eid
        for eid in on_shift
        if team1 in ENGINEERS[eid]["teams"]
    }
    assert owners1 == {"fardin", "priya"}, f"inc-1 on-shift owners {owners1}"
    assert sorted(owners1)[0] == "fardin", "routing should page Fardin first"

    # Unique on-shift owner of inc-3's team (networking) must be lena.
    team3 = INCIDENTS["inc-3"]["affected_team"]
    owners3 = {
        eid
        for eid in on_shift
        if team3 in ENGINEERS[eid]["teams"]
    }
    assert owners3 == {"lena"}, f"inc-3 on-shift owners {owners3} != {{lena}}"
