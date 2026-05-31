"""Live end-to-end incident simulation for the dashboard demo.

Investigates the incident through the REAL tools, routes to the best-fit on-call
engineer, **calls that engineer directly** (real Twilio — no intermediary), then
proposes & applies the fix — streaming every event to the relay so the dashboard
renders it. With Fardin in the directory as the always-on payments owner, the
agent routes to him and dials +17653506634.

Run (relay+dashboard already up on :8080):
  cd server
  CONFIRM_CALL=1 uv run --no-project --with tzdata python simulate_and_call.py
Env: CONFIRM_CALL=1 to place the real call; DEMO_INCIDENT (default inc-1);
     PACE seconds between events (default 1.0); RELAY_URL (from .env).
"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DEMO_NOW", "2026-05-30T03:00:00-07:00")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PACE = float(os.environ.get("PACE", "1.0"))


def load_env(path=".env"):
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def post_event(relay_url, token, evt):
    try:
        req = urllib.request.Request(
            relay_url + "/events", data=json.dumps(evt).encode(),
            headers={"Content-Type": "application/json", "X-Relay-Token": token},
            method="POST")
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass  # dashboard is optional


def main():
    load_env()
    from events import EventBus
    from mock_backend import ENGINEERS
    from tools import (InvestigationState, find_on_call_engineer, get_alerts,
                       get_deploy_history, get_logs, get_metrics)
    import telephony

    relay_url = os.getenv("RELAY_URL")
    token = os.getenv("RELAY_TOKEN", "yc-hack-relay-7f3a9c2e")

    if relay_url:  # clear the dashboard so each run starts fresh
        try:
            urllib.request.urlopen(urllib.request.Request(
                relay_url + "/reset", headers={"X-Relay-Token": token}, method="POST"), timeout=3).read()
        except Exception:
            pass

    def sink(evt):
        if relay_url:
            post_event(relay_url, token, evt)
        time.sleep(PACE)

    bus = EventBus("sim-call", sink=sink)
    state = InvestigationState()
    state.set_incident(os.getenv("DEMO_INCIDENT", "inc-1"))
    inc = state.incident

    def t(role, text):
        bus.emit("transcript", {"role": role, "text": text, "final": True})

    bus.emit("session_start", {"session_id": "sim-call", "backend": "simulation"})
    t("user", "I'm getting paged — payments API is throwing 500s, started a few minutes ago.")
    t("agent", "On it — checking what's firing and any recent deploys.")
    get_alerts(state, emit=bus.emit)
    get_deploy_history(state, emit=bus.emit)
    get_logs(state, emit=bus.emit)
    get_metrics(state, emit=bus.emit)
    t("agent", "Deploy abc123 exhausted the payments database connection pool — that's the root cause.")
    bus.emit("rca", {
        "root_cause": inc["ground_truth_root_cause"],
        "evidence": inc["logs"][0] if inc["logs"] else "",
        "remediation": inc["proposed_remediation"],
        "code_fixable": bool(inc.get("fix")),
    })

    # Route to the best-fit on-call engineer, then call them DIRECTLY.
    routing = find_on_call_engineer(inc["affected_team"], emit=bus.emit)
    chosen = routing.get("chosen")
    eng = ENGINEERS.get(chosen["id"]) if chosen else None
    name = chosen["name"] if chosen else "the on-call engineer"
    phone = eng["phone"] if eng else None
    briefing = (
        f"Hi {name}, this is the on-call triage agent. We have a {inc['alert']['severity']} "
        f"incident on {inc['alert']['service']}. {inc['ground_truth_root_cause']}. "
        f"Recommended action: {inc['proposed_remediation']}. Paging you to take a look. Goodbye."
    )
    t("agent", f"Paging {name} — on-shift, owns {inc['affected_team']}. Calling now.")
    bus.emit("outbound_call", {"engineer_id": chosen["id"] if chosen else None,
                               "name": name, "phone": phone, "briefing": briefing, "status": "ringing"})

    call_sid = None
    creds = all(os.getenv(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_NUMBER"))
    service_host = os.getenv("PIPECAT_SERVICE_HOST")
    if os.getenv("CONFIRM_CALL") == "1" and phone and creds and service_host:
        try:
            # TWO-WAY: connect the engineer straight to the live agent so they can talk.
            res = telephony.place_agent_call(phone, service_host)
            call_sid = res.get("sid")
            print(f"[call] connecting {name} at {phone} to the agent (two-way) -> sid {call_sid} status {res.get('status')}")
        except Exception as e:
            print(f"[call error] {e}")
    elif os.getenv("CONFIRM_CALL") == "1" and not service_host:
        print("[skip] set PIPECAT_SERVICE_HOST=flower-bot.<org> for a two-way call to the agent")
    elif os.getenv("CONFIRM_CALL") != "1":
        print(f"[dry-run] would connect {name} at {phone} to the agent. Set CONFIRM_CALL=1 to dial.")

    bus.emit("outbound_call", {"engineer_id": chosen["id"] if chosen else None,
                               "name": name, "phone": phone, "briefing": briefing,
                               "status": "dialing" if call_sid else "announced", "call_sid": call_sid})

    # PROPOSE the fix — but DO NOT apply. The approval-gated safety invariant:
    # the agent only applies a code fix after the engineer says yes OUT LOUD.
    # This simulation has no human approver, so it stops at "proposed" — the
    # real apply happens when the on-call engineer approves on the live call
    # (the deployed agent's two-step gate enforces it). Never fake the yes.
    if inc.get("fix"):
        state.remediator.read_repo_file(inc["fix"]["file"], emit=bus.emit)
        state.remediator.propose_code_fix(state.active_incident_id, emit=bus.emit)
        t("agent", f"I've located the bug in {inc['fix']['file']} and staged a one-line fix. "
                   f"I'll apply it only once {name} approves on the call — not before.")

    bus.emit("session_end", {"session_id": "sim-call", "reason": "awaiting approval"})
    print("simulation complete.")


if __name__ == "__main__":
    main()
