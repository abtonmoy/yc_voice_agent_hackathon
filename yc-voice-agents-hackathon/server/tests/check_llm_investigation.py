"""OPTIONAL: real-LLM-driven E1 investigation against the REAL pure tools.

Pipecat-free. Runs a minimal Anthropic tool-use loop: Claude gets a short
triage system prompt + the real tool schemas, and we execute its tool calls
against the actual ``tools/`` logic wired to a ``ListSink`` EventBus. A scripted
"engineer" answers the agent's questions and says "yes, apply it" when asked.

Asserts the run emits at least: alert, routing_decision, fix_proposed, code_fix.

Skips cleanly (exit 0, clearly marked) if the ``anthropic`` SDK isn't installed
or ``ANTHROPIC_API_KEY`` is unset. Best-effort — never blocks the suite.

Run::

    uv run --no-project --with anthropic --with python-dotenv --with tzdata \
        python tests/check_llm_investigation.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Freeze the clock so routing -> priya, BEFORE importing tools/state.
os.environ.setdefault("DEMO_NOW", "2026-05-30T03:00:00-07:00")

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)


def _skip(msg: str) -> int:
    print(f"SKIP (LLM investigation): {msg}")
    return 0


# Short inline triage prompt (triage.py imports pipecat, so we don't import it).
SYSTEM = (
    "You are an on-call incident-triage assistant on a live phone call with an "
    "engineer who just got paged. Investigate with tools in a sane order: pull "
    "alerts, deploy history, logs and metrics BEFORE stating a root cause. When "
    "the evidence converges, call report_root_cause with code_fixable set. If "
    "it's code-fixable, find the on-call owner (find_on_call_engineer) and page "
    "them (call_engineer), then read the relevant repo file, propose a fix "
    "(propose_code_fix), ask the engineer to approve, and only after they say "
    "yes call apply_code_fix(engineer_approved=true). Keep going until the fix "
    "is applied; do not stop after merely proposing it."
)

TOOLS = [
    {
        "name": "get_alerts",
        "description": "Get the active paging alert for this incident.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_deploy_history",
        "description": "Get recent deploys (last 24h) for the affected service.",
        "input_schema": {
            "type": "object",
            "properties": {"service": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "get_logs",
        "description": "Get the pre-correlated recent error logs for the service.",
        "input_schema": {
            "type": "object",
            "properties": {"service": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "get_metrics",
        "description": "Get the key metric series snapshot for the incident.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "report_root_cause",
        "description": "Report the converged root-cause analysis.",
        "input_schema": {
            "type": "object",
            "properties": {
                "root_cause": {"type": "string"},
                "evidence": {"type": "string"},
                "remediation": {"type": "string"},
                "code_fixable": {"type": "boolean"},
            },
            "required": ["root_cause", "evidence", "remediation", "code_fixable"],
        },
    },
    {
        "name": "find_on_call_engineer",
        "description": "Find the on-shift owner to page for the affected area.",
        "input_schema": {
            "type": "object",
            "properties": {"incident_area": {"type": "string"}},
            "required": ["incident_area"],
        },
    },
    {
        "name": "call_engineer",
        "description": "Place an outbound call to the chosen engineer and brief them.",
        "input_schema": {
            "type": "object",
            "properties": {
                "engineer_id": {"type": "string"},
                "briefing": {"type": "string"},
            },
            "required": ["engineer_id"],
        },
    },
    {
        "name": "read_repo_file",
        "description": "Read a file from the monitored service repo (read-only).",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "propose_code_fix",
        "description": "Propose (do NOT apply) the staged fix for an incident.",
        "input_schema": {
            "type": "object",
            "properties": {"incident_id": {"type": "string"}},
            "required": ["incident_id"],
        },
    },
    {
        "name": "apply_code_fix",
        "description": (
            "Apply the previously proposed fix. Only call with "
            "engineer_approved=true after the engineer explicitly approved."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"engineer_approved": {"type": "boolean"}},
            "required": ["engineer_approved"],
        },
    },
]

USER_OPENER = (
    "I'm getting paged — payments API is throwing a ton of 500s, started a few "
    "minutes ago."
)


def main() -> int:
    try:
        from anthropic import Anthropic
    except Exception as e:  # noqa: BLE001
        return _skip(f"anthropic SDK not available ({e})")

    try:
        from dotenv import load_dotenv

        load_dotenv(Path(_SERVER_DIR) / ".env", override=False)
    except Exception:
        pass

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return _skip("ANTHROPIC_API_KEY not set")

    from events import EventBus, ListSink
    from tools import (
        InvestigationState,
        find_on_call_engineer,
        get_alerts,
        get_deploy_history,
        get_logs,
        get_metrics,
    )

    sink = ListSink()
    bus = EventBus(session_id="sess_llm", sink=sink)
    state = InvestigationState(active_incident_id="inc-1")
    rem = state.remediator
    emit = bus.emit

    def run_tool(name: str, args: dict) -> dict:
        if name == "get_alerts":
            return get_alerts(state, emit=emit)
        if name == "get_deploy_history":
            return get_deploy_history(state, emit=emit, service=args.get("service"))
        if name == "get_logs":
            return get_logs(state, emit=emit, service=args.get("service"))
        if name == "get_metrics":
            return get_metrics(state, emit=emit, name=args.get("name"))
        if name == "report_root_cause":
            emit("rca", {
                "root_cause": args.get("root_cause", ""),
                "evidence": args.get("evidence", ""),
                "remediation": args.get("remediation", ""),
                "code_fixable": bool(args.get("code_fixable")),
            })
            return {"ok": True}
        if name == "find_on_call_engineer":
            return find_on_call_engineer(args.get("incident_area", "payments"), emit=emit)
        if name == "call_engineer":
            eid = args.get("engineer_id", "priya")
            emit("outbound_call", {"engineer_id": eid, "status": "ringing"})
            emit("outbound_call", {"engineer_id": eid, "status": "answered"})
            return {"status": "answered", "engineer_id": eid}
        if name == "read_repo_file":
            return rem.read_repo_file(args.get("path", "app/db.py"), emit=emit)
        if name == "propose_code_fix":
            # The bot's tool layer knows the active incident; ignore any
            # hallucinated id the model passes and use the real active one.
            return rem.propose_code_fix(state.active_incident_id, emit=emit)
        if name == "apply_code_fix":
            return rem.apply_code_fix(bool(args.get("engineer_approved")), emit=emit)
        return {"error": f"unknown tool {name}"}

    client = Anthropic(api_key=api_key)
    model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    messages = [{"role": "user", "content": USER_OPENER}]

    max_turns = 16
    for _ in range(max_turns):
        resp = client.messages.create(
            model=model, max_tokens=1024, system=SYSTEM, tools=TOOLS, messages=messages
        )
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            # Agent spoke without calling a tool — scripted engineer nudges it on.
            text = " ".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            ).lower()
            if "apply" in text or "approve" in text or "should i" in text or "?" in text:
                reply = "Yes, apply it — go ahead."
            else:
                reply = "Okay, keep going and fix it if you can."
            messages.append({"role": "user", "content": reply})
            continue

        tool_results = []
        for block in resp.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            out = run_tool(block.name, dict(block.input or {}))
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": str(out),
            })
        messages.append({"role": "user", "content": tool_results})

        emitted = {e["type"] for e in sink.events}
        if "code_fix" in emitted:
            break

    emitted_types = [e["type"] for e in sink.events]
    required = {"alert", "routing_decision", "fix_proposed", "code_fix"}
    missing = required - set(emitted_types)

    print(f"model={model} emitted={emitted_types}")
    if missing:
        print(f"FAIL (LLM investigation): missing required events {sorted(missing)}")
        return 1
    print("PASS (LLM investigation): emitted alert, routing_decision, fix_proposed, code_fix")
    return 0


if __name__ == "__main__":
    sys.exit(main())
