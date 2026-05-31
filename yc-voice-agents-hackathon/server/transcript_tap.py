#
# Copyright (c) 2024-2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Transcript tap: stream spoken turns to the dashboard as ``transcript`` events.

A Pipecat **Observer** that watches every frame flowing through the pipeline
(without being inserted into the pipeline path — same mechanism as
``pipecat/observers/loggers/transcription_log_observer.py``) and emits
``bus.emit("transcript", {"role", "text", "final": True})`` for each finalized
spoken turn so the dashboard (which already renders ``transcript`` events) shows
the live conversation.

Frame mapping (confirmed against the installed pipecat 1.3.0 source):

- USER turn  -> ``TranscriptionFrame`` (final STT result, has ``.text``). We emit
  role="user" once per final transcription.
- AGENT turn -> ``TTSTextFrame`` (a sentence-aggregated ``AggregatedTextFrame``
  carrying the text the TTS service actually speaks). We AGGREGATE the per-
  sentence ``TTSTextFrame`` chunks and FLUSH them as a single role="agent" turn
  when the bot stops speaking (``BotStoppedSpeakingFrame``). Using ``TTSTextFrame``
  (not per-token ``LLMTextFrame``) gives the bot's real spoken text and avoids
  per-word duplication.

The observer NEVER raises into the pipeline: every emit is wrapped in
try/except, and any missing frame attribute is skipped silently.
"""

from __future__ import annotations

import re
import time

from loguru import logger

# We import these from the real installed pipecat. If a frame type is ever
# missing in some build, the import would fail loudly at module load (the bots
# import this module), which is the intended "fail at wiring time, not runtime"
# behavior — but all three exist in pipecat 1.3.0.
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    TranscriptionFrame,
    TTSTextFrame,
)

# BaseObserver + the on_push_frame event payload (FramePushed) live here. We
# subclass BaseObserver and override the async ``on_push_frame(self, data)``
# coroutine — exactly the interface TranscriptionLogObserver uses.
from pipecat.observers.base_observer import BaseObserver, FramePushed


class TranscriptObserver(BaseObserver):
    """Observer that emits finalized spoken turns as ``transcript`` events.

    Holds an :class:`events.EventBus`. On each finalized user transcription it
    emits ``role="user"``; it aggregates the bot's spoken ``TTSTextFrame`` chunks
    and emits ``role="agent"`` once the bot stops speaking.

    De-duplication: observers see the SAME frame object pushed across every
    processor hop, so we track processed frame ids and skip repeats — each
    utterance emits exactly once (not per processor, not per word).
    """

    def __init__(self, bus) -> None:
        """Initialize the observer.

        Args:
            bus: The per-session ``EventBus`` to emit ``transcript`` events on.
        """
        super().__init__()
        self._bus = bus
        # Frame ids we've already handled (a frame is pushed at every hop).
        self._seen_frame_ids: set[int] = set()
        # Buffer of the bot's spoken sentence chunks for the current utterance.
        self._agent_chunks: list[str] = []
        # Wall-clock of the last finalized user turn, for per-turn V2V latency.
        self._last_user_ts: float | None = None

    async def on_push_frame(self, data: FramePushed):
        """Handle a frame push event; emit transcript events for spoken turns.

        Args:
            data: Frame push event data (source, destination, frame, timestamp).
        """
        try:
            frame = data.frame

            # A given frame object traverses many processors; only act once.
            frame_id = getattr(frame, "id", None)
            if frame_id is not None:
                if frame_id in self._seen_frame_ids:
                    return
                self._seen_frame_ids.add(frame_id)
                # Bound memory on long calls — keep the set from growing forever.
                if len(self._seen_frame_ids) > 4096:
                    self._seen_frame_ids.clear()
                    self._seen_frame_ids.add(frame_id)

            # USER: a finalized STT transcription. Stamp it for V2V latency.
            if isinstance(frame, TranscriptionFrame):
                text = (getattr(frame, "text", "") or "").strip()
                if text:
                    self._last_user_ts = time.time()
                    self._emit("user", text)
                return

            # AGENT: accumulate the per-sentence spoken text. On the FIRST chunk of
            # a new utterance, emit V2V = time from the user's turn to first audio.
            if isinstance(frame, TTSTextFrame):
                text = getattr(frame, "text", "") or ""
                if text:
                    if not self._agent_chunks and self._last_user_ts is not None:
                        ms = int((time.time() - self._last_user_ts) * 1000)
                        if 0 < ms < 30000:
                            self._emit_latency(ms)
                        self._last_user_ts = None
                    self._agent_chunks.append(text)
                return

            # ...and flush it as one turn when the bot stops speaking.
            if isinstance(frame, BotStoppedSpeakingFrame):
                self._flush_agent()
                return
        except Exception:
            # NEVER let the transcript tap raise into the pipeline.
            logger.exception("TranscriptObserver.on_push_frame failed (ignored)")

    def _flush_agent(self) -> None:
        """Emit the buffered bot utterance as a single agent transcript turn."""
        if not self._agent_chunks:
            return
        # TTSTextFrame chunks are word/sentence tokens WITHOUT inter-token spaces,
        # so join with a space, then tidy spacing around punctuation.
        text = " ".join(c.strip() for c in self._agent_chunks if c.strip())
        text = re.sub(r"\s+([.,!?;:'’])", r"\1", text)
        text = re.sub(r"\s{2,}", " ", text).strip()
        self._agent_chunks = []
        if text:
            self._emit("agent", text)

    def _emit_latency(self, ms: int) -> None:
        """Emit one per-turn voice-to-voice latency event (user turn -> first audio)."""
        try:
            self._bus.emit("latency", {"metric": "v2v", "ms": ms})
        except Exception:
            logger.exception("TranscriptObserver latency emit failed (ignored)")

    def _emit(self, role: str, text: str) -> None:
        """Emit one ``transcript`` event, swallowing any sink error."""
        try:
            self._bus.emit("transcript", {"role": role, "text": text, "final": True})
        except Exception:
            # A relay/sink hiccup must never affect the voice path.
            logger.exception("TranscriptObserver emit failed (ignored)")
