"""Run the full 8-scenario Cekura suite in small batches (the account balance
can't hold all 8 at once → HTTP 402), poll each batch to completion, and print
the result_ids so they can be scored with bench_accuracy.py.

The per-run Cekura "success" is just "call connected" (its Expected-Outcome metric
isn't attached), so accuracy comes from scoring the captured transcripts with
bench_accuracy.py — this just makes sure every scenario actually RUNS and produces
a transcript on whatever backend is currently deployed.

    uv run python cekura_eval.py                 # batch size 3
    uv run python cekura_eval.py --batch 2       # smaller if 402s persist
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

from dotenv import dotenv_values

ENV = dotenv_values(".env")
KEY = ENV["CEKURA_API_KEY"]
BASE = "https://api.cekura.ai"


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"X-CEKURA-API-KEY": KEY, "Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(r, timeout=60)
        return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, e.read().decode()[:200]


def run_batch(sids):
    body = {"scenarios": [{"scenario": s} for s in sids], "frequency": 1}
    return req("POST", "/test_framework/v1/scenarios/run_scenarios_pipecat_v2/", body)


def poll(rid, timeout=240):
    t0 = time.time()
    while time.time() - t0 < timeout:
        c, d = req("GET", f"/test_framework/v1/results/{rid}/")
        if isinstance(d, dict) and d.get("status") == "completed":
            return d
        time.sleep(12)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=3)
    ap.add_argument("--scenarios", type=str, default=None,
                    help="comma-separated scenario ids; default = all from state")
    args = ap.parse_args()

    if args.scenarios:
        sids = [int(x) for x in args.scenarios.split(",")]
    else:
        sids = json.load(open(".cekura_state.json"))["scenario_ids"]

    batch = args.batch
    result_ids = []
    i = 0
    while i < len(sids):
        chunk = sids[i:i + batch]
        c, d = run_batch(chunk)
        if c == 402:
            if batch > 1:
                batch -= 1
                print(f"  402 low-balance hold; shrinking batch to {batch} and retrying")
                continue
            print(f"  402 low balance even at batch=1 — stopping. got {result_ids}")
            break
        if c != 200 or not isinstance(d, dict):
            print(f"  run failed [{c}]: {str(d)[:160]}")
            break
        rid = d["id"]
        result_ids.append(rid)
        print(f"  batch {chunk} -> result {rid} (running)")
        poll(rid)
        print(f"    result {rid} completed")
        i += batch

    print("\nRESULT_IDS:", ",".join(str(r) for r in result_ids))


if __name__ == "__main__":
    main()
