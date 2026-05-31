"""LLM latency benchmark — identical test case across our candidate backends.

Compares time-to-first-token (TTFT, what drives first-audio latency on the voice
leg), end-to-end wall time, and streaming throughput (tok/s) for:

  1. current   — Claude (claude-haiku-4-5), our deployed LLM, via the Anthropic API.
  2. nemotron-public  — documented public endpoint, nvidia/nemotron-3-super over
     QUIC/HTTP-3 (relayed via trycloudflare).
  3. nemotron-direct  — the same model on the direct tailnet (~52 ms), our own host.

Every backend gets the SAME messages, the same max_tokens, and is STREAMED (the
Nemotron reasoning parser returns null content when not streamed). TTFT is measured
at the first chunk carrying actual text — the empty role-priming chunk doesn't count.

Usage:
    uv run python bench_llm.py            # default 5 timed runs (+1 warmup) each
    uv run python bench_llm.py --runs 8
    uv run python bench_llm.py --json out.json   # also dump raw numbers
"""

from __future__ import annotations

import argparse
import json
import statistics
import time

from dotenv import dotenv_values

ENV = dotenv_values(".env")

# ---------------------------------------------------------------------------
# Identical test case — a representative first triage turn (bounded so the run
# is cheap and the comparison is about latency, not output length).
# ---------------------------------------------------------------------------
SYSTEM = (
    "You are an on-call incident-triage voice agent. You have already investigated "
    "the active incident. Brief the engineer concisely and conversationally: what is "
    "broken, the root cause, and the one-line fix you propose. Never apply a fix "
    "without explicit verbal approval."
)
USER = (
    "The on-call engineer just answered. Facts: SEV2 on payments-api — elevated 5xx "
    "since 14:02 UTC, traced to deploy abc123 which shipped a null-check regression in "
    "charge_handler.py. Open the call and brief them now."
)
MAX_TOKENS = 160

# Each backend: a callable that streams the test case and returns
# (ttft_s, total_s, completion_tokens). Built lazily so a missing SDK / unreachable
# host only fails its own backend.
# Ours: the public Cloudflare/QUIC tunnel we stood up (NEMOTRON_LLM_URL in .env).
NEMOTRON_OURS_URL = "https://bottle-kent-oriented-upload.trycloudflare.com/v1"
# The provider's endpoint: the AWS ALB fronting their vLLM fleet.
NEMOTRON_PROVIDER_URL = "http://nemotron-fleet-alb-1322439314.us-west-2.elb.amazonaws.com/v1"
NEMOTRON_MODEL = "nvidia/nemotron-3-super"


def _run_claude() -> tuple[float, float, int]:
    from anthropic import Anthropic

    client = Anthropic(api_key=ENV["ANTHROPIC_API_KEY"])
    model = ENV.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    t0 = time.perf_counter()
    ttft = None
    out_tokens = 0
    with client.messages.stream(
        model=model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM,
        messages=[{"role": "user", "content": USER}],
    ) as stream:
        for text in stream.text_stream:
            if text and ttft is None:
                ttft = time.perf_counter() - t0
        final = stream.get_final_message()
        out_tokens = final.usage.output_tokens
    total = time.perf_counter() - t0
    return ttft if ttft is not None else total, total, out_tokens


def _make_nemotron(base_url: str):
    def _run() -> tuple[float, float, int]:
        from openai import OpenAI

        client = OpenAI(base_url=base_url, api_key="x", timeout=30.0)
        t0 = time.perf_counter()
        ttft = None
        out_tokens = 0
        stream = client.chat.completions.create(
            model=NEMOTRON_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": USER},
            ],
            max_tokens=MAX_TOKENS,
            stream=True,
            stream_options={"include_usage": True},
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        for chunk in stream:
            if chunk.usage is not None:
                out_tokens = chunk.usage.completion_tokens
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and delta.content and ttft is None:
                ttft = time.perf_counter() - t0
        total = time.perf_counter() - t0
        return ttft if ttft is not None else total, total, out_tokens

    return _run


BACKENDS = {
    "Claude haiku-4-5 (current)": _run_claude,
    "Nemotron — ours (Cloudflare QUIC)": _make_nemotron(NEMOTRON_OURS_URL),
    "Nemotron — provider (AWS ALB)": _make_nemotron(NEMOTRON_PROVIDER_URL),
}


def bench(name: str, fn, runs: int) -> dict:
    # One warmup (connection setup / cold path) — discarded.
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        return {"name": name, "ok": False, "error": f"{type(e).__name__}: {e}"}

    ttfts, totals, tps = [], [], []
    for _ in range(runs):
        try:
            ttft, total, toks = fn()
        except Exception as e:  # noqa: BLE001
            return {"name": name, "ok": False, "error": f"{type(e).__name__}: {e}"}
        ttfts.append(ttft)
        totals.append(total)
        gen = max(total - ttft, 1e-6)
        tps.append(toks / gen if toks else 0.0)
    return {
        "name": name,
        "ok": True,
        "runs": runs,
        "ttft_ms_median": round(statistics.median(ttfts) * 1000, 1),
        "ttft_ms_min": round(min(ttfts) * 1000, 1),
        "total_ms_median": round(statistics.median(totals) * 1000, 1),
        "tok_s_median": round(statistics.median(tps), 1),
        "raw_ttft_ms": [round(x * 1000, 1) for x in ttfts],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--json", type=str, default=None)
    args = ap.parse_args()

    print(f"\nLLM latency benchmark — {args.runs} timed runs each (+1 warmup discarded)")
    print(f"test case: {len(SYSTEM)}-char system + {len(USER)}-char user, max_tokens={MAX_TOKENS}\n")

    results = []
    for name, fn in BACKENDS.items():
        print(f"  running {name} ...", flush=True)
        r = bench(name, fn, args.runs)
        results.append(r)

    print("\n" + "=" * 78)
    print(f"{'backend':<34}{'TTFT med':>10}{'TTFT min':>10}{'total med':>11}{'tok/s':>8}")
    print("-" * 78)
    for r in results:
        if not r["ok"]:
            print(f"{r['name']:<34}  BLOCKED — {r['error'][:30]}")
            continue
        print(
            f"{r['name']:<34}{r['ttft_ms_median']:>9}m{r['ttft_ms_min']:>9}m"
            f"{r['total_ms_median']:>10}m{r['tok_s_median']:>8}"
        )
    print("=" * 78)
    print("TTFT = time to first *text* token (drives first-audio latency). lower is better.\n")

    if args.json:
        with open(args.json, "w") as f:
            json.dump(results, f, indent=2)
        print(f"raw numbers -> {args.json}\n")


if __name__ == "__main__":
    main()
