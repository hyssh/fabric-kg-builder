"""GRP-007 (revised): Overlap occurrence dedup.

Fix: null spans do NOT collapse all occurrences in a text unit.
Only occurrences with the same semantic_key AND overlapping spans are merged.
Null-span occurrences with different semantic_keys are kept distinct.

Provides entity, relationship, and claim-aware dedup helpers.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Union

from fabric_kg_builder.graph.occurrence import EntityOccurrence, OccurrenceContext, RelationshipOccurrence


# ---------------------------------------------------------------------------
# Generic OccurrenceContext dedup (used for claim/general occurrences)
# ---------------------------------------------------------------------------


def _spans_overlap(a: OccurrenceContext, b: OccurrenceContext) -> bool:
    """Overlap only within same text_unit AND same semantic_key."""
    if a.text_unit_id != b.text_unit_id:
        return False
    if a.semantic_key != b.semantic_key:
        return False
    # Both have explicit spans — check interval overlap
    if a.span_start is not None and a.span_end is not None and \
       b.span_start is not None and b.span_end is not None:
        return max(a.span_start, b.span_start) < min(a.span_end, b.span_end)
    # Same semantic_key + both null spans → merge
    if a.span_start is None and b.span_start is None:
        return True
    # One has span, one does not → keep separate (different granularity)
    return False


def dedup_overlapping_occurrences(
    occurrences: list[OccurrenceContext],
) -> list[OccurrenceContext]:
    """Remove duplicates preserving all evidence IDs."""
    by_key: dict[tuple[str, str], list[OccurrenceContext]] = {}
    for occ in occurrences:
        k = (occ.text_unit_id, occ.semantic_key)
        by_key.setdefault(k, []).append(occ)

    result: list[OccurrenceContext] = []
    for unit_key_occs in by_key.values():
        sorted_occs = sorted(
            unit_key_occs,
            key=lambda o: (-o.confidence, o.span_start if o.span_start is not None else 0),
        )
        kept: list[OccurrenceContext] = []
        for occ in sorted_occs:
            merged_into: OccurrenceContext | None = None
            for candidate in kept:
                if _spans_overlap(occ, candidate):
                    merged_into = candidate
                    break
            if merged_into is not None:
                existing = set(merged_into.evidence_ids)
                extra = [eid for eid in occ.evidence_ids if eid not in existing]
                if extra:
                    merged_into.evidence_ids.extend(extra)
            else:
                kept.append(deepcopy(occ))
        result.extend(kept)
    return result


# ---------------------------------------------------------------------------
# Entity occurrence dedup
# ---------------------------------------------------------------------------


def _entity_spans_overlap(a: EntityOccurrence, b: EntityOccurrence) -> bool:
    """Same text_unit + same type+name key + overlapping spans."""
    if a.text_unit_id != b.text_unit_id:
        return False
    if a.entity_type.lower() != b.entity_type.lower():
        return False
    if a.display_name.lower() != b.display_name.lower():
        return False
    if a.span is not None and b.span is not None:
        return max(a.span.start, b.span.start) < min(a.span.end, b.span.end)
    # Both null spans + same name → merge
    return a.span is None and b.span is None


def dedup_entity_occurrences(
    occurrences: list[EntityOccurrence],
) -> list[EntityOccurrence]:
    """Dedup entity occurrences by type+name+span; preserve all evidence IDs."""
    # Group by (text_unit_id, entity_type, display_name)
    by_key: dict[tuple[str, str, str], list[EntityOccurrence]] = {}
    for occ in occurrences:
        k = (occ.text_unit_id, occ.entity_type.lower(), occ.display_name.lower())
        by_key.setdefault(k, []).append(occ)

    result: list[EntityOccurrence] = []
    for group in by_key.values():
        sorted_group = sorted(
            group,
            key=lambda o: (-o.confidence, o.span.start if o.span else 0),
        )
        kept: list[EntityOccurrence] = []
        for occ in sorted_group:
            merged: EntityOccurrence | None = None
            for candidate in kept:
                if _entity_spans_overlap(occ, candidate):
                    merged = candidate
                    break
            if merged is not None:
                existing = set(merged.evidence_ids)
                for eid in occ.evidence_ids:
                    if eid not in existing:
                        merged.evidence_ids.append(eid)
                        existing.add(eid)
            else:
                kept.append(deepcopy(occ))
        result.extend(kept)
    return result


# ---------------------------------------------------------------------------
# Relationship occurrence dedup
# ---------------------------------------------------------------------------


def dedup_relationship_occurrences(
    occurrences: list[RelationshipOccurrence],
) -> list[RelationshipOccurrence]:
    """Dedup relationship occurrences by (type, source, target, span)."""
    by_key: dict[tuple[str, str, str, str], list[RelationshipOccurrence]] = {}
    for occ in occurrences:
        k = (
            occ.text_unit_id,
            occ.relationship_type.lower(),
            occ.source_local_id,
            occ.target_local_id,
        )
        by_key.setdefault(k, []).append(occ)

    result: list[RelationshipOccurrence] = []
    for group in by_key.values():
        sorted_group = sorted(
            group,
            key=lambda o: (-o.confidence, o.span.start if o.span else 0),
        )
        kept: list[RelationshipOccurrence] = []
        for occ in sorted_group:
            merged: RelationshipOccurrence | None = None
            for candidate in kept:
                if occ.span is None and candidate.span is None:
                    merged = candidate
                    break
                if (occ.span and candidate.span and
                        max(occ.span.start, candidate.span.start) <
                        min(occ.span.end, candidate.span.end)):
                    merged = candidate
                    break
            if merged is not None:
                existing = set(merged.evidence_ids)
                for eid in occ.evidence_ids:
                    if eid not in existing:
                        merged.evidence_ids.append(eid)
                        existing.add(eid)
            else:
                kept.append(deepcopy(occ))
        result.extend(kept)
    return result
