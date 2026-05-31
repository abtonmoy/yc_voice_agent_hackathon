"""Wiring tests for optimization C (pre-cached TTS opener).

Covers the parts that don't need a live Gradium socket (that path is exercised
manually / on a real call):

- ``triage`` exposes ``OPT_C`` and ``build_triage`` returns a ``kickoff_opened``
  variant that drops the "greet the engineer" directive (because the cached opener
  already spoke it) while keeping every incident FACT byte-identical to ``kickoff``.
- ``tts_cache.opener_frames`` slices PCM into correctly-sized ``OutputAudioRawFrame``s.
"""

from __future__ import annotations

import importlib
import sys

import pytest


@pytest.fixture(autouse=True)
def _restore_pipecat_modules():
    """Scrub pipecat-importing modules so the `tools` no-pipecat guard holds
    regardless of test ordering (mirrors test_opt_g_wiring.py)."""
    yield
    for name in list(sys.modules):
        if name == "pipecat" or name.startswith("pipecat.") or name in ("triage", "conf_filter", "tts_cache"):
            sys.modules.pop(name, None)


class _FakeLLM:
    def register_direct_function(self, fn):
        pass


def _build():
    import triage

    mod = importlib.reload(triage)
    return mod, mod.build_triage("test-session", _FakeLLM())


class TestOptCToggle:
    def test_opt_c_defaults_off(self, monkeypatch):
        monkeypatch.delenv("OPT_C", raising=False)
        import triage

        mod = importlib.reload(triage)
        assert mod.OPT_C is False

    def test_opt_c_on_when_set(self, monkeypatch):
        monkeypatch.setenv("OPT_C", "1")
        import triage

        mod = importlib.reload(triage)
        assert mod.OPT_C is True
        monkeypatch.delenv("OPT_C", raising=False)
        importlib.reload(triage)


class TestKickoffOpenedVariant:
    def test_build_returns_both_kickoffs(self):
        _, t = _build()
        assert "kickoff" in t and "kickoff_opened" in t
        assert t["kickoff_opened"] != t["kickoff"]

    def test_opened_does_not_re_greet(self):
        _, t = _build()
        opened = t["kickoff_opened"]
        # The "open the call now / greet" directive is gone...
        assert "Open the call NOW" not in opened
        # ...replaced by an explicit don't-re-greet instruction referencing the
        # already-spoken opener.
        assert "ALREADY spoken your opening line" in opened
        assert "do NOT greet" in opened

    def test_opened_keeps_all_incident_facts(self):
        """Every fact line in the normal kickoff must survive verbatim in the
        opened variant — only the opening directive differs."""
        mod, t = _build()
        kickoff, opened = t["kickoff"], t["kickoff_opened"]
        # The fact block is everything up to the opening directive sentence.
        facts = kickoff.split("Open the call NOW")[0]
        assert facts in opened
        # Safety-critical clause is preserved.
        assert "apply it only after they say yes" in opened

    def test_opener_text_embedded(self):
        mod, t = _build()
        assert mod.GREETING_PROMPT in t["kickoff_opened"]


class TestOpenerFrames:
    def test_chunks_pcm_into_frames(self):
        from tts_cache import GRADIUM_SAMPLE_RATE, opener_frames

        # 100 ms of 48 kHz 16-bit mono = 0.1 * 48000 * 2 = 9600 bytes.
        pcm = b"\x01\x02" * (GRADIUM_SAMPLE_RATE // 10)
        frames = opener_frames(pcm, sample_rate=GRADIUM_SAMPLE_RATE, chunk_ms=20)
        # 20 ms frame = 960 samples * 2 bytes = 1920 bytes -> 5 frames for 100 ms.
        assert len(frames) == 5
        assert all(f.sample_rate == GRADIUM_SAMPLE_RATE for f in frames)
        assert all(f.num_channels == 1 for f in frames)
        assert sum(len(f.audio) for f in frames) == len(pcm)

    def test_handles_ragged_tail(self):
        from tts_cache import opener_frames

        pcm = b"\x00" * 2001  # not a clean multiple of the frame size
        frames = opener_frames(pcm, sample_rate=48000, chunk_ms=20)
        assert sum(len(f.audio) for f in frames) == 2001

    def test_empty_pcm_yields_no_frames(self):
        from tts_cache import opener_frames

        assert opener_frames(b"") == []
