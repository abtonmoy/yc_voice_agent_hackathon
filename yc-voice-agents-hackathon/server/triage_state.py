"""Shared per-call investigation state + ``<conf>`` parsing for the agent
optimization layer (optimization-plan.md §3, §4 G, §5).

Pure standard-library only — no pipecat, no third-party imports — so this module
imports cleanly anywhere and is fully unit-testable. The Pipecat bots instantiate
:class:`InvestigationState` once per call and the layered optimizations (A/E/G/H)
read and write it.

The ``<conf>`` keystone (§5): the model emits one ``<conf>0.0</conf>`` line per
turn; :func:`parse_conf` lifts the value into ``state.confidence`` and
:func:`strip_conf` removes the tag so it never reaches TTS. :func:`termination_hint`
turns the state into the CONCLUDE / DIG system-role nudges of §4 G step 4.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# The four diagnostic evidence categories tracked across the investigation.
EVIDENCE_CATEGORIES: frozenset[str] = frozenset({"alerts", "deploys", "logs", "metrics"})


@dataclass
class InvestigationState:
    """Per-call mutable scoreboard shared across optimizations A/E/G/H.

    Shape is fixed by optimization-plan.md §3. ``confidence`` is the model's own
    certainty in the current root-cause hypothesis (written by the ``<conf>``
    parser), ``turn_count`` increments once per assistant turn, and
    ``evidence_gathered`` accumulates the tool categories that have been called.
    """

    confidence: float = 0.0
    turn_count: int = 0
    evidence_gathered: set[str] = field(default_factory=set)
    incident_hypothesis: str | None = None
    prefetch_cache: dict[tuple, object] = field(default_factory=dict)
    root_cause_stated: bool = False

    def needs_thinking(self) -> bool:
        """Decide whether to enable thinking for the NEXT turn (§3 / §4 A).

        Warm-up turns (<=2) and high-confidence turns (>0.85) skip thinking;
        a stuck investigation (confidence <0.5 and at least 4 turns deep) turns
        it on. Everything else stays off.
        """
        if self.turn_count <= 2:
            return False
        if self.confidence > 0.85:
            return False
        if self.confidence < 0.5 and self.turn_count >= 4:
            return True
        return False

    def should_terminate(self) -> bool:
        """True once confidence is high AND a root cause has been stated (§3)."""
        return self.confidence > 0.85 and self.root_cause_stated

    def missing_evidence(self) -> set[str]:
        """The evidence categories not yet gathered (§3)."""
        return set(EVIDENCE_CATEGORIES) - self.evidence_gathered


# ---------------------------------------------------------------------------
# The <conf> keystone (§5): parse / strip the per-turn confidence tag.
# ---------------------------------------------------------------------------

# Matches a full <conf>0.7</conf> tag, capturing the numeric body. Tolerant of
# surrounding whitespace inside the tag.
_CONF_RE = re.compile(r"<conf>\s*([0-9.]+)\s*</conf>", re.IGNORECASE)

# Removes any full <conf>...</conf> tag (non-greedy body, any content) as well
# as stray opening/closing partials that survive a split stream.
_CONF_STRIP_FULL_RE = re.compile(r"<conf>.*?</conf>", re.IGNORECASE | re.DOTALL)
_CONF_STRIP_PARTIAL_RE = re.compile(r"</?conf>", re.IGNORECASE)


def parse_conf(text: str) -> float | None:
    """Extract the ``<conf>`` value from ``text``.

    Returns the float (e.g. ``0.7``) of the FIRST well-formed tag, or ``None``
    when no tag is present or the body is not a parseable float ("no update,
    last known value persists" per §4 G).
    """
    if not text:
        return None
    match = _CONF_RE.search(text)
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def strip_conf(text: str) -> str:
    """Remove every ``<conf>...</conf>`` tag (and stray partial tags) from ``text``.

    Full tags are removed first; any lingering bare ``<conf>`` / ``</conf>``
    partials (from a tag split across a stream boundary) are then scrubbed so
    nothing tag-shaped reaches TTS.
    """
    if not text:
        return text
    cleaned = _CONF_STRIP_FULL_RE.sub("", text)
    cleaned = _CONF_STRIP_PARTIAL_RE.sub("", cleaned)
    return cleaned


# ---------------------------------------------------------------------------
# Termination controller hint (§4 G step 4).
# ---------------------------------------------------------------------------

_CONCLUDE_MSG = (
    "Confidence is high. State the root cause concisely and propose remediation, "
    "then end."
)


def _dig_msg(state: InvestigationState) -> str:
    missing = ", ".join(sorted(state.missing_evidence()))
    return (
        f"Confidence still low after {state.turn_count} turns. You haven't "
        f"checked: {missing}. Investigate at least one before concluding."
    )


def termination_hint(state: InvestigationState) -> str | None:
    """Return the system-role nudge to append this turn, or ``None`` (§4 G step 4).

    - ``should_terminate()`` → the CONCLUDE message.
    - else if confidence < 0.7 and turn_count >= 4 and not root_cause_stated →
      the DIG message naming the missing evidence categories.
    - else ``None``.
    """
    if state.should_terminate():
        return _CONCLUDE_MSG
    if state.confidence < 0.7 and state.turn_count >= 4 and not state.root_cause_stated:
        return _dig_msg(state)
    return None


__all__ = [
    "InvestigationState",
    "EVIDENCE_CATEGORIES",
    "parse_conf",
    "strip_conf",
    "termination_hint",
]
