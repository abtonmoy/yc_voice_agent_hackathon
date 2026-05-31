"""Real Twilio outbound calling — stdlib only (urllib), so it runs in the
Pipecat Cloud container with no extra deps. Used by the agent's `call_engineer`
tool to autonomously page a human, and reusable by scripts.

Gated: `outbound_enabled()` is False unless ENABLE_OUTBOUND_CALL=1 AND the Twilio
creds are present — so test/eval runs never place real calls by accident.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape

_TWILIO_KEYS = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_NUMBER")


def outbound_enabled() -> bool:
    """True only when explicitly enabled AND all Twilio creds are configured."""
    return os.getenv("ENABLE_OUTBOUND_CALL") == "1" and all(os.getenv(k) for k in _TWILIO_KEYS)


def say_twiml(text: str) -> str:
    """Inline TwiML that reads `text` aloud (one-way briefing)."""
    return f'<Response><Say voice="Polly.Matthew">{escape(text)}</Say></Response>'


def place_call(to: str, twiml: str) -> dict:
    """Place a Twilio outbound call to `to` that executes `twiml` on answer.

    Raises urllib.error.HTTPError on a Twilio error. Blocking (urllib) — call it
    from a thread (e.g. asyncio.to_thread) inside async code.
    """
    sid = os.environ["TWILIO_ACCOUNT_SID"]
    token = os.environ["TWILIO_AUTH_TOKEN"]
    frm = os.environ["TWILIO_NUMBER"]
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json"
    data = urllib.parse.urlencode({"To": to, "From": frm, "Twiml": twiml}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode())
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def place_briefing_call(to: str, briefing: str) -> dict:
    """Page `to` and read the `briefing` aloud (ONE-WAY). Returns the call resource."""
    return place_call(to, say_twiml(briefing))


def agent_stream_twiml(service_host: str) -> str:
    """TwiML that connects the answering party to a deployed Pipecat Cloud agent
    for a TWO-WAY conversation (not a one-way message)."""
    return (
        "<Response><Connect>"
        '<Stream url="wss://api.pipecat.daily.co/ws/twilio">'
        f'<Parameter name="_pipecatCloudServiceHost" value="{escape(service_host)}"/>'
        "</Stream></Connect></Response>"
    )


def place_agent_call(to: str, service_host: str) -> dict:
    """Call `to` and connect them to the deployed agent — two-way. The engineer
    talks to the triage agent live. Returns the Twilio call resource."""
    return place_call(to, agent_stream_twiml(service_host))
