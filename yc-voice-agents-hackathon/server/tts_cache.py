"""Optimization C — pre-cached TTS for the fixed opener (first-audio latency).

The briefing always opens with one fixed line (``triage.GREETING_PROMPT``); only
the *specifics* after it are LLM-generated. On a cold call the engineer otherwise
waits for the full STT-warmup → LLM TTFT → TTS first-chunk chain (~1–1.5 s) before
hearing anything. This module pre-renders that fixed opener to raw PCM ONCE at
worker startup (over a direct Gradium websocket, off the pipeline), so on connect
the bot can push the audio immediately — a voice in well under ~200 ms — while the
LLM generates the rest of the briefing in parallel.

Everything here is gated by ``OPT_C`` at the call sites; with the toggle off this
module is never imported into the hot path and behavior is byte-for-byte unchanged.

Gradium's TTS websocket protocol (see pipecat.services.gradium.tts):
  → {"type":"setup","output_format":"pcm","voice_id":...,"client_req_id":id,
     "close_ws_on_eos":true}
  → {"type":"text","text":...,"client_req_id":id}
  → {"type":"end_of_stream","client_req_id":id}
  ← {"type":"audio","audio":<base64 pcm>,...}*   (16-bit LE mono @ 48 kHz)
  ← {"type":"end_of_stream",...}
"""

from __future__ import annotations

import asyncio
import base64
import json

from loguru import logger

# Gradium streams 16-bit little-endian mono PCM at this rate (SAMPLE_RATE in the
# pipecat Gradium service). The output transport resamples to the call's rate.
GRADIUM_SAMPLE_RATE = 48000
GRADIUM_TTS_URL = "wss://api.gradium.ai/api/speech/tts"

# Per-phrase ceiling so a hung socket can never block connect indefinitely; the
# caller treats a timeout/failure as "no cache" and falls back to live TTS.
_RENDER_TIMEOUT_S = 8.0


async def _render_one(
    text: str,
    *,
    api_key: str,
    voice_id: str,
    url: str,
    req_id: str = "prerender",
) -> bytes:
    """Render a single phrase to PCM bytes over a fresh Gradium websocket.

    Returns the concatenated PCM (16-bit LE mono @ ``GRADIUM_SAMPLE_RATE``), or
    ``b""`` if anything goes wrong — the caller is expected to degrade to live TTS.
    """
    from websockets.asyncio.client import connect as websocket_connect

    headers = {"x-api-key": api_key, "x-api-source": "pipecat"}
    chunks: list[bytes] = []
    async with websocket_connect(url, additional_headers=headers) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "setup",
                    "output_format": "pcm",
                    "voice_id": voice_id,
                    "close_ws_on_eos": True,
                    "client_req_id": req_id,
                }
            )
        )
        await ws.send(json.dumps({"type": "text", "text": text, "client_req_id": req_id}))
        await ws.send(json.dumps({"type": "end_of_stream", "client_req_id": req_id}))

        async for message in ws:
            msg = json.loads(message)
            mtype = msg.get("type")
            if mtype == "audio":
                chunks.append(base64.b64decode(msg["audio"]))
            elif mtype == "end_of_stream":
                break
            elif mtype == "error":
                logger.error(f"tts_cache: Gradium error rendering opener: {msg.get('message', msg)}")
                break
    return b"".join(chunks)


async def prerender(
    phrases: list[str],
    *,
    api_key: str,
    voice_id: str,
    url: str = GRADIUM_TTS_URL,
) -> dict[str, bytes]:
    """Pre-render ``phrases`` to PCM, keyed by the phrase text.

    Failures are swallowed per-phrase (logged, omitted from the result) so a flaky
    render never blocks startup or the call — a missing key just means that phrase
    falls back to live TTS.
    """
    out: dict[str, bytes] = {}
    for text in phrases:
        try:
            pcm = await asyncio.wait_for(
                _render_one(text, api_key=api_key, voice_id=voice_id, url=url),
                timeout=_RENDER_TIMEOUT_S,
            )
        except Exception as e:  # noqa: BLE001 — never let prerender break startup
            logger.warning(f"tts_cache: prerender failed for {text[:40]!r}: {e}")
            continue
        if pcm:
            out[text] = pcm
            logger.info(f"tts_cache: cached {len(pcm)} PCM bytes for opener {text[:40]!r}")
        else:
            logger.warning(f"tts_cache: empty render for {text[:40]!r} — will use live TTS")
    return out


def opener_frames(pcm: bytes, *, sample_rate: int = GRADIUM_SAMPLE_RATE, chunk_ms: int = 20):
    """Slice cached PCM into ~``chunk_ms`` ``OutputAudioRawFrame``s for smooth playout.

    The output transport resamples these to the call's output rate exactly as it
    does live TTS audio, so the 48 kHz source plays correctly on an 8 kHz Twilio leg.
    Imported lazily so this module stays importable (and unit-testable) without a
    full pipecat install present.
    """
    from pipecat.frames.frames import OutputAudioRawFrame

    bytes_per_sample = 2  # 16-bit mono
    frame_bytes = max(bytes_per_sample, int(sample_rate * chunk_ms / 1000) * bytes_per_sample)
    frames = []
    for i in range(0, len(pcm), frame_bytes):
        frames.append(
            OutputAudioRawFrame(
                audio=pcm[i : i + frame_bytes],
                sample_rate=sample_rate,
                num_channels=1,
            )
        )
    return frames
