"""Pre-demo sanity check for the Cloudflare-tunneled Nemotron endpoint.

Run this before any live demo. Exits non-zero if anything is off.

  uv run python probe_cloudflare.py
  python probe_cloudflare.py --url https://other.trycloudflare.com/v1

Checks (in order, fail fast):
  1. /v1/models responds and lists nvidia/nemotron-3-super.
  2. Non-streaming chat completion returns a non-null content.
  3. Streaming chat completion's first content delta lands in <500 ms.
  4. enable_thinking=false is honored (no reasoning tokens generated when
     thinking is disabled — otherwise voice turns will burn budget on
     internal monologue and truncate the spoken answer).

Why this exists: trycloudflare URLs are ephemeral (the tunnel dies with the
process). Before a demo, run this to catch a stale URL, a vLLM restart that
broke the thinking flag, or a NAT/QUIC hiccup that landed us on a slow path.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = "https://bottle-kent-oriented-upload.trycloudflare.com/v1"
MODEL = "nvidia/nemotron-3-super"
TTFT_BUDGET_S = 0.500


class Failed(Exception):
    pass


def _get(url: str, timeout: float = 10.0):
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read())


def _post_stream(url: str, body: dict, timeout: float = 30.0):
    req = urllib.request.Request(
        url, method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps(body).encode(),
    )
    return urllib.request.urlopen(req, timeout=timeout)


def check_models(base: str) -> None:
    print(f"[1/4] GET {base}/models ... ", end="", flush=True)
    code, body = _get(f"{base}/models")
    if code != 200:
        raise Failed(f"models endpoint returned {code}")
    ids = [m.get("id") for m in body.get("data", [])]
    if MODEL not in ids:
        raise Failed(f"model {MODEL} not in /models listing: {ids}")
    print(f"OK ({MODEL} present)")


def check_completion(base: str) -> None:
    print("[2/4] non-stream chat.completions ... ", end="", flush=True)
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Say PONG in one word."}],
        "max_tokens": 32,
        "temperature": 0,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }
    resp = _post_stream(f"{base}/chat/completions", body, timeout=30.0)
    data = json.loads(resp.read())
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content")
    if not content:
        raise Failed(
            "content is null/empty — model may be thinking despite "
            "enable_thinking=false (see check [4/4])"
        )
    print(f"OK (got {len(content)} chars)")


def check_ttft(base: str) -> None:
    print(f"[3/4] stream first-content-delta ≤ {TTFT_BUDGET_S*1000:.0f}ms ... ", end="", flush=True)
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Hi."}],
        "max_tokens": 8,
        "stream": True,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }
    t0 = time.perf_counter()
    resp = _post_stream(f"{base}/chat/completions", body, timeout=15.0)
    first_content_at = None
    for line in resp:
        if not line.startswith(b"data: "):
            continue
        chunk = line[6:].strip()
        if chunk in (b"[DONE]", b""):
            continue
        evt = json.loads(chunk)
        delta = (evt.get("choices") or [{}])[0].get("delta", {})
        if delta.get("content"):
            first_content_at = time.perf_counter() - t0
            break
    if first_content_at is None:
        raise Failed("stream finished with no content delta")
    if first_content_at > TTFT_BUDGET_S:
        raise Failed(f"TTFT {first_content_at*1000:.0f}ms exceeds {TTFT_BUDGET_S*1000:.0f}ms budget")
    print(f"OK ({first_content_at*1000:.0f}ms)")


def check_thinking_honored(base: str) -> None:
    print("[4/4] enable_thinking=false actually honored ... ", end="", flush=True)
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
        "max_tokens": 32,
        "temperature": 0,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }
    resp = _post_stream(f"{base}/chat/completions", body, timeout=30.0)
    data = json.loads(resp.read())
    msg = (data.get("choices") or [{}])[0].get("message", {})
    reasoning = msg.get("reasoning")
    if reasoning:
        raise Failed(
            f"server emitted reasoning tokens despite enable_thinking=false; "
            f"reasoning starts: {reasoning[:80]!r}. This will burn the voice "
            f"agent's max_tokens budget and may truncate the spoken answer. "
            f"Relaunch vLLM with the thinking-off default, or fix the chat template."
        )
    print("OK (no reasoning field)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL, help=f"base URL (default: {DEFAULT_URL})")
    args = ap.parse_args()
    base = args.url.rstrip("/")
    print(f"probing {base}\n")
    try:
        check_models(base)
        check_completion(base)
        check_ttft(base)
        check_thinking_honored(base)
    except Failed as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print(f"FAIL (network): {e}", file=sys.stderr)
        return 1
    print("\nall checks passed — endpoint is demo-ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
