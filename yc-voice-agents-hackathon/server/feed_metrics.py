"""Feed the dashboard the real latency curve (from agent logs) + the latest
Cekura pass/fail, so the metrics panels render reliably (no tunnel needed —
posts straight to the local relay).

Usage:
  pc cloud agent logs flower-bot -n 2500 | uv run --no-project python feed_metrics.py
"""
import json
import re
import sys
import urllib.request

RELAY = "http://127.0.0.1:8080"


def envv(k):
    for line in open(".env", encoding="utf-8"):
        if line.startswith(k + "="):
            return line.split("=", 1)[1].strip()


TOKEN = envv("RELAY_TOKEN")
CEKURA = envv("CEKURA_API_KEY")
_seq = [0]


def relay(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(RELAY + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", "X-Relay-Token": TOKEN})
    try:
        return urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        print("relay err:", e)


def emit(t, p):
    relay("POST", "/events", {"id": f"evt_{_seq[0]:06d}", "seq": _seq[0], "ts": _seq[0],
                              "session_id": "metrics", "type": t, "payload": p})
    _seq[0] += 1


def cekura_get(path):
    return json.load(urllib.request.urlopen(urllib.request.Request(
        "https://api.cekura.ai" + path, headers={"X-CEKURA-API-KEY": CEKURA}), timeout=30))


def main():
    text = sys.stdin.read()
    v2v = [float(x) * 1000 for x in re.findall(r"first_bot_speech_latency[^\n]*\n?[^\n]*?latency=([0-9.]+)s", text)]
    if not v2v:
        v2v = [float(x) * 1000 for x in re.findall(r"latency=([0-9.]+)s", text)]
    llm = [float(x) * 1000 for x in re.findall(r"AnthropicLLMService#0: TTFB ([0-9.]+)s", text)]
    tts = [float(x) * 1000 for x in re.findall(r"GradiumTTSService#0: TTFB ([0-9.]+)s", text)]

    relay("POST", "/reset")
    for ms in v2v:
        emit("latency", {"metric": "v2v", "ms": int(ms)})
    for ms in llm:
        emit("latency", {"metric": "llm_ttfb", "ms": int(ms)})
    for ms in tts:
        emit("latency", {"metric": "tts_ttfb", "ms": int(ms)})
    print(f"fed latency: v2v={len(v2v)} llm={len(llm)} tts={len(tts)}")

    # Latest completed Cekura result -> pass dots
    try:
        if len(sys.argv) > 1:
            rid = sys.argv[1]
        else:
            results = cekura_get("/test_framework/v1/results/")
            results = results if isinstance(results, list) else results.get("results", [])
            rid = next((r["id"] for r in results if r.get("status") == "completed"), results[0]["id"])
        res = cekura_get(f"/test_framework/v1/results/{rid}/")
        runs = res.get("runs"); runs = list(runs.values()) if isinstance(runs, dict) else runs
        dots = [{"name": (r.get("scenario") or {}).get("name", "") if isinstance(r.get("scenario"), dict) else r.get("scenario_name", ""),
                 "passed": bool(r.get("passed") if "passed" in r else r.get("success"))} for r in runs]
        npass = sum(1 for d in dots if d["passed"])
        emit("cekura", {"pass": npass, "total": len(dots), "runs": dots})
        print(f"fed cekura: {npass}/{len(dots)} (result {rid})")
    except Exception as e:
        print("cekura feed skipped:", e)


if __name__ == "__main__":
    main()
