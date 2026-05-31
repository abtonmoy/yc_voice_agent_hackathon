"""Pipecat ``FrameProcessor`` that strips the ``<conf>`` keystone tag from the
LLM→TTS text stream (optimization-plan.md §4 G / §5).

Sits between the LLM service and the TTS service. As ``LLMTextFrame`` tokens
stream through, it buffers them, extracts any ``<conf>0.7</conf>`` value into
``state.confidence`` (incrementing ``state.turn_count`` once per assistant turn),
and removes the tag — including the case where the tag is split across several
frames — so the metadata never reaches TTS.

This module is bot-only: it imports pipecat. ``triage_state`` (the pure parsing
logic) stays import-clean without pipecat.
"""

from __future__ import annotations

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from triage_state import InvestigationState, parse_conf, strip_conf

# The longest opening prefix we must hold back at a frame boundary so a tag split
# across frames is never emitted half-formed. "<conf>" is 6 chars; we also guard
# the closing "</conf>" (7). Hold back the longest tag token length.
_MAX_TAG_TAIL = len("</conf>")


class ConfFilterProcessor(FrameProcessor):
    """Strip ``<conf>...</conf>`` from streamed LLM text before it reaches TTS.

    Robust to the tag spanning multiple frames: text is buffered and only the
    portion that cannot be the start of a (possibly future) tag is pushed
    downstream. The remaining tail is carried to the next frame. On the LLM
    response boundary the buffer is flushed and ``turn_count`` is incremented
    exactly once for the assistant turn.
    """

    def __init__(self, state: InvestigationState, bus=None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._state = state
        # Optional EventBus: when set, each parsed confidence value is emitted as
        # a best-effort "hypothesis" event so the dashboard trace shows it. The
        # bus is optional (default None) so the existing tests stay green.
        self._bus = bus
        # Text accumulated for the CURRENT assistant turn (tag may still be
        # forming). We only release the safe prefix downstream.
        self._buffer: str = ""
        # All raw text seen this turn — confidence is parsed from the full turn
        # text so a tag split across the emit/hold boundary is still captured.
        self._seen: str = ""
        # Guards once-per-turn turn_count increment between start/end markers.
        self._turn_open: bool = False

    # -- helpers -------------------------------------------------------------

    def _apply_conf(self, value: float | None) -> None:
        """Write a newly-parsed confidence value and emit it (best-effort).

        Only updates / emits when ``value`` is not None and differs from the
        last value already on the state, so the dashboard sees one event per new
        confidence reading. The emit is wrapped so a bus failure never breaks the
        text stream.
        """
        if value is None or value == self._state.confidence:
            return
        self._state.confidence = value
        if self._bus is not None:
            try:
                self._bus.emit(
                    "hypothesis", {"text": "confidence", "confidence": value}
                )
            except Exception:  # never let a bus error reach the pipeline
                pass

    def _safe_split(self, buffer: str) -> tuple[str, str]:
        """Split ``buffer`` into (emit_now, hold_tail).

        ``hold_tail`` is the shortest suffix that could still be the beginning of
        a ``<conf>`` / ``</conf>`` tag, so we never emit a partial tag. If there
        is no such suffix the whole buffer is safe to emit.
        """
        # If a full tag is present we can let the caller strip + emit everything
        # up to and including it; only an UNCLOSED trailing tag needs holding.
        # Find the last '<' that has no '>' after it — that's a possible partial.
        idx = buffer.rfind("<")
        if idx == -1:
            return buffer, ""
        if ">" in buffer[idx:]:
            # The last '<' is closed; nothing tag-shaped is dangling.
            return buffer, ""
        # buffer[idx:] is an unclosed '<...'. Only hold it if it could grow into
        # one of our tags; otherwise it is ordinary text (e.g. "a < b").
        dangling = buffer[idx:]
        if _could_be_tag_prefix(dangling):
            return buffer[:idx], dangling
        return buffer, ""

    def _consume(self, chunk: str) -> str:
        """Add ``chunk`` to the buffer; return text safe to push downstream now.

        Updates ``state.confidence`` whenever a complete tag has been seen.
        """
        self._buffer += chunk
        self._seen += chunk
        # Confidence is parsed from the full turn text (the tag may straddle the
        # emit/hold split); "no tag yet" leaves the last known value in place.
        self._apply_conf(parse_conf(self._seen))

        emit_now, hold = self._safe_split(self._buffer)
        self._buffer = hold
        # Strip any complete-or-partial tag fragments before releasing to TTS.
        return strip_conf(emit_now)

    def _flush(self) -> str:
        """Flush the held buffer at the turn boundary, returning text for TTS."""
        remaining = self._buffer
        self._buffer = ""
        self._apply_conf(parse_conf(self._seen))
        self._seen = ""
        return strip_conf(remaining)

    # -- frame processing ----------------------------------------------------

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        try:
            await super().process_frame(frame, direction)

            if isinstance(frame, LLMFullResponseStartFrame):
                # New assistant turn: count it once, reset the buffers.
                self._buffer = ""
                self._seen = ""
                if not self._turn_open:
                    self._turn_open = True
                    self._state.turn_count += 1
                await self.push_frame(frame, direction)
                return

            if isinstance(frame, LLMTextFrame):
                cleaned = self._consume(frame.text)
                if cleaned:
                    await self.push_frame(LLMTextFrame(cleaned), direction)
                # Suppress empty frames (the tag was entirely consumed).
                return

            if isinstance(frame, LLMFullResponseEndFrame):
                cleaned = self._flush()
                if cleaned:
                    await self.push_frame(LLMTextFrame(cleaned), direction)
                self._turn_open = False
                await self.push_frame(frame, direction)
                return

            # Everything else passes through untouched.
            await self.push_frame(frame, direction)
        except Exception as exc:  # never raise into the pipeline
            logger.error(f"ConfFilterProcessor swallowed error: {exc!r}")
            try:
                await self.push_frame(frame, direction)
            except Exception:
                pass


def _could_be_tag_prefix(dangling: str) -> bool:
    """True if an unclosed ``<...`` could still grow into ``<conf>``/``</conf>``.

    ``dangling`` starts with ``<`` and contains no ``>``. We keep it buffered
    only when it is a prefix of one of our tag openings (case-insensitive),
    capped at the longest tag-token length so we never hold unbounded text.
    """
    if len(dangling) > _MAX_TAG_TAIL:
        return False
    low = dangling.lower()
    return "<conf>".startswith(low) or "</conf>".startswith(low)


__all__ = ["ConfFilterProcessor"]
