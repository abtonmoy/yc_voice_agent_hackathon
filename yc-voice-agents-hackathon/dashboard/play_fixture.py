"""Play the recorded E1 fixture into a running relay, so you can WATCH the
dashboard render a full investigation — no bot, no phone, no event-day services.

Terminal 1 — start the relay (also serves the dashboard):
    cd server
    DASHBOARD_DIR=../dashboard RELAY_NDJSON=./events.ndjson \
      uv run --no-project --with fastapi --with uvicorn uvicorn relay:app --port 8080
  then open http://localhost:8080/ in a browser.

Terminal 2 — play the fixture:
    uv run --no-project python ../dashboard/play_fixture.py

Env: RELAY_URL (default http://localhost:8080), RELAY_TOKEN, PACE seconds between
events (default 0.5; set PACE=0 for an instant flood, used by the e2e test).
"""
import json
import os
import sys
import time
import urllib.request

# Windows consoles default to cp1252 and choke on the unicode in event text.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

RELAY = os.environ.get("RELAY_URL", "http://localhost:8080")
TOKEN = os.environ.get("RELAY_TOKEN", "yc-hack-relay-7f3a9c2e")
PACE = float(os.environ.get("PACE", "0.5"))
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "events.ndjson")


def _post(path: str, body: bytes | None = None) -> None:
    req = urllib.request.Request(
        RELAY + path,
        data=body,
        headers={"Content-Type": "application/json", "X-Relay-Token": TOKEN},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=5).read()


def main() -> None:
    events = [json.loads(line) for line in open(FIXTURE, encoding="utf-8") if line.strip()]
    try:
        _post("/reset")
    except Exception:
        pass  # relay may not be up yet for /reset; ingest will still work
    for i, evt in enumerate(events):
        if PACE and i:
            time.sleep(PACE)
        _post("/events", json.dumps(evt).encode())
        p = evt["payload"]
        note = p.get("text") or p.get("summary") or p.get("root_cause") or ""
        print(f"→ {evt['type']:<16} {note[:60]}")
    print(f"done: {len(events)} events")


if __name__ == "__main__":
    main()
