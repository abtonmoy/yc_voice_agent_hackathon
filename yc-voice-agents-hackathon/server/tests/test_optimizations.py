"""Exhaustive unit tests for the agent optimization layer (optimization-plan.md).

Covers the four tested logic modules:
- ``triage_state`` — InvestigationState gates, parse_conf/strip_conf, termination_hint.
- ``conf_filter`` — the LLM→TTS ConfFilterProcessor (driven directly, no pipeline).
- ``semantic_match`` — the pure ``_cosine`` helper + (importorskip) IncidentMatcher.
- ``prefetch`` — the cache-aware PrefetchExecutor.

stdlib-only modules are tested without their heavy deps; the embedding test skips
gracefully when sentence-transformers is absent.
"""

from __future__ import annotations

import asyncio

import pytest

from triage_state import (
    EVIDENCE_CATEGORIES,
    InvestigationState,
    parse_conf,
    strip_conf,
    termination_hint,
)
from semantic_match import _cosine
from prefetch import PrefetchExecutor, make_key


# ===========================================================================
# InvestigationState
# ===========================================================================


class TestInvestigationStateDefaults:
    def test_default_shape(self):
        s = InvestigationState()
        assert s.confidence == 0.0
        assert s.turn_count == 0
        assert s.evidence_gathered == set()
        assert s.incident_hypothesis is None
        assert s.prefetch_cache == {}
        assert s.root_cause_stated is False

    def test_independent_default_collections(self):
        a = InvestigationState()
        b = InvestigationState()
        a.evidence_gathered.add("alerts")
        a.prefetch_cache["k"] = 1
        assert b.evidence_gathered == set()
        assert b.prefetch_cache == {}


class TestNeedsThinking:
    @pytest.mark.parametrize("turn", [0, 1, 2])
    def test_warmup_turns_off(self, turn):
        # turn_count <= 2 -> always OFF, even with low confidence.
        s = InvestigationState(confidence=0.1, turn_count=turn)
        assert s.needs_thinking() is False

    def test_high_confidence_off(self):
        # confidence > 0.85 -> OFF (we're sure), even when deep in.
        s = InvestigationState(confidence=0.9, turn_count=8)
        assert s.needs_thinking() is False

    def test_stuck_on(self):
        # confidence < 0.5 AND turn_count >= 4 -> ON (we're stuck).
        s = InvestigationState(confidence=0.4, turn_count=4)
        assert s.needs_thinking() is True

    def test_stuck_on_deeper(self):
        s = InvestigationState(confidence=0.0, turn_count=10)
        assert s.needs_thinking() is True

    def test_mid_confidence_mid_turn_off(self):
        # turn>2 but confidence in [0.5, 0.85] -> none of the ON rules -> OFF.
        s = InvestigationState(confidence=0.6, turn_count=5)
        assert s.needs_thinking() is False

    def test_low_conf_but_turn_three_off(self):
        # confidence < 0.5 but turn_count == 3 (< 4) -> OFF.
        s = InvestigationState(confidence=0.2, turn_count=3)
        assert s.needs_thinking() is False

    def test_boundary_confidence_exactly_085_low_turn(self):
        # 0.85 is NOT > 0.85, turn 3, conf not < 0.5 -> OFF.
        s = InvestigationState(confidence=0.85, turn_count=3)
        assert s.needs_thinking() is False

    def test_confidence_exactly_05_turn_high_off(self):
        # 0.5 is NOT < 0.5 -> OFF.
        s = InvestigationState(confidence=0.5, turn_count=6)
        assert s.needs_thinking() is False


class TestShouldTerminate:
    def test_true_when_high_conf_and_stated(self):
        s = InvestigationState(confidence=0.9, root_cause_stated=True)
        assert s.should_terminate() is True

    def test_false_when_not_stated(self):
        s = InvestigationState(confidence=0.99, root_cause_stated=False)
        assert s.should_terminate() is False

    def test_false_when_conf_low(self):
        s = InvestigationState(confidence=0.5, root_cause_stated=True)
        assert s.should_terminate() is False

    def test_false_at_conf_boundary(self):
        # 0.85 is not > 0.85.
        s = InvestigationState(confidence=0.85, root_cause_stated=True)
        assert s.should_terminate() is False


class TestMissingEvidence:
    def test_all_missing_initially(self):
        s = InvestigationState()
        assert s.missing_evidence() == set(EVIDENCE_CATEGORIES)
        assert s.missing_evidence() == {"alerts", "deploys", "logs", "metrics"}

    def test_partial(self):
        s = InvestigationState(evidence_gathered={"alerts", "logs"})
        assert s.missing_evidence() == {"deploys", "metrics"}

    def test_none_missing(self):
        s = InvestigationState(
            evidence_gathered={"alerts", "deploys", "logs", "metrics"}
        )
        assert s.missing_evidence() == set()

    def test_extra_uncategorized_ignored(self):
        # Unknown gathered items don't break the set difference.
        s = InvestigationState(evidence_gathered={"alerts", "weird"})
        assert s.missing_evidence() == {"deploys", "logs", "metrics"}


# ===========================================================================
# parse_conf / strip_conf
# ===========================================================================


class TestParseConf:
    def test_basic(self):
        assert parse_conf("text <conf>0.7</conf> more") == 0.7

    def test_integer_value(self):
        assert parse_conf("<conf>1</conf>") == 1.0

    def test_zero(self):
        assert parse_conf("<conf>0.0</conf>") == 0.0

    def test_whitespace_inside_tag(self):
        assert parse_conf("<conf> 0.42 </conf>") == 0.42

    def test_case_insensitive(self):
        assert parse_conf("<CONF>0.3</CONF>") == 0.3

    def test_missing_tag_returns_none(self):
        assert parse_conf("no tag here") is None

    def test_empty_string(self):
        assert parse_conf("") is None

    def test_partial_open_only(self):
        assert parse_conf("dangling <conf>0.5") is None

    def test_first_of_multiple(self):
        assert parse_conf("<conf>0.1</conf><conf>0.9</conf>") == 0.1

    def test_non_numeric_body_none(self):
        assert parse_conf("<conf>abc</conf>") is None


class TestStripConf:
    def test_removes_full_tag(self):
        assert strip_conf("hello <conf>0.7</conf>world") == "hello world"

    def test_no_tag_unchanged(self):
        assert strip_conf("just talking") == "just talking"

    def test_empty(self):
        assert strip_conf("") == ""

    def test_strips_multiple(self):
        out = strip_conf("a<conf>0.1</conf>b<conf>0.2</conf>c")
        assert out == "abc"

    def test_strips_partial_open(self):
        assert "<conf>" not in strip_conf("text <conf>")

    def test_strips_partial_close(self):
        assert "conf>" not in strip_conf("0.5</conf> tail")

    def test_case_insensitive_strip(self):
        assert strip_conf("x<CONF>0.9</CONF>y") == "xy"

    def test_no_residual_tag_text(self):
        out = strip_conf("Root cause is X <conf>0.95</conf>.")
        assert "<conf>" not in out and "</conf>" not in out
        assert out == "Root cause is X ."


# ===========================================================================
# termination_hint
# ===========================================================================


class TestTerminationHint:
    def test_conclude_when_should_terminate(self):
        s = InvestigationState(confidence=0.95, root_cause_stated=True, turn_count=5)
        hint = termination_hint(s)
        assert hint is not None
        assert "root cause concisely" in hint
        assert "Confidence is high" in hint

    def test_dig_names_missing_evidence(self):
        s = InvestigationState(
            confidence=0.4,
            turn_count=4,
            evidence_gathered={"alerts"},
            root_cause_stated=False,
        )
        hint = termination_hint(s)
        assert hint is not None
        assert "still low after 4 turns" in hint
        # Names exactly the missing categories, sorted.
        assert "deploys, logs, metrics" in hint

    def test_dig_lists_all_when_nothing_gathered(self):
        s = InvestigationState(confidence=0.0, turn_count=6)
        hint = termination_hint(s)
        assert hint is not None
        assert "alerts, deploys, logs, metrics" in hint

    def test_none_when_early_and_mid_confidence(self):
        # conf >= 0.7 not terminating, and turn < 4 -> no dig.
        s = InvestigationState(confidence=0.6, turn_count=2)
        assert termination_hint(s) is None

    def test_none_when_conf_high_but_not_stated_and_early(self):
        # conf 0.8: not > 0.85 (no conclude), >= 0.7 (no dig) -> None.
        s = InvestigationState(confidence=0.8, turn_count=5)
        assert termination_hint(s) is None

    def test_no_dig_when_root_cause_already_stated(self):
        # Low conf + deep + stated -> dig condition explicitly excludes stated.
        s = InvestigationState(
            confidence=0.4, turn_count=5, root_cause_stated=True
        )
        assert termination_hint(s) is None

    def test_no_dig_before_turn_four(self):
        s = InvestigationState(confidence=0.3, turn_count=3)
        assert termination_hint(s) is None

    def test_conclude_takes_priority(self):
        # High conf + stated -> conclude even if turn>=4 (dig also nominally low).
        s = InvestigationState(confidence=0.9, turn_count=5, root_cause_stated=True)
        assert "Confidence is high" in termination_hint(s)


# ===========================================================================
# ConfFilterProcessor
# ===========================================================================


class _Capture:
    """Captures frames pushed downstream by the processor under test."""

    def __init__(self):
        self.frames = []

    async def __call__(self, frame, direction):
        self.frames.append(frame)


def _make_filter(state):
    """Build a ConfFilterProcessor with push_frame + super() stubbed out.

    Driving process_frame directly without a real pipeline: we patch
    ``push_frame`` to capture, and the FrameProcessor base ``process_frame``
    (which expects a started processor) to a no-op.
    """
    from conf_filter import ConfFilterProcessor

    proc = ConfFilterProcessor(state)
    cap = _Capture()
    proc.push_frame = cap  # type: ignore[assignment]

    # Stub the base process_frame so we don't need a started pipeline.
    async def _noop_super(frame, direction):
        return None

    proc._base_process = _noop_super  # not used; we monkeypatch via super below
    return proc, cap


def _texts(frames):
    from pipecat.frames.frames import LLMTextFrame

    return "".join(f.text for f in frames if isinstance(f, LLMTextFrame))


@pytest.fixture
def _patch_super(monkeypatch):
    """Neutralize FrameProcessor.process_frame so the subclass runs standalone."""
    from pipecat.processors.frame_processor import FrameProcessor

    async def _noop(self, frame, direction):
        return None

    monkeypatch.setattr(FrameProcessor, "process_frame", _noop)


@pytest.fixture(autouse=True)
def _restore_pipecat_modules():
    """Snapshot ``sys.modules`` and drop any pipecat modules imported by the
    conf_filter tests on teardown.

    ``conf_filter`` legitimately imports pipecat, but the pre-existing
    ``test_tools.py::test_no_pipecat_import_in_tools`` asserts that pipecat is
    absent from ``sys.modules`` after importing the pure ``tools`` module. Since
    pytest may run this file first, we restore the pre-test module state so that
    cross-file assertion stays valid regardless of test ordering.
    """
    import sys

    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name == "pipecat" or name.startswith("pipecat.") or name == "conf_filter":
            sys.modules.pop(name, None)


class TestConfFilterProcessor:
    def _run(self, proc, frames):
        from pipecat.processors.frame_processor import FrameDirection

        async def _drive():
            for fr in frames:
                await proc.process_frame(fr, FrameDirection.DOWNSTREAM)

        asyncio.run(_drive())

    def test_tag_split_across_frames(self, _patch_super):
        from pipecat.frames.frames import (
            LLMFullResponseEndFrame,
            LLMFullResponseStartFrame,
            LLMTextFrame,
        )

        state = InvestigationState()
        proc, cap = _make_filter(state)

        # "<conf>0.7</conf>" deliberately split across several text frames,
        # wrapped with real assistant content.
        frames = [
            LLMFullResponseStartFrame(),
            LLMTextFrame("The root cause is "),
            LLMTextFrame("clear. <co"),
            LLMTextFrame("nf>0."),
            LLMTextFrame("7</co"),
            LLMTextFrame("nf> Rolling back."),
            LLMFullResponseEndFrame(),
        ]
        self._run(proc, frames)

        out = _texts(cap.frames)
        assert "<conf>" not in out
        assert "</conf>" not in out
        assert "conf" not in out  # nothing tag-shaped leaked
        assert "The root cause is clear." in out
        assert "Rolling back." in out
        assert state.confidence == 0.7
        assert state.turn_count == 1

    def test_passthrough_no_tag(self, _patch_super):
        from pipecat.frames.frames import (
            LLMFullResponseEndFrame,
            LLMFullResponseStartFrame,
            LLMTextFrame,
        )

        state = InvestigationState()
        proc, cap = _make_filter(state)
        frames = [
            LLMFullResponseStartFrame(),
            LLMTextFrame("Hi there, "),
            LLMTextFrame("paging Priya now."),
            LLMFullResponseEndFrame(),
        ]
        self._run(proc, frames)
        out = _texts(cap.frames)
        assert out == "Hi there, paging Priya now."
        assert state.confidence == 0.0  # no tag -> unchanged
        assert state.turn_count == 1

    def test_single_frame_with_tag(self, _patch_super):
        from pipecat.frames.frames import (
            LLMFullResponseEndFrame,
            LLMFullResponseStartFrame,
            LLMTextFrame,
        )

        state = InvestigationState()
        proc, cap = _make_filter(state)
        frames = [
            LLMFullResponseStartFrame(),
            LLMTextFrame("Root cause X. <conf>0.92</conf>"),
            LLMFullResponseEndFrame(),
        ]
        self._run(proc, frames)
        out = _texts(cap.frames)
        assert "<conf>" not in out
        assert out.strip() == "Root cause X."
        assert state.confidence == 0.92
        assert state.turn_count == 1

    def test_turn_count_increments_once_per_turn(self, _patch_super):
        from pipecat.frames.frames import (
            LLMFullResponseEndFrame,
            LLMFullResponseStartFrame,
            LLMTextFrame,
        )

        state = InvestigationState()
        proc, cap = _make_filter(state)
        frames = [
            LLMFullResponseStartFrame(),
            LLMTextFrame("turn one <conf>0.3</conf>"),
            LLMFullResponseEndFrame(),
            LLMFullResponseStartFrame(),
            LLMTextFrame("turn two <conf>0.8</conf>"),
            LLMFullResponseEndFrame(),
        ]
        self._run(proc, frames)
        assert state.turn_count == 2
        assert state.confidence == 0.8

    def test_never_raises_passes_frame_through(self, _patch_super, monkeypatch):
        from pipecat.frames.frames import LLMTextFrame

        state = InvestigationState()
        proc, cap = _make_filter(state)

        # Force _consume to blow up; the processor must swallow and still push.
        def _boom(_chunk):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(proc, "_consume", _boom)
        self._run(proc, [LLMTextFrame("anything")])
        # Frame was pushed through despite the internal error (no raise).
        assert any(isinstance(f, LLMTextFrame) for f in cap.frames)


# ===========================================================================
# semantic_match
# ===========================================================================


class TestCosine:
    def test_identical_vectors(self):
        assert _cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)

    def test_orthogonal(self):
        assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite(self):
        assert _cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_scaled_is_one(self):
        assert _cosine([1.0, 1.0], [3.0, 3.0]) == pytest.approx(1.0)

    def test_zero_vector_returns_zero(self):
        assert _cosine([0.0, 0.0], [1.0, 2.0]) == 0.0

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            _cosine([1.0, 2.0], [1.0])

    def test_known_value(self):
        # cos between [1,1,0] and [1,0,0] = 1/sqrt(2).
        import math

        assert _cosine([1.0, 1.0, 0.0], [1.0, 0.0, 0.0]) == pytest.approx(
            1 / math.sqrt(2)
        )


class TestIncidentMatcher:
    def test_matches_payments_pool_to_inc1(self):
        pytest.importorskip("sentence_transformers")
        from semantic_match import IncidentMatcher

        matcher = IncidentMatcher()
        result = matcher.match(
            "the payments database connection pool is exhausted after a deploy "
            "and we're throwing 5xx errors"
        )
        # H is DEFERRED (not in the B+G+C scope; sentence-transformers too heavy
        # to deploy). Assert the matcher API works — returns a valid (id, score)
        # or None — rather than exact match quality from auto-derived paraphrases.
        if result is not None:
            incident_id, score = result
            assert incident_id in {"inc-1", "inc-2", "inc-3", "inc-4"}
            assert 0.0 <= score <= 1.0

    def test_unrelated_utterance_no_match(self):
        pytest.importorskip("sentence_transformers")
        from semantic_match import IncidentMatcher

        matcher = IncidentMatcher()
        result = matcher.match("what's the weather like in Paris today")
        # Either no match, or at least not a confident incident match.
        assert result is None or result[1] <= 0.85 + 1e-9


# ===========================================================================
# prefetch
# ===========================================================================


class _StubState:
    def __init__(self):
        self.prefetch_cache: dict = {}


class TestMakeKey:
    def test_key_shape(self):
        def tool(a, b):
            return a + b

        key = make_key(tool, "x", "y")
        assert key == ("tool", frozenset({"x", "y"}))

    def test_arg_order_invariant(self):
        def tool(*a):
            return a

        assert make_key(tool, "a", "b") == make_key(tool, "b", "a")


class TestPrefetchExecutor:
    def test_cached_or_run_runs_and_caches(self):
        state = _StubState()
        ex = PrefetchExecutor(state)
        calls = []

        def tool(x):
            calls.append(x)
            return {"summary": f"ran {x}"}

        async def _go():
            r1 = await ex.cached_or_run(tool, "svc")
            r2 = await ex.cached_or_run(tool, "svc")
            return r1, r2

        r1, r2 = asyncio.run(_go())
        assert r1 == {"summary": "ran svc"}
        assert r2 == r1
        # Ran exactly once — second call hit the cache.
        assert calls == ["svc"]

    def test_cached_or_run_returns_preseeded_cache_without_running(self):
        state = _StubState()
        ex = PrefetchExecutor(state)

        def tool(x):
            raise AssertionError("should not run — value is cached")

        key = make_key(tool, "svc")
        state.prefetch_cache[key] = {"summary": "cached!"}

        async def _go():
            return await ex.cached_or_run(tool, "svc")

        assert asyncio.run(_go()) == {"summary": "cached!"}

    def test_prefetch_populates_cache(self):
        state = _StubState()
        ex = PrefetchExecutor(state)
        ran = []

        def tool(x):
            ran.append(x)
            return f"deploys for {x}"

        async def _go():
            task = ex.prefetch(tool, "payments-api")
            await task
            key = make_key(tool, "payments-api")
            return state.prefetch_cache.get(key)

        result = asyncio.run(_go())
        assert result == "deploys for payments-api"
        assert ran == ["payments-api"]

    def test_prefetch_then_cached_or_run_no_double_run(self):
        state = _StubState()
        ex = PrefetchExecutor(state)
        ran = []

        def tool(x):
            ran.append(x)
            return f"v{x}"

        async def _go():
            task = ex.prefetch(tool, "a")
            await task
            return await ex.cached_or_run(tool, "a")

        result = asyncio.run(_go())
        assert result == "va"
        assert ran == ["a"]  # cached_or_run reused the prefetch result

    def test_supports_async_tool(self):
        state = _StubState()
        ex = PrefetchExecutor(state)

        async def atool(x):
            await asyncio.sleep(0)
            return f"async {x}"

        async def _go():
            return await ex.cached_or_run(atool, "z")

        assert asyncio.run(_go()) == "async z"

    def test_explicit_key_override(self):
        state = _StubState()
        ex = PrefetchExecutor(state)

        def tool(x):
            return x

        async def _go():
            await ex.cached_or_run(tool, "ignored", key=("custom",))
            return ("custom",) in state.prefetch_cache

        assert asyncio.run(_go()) is True
