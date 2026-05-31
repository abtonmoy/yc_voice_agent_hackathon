"""Standalone proof that the Claude tool-calling contract works for triage.

No pipecat. Uses the `anthropic` SDK directly to confirm (a) the API key works
and (b) given the triage system prompt + a realistic incident report, Claude
CHOOSES to call a diagnostic tool (stop_reason == "tool_use"). This de-risks the
voice path: if Claude won't tool-call here, it won't in the bot either.

Tool schemas below mirror the real triage tools (get_deploy_history, get_logs,
report_root_cause) so this exercises the same shape the bot registers.

Run it::

    uv run --no-project --with anthropic --with python-dotenv \
        python tests/check_anthropic_tools.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

# Load server/.env (this file lives in server/tests/).
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH, override=True)

# Trimmed triage system prompt — same role/behavior as triage.py, kept short.
SYSTEM = (
    "You are an on-call incident-triage assistant on a live phone call with an "
    "engineer who just got paged. Investigate with tools in a sane order: pull "
    "deploy history and logs before guessing a root cause. When the evidence "
    "converges, call report_root_cause. Do not guess a cause you haven't checked."
)

# Tool schemas mirroring the real triage tools.
TOOLS = [
    {
        "name": "get_deploy_history",
        "description": (
            "Get recent deploys (last 24h) for the affected service. Use this to "
            "test whether a recent deploy broke it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "Affected service name, e.g. 'payments-api'.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "get_logs",
        "description": (
            "Get the pre-correlated recent error logs for the affected service — "
            "the concrete error signal."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "Affected service name, e.g. 'payments-api'.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "report_root_cause",
        "description": (
            "Report the final root-cause analysis once the evidence has converged "
            "on a single most-likely cause."
        ),
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
]

USER_MSG = "payments api is throwing 500s, started a few minutes ago"


def main() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    model = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    if not api_key:
        print(f"FAIL: ANTHROPIC_API_KEY not set (looked in {ENV_PATH})")
        return 1

    client = Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=1024,
        system=SYSTEM,
        tools=TOOLS,
        messages=[{"role": "user", "content": USER_MSG}],
    )

    print(f"model={model} stop_reason={resp.stop_reason}")

    if resp.stop_reason != "tool_use":
        # Surface any text so failures are debuggable.
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                print("text:", block.text)
        print("FAIL: expected stop_reason == 'tool_use' (Claude did not choose a tool)")
        return 1

    tool_names = [b.name for b in resp.content if getattr(b, "type", None) == "tool_use"]
    print(f"PASS: Claude chose tool_use -> {', '.join(tool_names)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
