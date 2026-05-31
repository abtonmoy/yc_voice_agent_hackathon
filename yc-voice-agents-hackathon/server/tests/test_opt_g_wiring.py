"""Wiring tests for optimization G (confidence + termination controller).

These verify the *integration* of the already-tested G logic modules into the
live triage module — NOT the logic itself (that's `test_optimizations.py`):

- ``triage.system_instruction()`` appends the ``<conf>`` instruction only when
  the ``OPT_G`` toggle is on, and is byte-identical to ``TRIAGE_SYSTEM_INSTRUCTION``
  when off.
- ``build_triage`` returns a ``loop`` of the optimization ``InvestigationState``
  (aliased ``LoopState``), and each diagnostic tool wrapper records its evidence
  category onto ``loop.evidence_gathered`` when invoked.

Runs under a real pipecat import (triage.py imports pipecat types).
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

from triage_state import InvestigationState as LoopState


@pytest.fixture(autouse=True)
def _restore_pipecat_modules():
    """Drop the pipecat modules this file pulls in (via ``triage``) on teardown.

    ``triage`` legitimately imports pipecat, but ``test_tools.py::
    test_no_pipecat_import_in_tools`` asserts pipecat is absent from
    ``sys.modules`` after importing the pure ``tools`` module. Scrub pipecat (and
    the modules that import it) so that cross-file assertion holds regardless of
    test ordering (mirrors the same guard in ``test_optimizations.py``).
    """
    import sys

    yield
    for name in list(sys.modules):
        if (
            name == "pipecat"
            or name.startswith("pipecat.")
            or name in ("triage", "conf_filter")
        ):
            sys.modules.pop(name, None)


def _triage():
    """Import (or reload) the triage module on demand — kept out of module scope
    so the teardown fixture can fully scrub it between files."""
    import triage

    return importlib.reload(triage)


# ---------------------------------------------------------------------------
# system_instruction() toggle
# ---------------------------------------------------------------------------


class _FakeLLM:
    """Minimal stand-in: build_triage only needs register_direct_function."""

    def __init__(self):
        self.registered = []

    def register_direct_function(self, fn):
        self.registered.append(fn)


def _reload_triage(monkeypatch, opt_g: str | None):
    """Reload the triage module with OPT_G set/unset so the module-level
    ``OPT_G`` constant is recomputed."""
    import triage
    if opt_g is None:
        monkeypatch.delenv("OPT_G", raising=False)
    else:
        monkeypatch.setenv("OPT_G", opt_g)
    return importlib.reload(triage)


class TestSystemInstructionToggle:
    def test_off_equals_bare_constant(self, monkeypatch):
        mod = _reload_triage(monkeypatch, None)
        try:
            assert mod.OPT_G is False
            assert mod.system_instruction() == mod.TRIAGE_SYSTEM_INSTRUCTION
            assert "<conf>" not in mod.system_instruction()
        finally:
            _reload_triage(monkeypatch, None)

    def test_on_appends_conf_instruction(self, monkeypatch):
        mod = _reload_triage(monkeypatch, "1")
        try:
            assert mod.OPT_G is True
            si = mod.system_instruction()
            assert "<conf>" in si
            assert si.startswith(mod.TRIAGE_SYSTEM_INSTRUCTION)
            assert si.endswith(mod.CONF_INSTRUCTION)
        finally:
            _reload_triage(monkeypatch, None)

    def test_constant_unchanged_by_toggle(self, monkeypatch):
        off = _reload_triage(monkeypatch, None).TRIAGE_SYSTEM_INSTRUCTION
        on = _reload_triage(monkeypatch, "1").TRIAGE_SYSTEM_INSTRUCTION
        _reload_triage(monkeypatch, None)
        # The exported constant is identical regardless of the toggle.
        assert off == on
        assert "<conf>" not in off


# ---------------------------------------------------------------------------
# build_triage loop + evidence tracking
# ---------------------------------------------------------------------------


class _FakeParams:
    """Fake FunctionCallParams whose result_callback is an async no-op."""

    async def result_callback(self, *args, **kwargs):
        return None


def _build():
    import triage
    mod = importlib.reload(triage)
    llm = _FakeLLM()
    return mod, mod.build_triage("test-session", llm)


def _wrapper(triage_dict, name):
    for fn in triage_dict["tool_functions"]:
        if fn.__name__ == name:
            return fn
    raise AssertionError(f"wrapper {name!r} not found")


class TestBuildTriageLoop:
    def test_returns_loop_of_loopstate(self):
        _, t = _build()
        assert isinstance(t["loop"], LoopState)
        assert t["loop"].evidence_gathered == set()

    @pytest.mark.parametrize(
        "wrapper_name,category",
        [
            ("get_alerts", "alerts"),
            ("get_deploy_history", "deploys"),
            ("get_logs", "logs"),
            ("get_metrics", "metrics"),
        ],
    )
    def test_wrapper_records_evidence(self, wrapper_name, category):
        _, t = _build()
        loop = t["loop"]
        fn = _wrapper(t, wrapper_name)

        asyncio.run(fn(_FakeParams()))

        assert category in loop.evidence_gathered

    def test_all_four_accumulate(self):
        _, t = _build()
        loop = t["loop"]
        for name in ("get_alerts", "get_deploy_history", "get_logs", "get_metrics"):
            asyncio.run(_wrapper(t, name)(_FakeParams()))
        assert loop.evidence_gathered == {"alerts", "deploys", "logs", "metrics"}
