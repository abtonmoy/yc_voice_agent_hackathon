#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Shared incident-triage module for the voice agents (Claude + Nemotron).

This is the ONE place the triage behavior lives. Both ``bot-claude.py`` and
``bot-nemotron.py`` import :func:`build_triage`, which is LLM-agnostic: it sets
up the per-session :class:`InvestigationState`, the :class:`EventBus` (wired to
the relay when ``RELAY_URL`` is set), the Pipecat direct-function tool wrappers,
the triage system instruction, and the spoken greeting.

The wrappers are thin: they only adapt the Pipecat ``FunctionCallParams`` calling
convention onto the PURE tool logic under ``tools/`` (which has ZERO pipecat /
fastapi imports). All dashboard events are emitted by the pure tools via the
injected ``bus.emit`` callback; the wrappers add ``rca`` / ``outbound_call`` /
``session_end`` where the orchestration (not the pure logic) owns the event.

This module imports a few Pipecat types for the wrappers — that is fine, it is
only ever imported by the bots. ``tools/`` and ``events.py`` stay pipecat-free.
"""

from __future__ import annotations

import asyncio
import os

from loguru import logger
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import EndTaskFrame, FunctionCallResultProperties
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import FunctionCallParams

from events import EventBus, make_http_relay_sink
from mock_backend import ENGINEERS
from telephony import outbound_enabled, place_agent_call, place_briefing_call
from tools import get_alerts as _get_alerts
from tools import get_deploy_history as _get_deploy_history
from tools import get_logs as _get_logs
from tools import get_metrics as _get_metrics
from tools import find_on_call_engineer as _find_on_call_engineer
from tools import InvestigationState
from triage_state import InvestigationState as LoopState

# The relay token default mirrors relay.py's shared secret for the hackathon.
DEFAULT_RELAY_TOKEN = "yc-hack-relay-7f3a9c2e"

# Optimization G (confidence + termination controller) toggle. Default OFF =
# current behavior unchanged; flip OPT_G=1 to add the <conf> instruction and
# wire the ConfFilterProcessor into the bots.
OPT_G = os.getenv("OPT_G") == "1"

# Optimization C (pre-cached TTS opener) toggle. Default OFF = current behavior
# unchanged; flip OPT_C=1 to pre-render GREETING_PROMPT at startup and push it as
# instant first audio on connect (see tts_cache.py). When on, the bot uses the
# ``kickoff_opened`` variant so the LLM continues from the spoken opener instead
# of greeting again.
OPT_C = os.getenv("OPT_C") == "1"

# The <conf> keystone (optimization-plan.md §5). Appended to the static system
# prompt only when OPT_G is on; the model emits one <conf>0.0</conf> line per
# turn which the ConfFilterProcessor strips before TTS.
CONF_INSTRUCTION = (
    "Before any tool call or final answer, emit exactly one line of the form "
    "<conf>0.0</conf> reflecting your certainty from 0 to 1 in the current "
    "root-cause hypothesis. Do not speak this aloud — it is internal metadata."
)


def system_instruction() -> str:
    """The triage system prompt, plus the <conf> instruction when OPT_G is on.

    With OPT_G off this returns ``TRIAGE_SYSTEM_INSTRUCTION`` byte-for-byte, so
    the deployed agent's behavior is unchanged unless the toggle is set.
    """
    return TRIAGE_SYSTEM_INSTRUCTION + (("\n\n" + CONF_INSTRUCTION) if OPT_G else "")


# --- Triage system instruction ---------------------------------------------
# Static prefix: role + behavior + the approval-gated remediation invariant
# (build plan §I3 / §Layer-3 / safety invariant). Kept stable at the front so it
# forms a clean cache prefix and reads as the tool-calling spec.
TRIAGE_SYSTEM_INSTRUCTION = (
    "You are an automated on-call triage agent, and you have just PLACED A PHONE "
    "CALL to an on-call engineer to brief them on a live production incident. They "
    "do NOT know about this incident yet — you do. YOU lead the call. Introduce "
    "yourself in one line and start briefing them right away. NEVER ask them what "
    "the problem is or what they need — you are the one informing them.\n\n"
    "YOU ALREADY HAVE THE FACTS. The incident details — the alert, the root cause, "
    "the evidence, and the proposed fix — are given to you the moment the call "
    "connects. Do NOT call the diagnostic tools to re-investigate, and NEVER "
    "narrate tool use or say things like \"let me check the logs\" or \"now let me "
    "get the alert.\" Just brief them. Call report_root_cause once, as you state "
    "the root cause, to log the RCA to the dashboard. (You may use a tool only if "
    "the engineer asks for a specific detail you weren't given.)\n\n"
    "HOW TO TALK — like a real on-call peer on the phone:\n"
    "- Open proactively, e.g.: \"Hi, this is the automated on-call triage agent — "
    "sorry to call out of the blue. We've got a P1 on payments-api and I've already "
    "dug in — here's what's going on.\" Then walk them through what's broken, the "
    "root cause, and the evidence in plain English. Synthesize — never read raw "
    "logs or tables aloud.\n"
    "- Keep each turn to 1–2 short sentences. Use contractions. No filler, no "
    "bullet points, no emojis — this is spoken.\n\n"
    "REMEDIATION — approval-gated (the hard safety line):\n"
    "- Tell them the fix you propose. Use read_repo_file to inspect the suspect "
    "source and propose_code_fix to stage the patch, then describe the one-line "
    "change out loud and ASK for their go-ahead.\n"
    "- Call apply_code_fix(engineer_approved=True) ONLY AFTER the engineer says yes "
    "OUT LOUD on this call. No verbal yes → never apply. Never pass "
    "engineer_approved=True on your own judgment. The patch is scoped to the root "
    "cause, nothing more. For incidents that are NOT a code bug (e.g. an expired "
    "certificate) propose a runbook instead — do not patch.\n\n"
    "STAY ON THE LINE — this is the engineer's call to end, not yours. Keep "
    "briefing, answer their questions, and wait. Do NOT call end_call on your own "
    "initiative. Only end the call (say a short goodbye, then call end_call in the "
    "same turn) when the engineer clearly signals they're done — e.g. \"that's "
    "all\", \"thanks, bye\", \"you can hang up\", or they explicitly ask you to "
    "end the call."
)

GREETING_PROMPT = (
    "Hi, this is the automated on-call triage agent — sorry to call out of the "
    "blue. We've got a live incident I need to brief you on."
)


def build_triage(session_id: str, llm) -> dict:
    """Wire up the shared triage agent for one session.

    LLM-agnostic: both the Claude and Nemotron bots call this with their own
    already-constructed ``llm`` service. It creates the per-session state and
    event bus, defines the Pipecat direct-function tool wrappers (which adapt
    the ``FunctionCallParams`` convention onto the PURE ``tools/`` logic),
    registers them on ``llm``, and returns everything the bot's pipeline needs.

    Args:
        session_id: Stable id for this call, used to stamp every emitted event.
        llm: The constructed Pipecat LLM service to register the tools on.

    Returns:
        dict with ``state``, ``bus``, ``tools`` (a ``ToolsSchema``),
        ``tool_functions`` (the list registered on the LLM),
        ``system_instruction`` (the triage prompt) and ``greeting``.
    """
    # Per-session investigation state. Active incident comes from DEMO_INCIDENT
    # (default inc-1, the payments-db pool-exhaustion headline incident).
    state = InvestigationState()
    state.set_incident(os.getenv("DEMO_INCIDENT", "inc-1"))

    # Optimization G per-call scoreboard (confidence / turn_count / evidence).
    # Only meaningful under OPT_G but harmless to track always — the diagnostic
    # wrappers record which evidence categories have been gathered onto it.
    loop = LoopState()

    # Event bus → relay over HTTP when RELAY_URL is configured, else no sink
    # (events are still recorded on the bus for inspection).
    relay_url = os.getenv("RELAY_URL")
    sink = (
        make_http_relay_sink(relay_url, os.getenv("RELAY_TOKEN", DEFAULT_RELAY_TOKEN))
        if relay_url
        else None
    )
    bus = EventBus(session_id, sink=sink)

    # --- Direct-function tool wrappers --------------------------------------
    # Each wrapper is async, takes (params: FunctionCallParams, **kwargs), calls
    # the PURE tool passing `state`/`bus.emit`, and returns via the callback.
    # The DOCSTRING below is the LLM tool schema — write it for the model.

    async def get_alerts(params: FunctionCallParams) -> None:
        """Get the active paging alert for the current incident.

        Call this FIRST, before any other diagnostic, to see what's firing:
        the service, severity, title, and when it started. Returns a synthesized
        summary you can read meaning from (not raw rows).
        """
        result = _get_alerts(state, emit=bus.emit)
        loop.evidence_gathered.add("alerts")
        await params.result_callback(result)

    async def get_deploy_history(params: FunctionCallParams) -> None:
        """Get recent deploys (last 24h) for the affected service.

        Use this to test the "a recent deploy broke it" hypothesis. The summary
        explicitly says when there were NO deploys in the window — that absence
        is itself a strong signal (the cause is likely operational, not a
        deploy). Take it at face value; don't infer a deploy that isn't listed.
        """
        result = _get_deploy_history(state, emit=bus.emit)
        loop.evidence_gathered.add("deploys")
        await params.result_callback(result)

    async def get_logs(params: FunctionCallParams) -> None:
        """Get the pre-correlated recent error logs for the affected service.

        Use this for the concrete error signal (e.g. connection-pool
        exhaustion, an upstream timeout, an expired certificate). Returns a
        correlated summary — synthesize its meaning aloud, don't read it verbatim.
        """
        result = _get_logs(state, emit=bus.emit)
        loop.evidence_gathered.add("logs")
        await params.result_callback(result)

    async def get_metrics(params: FunctionCallParams) -> None:
        """Get the key metric snapshot for the incident.

        Use this to confirm a resource/latency story behind the logs (e.g.
        active_connections at the cap, a p99 spike, a handshake success rate at
        zero). Returns a synthesized snapshot summary.
        """
        result = _get_metrics(state, emit=bus.emit)
        loop.evidence_gathered.add("metrics")
        await params.result_callback(result)

    async def find_on_call_engineer(
        params: FunctionCallParams, incident_area: str
    ) -> None:
        """Decide which engineer to PAGE for this incident (follow-the-sun).

        Filters the on-call directory to whoever is ON-SHIFT right now in their
        local working hours, THEN ranks by team ownership of the affected area.
        Returns the best person plus one backup, each with a one-line reason,
        and the full candidate breakdown. Call this once you know the affected
        area; state the chosen engineer aloud before paging.

        Args:
            incident_area: The team/system that owns the affected service —
                one of "payments", "database", "networking", "infra",
                "backend", "frontend", "web", "platform". Use the incident's
                affected area (e.g. payments-db pool exhaustion → "database",
                payments-api 5xx → "payments").
        """
        result = _find_on_call_engineer(incident_area, emit=bus.emit)
        await params.result_callback(result)

    async def call_engineer(
        params: FunctionCallParams, engineer_id: str, briefing: str
    ) -> None:
        """Place an outbound page to the chosen engineer and brief them by voice.

        Call this only AFTER find_on_call_engineer told you who to page and you
        have a concise spoken briefing (incident + root cause + proposed fix).
        When outbound calling is enabled this REALLY phones the engineer and reads
        the briefing aloud; otherwise it announces the page (the dashboard shows
        it either way). Returns the call sid when a real call was placed.

        Args:
            engineer_id: The id of the engineer to page (e.g. "priya"), as
                returned in the `chosen.id` field of find_on_call_engineer.
            briefing: A one or two sentence spoken briefing for the engineer:
                what's broken, the root cause, and the proposed remediation.
        """
        engineer = ENGINEERS.get(engineer_id)
        name = engineer["name"] if engineer else engineer_id
        # DEMO_PAGE_NUMBER routes every page to one controlled phone for the demo;
        # otherwise dial the engineer's real directory number.
        phone = os.getenv("DEMO_PAGE_NUMBER") or (engineer["phone"] if engineer else None)
        bus.emit(
            "outbound_call",
            {"engineer_id": engineer_id, "name": name, "phone": phone,
             "briefing": briefing, "status": "ringing"},
        )
        call_sid = None
        if outbound_enabled() and phone:
            # Prefer a TWO-WAY page (connect the engineer to a live agent session)
            # when the service host is known; else fall back to a spoken briefing.
            service_host = os.getenv("PIPECAT_SERVICE_HOST")
            try:
                # urllib is blocking — run off the event loop so the voice path
                # never stalls. A paging failure must NOT break the live call.
                if service_host:
                    res = await asyncio.to_thread(place_agent_call, phone, service_host)
                else:
                    res = await asyncio.to_thread(place_briefing_call, phone, briefing)
                call_sid = res.get("sid")
                logger.info(f"paged {name} at {phone} (two-way={bool(service_host)}) — call {call_sid}")
            except Exception as e:
                logger.warning(f"outbound page failed: {e}")
        bus.emit(
            "outbound_call",
            {"engineer_id": engineer_id, "name": name, "phone": phone,
             "briefing": briefing,
             "status": "dialing" if call_sid else "announced",
             "call_sid": call_sid},
        )
        await params.result_callback(
            {"ok": True, "note": f"paging {name}", "call_sid": call_sid}
        )

    async def read_repo_file(params: FunctionCallParams, path: str) -> None:
        """Read a file from the monitored service repo (READ-ONLY).

        Use this to inspect the suspect source once the logs point at a file, so
        you can speak the root-cause line before proposing a fix. Reflects this
        session's applied patches; missing files return found=False (no error).

        Args:
            path: Repo-relative path, e.g. "app/db.py", "app/batch_jobs.py",
                "app/tax_service.py".
        """
        result = state.remediator.read_repo_file(path, emit=bus.emit)
        await params.result_callback(result)

    async def propose_code_fix(params: FunctionCallParams) -> None:
        """Propose (but DO NOT apply) the scoped fix for the incident you're
        investigating. Takes NO arguments — it always targets the active incident.

        Stages a pending patch and returns the unified diff plus needs_approval.
        Read the change aloud and ASK the engineer for a verbal yes — this does
        NOT modify anything. For incidents that aren't code-fixable it returns
        advisory remediation instead (propose a runbook; never patch those).
        """
        # Always scope to the active incident — never trust a model-supplied id.
        result = state.remediator.propose_code_fix(state.active_incident_id, emit=bus.emit)
        await params.result_callback(result)

    async def apply_code_fix(
        params: FunctionCallParams, engineer_approved: bool
    ) -> None:
        """Apply the previously proposed fix — ONLY after a verbal yes.

        HARD GATE: this applies only if a pending proposal exists AND
        engineer_approved is True. Pass engineer_approved=True ONLY when the
        engineer explicitly said yes OUT LOUD on this call — never on your own
        judgment. Emits the diff as a reviewable code_fix event; a refusal is
        logged when not approved.

        Args:
            engineer_approved: True ONLY if the engineer verbally approved this
                specific fix on the call. Otherwise False.
        """
        result = state.remediator.apply_code_fix(engineer_approved, emit=bus.emit)
        await params.result_callback(result)

    async def report_root_cause(
        params: FunctionCallParams,
        root_cause: str,
        evidence: str,
        remediation: str,
        code_fixable: bool,
    ) -> None:
        """Report the final root-cause analysis (RCA) at convergence.

        Call this ONCE, when the evidence has converged on a single most-likely
        cause. This is how the dashboard receives the RCA — emits an `rca` event
        with your synthesis. Say the synthesis aloud too.

        Args:
            root_cause: The single most-likely root cause, in plain English.
            evidence: The key evidence that supports it (deploy timing, the log
                signal, the metric), synthesized — not raw rows.
            remediation: The proposed remediation (a code fix or an ops runbook).
            code_fixable: True if this is fixable by a scoped code patch, False
                if it's an ops action (e.g. cert rotation).
        """
        bus.emit(
            "rca",
            {
                "root_cause": root_cause,
                "evidence": evidence,
                "remediation": remediation,
                "code_fixable": code_fixable,
            },
        )
        await params.result_callback({"ok": True})

    async def end_call(params: FunctionCallParams) -> None:
        """End the call. Call this AFTER saying goodbye in the same turn.

        Emits a session_end event, then pushes an EndTaskFrame so the pipeline
        flushes queued speech and hangs up. Never call this without a closing
        line first.
        """
        logger.info("end_call invoked — emitting session_end + pushing EndTaskFrame")
        bus.emit("session_end", {"session_id": session_id, "reason": "end_call"})
        await params.llm.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)
        # run_llm=False: the goodbye is already in flight; don't generate more.
        await params.result_callback(
            {"ok": True}, properties=FunctionCallResultProperties(run_llm=False)
        )

    tool_functions = [
        get_alerts,
        get_deploy_history,
        get_logs,
        get_metrics,
        find_on_call_engineer,
        call_engineer,
        read_repo_file,
        propose_code_fix,
        apply_code_fix,
        report_root_cause,
        end_call,
    ]

    # ToolsSchema describes the tools to the LLM; register_direct_function wires
    # the actual handlers. Both are required (mirrors the starter).
    for fn in tool_functions:
        llm.register_direct_function(fn)
    tools = ToolsSchema(standard_tools=tool_functions)

    # Fact-loaded kickoff: the agent has already triaged, so hand it the incident
    # details and have it brief IMMEDIATELY (no live investigation, no narration).
    inc = state.incident
    al = inc["alert"]
    fix = inc.get("fix")
    fix_line = f"a one-line code fix in {fix['file']}" if fix else "an ops action, not a code patch"
    kickoff = (
        "The on-call engineer just answered your outbound call. You have ALREADY "
        "triaged this incident. The facts:\n"
        f"- Alert: {al['severity']} on {al['service']} — {al['title']} (since {al['started_at']}).\n"
        f"- Root cause: {inc['ground_truth_root_cause']}.\n"
        f"- Evidence: {inc['logs'][0] if inc.get('logs') else 'see metrics'}.\n"
        f"- Proposed remediation: {inc['proposed_remediation']} ({fix_line}).\n\n"
        "Open the call NOW: a one-line intro, then brief them — what's broken, the "
        "root cause, and the fix you propose. Speak immediately; do NOT call any "
        "tools before you talk and never narrate tool use. Call report_root_cause "
        "as you state the cause. Offer the fix and apply it only after they say yes."
    )

    # OPT_C variant: the fixed opener (GREETING_PROMPT) has ALREADY been spoken
    # aloud from cache, so the model must continue from it rather than re-greet.
    # Byte-identical facts; only the opening directive differs.
    kickoff_opened = kickoff.replace(
        "Open the call NOW: a one-line intro, then brief them — what's broken, the "
        "root cause, and the fix you propose. Speak immediately; do NOT call any "
        "tools before you talk and never narrate tool use.",
        "You have ALREADY spoken your opening line aloud: "
        f"\"{GREETING_PROMPT}\" — do NOT greet or re-introduce yourself. Continue "
        "STRAIGHT into the briefing now — what's broken, the root cause, and the "
        "fix you propose — picking up naturally from that opener. Speak "
        "immediately; do NOT call any tools before you talk and never narrate tool use.",
    )

    return {
        "state": state,
        "loop": loop,
        "bus": bus,
        "tools": tools,
        "tool_functions": tool_functions,
        "system_instruction": TRIAGE_SYSTEM_INSTRUCTION,
        "greeting": GREETING_PROMPT,
        "kickoff": kickoff,
        "kickoff_opened": kickoff_opened,
    }


def prime_investigation(state, bus) -> None:
    """Run the diagnostics once on connect so the dashboard shows the full
    investigation instantly. The agent has already triaged, so it does NOT narrate
    this live. Best-effort — never raises into the call."""
    try:
        _get_alerts(state, emit=bus.emit)
        _get_deploy_history(state, emit=bus.emit)
        _get_logs(state, emit=bus.emit)
        _get_metrics(state, emit=bus.emit)
    except Exception:
        logger.exception("prime_investigation failed (ignored)")
