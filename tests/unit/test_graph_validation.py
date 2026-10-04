"""Tests for graph/validation.py — graph structure validation (GRP-010)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from fabric_kg_builder.graph.validation import (
    GraphValidationResult,
    HierarchyValidationResult,
    VAL_038,
    _find_directed_cycles,
    validate_graph,
    validate_hierarchy,
)
from fabric_kg_builder.model.schemas import (
    ClusterMembershipRow,
    ClusterRow,
    EntityRow,
    RelationshipRow,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entity(entity_id: str, entity_type: str = "org") -> EntityRow:
    key = hashlib.sha1(entity_id.encode()).hexdigest()[:16]
    return EntityRow(
        entity_id=entity_id,
        entity_type=entity_type,
        display_name=entity_id,
        canonical_key=key,
        content_hash=key,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _relationship(
    src: str,
    tgt: str,
    rel_type: str = "knows",
    rid: str | None = None,
) -> RelationshipRow:
    rid = rid or f"rel:{src}:{tgt}:{rel_type}"
    key = hashlib.sha1(rid.encode()).hexdigest()[:16]
    return RelationshipRow(
        relationship_id=rid,
        relationship_type=rel_type,
        source_entity_id=src,
        target_entity_id=tgt,
        content_hash=key,
        created_at=datetime.now(timezone.utc),
    )


def _cluster(
    cluster_id: str,
    level: int = 0,
    parent_id: str | None = None,
) -> ClusterRow:
    return ClusterRow(
        cluster_id=cluster_id,
        hierarchy_version="1.0",
        level=level,
        parent_cluster_id=parent_id,
        label=cluster_id,
        description="",
        method="test",
    )


def _membership(
    cluster_id: str,
    entity_id: str | None = None,
    relationship_id: str | None = None,
    claim_id: str | None = None,
    rationale: str = "reason",
) -> ClusterMembershipRow:
    return ClusterMembershipRow(
        cluster_id=cluster_id,
        entity_id=entity_id,
        relationship_id=relationship_id,
        claim_id=claim_id,
        rationale=rationale,
        score=1.0,
    )


# ---------------------------------------------------------------------------
# GraphValidationResult helpers
# ---------------------------------------------------------------------------


class TestGraphValidationResult:
    def test_recompute_passed_no_issues(self) -> None:
        r = GraphValidationResult()
        r._recompute_passed()
        assert r.passed

    def test_recompute_passed_with_cycles(self) -> None:
        r = GraphValidationResult(has_cycles=True)
        r._recompute_passed()
        assert not r.passed

    def test_recompute_passed_with_coherence_issues(self) -> None:
        r = GraphValidationResult(coherence_issues=["issue"])
        r._recompute_passed()
        assert not r.passed


# ---------------------------------------------------------------------------
# HierarchyValidationResult helpers
# ---------------------------------------------------------------------------


class TestHierarchyValidationResult:
    def test_gate_is_val_038(self) -> None:
        r = HierarchyValidationResult()
        assert r.gate == VAL_038

    def test_recompute_clean(self) -> None:
        r = HierarchyValidationResult()
        r._recompute()
        assert r.passed
        assert not r.block_publication

    def test_recompute_parent_cycle_blocks(self) -> None:
        r = HierarchyValidationResult(parent_cycle_ids=["c1"])
        r._recompute()
        assert not r.passed
        assert r.block_publication

    def test_recompute_invalid_level_blocks(self) -> None:
        r = HierarchyValidationResult(invalid_level_ids=["c1"])
        r._recompute()
        assert r.block_publication

    def test_recompute_orphan_parent_blocks(self) -> None:
        r = HierarchyValidationResult(orphan_parent_ids=["c1"])
        r._recompute()
        assert r.block_publication

    def test_recompute_uncovered_entities_fails_not_blocks(self) -> None:
        r = HierarchyValidationResult(uncovered_entity_ids=["e1"])
        r._recompute()
        assert not r.passed
        assert not r.block_publication


# ---------------------------------------------------------------------------
# _find_directed_cycles
# ---------------------------------------------------------------------------


class TestFindDirectedCycles:
    def test_no_cycle(self) -> None:
        adj = {"a": ["b"], "b": ["c"], "c": []}
        cycles = _find_directed_cycles(["a", "b", "c"], adj)
        assert cycles == []

    def test_simple_cycle(self) -> None:
        adj = {"a": ["b"], "b": ["c"], "c": ["a"]}
        cycles = _find_directed_cycles(["a", "b", "c"], adj)
        assert len(cycles) > 0

    def test_self_loop(self) -> None:
        adj = {"a": ["a"]}
        cycles = _find_directed_cycles(["a"], adj)
        assert len(cycles) > 0

    def test_max_examples_respected(self) -> None:
        adj = {"a": ["a"], "b": ["b"], "c": ["c"], "d": ["d"]}
        cycles = _find_directed_cycles(["a", "b", "c", "d"], adj, max_examples=2)
        assert len(cycles) <= 2


# ---------------------------------------------------------------------------
# validate_graph
# ---------------------------------------------------------------------------


class TestValidateGraph:
    def test_empty_graph_passes(self) -> None:
        result = validate_graph([], [])
        assert result.passed
        assert not result.has_cycles

    def test_clean_graph_passes(self) -> None:
        entities = [_entity("e1"), _entity("e2")]
        rels = [_relationship("e1", "e2")]
        result = validate_graph(entities, rels)
        assert result.passed

    def test_dangling_source_flagged(self) -> None:
        entities = [_entity("e1")]
        rels = [_relationship("bad-src", "e1")]
        result = validate_graph(entities, rels)
        assert len(result.coherence_issues) > 0
        assert not result.passed

    def test_dangling_target_flagged(self) -> None:
        entities = [_entity("e1")]
        rels = [_relationship("e1", "bad-tgt")]
        result = validate_graph(entities, rels)
        assert len(result.coherence_issues) > 0

    def test_self_loop_flagged_by_default(self) -> None:
        entities = [_entity("e1")]
        rels = [_relationship("e1", "e1")]
        result = validate_graph(entities, rels)
        assert any("Self-loop" in issue for issue in result.coherence_issues)

    def test_self_loop_allowed(self) -> None:
        entities = [_entity("e1")]
        rels = [_relationship("e1", "e1")]
        result = validate_graph(entities, rels, allow_self_loops=True)
        assert not any("Self-loop" in issue for issue in result.coherence_issues)

    def test_cycle_not_flagged_without_hierarchical_types(self) -> None:
        entities = [_entity("e1"), _entity("e2"), _entity("e3")]
        rels = [
            _relationship("e1", "e2"),
            _relationship("e2", "e3"),
            _relationship("e3", "e1"),
        ]
        # Default: no hierarchical types → cycles ignored
        result = validate_graph(entities, rels)
        assert not result.has_cycles

    def test_cycle_flagged_for_hierarchical_type(self) -> None:
        entities = [_entity("e1"), _entity("e2"), _entity("e3")]
        rels = [
            _relationship("e1", "e2", rel_type="parent_of"),
            _relationship("e2", "e3", rel_type="parent_of"),
            _relationship("e3", "e1", rel_type="parent_of"),
        ]
        result = validate_graph(
            entities, rels,
            hierarchical_relation_types=frozenset({"parent_of"}),
        )
        assert result.has_cycles
        assert not result.passed

    def test_isolated_entities_in_coverage_gaps(self) -> None:
        entities = [_entity("isolated")]
        result = validate_graph(entities, [])
        assert len(result.coverage_gaps) > 0
        assert any("isolated" in gap for gap in result.coverage_gaps)

    def test_expected_types_gap(self) -> None:
        entities = [_entity("e1", "org")]
        result = validate_graph(entities, [], expected_types=["org", "location"])
        assert any("location" in gap for gap in result.coverage_gaps)

    def test_expected_types_all_present(self) -> None:
        entities = [_entity("e1", "org"), _entity("e2", "location")]
        rels = [_relationship("e1", "e2")]
        result = validate_graph(entities, rels, expected_types=["org", "location"])
        assert not any("location" in gap for gap in result.coverage_gaps)


# ---------------------------------------------------------------------------
# validate_hierarchy
# ---------------------------------------------------------------------------


class TestValidateHierarchy:
    def test_empty_hierarchy(self) -> None:
        result = validate_hierarchy([], [], [])
        assert result.passed

    def test_all_entities_covered(self) -> None:
        entities = [_entity("e1"), _entity("e2")]
        clusters = [_cluster("c1")]
        memberships = [
            _membership("c1", entity_id="e1"),
            _membership("c1", entity_id="e2"),
        ]
        result = validate_hierarchy(clusters, memberships, entities)
        assert result.uncovered_entity_ids == []

    def test_uncovered_entity_detected(self) -> None:
        entities = [_entity("e1"), _entity("e2")]
        clusters = [_cluster("c1")]
        memberships = [_membership("c1", entity_id="e1")]  # e2 not covered
        result = validate_hierarchy(clusters, memberships, entities)
        assert "e2" in result.uncovered_entity_ids
        assert not result.passed

    def test_orphan_parent_detected(self) -> None:
        entities = []
        clusters = [_cluster("c1", parent_id="non-existent")]
        result = validate_hierarchy(clusters, [], entities)
        assert "c1" in result.orphan_parent_ids

    def test_invalid_level_detected(self) -> None:
        entities = []
        clusters = [_cluster("c1", level=99)]
        result = validate_hierarchy(clusters, [], entities, expected_levels=3)
        assert "c1" in result.invalid_level_ids

    def test_all_null_membership_issue(self) -> None:
        entities = [_entity("e1")]
        clusters = [_cluster("c1")]
        m = ClusterMembershipRow(
            cluster_id="c1",
            entity_id=None,
            relationship_id=None,
            claim_id=None,
            rationale="",
            score=1.0,
        )
        result = validate_hierarchy(clusters, [m], entities)
        assert any("all-null" in issue for issue in result.membership_issues)

    def test_missing_rationale_flagged(self) -> None:
        entities = [_entity("e1")]
        clusters = [_cluster("c1")]
        m = _membership("c1", entity_id="e1", rationale="")
        result = validate_hierarchy(clusters, [m], entities)
        assert any("rationale" in issue for issue in result.membership_issues)

    def test_valid_hierarchy_passes(self) -> None:
        entities = [_entity("e1")]
        clusters = [_cluster("c2", level=2), _cluster("c1", level=1, parent_id="c2"), _cluster("c0", level=0, parent_id="c1")]
        memberships = [_membership("c0", entity_id="e1")]
        result = validate_hierarchy(clusters, memberships, entities, expected_levels=3)
        assert result.passed
