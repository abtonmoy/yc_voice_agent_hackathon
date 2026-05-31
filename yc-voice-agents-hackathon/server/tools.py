"""Pure, unit-testable core tool logic for the incident-triage voice agent.

ZERO pipecat / fastapi imports anywhere in this module — the Pipecat bot wraps
these functions later. Every tool returns a ``dict`` (the value the bot's
``result_callback`` sends to the LLM) and emits dashboard events via an
injectable ``emit`` callback (default no-op).

This is a single flattened module (previously the ``tools/`` package): it
combines remediation (the safety-critical two-step approval gate), shared
investigation state, the diagnostic tools, and follow-the-sun routing. It stays
pure standard-library Python so the tool logic is fully unit-testable.
"""

from __future__ import annotations

import difflib
import os
from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from mock_backend import INCIDENTS, REPO_FILES, ENGINEERS

# Default no-op emit so tools work without a wired event bus; tests inject a
# capturing callback to assert event types/payloads.
Emit = Callable[[str, dict], None]


def _noop(*_args, **_kwargs) -> None:  # pragma: no cover - trivial
    pass


# ---------------------------------------------------------------------------
# Remediation — the SAFETY-CRITICAL two-step approval gate.
#
# A fix is *never* applied by a single call. ``propose_code_fix`` stages a
# pending patch and asks for approval; ``apply_code_fix`` only "applies" when
# (a) a pending proposal exists from a prior ``propose_code_fix`` AND (b) the
# engineer explicitly approved. The gate lives in code, not just the prompt.
#
# Patches are **per-session**: applying records the patched content on this
# ``Remediator`` only — the shared ``mock_backend.REPO_FILES`` is never mutated,
# so repeated demo runs (and concurrent sessions) stay independent. The
# authoritative artifact is the ``code_fix`` event (carries before/after/diff);
# ``read_repo_file`` reflects this session's applied patches.
# ---------------------------------------------------------------------------


def _unified(before: str, after: str, path: str) -> str:
    """Build a unified diff string for ``before`` -> ``after`` on ``path``."""
    # Ensure each side ends with a newline so single-line fixes still produce
    # properly separated -/+ lines (not "-OLD+NEW" joined on one physical line).
    b = before if before.endswith("\n") else before + "\n"
    a = after if after.endswith("\n") else after + "\n"
    diff_lines = difflib.unified_diff(
        b.splitlines(keepends=True),
        a.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        lineterm="\n",
    )
    return "".join(diff_lines)


class Remediator:
    """Owns the pending-fix state AND this session's applied patches.

    Never mutates the global ``REPO_FILES`` — applied content lives in
    ``self.applied_files`` so a second demo run starts from a pristine repo.
    """

    def __init__(self) -> None:
        self._pending: dict | None = None
        self.applied_files: dict[str, str] = {}  # path -> patched content (this session)

    def read_repo_file(self, path: str, emit: Emit = _noop) -> dict:
        """Read a file from the monitored repo (read-only).

        Returns this session's patched content if a fix was applied, else the
        pristine ``REPO_FILES`` content. Missing files return ``found: False``
        rather than raising.
        """
        emit("tool_call", {"tool": "read_repo_file", "args": {"path": path}})
        content = self.applied_files.get(path, REPO_FILES.get(path))
        if content is None:
            summary = f"No such file: {path}"
            emit("tool_result", {"tool": "read_repo_file", "summary": summary})
            return {"path": path, "found": False, "summary": summary}
        summary = f"Read {path} ({len(content.splitlines())} lines)"
        emit("tool_result", {"tool": "read_repo_file", "summary": summary})
        return {"path": path, "found": True, "content": content}

    def propose_code_fix(self, incident_id: str, emit: Emit = _noop) -> dict:
        """Propose (but do NOT apply) the fix for ``incident_id``.

        For non-code-fixable incidents (``fix`` is ``None`` / ``code_fixable``
        false), returns advisory remediation, sets no pending state, and emits
        nothing destructive. Otherwise stages the patch, emits ``fix_proposed``,
        and returns the diff with ``needs_approval`` true.
        """
        incident = INCIDENTS[incident_id]
        fix = incident.get("fix")
        if not fix or not fix.get("code_fixable"):
            return {
                "code_fixable": False,
                "advice": incident["proposed_remediation"],
            }

        before = fix["before"]
        after = fix["after"]
        file = fix["file"]
        diff = _unified(before, after, file)
        summary = f"Proposed fix for {incident_id}: edit {file}"

        self._pending = {
            "incident_id": incident_id,
            "file": file,
            "before": before,
            "after": after,
            "diff": diff,
        }
        emit(
            "fix_proposed",
            {
                "incident_id": incident_id,
                "file": file,
                "diff": diff,
                "summary": summary,
            },
        )
        return {"diff": diff, "needs_approval": True}

    def apply_code_fix(self, engineer_approved: bool, emit: Emit = _noop) -> dict:
        """Apply the previously proposed fix — HARD GATE.

        Refuses (applies/clears nothing) unless a pending proposal exists AND
        ``engineer_approved is True``. On success records the patched content on
        this session (never the global repo), emits ``fix_decision`` then
        ``code_fix``, and clears the pending state.
        """
        if self._pending is None or engineer_approved is not True:
            emit("fix_decision", {"approved": False})
            return {
                "applied": False,
                "reason": "no approved pending fix — refusing",
            }

        pending = self._pending
        file = pending["file"]
        before = pending["before"]
        after = pending["after"]

        # Base on this session's prior patch if any, else the pristine repo file.
        base = self.applied_files.get(file, REPO_FILES.get(file, ""))
        patched = base.replace(before, after) if before in base else base
        self.applied_files[file] = patched  # per-session only — global repo untouched

        emit("fix_decision", {"approved": True})
        emit(
            "code_fix",
            {
                "file": file,
                "before": before,
                "after": after,
                "diff": pending["diff"],
                "applied": True,
            },
        )
        self._pending = None
        return {"applied": True, "file": file}


# ---------------------------------------------------------------------------
# Shared, pure investigation state for the incident-triage tools.
# ---------------------------------------------------------------------------


def demo_now() -> datetime:
    """Return the current time as an *aware* datetime.

    If the ``DEMO_NOW`` env var is set it is parsed as an ISO8601 timestamp
    *with* a UTC offset (e.g. ``2026-05-30T03:00:00-07:00``) and used as a
    frozen clock for deterministic routing demos. Otherwise the real wall
    clock (``datetime.now(timezone.utc)``) is returned.
    """
    raw = os.environ.get("DEMO_NOW")
    if raw:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return datetime.now(timezone.utc)


class InvestigationState:
    """Per-session investigation state shared across the diagnostic tools.

    Holds the currently active incident id and owns exactly one
    :class:`Remediator` (the safety-critical two-step fix gate).
    """

    def __init__(self, active_incident_id: str | None = None) -> None:
        self.active_incident_id: str | None = active_incident_id
        self.remediator: Remediator = Remediator()

    def set_incident(self, incident_id: str) -> None:
        """Set the active incident id for this investigation."""
        self.active_incident_id = incident_id

    @property
    def incident(self) -> dict:
        """The active incident record from ``mock_backend.INCIDENTS``.

        Raises ``KeyError`` if no incident is active / the id is unknown — the
        diagnostic tools assume an incident has been set.
        """
        return INCIDENTS[self.active_incident_id]


# ---------------------------------------------------------------------------
# Diagnostic tools — synthesize summaries for the LLM, emit dashboard events.
#
# Each function takes the shared :class:`InvestigationState`, an injectable
# ``emit`` callback (default no-op), and returns a ``dict`` whose ``summary`` is
# a synthesized string — never raw rows. Each emits ``tool_call`` then
# ``tool_result``; ``get_alerts`` additionally emits ``alert``.
# ---------------------------------------------------------------------------


def get_alerts(state, emit: Emit = _noop) -> dict:
    """Return a summary of the active paging alert for the investigation."""
    emit("tool_call", {"tool": "get_alerts", "args": {}})
    alert = state.incident["alert"]
    summary = (
        f"Active {alert['severity']} alert: {alert['title']} on "
        f"{alert['service']}, firing since {alert['started_at']}."
    )
    emit(
        "alert",
        {
            "title": alert["title"],
            "service": alert["service"],
            "severity": alert["severity"],
            "started_at": alert["started_at"],
        },
    )
    emit("tool_result", {"tool": "get_alerts", "summary": summary})
    return {"summary": summary}


def get_deploy_history(state, emit: Emit = _noop, service: str | None = None) -> dict:
    """Return a summary of recent deploys for the affected service.

    If the incident has no deploys in the window, the summary explicitly says
    there were no deploys in the last 24h (the discrimination trap vs a
    deploy-caused incident).
    """
    emit(
        "tool_call",
        {"tool": "get_deploy_history", "args": {"service": service}},
    )
    deploys = state.incident["deploys"]
    if not deploys:
        summary = "No deploys in the last 24h for the affected service."
    else:
        parts = [
            f"{d['id']} on {d['service']} at {d['at']}" for d in deploys
        ]
        summary = "Recent deploys in the last 24h: " + "; ".join(parts) + "."
    emit("tool_result", {"tool": "get_deploy_history", "summary": summary})
    return {"summary": summary}


def get_logs(state, emit: Emit = _noop, service: str | None = None) -> dict:
    """Return a pre-correlated summary of recent error logs for the service."""
    emit("tool_call", {"tool": "get_logs", "args": {"service": service}})
    logs = state.incident["logs"]
    summary = "Correlated log signal: " + " | ".join(logs)
    emit("tool_result", {"tool": "get_logs", "summary": summary})
    return {"summary": summary}


def get_metrics(state, emit: Emit = _noop, name: str | None = None) -> dict:
    """Return a snapshot summary of the key metric series for the incident."""
    emit("tool_call", {"tool": "get_metrics", "args": {"name": name}})
    metrics = state.incident["metrics"]
    summary = "Key metric snapshot: " + " | ".join(metrics)
    emit("tool_result", {"tool": "get_metrics", "summary": summary})
    return {"summary": summary}


# ---------------------------------------------------------------------------
# Routing — follow-the-sun on-call selection.
#
# Filter by working hours FIRST (must be on-shift in their local time), THEN by
# expertise (team owns the affected area). Expertise beats raw availability: an
# awake engineer on the wrong team is never chosen over the on-shift owner.
# ---------------------------------------------------------------------------


def _excluded_reason(on_shift: bool, match: bool) -> str | None:
    """None when fully eligible; else why this engineer is excluded."""
    if on_shift and match:
        return None
    if not on_shift:
        return "asleep"
    return "wrong team"


def _person(eid: str, engineer: dict, local: datetime, reason: str) -> dict:
    return {
        "id": eid,
        "name": engineer["name"],
        "location": engineer["location"],
        "local_time": local.strftime("%H:%M"),
        "reason": reason,
    }


def find_on_call_engineer(
    incident_area: str,
    emit: Emit = _noop,
    now: datetime | None = None,
) -> dict:
    """Find the engineer to page for ``incident_area``.

    Must be ON-SHIFT now (their local working hours) AND own the affected
    system (team match). Returns the best person + one backup, each with a
    reason, plus the full candidate breakdown. Deterministic: ties broken by
    stable sorted engineer id. Emits ``routing_decision``.
    """
    if now is None:
        now = demo_now()

    candidates: list[dict] = []
    eligible: list[tuple[str, dict, datetime]] = []
    on_shift_same_team_off: list[tuple[str, dict, datetime]] = []

    for eid in sorted(ENGINEERS):
        engineer = ENGINEERS[eid]
        local = now.astimezone(ZoneInfo(engineer["timezone"]))
        start, end = engineer["working_hours"]
        on_shift = start <= local.hour < end
        match = incident_area in engineer["teams"]

        candidates.append(
            {
                "name": engineer["name"],
                "local_time": local.strftime("%H:%M"),
                "on_shift": on_shift,
                "teams": engineer["teams"],
                "excluded_reason": _excluded_reason(on_shift, match),
            }
        )

        if on_shift and match:
            eligible.append((eid, engineer, local))
        elif match and not on_shift:
            on_shift_same_team_off.append((eid, engineer, local))

    chosen: dict | None
    backup: dict | None = None

    if eligible:
        ceid, ceng, clocal = eligible[0]
        chosen = _person(
            ceid,
            ceng,
            clocal,
            f"on-shift ({clocal.strftime('%H:%M')} local) and owns {incident_area}",
        )
        if len(eligible) > 1:
            beid, beng, blocal = eligible[1]
            backup = _person(
                beid,
                beng,
                blocal,
                f"on-shift backup owner of {incident_area}",
            )
        reason = chosen["reason"]
    elif on_shift_same_team_off:
        # No on-shift owner; fall back to nearest same-team engineer (still
        # surfaced, with a clear off-shift reason).
        feid, feng, flocal = on_shift_same_team_off[0]
        chosen = _person(
            feid,
            feng,
            flocal,
            f"no on-shift owner of {incident_area}; nearest same-team engineer "
            f"(off-shift at {flocal.strftime('%H:%M')} local)",
        )
        reason = chosen["reason"]
    else:
        chosen = None
        reason = "no on-call available"

    payload = {
        "chosen": chosen,
        "backup": backup,
        "reason": reason,
        "candidates": candidates,
    }
    emit("routing_decision", payload)
    return payload


__all__ = [
    "InvestigationState",
    "demo_now",
    "Remediator",
    "get_alerts",
    "get_deploy_history",
    "get_logs",
    "get_metrics",
    "find_on_call_engineer",
]
