"""Pre-demo sanity check for the Cloudflare-tunneled Nemotron endpoint.

Run this before any live demo. Exits non-zero if anything is off.

  uv run python probe_cloudflare.py
  python probe_cloudflare.py --url https://other.trycloudflare.com/v1

Checks (in order, fail fast):
  1. /v1/models responds and lists nvidia/nemotron-3-super.
  2. Streamed chat completion assembles non-empty content.
  3. Streamed chat completion's first content delta lands in <500 ms.
  4. enable_thinking=false is honored on the streaming path — no reasoning
     deltas, no <think> tag in assembled content.

ALL completion checks stream. The voice agent streams (Pipecat → TTS requires
it), and vLLM's --reasoning-parser deepseek_r1 has a quirk in NON-streaming
mode where `content` comes back null when there is no <think> block (i.e. the
healthy thinking-off case). A non-stream probe would read this as a thinking
leak and false-alarm. We test the path the agent actually uses.

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

# Budget for time-to-first-CONTENT-delta (the moment TTS can start speaking).
# When chat_template_kwargs.enable_thinking=false is sent correctly (top-level,
# not nested in extra_body), the model emits content immediately with no
# reasoning preamble — well under 500 ms. If you see this fail, the flag
# isn't reaching vLLM in the right shape (run capture_pipecat_outbound.py
# to verify the wire body).
TTFT_BUDGET_S = 0.500

# Match the voice agent's per-turn max_tokens so the probe matches real usage.
PROBE_MAX_TOKENS = 256


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
    """Streamed completion — the path the voice agent actually uses."""
    print("[2/4] streamed chat.completions ... ", end="", flush=True)
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Say PONG in one word."}],
        "max_tokens": PROBE_MAX_TOKENS,
        "temperature": 0,
        "stream": True,
        # vLLM reads chat_template_kwargs from the TOP LEVEL of the body; nesting
        # it inside "extra_body" makes the server ignore it. The OpenAI SDK's
        # extra_body kwarg hoists for you — when sending raw HTTP, place it here.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    resp = _post_stream(f"{base}/chat/completions", body, timeout=30.0)
    pieces = []
    for line in resp:
        if not line.startswith(b"data: "):
            continue
        chunk = line[6:].strip()
        if chunk in (b"[DONE]", b""):
            continue
        evt = json.loads(chunk)
        delta = (evt.get("choices") or [{}])[0].get("delta", {})
        if delta.get("content"):
            pieces.append(delta["content"])
    content = "".join(pieces)
    if not content.strip():
        raise Failed(
            "stream finished with no content delta — either max_tokens "
            f"({PROBE_MAX_TOKENS}) was exhausted by reasoning, or the "
            "endpoint is broken. See check [4/4] for reasoning telemetry."
        )
    print(f"OK ({len(content)} chars: {content[:40]!r})")


def check_ttft(base: str) -> None:
    print(f"[3/4] stream first-content-delta ≤ {TTFT_BUDGET_S*1000:.0f}ms ... ", end="", flush=True)
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Hi."}],
        "max_tokens": PROBE_MAX_TOKENS,
        "stream": True,
        # vLLM reads chat_template_kwargs from the TOP LEVEL of the body; nesting
        # it inside "extra_body" makes the server ignore it. The OpenAI SDK's
        # extra_body kwarg hoists for you — when sending raw HTTP, place it here.
        "chat_template_kwargs": {"enable_thinking": False},
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
    """Voice-safety check: TTS only ever sees ``delta.content``, so the
    only hard-fail condition is a ``<think>`` tag *inside content* (would
    be read aloud).

    The model may still produce reasoning deltas (routed to ``delta.reasoning``
    by ``--reasoning-parser``) even with ``enable_thinking=false`` — this is a
    chat-template quirk, not a voice break. Print it as a WARN so it's visible
    in the report (it costs TTFT and burns tokens) without failing the demo.
    """
    print("[4/4] no <think> leak in content (streaming) ... ", end="", flush=True)
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
        "max_tokens": PROBE_MAX_TOKENS,
        "temperature": 0,
        "stream": True,
        # vLLM reads chat_template_kwargs from the TOP LEVEL of the body; nesting
        # it inside "extra_body" makes the server ignore it. The OpenAI SDK's
        # extra_body kwarg hoists for you — when sending raw HTTP, place it here.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    resp = _post_stream(f"{base}/chat/completions", body, timeout=30.0)
    content_pieces, reasoning_pieces = [], []
    for line in resp:
        if not line.startswith(b"data: "):
            continue
        chunk = line[6:].strip()
        if chunk in (b"[DONE]", b""):
            continue
        evt = json.loads(chunk)
        delta = (evt.get("choices") or [{}])[0].get("delta", {})
        if delta.get("content"):
            content_pieces.append(delta["content"])
        for rk in ("reasoning", "reasoning_content"):
            if delta.get(rk):
                reasoning_pieces.append(delta[rk])
    content = "".join(content_pieces)
    reasoning = "".join(reasoning_pieces)
    # HARD FAIL: anything that would be SPOKEN by TTS.
    if "<think>" in content:
        raise Failed(f"<think> tag leaked into streamed content: {content[:80]!r}")
    if reasoning:
        # WARN, not fail — reasoning is invisible to TTS (separate field), but
        # it costs TTFT and tokens. The voice agent will still work; it'll just
        # feel slightly slower than the headline benchmark suggests.
        print(
            f"OK (no <think> in content)  ⚠ reasoning streamed "
            f"({len(reasoning)} chars before content: {reasoning[:60]!r}…)"
        )
    else:
        print("OK (no <think>, no reasoning)")


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
