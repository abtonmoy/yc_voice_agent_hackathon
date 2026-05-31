"""Prompt stability lint (optimization-plan.md §4 B / §8.1).

The prefix-cache win in B requires the system prompt to be a byte-identical
static constant — no ``date.today()``, no ``DEMO_NOW``, no clock time spliced
into the prefix. These tests assert TRIAGE_SYSTEM_INSTRUCTION carries no obvious
dynamic tokens and is the same object/value across repeated imports.

Cekura can't see this directly (§8.1 out-of-band note), so it lives in unit tests.
"""

from __future__ import annotations

import re
import importlib

import pytest


@pytest.fixture(autouse=True)
def _restore_pipecat_modules():
    """Drop pipecat / triage modules imported here on teardown.

    ``triage`` imports pipecat; the pre-existing
    ``test_tools.py::test_no_pipecat_import_in_tools`` asserts pipecat is absent
    from ``sys.modules`` after importing the pure ``tools`` module. Restore the
    pre-test module state so that assertion holds regardless of test ordering.
    """
    import sys

    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name == "pipecat" or name.startswith("pipecat.") or name == "triage":
            sys.modules.pop(name, None)


def _instruction() -> str:
    from triage import TRIAGE_SYSTEM_INSTRUCTION

    return TRIAGE_SYSTEM_INSTRUCTION


def test_is_a_string():
    assert isinstance(_instruction(), str)
    assert len(_instruction()) > 0


def test_no_four_digit_year():
    # A literal year (e.g. 2026) in the prefix is a tell-tale dynamic insertion.
    assert re.search(r"\b\d{4}\b", _instruction()) is None


def test_no_demo_now_token():
    assert "DEMO_NOW" not in _instruction()


def test_no_clock_time_pattern():
    # HH:MM style timestamps would mean a live clock leaked into the prefix.
    assert re.search(r"\b\d{1,2}:\d{2}\b", _instruction()) is None


def test_no_iso_date_pattern():
    assert re.search(r"\d{4}-\d{2}-\d{2}", _instruction()) is None


def test_stable_across_three_imports():
    # Re-import the module three times; the constant must be byte-identical.
    values = []
    for _ in range(3):
        mod = importlib.import_module("triage")
        importlib.reload(mod)
        values.append(mod.TRIAGE_SYSTEM_INSTRUCTION)
    assert values[0] == values[1] == values[2]


def test_identical_to_itself_repeatedly():
    # Calling the accessor multiple times yields equal strings (no per-call work).
    assert _instruction() == _instruction() == _instruction()
