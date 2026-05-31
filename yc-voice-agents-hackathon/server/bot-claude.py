#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Incident-triage voice agent on Claude (Anthropic).

Forked from the flower-shop starter (bot-gpt.py). Keeps the same transport /
pipeline / runner plumbing and Gradium STT + Gradium TTS; swaps the LLM for
Pipecat's AnthropicLLMService and the flower tools/prompt for the shared triage
module (see triage.py). An engineer calls in (or is connected to) on-call
triage; the bot investigates the active incident with tools, synthesizes the
root cause aloud, can page the right on-shift engineer, and — only after a
verbal yes — applies a scoped code fix.

Pipeline: Gradium STT → Anthropic (Claude) LLM → Gradium TTS, with the shared
direct-function triage tools registered on the LLM context.

Run the bot using::

    uv run bot-claude.py
"""

import asyncio
import os
import uuid

import aiohttp
from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame

# verify: Anthropic LLM service lives at pipecat.services.anthropic.llm
# (confirmed against pipecat-ai main: class AnthropicLLMService, system_instruction
# is passed via Settings — mirrors how bot-gpt.py uses OpenAIResponsesLLMService.Settings).
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import (
    RunnerArguments,
    SmallWebRTCRunnerArguments,
    WebSocketRunnerArguments,
)
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.gradium.stt import GradiumSTTService
from pipecat.services.gradium.tts import GradiumTTSService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.turns.user_turn_strategies import FilterIncompleteUserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from conf_filter import ConfFilterProcessor
from transcript_tap import TranscriptObserver
from triage import (
    OPT_C,
    OPT_G,
    TRIAGE_SYSTEM_INSTRUCTION,
    build_triage,
    prime_investigation,
    system_instruction,
)
from tts_cache import opener_frames, prerender

load_dotenv(override=True)


async def get_call_info(call_sid: str) -> dict:
    """Fetch call information from Twilio REST API using aiohttp.

    Args:
        call_sid: The Twilio call SID

    Returns:
        Dictionary containing call information including from_number, to_number, status, etc.
    """
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")

    if not account_sid or not auth_token:
        logger.warning("Missing Twilio credentials, cannot fetch call info")
        return {}

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json"

    try:
        # Use HTTP Basic Auth with aiohttp
        auth = aiohttp.BasicAuth(account_sid, auth_token)

        async with aiohttp.ClientSession() as session:
            async with session.get(url, auth=auth) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Twilio API error ({response.status}): {error_text}")
                    return {}

                data = await response.json()

                call_info = {
                    "from_number": data.get("from"),
                    "to_number": data.get("to"),
                }

                return call_info

    except Exception as e:
        logger.error(f"Error fetching call info from Twilio: {e}")
        return {}


async def run_bot(
    transport: BaseTransport,
    from_number: str | None = None,
    audio_in_sample_rate: int = 16000,
    audio_out_sample_rate: int = 24000,
):
    """Main bot logic.

    Args:
        transport: The transport to use.
        from_number: Caller's phone number (Twilio path only). Unused for triage
            but kept for parity with the starter's transport plumbing.
        audio_in_sample_rate: Input audio sample rate in Hz. Defaults to 16000 (WebRTC).
        audio_out_sample_rate: Output audio sample rate in Hz. Defaults to 24000 (WebRTC).
    """
    logger.info("Starting bot")

    # Speech-to-Text service (unchanged from the starter)
    stt = GradiumSTTService(
        api_key=os.environ["GRADIUM_API_KEY"],
        settings=GradiumSTTService.Settings(
            language=Language.EN,
        ),
    )

    # LLM service. Default is Nemotron-3-Super on our self-hosted B200 (NVFP4 +
    # FlashInfer) via the Cloudflare QUIC tunnel. Set LLM_BACKEND=claude to swap
    # back to Anthropic Claude as a fallback (same STT/TTS/prompt/tools — used
    # for the per-provider accuracy A/B). The triage system instruction is a
    # module constant, passed at construction either way.
    backend = os.getenv("LLM_BACKEND", "nemotron").lower()
    if backend == "nemotron":
        from nemotron_llm import VLLMOpenAILLMService

        enable_thinking = os.getenv("NEMOTRON_ENABLE_THINKING", "false").lower() == "true"
        session_id = f"nemotron-{uuid.uuid4().hex[:12]}"
        llm = VLLMOpenAILLMService(
            api_key=os.getenv("NEMOTRON_LLM_API_KEY", "EMPTY"),
            # Cloudflare-tunneled B200 endpoint; override via NEMOTRON_LLM_URL
            # if the tunnel URL rotates (free-tier trycloudflare names are
            # ephemeral — bring up a fresh tunnel + update the secret set).
            base_url=os.getenv(
                "NEMOTRON_LLM_URL",
                "https://bottle-kent-oriented-upload.trycloudflare.com/v1",
            ),
            settings=VLLMOpenAILLMService.Settings(
                model=os.getenv("NEMOTRON_LLM_MODEL", "nvidia/nemotron-3-super"),
                system_instruction=system_instruction(),
                # chat_template_kwargs must reach vLLM at the TOP LEVEL of the
                # request body for the chat template to read enable_thinking;
                # Pipecat forwards Settings.extra as **kwargs to the OpenAI
                # SDK's chat.completions.create(), where extra_body is the
                # official kwarg the SDK hoists into the body. Verified by
                # server/capture_pipecat_outbound.py.
                extra={"extra_body": {"chat_template_kwargs": {"enable_thinking": enable_thinking}}},
            ),
        )
    else:
        session_id = f"claude-{uuid.uuid4().hex[:12]}"
        llm = AnthropicLLMService(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            settings=AnthropicLLMService.Settings(
                model=os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
                system_instruction=system_instruction(),
            ),
        )

    # Shared triage wiring: per-session state, event bus, tools (registered on
    # llm), greeting.
    triage = build_triage(session_id, llm)
    tools = triage["tools"]
    bus = triage["bus"]
    greeting = triage["greeting"]

    # Text-to-Speech service (unchanged from the starter)
    tts = GradiumTTSService(
        api_key=os.environ["GRADIUM_API_KEY"],
        settings=GradiumTTSService.Settings(
            voice=os.getenv("GRADIUM_VOICE_ID", "_6Aslh2DxfmnRLmP"),
        ),
    )

    # OPT_C: pre-render the fixed opener to PCM off the pipeline, concurrently with
    # the rest of setup, so it's ready to push as instant first audio on connect.
    prerender_task = None
    if OPT_C:
        prerender_task = asyncio.ensure_future(
            prerender(
                [greeting],
                api_key=os.environ["GRADIUM_API_KEY"],
                voice_id=os.getenv("GRADIUM_VOICE_ID", "_6Aslh2DxfmnRLmP"),
            )
        )

    context = LLMContext(tools=tools)
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            user_turn_strategies=FilterIncompleteUserTurnStrategies(),
        ),
    )

    # Pipeline - assembled from reusable components. Under OPT_G, the
    # ConfFilterProcessor sits BETWEEN the llm and tts so it strips the <conf>
    # keystone tag (and tracks/emits confidence) before TTS speaks the text.
    # With OPT_G off, the pipeline is exactly as before (no extra processor).
    processors = [
        transport.input(),
        stt,
        user_aggregator,
        llm,
    ]
    if OPT_G:
        processors.append(ConfFilterProcessor(triage["loop"], bus=bus))
    processors += [
        tts,
        transport.output(),
        assistant_aggregator,
    ]
    pipeline = Pipeline(processors)

    # Transcript tap: a non-intrusive Observer (sees every frame without being
    # placed in the pipeline path) that streams finalized spoken turns to the
    # dashboard as `transcript` events. Attached via PipelineWorker(observers=...).
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
            audio_in_sample_rate=audio_in_sample_rate,
            audio_out_sample_rate=audio_out_sample_rate,
        ),
        observers=[TranscriptObserver(bus)],
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")
        bus.emit("session_start", {"session_id": session_id, "backend": backend})
        # Show the investigation on the dashboard instantly (agent already triaged).
        prime_investigation(triage["state"], bus)

        # OPT_C: push the pre-rendered opener as instant first audio, then have the
        # LLM continue from it (the "_opened" kickoff doesn't re-greet). If the
        # cache missed (render failed/timed out), fall back to the normal kickoff
        # so the LLM speaks the opener itself — no behavior change.
        kickoff = triage["kickoff"]
        if OPT_C and prerender_task is not None:
            try:
                cache = await prerender_task
            except Exception as e:  # noqa: BLE001 — never block connect on prerender
                logger.warning(f"OPT_C: prerender task failed: {e}")
                cache = {}
            pcm = cache.get(greeting)
            if pcm:
                logger.info(f"OPT_C: pushing {len(pcm)} bytes of cached opener audio")
                await worker.queue_frames(opener_frames(pcm))
                kickoff = triage["kickoff_opened"]

        # Kick off with the fact-loaded briefing prompt — the agent leads + briefs now.
        context.add_message({"role": "user", "content": kickoff})
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        bus.emit("session_end", {"session_id": session_id, "reason": "disconnected"})
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)

    await runner.add_workers(worker)
    await runner.run()


async def bot(runner_args: RunnerArguments):
    """Main bot entry point."""

    from_number: str | None = None
    transport_overrides: dict = {}

    # Krisp is available when deployed to Pipecat Cloud
    if os.environ.get("ENV") != "local":
        from pipecat.audio.filters.krisp_viva_filter import KrispVivaFilter

        krisp_filter = KrispVivaFilter()
    else:
        krisp_filter = None

    match runner_args:
        case SmallWebRTCRunnerArguments():
            webrtc_connection: SmallWebRTCConnection = runner_args.webrtc_connection

            transport = SmallWebRTCTransport(
                webrtc_connection=webrtc_connection,
                params=TransportParams(
                    audio_in_enabled=True,
                    audio_in_filter=krisp_filter,
                    audio_out_enabled=True,
                ),
            )
        case WebSocketRunnerArguments():
            # Twilio media streams are 8 kHz μ-law in both directions.
            # This overrides the default sample rates: 16 kHz in / 24 kHz out.
            transport_overrides["audio_in_sample_rate"] = 8000
            transport_overrides["audio_out_sample_rate"] = 8000

            # Parse Twilio websocket and fetch call information
            _, call_data = await parse_telephony_websocket(runner_args.websocket)

            call_info = await get_call_info(call_data["call_id"])
            if call_info:
                from_number = call_info.get("from_number")
                logger.info(f"Call from: {from_number} to: {call_info.get('to_number')}")

            serializer = TwilioFrameSerializer(
                stream_sid=call_data["stream_id"],
                call_sid=call_data["call_id"],
                account_sid=os.getenv("TWILIO_ACCOUNT_SID", ""),
                auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""),
            )

            transport = FastAPIWebsocketTransport(
                websocket=runner_args.websocket,
                params=FastAPIWebsocketParams(
                    audio_in_enabled=True,
                    audio_in_filter=krisp_filter,
                    audio_out_enabled=True,
                    add_wav_header=False,
                    serializer=serializer,
                ),
            )
        case _:
            logger.error(f"Unsupported runner arguments type: {type(runner_args)}")
            return

    await run_bot(transport, from_number=from_number, **transport_overrides)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
