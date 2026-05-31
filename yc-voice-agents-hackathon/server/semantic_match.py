"""Semantic incident matching (optimization-plan.md §4 H).

At init, :class:`IncidentMatcher` embeds three short symptom paraphrases per
incident (derived from each ``INCIDENTS`` entry's alert/logs) with
``sentence-transformers`` (``all-MiniLM-L6-v2``). :meth:`IncidentMatcher.match`
embeds a caller utterance and returns the best-matching ``incident_id`` when the
cosine similarity clears 0.85, else ``None``.

``sentence-transformers`` is imported LAZILY inside ``__init__`` so the module
imports cleanly without the (~80MB) dependency installed. :func:`_cosine` is a
pure helper, unit-testable without the model.
"""

from __future__ import annotations

import math
from typing import Sequence

from mock_backend import INCIDENTS

# Match threshold from the plan: only seed a hypothesis above this cosine sim.
MATCH_THRESHOLD = 0.85

# Embedding model — small, local, no API call (§4 H step 1).
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity between two equal-length vectors.

    Pure stdlib so it is unit-testable without the embedding model. Returns
    ``0.0`` if either vector has zero magnitude (undefined direction).
    """
    if len(a) != len(b):
        raise ValueError(f"vector length mismatch: {len(a)} != {len(b)}")
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _paraphrases(incident_id: str, incident: dict) -> list[str]:
    """Derive three short symptom paraphrases from an incident record.

    Pulled from the alert title/service and the first correlated log line so the
    embedded symptoms resemble how a caller would describe the problem.
    """
    alert = incident["alert"]
    service = alert["service"]
    title = alert["title"]
    first_log = incident["logs"][0] if incident.get("logs") else title
    return [
        f"{title} on {service}",
        f"{service} is failing: {first_log}",
        incident["ground_truth_root_cause"],
    ]


class IncidentMatcher:
    """Embed incident symptom paraphrases and match caller utterances to them."""

    def __init__(self, threshold: float = MATCH_THRESHOLD) -> None:
        # Lazy import so the module loads without sentence-transformers.
        from sentence_transformers import SentenceTransformer

        self.threshold = threshold
        self._model = SentenceTransformer(MODEL_NAME)

        # Build (incident_id, paraphrase) pairs, embed once at init.
        self._incident_ids: list[str] = []
        texts: list[str] = []
        for incident_id, incident in INCIDENTS.items():
            for phrase in _paraphrases(incident_id, incident):
                self._incident_ids.append(incident_id)
                texts.append(phrase)

        # Returns a list of python-float vectors we can pass to _cosine.
        embeddings = self._model.encode(texts, normalize_embeddings=False)
        self._vectors: list[list[float]] = [list(map(float, vec)) for vec in embeddings]

    def match(self, utterance: str) -> tuple[str, float] | None:
        """Return ``(incident_id, score)`` for the best match above threshold.

        Embeds ``utterance``, scores it against every stored paraphrase vector,
        and returns the best-scoring incident if its cosine similarity exceeds
        the threshold; otherwise ``None``.
        """
        if not utterance or not utterance.strip():
            return None
        query = list(map(float, self._model.encode(utterance)))

        best_id: str | None = None
        best_score = -1.0
        for incident_id, vector in zip(self._incident_ids, self._vectors):
            score = _cosine(query, vector)
            if score > best_score:
                best_score = score
                best_id = incident_id

        if best_id is not None and best_score > self.threshold:
            return best_id, best_score
        return None


__all__ = ["IncidentMatcher", "_cosine", "MATCH_THRESHOLD", "MODEL_NAME"]
