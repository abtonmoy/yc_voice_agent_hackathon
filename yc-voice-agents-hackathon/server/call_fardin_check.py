"""Place a real two-way call to Fardin (connect to the deployed agent) and watch
the relay for the agent's events arriving via the tunnel — a live E2E health check.
"""
import base64
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

for line in open(".env", encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

import telephony

LOCAL_RELAY = "http://127.0.0.1:8080"
TOKEN = os.environ["RELAY_TOKEN"]
FARDIN = "+17653506634"
HOST = os.environ["PIPECAT_SERVICE_HOST"]
SID = os.environ["TWILIO_ACCOUNT_SID"]
TOK = os.environ["TWILIO_AUTH_TOKEN"]


def _twilio_auth():
    return "Basic " + base64.b64encode(f"{SID}:{TOK}".encode()).decode()


def relay_events():
    try:
        with urllib.request.urlopen(LOCAL_RELAY + "/stream?once=1", timeout=5) as r:
            return [json.loads(l[5:].strip()) for l in r.read().decode().splitlines() if l.startswith("data:")]
    except Exception:
        return []


def call_status(sid):
    req = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Calls/{sid}.json")
    req.add_header("Authorization", _twilio_auth())
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r).get("status")


def main():
    # clear the dashboard so we only see this call
    try:
        urllib.request.urlopen(urllib.request.Request(
            LOCAL_RELAY + "/reset", headers={"X-Relay-Token": TOKEN}, method="POST"), timeout=5).read()
    except Exception:
        pass

    res = telephony.place_agent_call(FARDIN, HOST)
    sid = res.get("sid")
    print(f"[call] two-way to Fardin {FARDIN} -> agent {HOST} | sid={sid} status={res.get('status')}")
    print("[watch] polling relay for the agent's events (~75s). Talk to the agent on Fardin's phone.\n")

    seen = 0
    for i in range(60):  # ~180s window
        time.sleep(3)
        evs = relay_events()
        for e in evs[seen:]:
            p = e.get("payload", {})
            if e["type"] == "transcript":
                who = p.get("role", "?").upper()
                print(f"   +{i*3:>3}s  {who:>5}: {str(p.get('text',''))[:80]}")
            else:
                extra = p.get("tool") or p.get("status") or p.get("root_cause") or p.get("applied") or ""
                print(f"   +{i*3:>3}s  · {e['type']:<16} {str(extra)[:50]}")
        seen = len(evs)
        try:
            st = call_status(sid)
        except Exception:
            st = "?"
        if i % 2 == 0:
            print(f"   +{i*3:>3}s  call={st}  total_events={len(evs)}")
        if st in ("completed", "failed", "busy", "no-answer", "canceled"):
            print(f"\n[call ended] status={st}")
            break

    evs = relay_events()
    types = [e["type"] for e in evs]
    print("\n=== summary ===")
    print("events from the deployed agent:", len(evs))
    print("types:", " ".join(types) if types else "(none — did Fardin answer + talk?)")
    print("got session_start:", "session_start" in types)
    print("got a code_fix (applied):", any(e["type"] == "code_fix" for e in evs))


if __name__ == "__main__":
    main()
