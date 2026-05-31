"""Lean live-call launcher for the dashboard demo.

Unlike simulate_and_call.py, this does NOT emit a scripted narrative. The deployed
agent now pushes its OWN real events (session_start, investigation tool calls, the
briefing transcript, RCA, the proposed/applied fix) to the relay — so emitting a
canned story here just DOUBLES every line on the dashboard ("repeating"). This
launcher only:

  1. resets the dashboard,
  2. emits the routing + outbound-call (paging) widget events — which the in-call
     agent does NOT emit (the orchestrator picks the engineer and dials),
  3. places the real two-way call,

and then gets out of the way so the live agent is the single source of truth.

Run (relay up on :8080):
  cd server
  CONFIRM_CALL=1 uv run python place_call.py
Env: CONFIRM_CALL=1 to actually dial; DEMO_INCIDENT (default inc-1); RELAY_URL (.env).
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DEMO_NOW", "2026-05-30T03:00:00-07:00")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def load_env(path=".env"):
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def post(relay_url, token, path, evt=None, method="POST"):
    try:
        data = json.dumps(evt).encode() if evt is not None else None
        req = urllib.request.Request(
            relay_url + path, data=data,
            headers={"Content-Type": "application/json", "X-Relay-Token": token},
            method=method)
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass  # dashboard is optional


def main():
    load_env()
    from mock_backend import ENGINEERS
    from tools import InvestigationState, find_on_call_engineer
    import telephony

    relay_url = os.getenv("RELAY_URL")
    token = os.getenv("RELAY_TOKEN", "yc-hack-relay-7f3a9c2e")

    # Clear the dashboard so ONLY this call's (live-agent) events render.
    if relay_url:
        post(relay_url, token, "/reset", method="POST")

    state = InvestigationState()
    state.set_incident(os.getenv("DEMO_INCIDENT", "inc-1"))
    inc = state.incident

    # Pick the best-fit on-call engineer and show the paging widget. These routing
    # events are NOT emitted by the in-call agent, so they don't duplicate anything.
    def sink(evt):
        if relay_url:
            post(relay_url, token, "/events", evt)

    from events import EventBus
    bus = EventBus("call-launcher", sink=sink)
    routing = find_on_call_engineer(inc["affected_team"], emit=bus.emit)
    chosen = routing.get("chosen")
    eng = ENGINEERS.get(chosen["id"]) if chosen else None
    name = chosen["name"] if chosen else "the on-call engineer"
    phone = eng["phone"] if eng else None

    bus.emit("outbound_call", {"engineer_id": chosen["id"] if chosen else None,
                               "name": name, "phone": phone, "status": "ringing"})

    call_sid = None
    creds = all(os.getenv(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_NUMBER"))
    service_host = os.getenv("PIPECAT_SERVICE_HOST")
    if os.getenv("CONFIRM_CALL") == "1" and phone and creds and service_host:
        try:
            res = telephony.place_agent_call(phone, service_host)
            call_sid = res.get("sid")
            print(f"[call] connecting {name} at {phone} to the agent (two-way) -> sid {call_sid} status {res.get('status')}")
        except Exception as e:
            print(f"[call error] {e}")
    elif os.getenv("CONFIRM_CALL") == "1" and not service_host:
        print("[skip] set PIPECAT_SERVICE_HOST=flower-bot.<org> for a two-way call")
    elif os.getenv("CONFIRM_CALL") != "1":
        print(f"[dry-run] would connect {name} at {phone}. Set CONFIRM_CALL=1 to dial.")

    bus.emit("outbound_call", {"engineer_id": chosen["id"] if chosen else None,
                               "name": name, "phone": phone,
                               "status": "dialing" if call_sid else "announced", "call_sid": call_sid})
    print("launcher done — the live agent now drives the dashboard.")


if __name__ == "__main__":
    main()
