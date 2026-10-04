"""GRP-004 (revised): Relationship occurrence merge.

MergedRelationship now retains:
- all distinct descriptions (from occurrences)
- all occurrence local_ids
- all evidence_ids
- combined noisy-OR confidence
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from fabric_kg_builder.graph.occurrence import RelationshipOccurrence


@dataclass
class MergedRelationship:
    """A canonicalized relationship retaining all occurrence-level evidence."""

    relationship_type: str
    source_local_id: str
    target_local_id: str
    occurrences: list[RelationshipOccurrence] = field(default_factory=list)
    occurrence_ids: list[str] = field(default_factory=list)
    all_evidence_ids: list[str] = field(default_factory=list)
    descriptions: list[str] = field(default_factory=list)
    confidence: float = 0.0
    properties_json: Optional[str] = None

    @property
    def occurrence_count(self) -> int:
        return len(self.occurrences)

    @property
    def primary_description(self) -> str:
        return self.descriptions[0] if self.descriptions else ""

    def _recompute(self) -> None:
        if not self.occurrences:
            self.confidence = 0.0
            return

        # Noisy-OR confidence
        combined = 1.0
        for occ in self.occurrences:
            combined *= 1.0 - max(0.0, min(1.0, occ.confidence))
        self.confidence = round(1.0 - combined, 6)

        # Distinct descriptions (preserve insertion order)
        seen_desc: set[str] = set()
        ordered_desc: list[str] = []
        for occ in self.occurrences:
            d = (occ.description or "").strip()
            if d and d not in seen_desc:
                seen_desc.add(d)
                ordered_desc.append(d)
        self.descriptions = ordered_desc

        # Occurrence IDs
        self.occurrence_ids = [occ.local_id for occ in self.occurrences]

        # Deduplicated evidence IDs
        seen_evid: set[str] = set()
        ordered_evid: list[str] = []
        for occ in self.occurrences:
            for eid in occ.evidence_ids:
                if eid not in seen_evid:
                    seen_evid.add(eid)
                    ordered_evid.append(eid)
        self.all_evidence_ids = ordered_evid


def _merge_key(occ: RelationshipOccurrence) -> tuple[str, str, str]:
    return (occ.relationship_type, occ.source_local_id, occ.target_local_id)


def merge_relationship_occurrences(
    occurrences: list[RelationshipOccurrence],
) -> list[MergedRelationship]:
    """Merge occurrences sharing (source, type, target) — preserve direction."""
    buckets: dict[tuple[str, str, str], MergedRelationship] = {}
    for occ in occurrences:
        key = _merge_key(occ)
        if key not in buckets:
            buckets[key] = MergedRelationship(
                relationship_type=key[0],
                source_local_id=key[1],
                target_local_id=key[2],
                properties_json=occ.properties_json,
            )
        buckets[key].occurrences.append(occ)

    results: list[MergedRelationship] = []
    for merged in buckets.values():
        merged._recompute()
        results.append(merged)
    return results
