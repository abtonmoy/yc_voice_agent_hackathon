"""Capture the outbound JSON body that Pipecat's OpenAI service sends to vLLM,
to confirm `chat_template_kwargs.enable_thinking=false` is actually on the wire.

Background — why this exists:
  bot-nemotron.py constructs the LLM service with
      Settings(extra={"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}})
  Pipecat forwards Settings.extra as **kwargs to AsyncOpenAI.chat.completions.create.
  The OpenAI Python SDK's `extra_body` kwarg is the official way to add custom
  fields to the JSON body — it HOISTS the contents up so they appear at the
  top level. vLLM's chat template only sees top-level `chat_template_kwargs`;
  if it ends up nested inside a literal `extra_body` field, it's ignored.

  A previous version of probe_cloudflare.py made exactly this mistake — sent
  `"extra_body": {...}` as a literal field in the body. vLLM ignored it,
  thinking ran, content was delayed. Two agents argued for an hour about
  whether the endpoint was broken; the bug was in the client.

This script stands up a localhost mock server, points an AsyncOpenAI client
at it (using the SDK call pattern Pipecat uses), captures the wire body,
and prints a verdict.

  uv run python capture_pipecat_outbound.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager

from aiohttp import web
from openai import AsyncOpenAI

CAPTURED: list[dict] = []


async def _handle_completion(request: web.Request) -> web.StreamResponse:
    body = await request.json()
    CAPTURED.append(body)
    # Minimal valid SSE so the SDK doesn't raise.
    resp = web.StreamResponse(
        headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"}
    )
    await resp.prepare(request)
    await resp.write(
        b'data: {"id":"x","object":"chat.completion.chunk","created":0,'
        b'"model":"t","choices":[{"index":0,"delta":{"content":"ok"},'
        b'"finish_reason":null}]}\n\n'
        b'data: {"id":"x","object":"chat.completion.chunk","created":0,'
        b'"model":"t","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        b'data: [DONE]\n\n'
    )
    await resp.write_eof()
    return resp


async def _handle_models(_: web.Request) -> web.Response:
    return web.json_response(
        {"object": "list", "data": [{"id": "nvidia/nemotron-3-super", "object": "model"}]}
    )


@asynccontextmanager
async def _mock_server(port: int = 9999):
    app = web.Application()
    app.router.add_post("/v1/chat/completions", _handle_completion)
    app.router.add_get("/v1/models", _handle_models)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        await runner.cleanup()


async def _drive_sdk(base_url: str) -> None:
    """Call AsyncOpenAI the same way Pipecat does: SDK kwargs include extra_body,
    which the SDK is documented to hoist into the JSON body."""
    client = AsyncOpenAI(base_url=base_url, api_key="EMPTY")
    stream = await client.chat.completions.create(
        model="nvidia/nemotron-3-super",
        messages=[{"role": "user", "content": "test"}],
        max_tokens=4,
        stream=True,
        # This is the kwarg Pipecat ends up calling with, via Settings(extra=...).
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    async for _ in stream:
        pass


def _verdict(body: dict) -> int:
    print("\n=== captured outbound JSON ===")
    print(json.dumps(body, indent=2, default=str))
    print()

    ct = body.get("chat_template_kwargs")
    if isinstance(ct, dict) and "enable_thinking" in ct:
        print(
            f"✅ chat_template_kwargs.enable_thinking = {ct['enable_thinking']!r} "
            f"is at the TOP LEVEL of the body — vLLM's chat template will see it."
        )
        if ct["enable_thinking"] is False:
            print(
                "   This is the path bot-nemotron.py + Pipecat take. If the deployed "
                "agent is on Nemotron and you still see reasoning, the issue is in "
                "Settings.extra propagation, not here."
            )
        return 0

    if "extra_body" in body:
        print(
            "❌ FOUND BUG: `extra_body` is a literal JSON field in the body — the "
            "client did NOT hoist its contents. vLLM ignores unknown top-level fields, "
            "so chat_template_kwargs never reaches the chat template, so the model "
            "thinks regardless of enable_thinking=false.\n"
            "Fix: pass chat_template_kwargs at the top level of the body (or use the "
            "OpenAI SDK's extra_body kwarg, which hoists automatically)."
        )
        return 1

    print(
        "❌ chat_template_kwargs is missing entirely. The client built the body "
        "without including it at all. Check how Settings.extra is being forwarded "
        "to the underlying OpenAI call."
    )
    return 1


async def main() -> int:
    async with _mock_server() as base_url:
        print(f"mock vLLM listening on {base_url}\n")
        try:
            await _drive_sdk(base_url)
        except Exception as e:
            print(f"sdk call raised (expected for minimal mock): {type(e).__name__}: {e}")

    if not CAPTURED:
        print("FAIL: no request reached the mock server.")
        return 1
    return _verdict(CAPTURED[-1])


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
