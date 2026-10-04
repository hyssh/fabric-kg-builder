"""Tests for graph/dedup.py — overlap occurrence dedup (GRP-007)."""

from __future__ import annotations

import pytest
from copy import deepcopy

from fabric_kg_builder.graph.dedup import (
    _entity_spans_overlap,
    _spans_overlap,
    dedup_entity_occurrences,
    dedup_overlapping_occurrences,
    dedup_relationship_occurrences,
)
from fabric_kg_builder.graph.occurrence import (
    EntityOccurrence,
    EvidenceSpan,
    OccurrenceContext,
    RelationshipOccurrence,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(
    text_unit_id: str = "tu1",
    semantic_key: str = "",
    span_start: int | None = None,
    span_end: int | None = None,
    confidence: float = 1.0,
    evidence_ids: list[str] | None = None,
) -> OccurrenceContext:
    return OccurrenceContext(
        text_unit_id=text_unit_id,
        semantic_key=semantic_key or text_unit_id,
        span_start=span_start,
        span_end=span_end,
        confidence=confidence,
        evidence_ids=evidence_ids or [],
    )


def _eocc(
    text_unit_id: str = "tu1",
    entity_type: str = "org",
    display_name: str = "Alpha",
    span_start: int | None = None,
    span_end: int | None = None,
    confidence: float = 1.0,
    evidence_ids: list[str] | None = None,
) -> EntityOccurrence:
    span = None
    if span_start is not None and span_end is not None:
        span = EvidenceSpan(
            text_unit_id=text_unit_id, start=span_start, end=span_end,
            text="x" * (span_end - span_start),
        )
    return EntityOccurrence(
        text_unit_id=text_unit_id,
        entity_type=entity_type,
        display_name=display_name,
        span=span,
        confidence=confidence,
        evidence_ids=evidence_ids or [],
    )


def _rocc(
    text_unit_id: str = "tu1",
    rel_type: str = "knows",
    src: str = "e1",
    tgt: str = "e2",
    span_start: int | None = None,
    span_end: int | None = None,
    evidence_ids: list[str] | None = None,
    confidence: float = 1.0,
) -> RelationshipOccurrence:
    span = None
    if span_start is not None and span_end is not None:
        span = EvidenceSpan(
            text_unit_id=text_unit_id, start=span_start, end=span_end,
            text="x" * (span_end - span_start),
        )
    return RelationshipOccurrence(
        text_unit_id=text_unit_id,
        relationship_type=rel_type,
        source_local_id=src,
        target_local_id=tgt,
        span=span,
        evidence_ids=evidence_ids or [],
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# _spans_overlap (OccurrenceContext)
# ---------------------------------------------------------------------------


class TestSpansOverlap:
    def test_different_text_units_never_overlap(self) -> None:
        a = _ctx("tu1", "key", 0, 10)
        b = _ctx("tu2", "key", 0, 10)
        assert not _spans_overlap(a, b)

    def test_different_semantic_keys_never_overlap(self) -> None:
        a = _ctx("tu1", "key_a", 0, 10)
        b = _ctx("tu1", "key_b", 0, 10)
        assert not _spans_overlap(a, b)

    def test_overlapping_spans_same_key(self) -> None:
        a = _ctx("tu1", "key", 0, 10)
        b = _ctx("tu1", "key", 5, 15)
        assert _spans_overlap(a, b)

    def test_non_overlapping_spans(self) -> None:
        a = _ctx("tu1", "key", 0, 5)
        b = _ctx("tu1", "key", 5, 10)
        # [0,5) and [5,10) are adjacent, not overlapping
        assert not _spans_overlap(a, b)

    def test_both_null_spans_same_key_merge(self) -> None:
        a = _ctx("tu1", "key")
        b = _ctx("tu1", "key")
        assert _spans_overlap(a, b)

    def test_one_null_one_span_no_merge(self) -> None:
        a = _ctx("tu1", "key", 0, 10)
        b = _ctx("tu1", "key")
        assert not _spans_overlap(a, b)


# ---------------------------------------------------------------------------
# dedup_overlapping_occurrences
# ---------------------------------------------------------------------------


class TestDedupOverlappingOccurrences:
    def test_empty_list(self) -> None:
        assert dedup_overlapping_occurrences([]) == []

    def test_single_occurrence_passes_through(self) -> None:
        ctx = _ctx("tu1", "key", 0, 5)
        result = dedup_overlapping_occurrences([ctx])
        assert len(result) == 1

    def test_two_overlapping_merged(self) -> None:
        a = _ctx("tu1", "key", 0, 10, evidence_ids=["ev-a"])
        b = _ctx("tu1", "key", 5, 15, evidence_ids=["ev-b"])
        result = dedup_overlapping_occurrences([a, b])
        assert len(result) == 1
        assert set(result[0].evidence_ids) >= {"ev-a", "ev-b"}

    def test_two_non_overlapping_kept_separate(self) -> None:
        a = _ctx("tu1", "key_a", 0, 10)
        b = _ctx("tu1", "key_b", 0, 10)
        result = dedup_overlapping_occurrences([a, b])
        assert len(result) == 2

    def test_different_text_units_kept_separate(self) -> None:
        a = _ctx("tu1", "key", 0, 10)
        b = _ctx("tu2", "key", 0, 10)
        result = dedup_overlapping_occurrences([a, b])
        assert len(result) == 2

    def test_evidence_ids_deduplicated(self) -> None:
        a = _ctx("tu1", "key", evidence_ids=["ev-shared"])
        b = _ctx("tu1", "key", evidence_ids=["ev-shared", "ev-extra"])
        result = dedup_overlapping_occurrences([a, b])
        assert len(result) == 1
        assert result[0].evidence_ids.count("ev-shared") == 1

    def test_higher_confidence_wins_position(self) -> None:
        # higher confidence should be kept as primary
        low = _ctx("tu1", "key", 0, 10, confidence=0.3, evidence_ids=["ev-low"])
        high = _ctx("tu1", "key", 0, 10, confidence=0.9, evidence_ids=["ev-high"])
        result = dedup_overlapping_occurrences([low, high])
        assert len(result) == 1


# ---------------------------------------------------------------------------
# _entity_spans_overlap
# ---------------------------------------------------------------------------


class TestEntitySpansOverlap:
    def test_different_text_units_no_overlap(self) -> None:
        a = _eocc("tu1", "org", "Alpha", 0, 10)
        b = _eocc("tu2", "org", "Alpha", 0, 10)
        assert not _entity_spans_overlap(a, b)

    def test_different_types_no_overlap(self) -> None:
        a = _eocc("tu1", "org", "Alpha", 0, 10)
        b = _eocc("tu1", "location", "Alpha", 0, 10)
        assert not _entity_spans_overlap(a, b)

    def test_different_names_no_overlap(self) -> None:
        a = _eocc("tu1", "org", "Alpha", 0, 10)
        b = _eocc("tu1", "org", "Beta", 0, 10)
        assert not _entity_spans_overlap(a, b)

    def test_same_type_name_overlapping_spans(self) -> None:
        a = _eocc("tu1", "org", "Alpha", 0, 15)
        b = _eocc("tu1", "org", "Alpha", 5, 20)
        assert _entity_spans_overlap(a, b)

    def test_both_null_spans_same_merge(self) -> None:
        a = _eocc("tu1", "org", "Alpha")
        b = _eocc("tu1", "org", "Alpha")
        assert _entity_spans_overlap(a, b)

    def test_case_insensitive_name_and_type(self) -> None:
        a = _eocc("tu1", "Org", "ALPHA", 0, 10)
        b = _eocc("tu1", "org", "alpha", 0, 10)
        assert _entity_spans_overlap(a, b)


# ---------------------------------------------------------------------------
# dedup_entity_occurrences
# ---------------------------------------------------------------------------


class TestDedupEntityOccurrences:
    def test_empty(self) -> None:
        assert dedup_entity_occurrences([]) == []

    def test_single_passes_through(self) -> None:
        occ = _eocc(evidence_ids=["ev-1"])
        result = dedup_entity_occurrences([occ])
        assert len(result) == 1

    def test_two_identical_null_spans_merged(self) -> None:
        a = _eocc(evidence_ids=["ev-a"])
        b = _eocc(evidence_ids=["ev-b"])
        result = dedup_entity_occurrences([a, b])
        assert len(result) == 1
        assert set(result[0].evidence_ids) == {"ev-a", "ev-b"}

    def test_different_types_kept_separate(self) -> None:
        a = _eocc(entity_type="org")
        b = _eocc(entity_type="location")
        result = dedup_entity_occurrences([a, b])
        assert len(result) == 2

    def test_different_names_kept_separate(self) -> None:
        a = _eocc(display_name="Alpha")
        b = _eocc(display_name="Beta")
        result = dedup_entity_occurrences([a, b])
        assert len(result) == 2

    def test_overlapping_spans_merged(self) -> None:
        a = _eocc(span_start=0, span_end=10, evidence_ids=["ev-a"])
        b = _eocc(span_start=5, span_end=15, evidence_ids=["ev-b"])
        result = dedup_entity_occurrences([a, b])
        assert len(result) == 1

    def test_non_overlapping_spans_kept(self) -> None:
        a = _eocc(span_start=0, span_end=5, evidence_ids=["ev-a"])
        b = _eocc(span_start=10, span_end=20, evidence_ids=["ev-b"])
        result = dedup_entity_occurrences([a, b])
        assert len(result) == 2

    def test_evidence_ids_not_duplicated(self) -> None:
        a = _eocc(evidence_ids=["shared"])
        b = _eocc(evidence_ids=["shared", "extra"])
        result = dedup_entity_occurrences([a, b])
        assert result[0].evidence_ids.count("shared") == 1


# ---------------------------------------------------------------------------
# dedup_relationship_occurrences
# ---------------------------------------------------------------------------


class TestDedupRelationshipOccurrences:
    def test_empty(self) -> None:
        assert dedup_relationship_occurrences([]) == []

    def test_single_passes_through(self) -> None:
        r = _rocc()
        result = dedup_relationship_occurrences([r])
        assert len(result) == 1

    def test_two_null_span_same_triple_merged(self) -> None:
        a = _rocc(evidence_ids=["ev-a"])
        b = _rocc(evidence_ids=["ev-b"])
        result = dedup_relationship_occurrences([a, b])
        assert len(result) == 1
        assert set(result[0].evidence_ids) == {"ev-a", "ev-b"}

    def test_different_rel_types_kept(self) -> None:
        a = _rocc(rel_type="knows")
        b = _rocc(rel_type="works_at")
        result = dedup_relationship_occurrences([a, b])
        assert len(result) == 2

    def test_different_source_target_kept(self) -> None:
        a = _rocc(src="e1", tgt="e2")
        b = _rocc(src="e3", tgt="e4")
        result = dedup_relationship_occurrences([a, b])
        assert len(result) == 2

    def test_overlapping_spans_merged(self) -> None:
        a = _rocc(span_start=0, span_end=20, evidence_ids=["ev-a"])
        b = _rocc(span_start=10, span_end=30, evidence_ids=["ev-b"])
        result = dedup_relationship_occurrences([a, b])
        assert len(result) == 1

    def test_non_overlapping_spans_kept(self) -> None:
        a = _rocc(span_start=0, span_end=10, evidence_ids=["ev-a"])
        b = _rocc(span_start=50, span_end=60, evidence_ids=["ev-b"])
        result = dedup_relationship_occurrences([a, b])
        assert len(result) == 2

    def test_evidence_ids_merged_no_dup(self) -> None:
        a = _rocc(evidence_ids=["shared"])
        b = _rocc(evidence_ids=["shared", "extra"])
        result = dedup_relationship_occurrences([a, b])
        assert result[0].evidence_ids.count("shared") == 1
