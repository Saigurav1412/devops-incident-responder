"""Long-term incident memory: the interface and a local in-memory implementation.

WHAT: `IncidentMemory` is the Protocol from INTERFACES §3 (save / find_similar).
      `InMemoryIncidentMemory` implements it in a Python list, for tests and local runs.
WHY an in-memory version first: the Firestore + vector-search store needs GCP, which is on hold.
      The graph only depends on the Protocol, so swapping in the Firestore store later is a
      one-line change where the deps are built, not a graph change.

The two rules from INTERFACES §3 are enforced HERE, in code, so every implementation must keep them:
  * Write rule: only verified fixes are stored (`save` refuses `verified=False`). An incident
    that was never confirmed healthy must not teach future incidents anything.
  * Read rule: recalled records are hypotheses for the planner, never actions to run. That part
    is enforced by the graph (a recalled fix still goes through propose -> approval -> execute).
"""

from __future__ import annotations

import re
from typing import Protocol

from contracts import IncidentRecord


class UnverifiedRecordError(ValueError):
    """Raised when something tries to store an incident whose fix was never verified."""


class IncidentMemory(Protocol):
    """INTERFACES §3. Implementations must refuse unverified records in `save`."""

    def save(self, record: IncidentRecord) -> None: ...

    def find_similar(self, symptoms: str, k: int = 3) -> list[IncidentRecord]: ...


_WORD = re.compile(r"[a-z0-9]+")


def _words(text: str) -> set[str]:
    """Lower-cased word set; the crude stand-in for an embedding in the local store."""
    return set(_WORD.findall(text.lower()))


class InMemoryIncidentMemory:
    """List-backed memory. Similarity = word overlap (Jaccard), not embeddings.

    Good enough to exercise the recall path in tests. It is NOT what the eval will measure;
    the Firestore store with real embeddings replaces it before any memory-ablation run.
    """

    def __init__(self) -> None:
        self._records: list[IncidentRecord] = []

    def save(self, record: IncidentRecord) -> None:
        if not record.verified:
            raise UnverifiedRecordError(
                f"refusing to store incident {record.incident_id}: fix was not verified"
            )
        self._records.append(record)

    def find_similar(self, symptoms: str, k: int = 3) -> list[IncidentRecord]:
        query = _words(symptoms)
        if not query:
            return []
        scored: list[tuple[float, IncidentRecord]] = []
        for record in self._records:
            words = _words(record.symptoms)
            overlap = len(query & words) / len(query | words)
            if overlap > 0:
                scored.append((overlap, record))
        # Highest overlap first; ties keep insertion order (sorted is stable).
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [record for _, record in scored[:k]]

    def __len__(self) -> int:
        return len(self._records)
