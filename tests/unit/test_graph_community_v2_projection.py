"""Tests for graph/community_projection.py — v2 derived-hierarchy canonical
projection builder (additive, independent of legacy graph/community.py).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from fabric_kg_builder.graph.community_projection import (
    CanonicalProjectionError,
    build_canonical_projection,
    to_native_edge_list,
)
from fabric_kg_builder.model.schemas import EntityRow, RelationshipRow

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entity(entity_id: str) -> EntityRow:
    key = hashlib.sha1(entity_id.encode()).hexdigest()[:16]
    return EntityRow(
        entity_id=entity_id,
        entity_type="org",
        display_name=entity_id,
        canonical_key=key,
        content_hash=key,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _rel(rel_id: str, source: str, target: str, rel_type: str = "knows") -> RelationshipRow:
    key = hashlib.sha1(rel_id.encode()).hexdigest()[:16]
    return RelationshipRow(
        relationship_id=rel_id,
        relationship_type=rel_type,
        source_entity_id=source,
        target_entity_id=target,
        content_hash=key,
        created_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Canonical node/edge rules
# ---------------------------------------------------------------------------


class TestCanonicalNodesAndEdges:
    def test_unique_nodes_from_entities(self) -> None:
        entities = [_entity("e1"), _entity("e2"), _entity("e3")]
        projection = build_canonical_projection(entities, [])
        assert projection.node_ids == ("e1", "e2", "e3")

    def test_duplicate_entity_id_raises(self) -> None:
        entities = [_entity("e1"), _entity("e1")]
        with pytest.raises(CanonicalProjectionError):
            build_canonical_projection(entities, [])

    def test_self_loop_excluded_with_diagnostic(self) -> None:
        entities = [_entity("e1")]
        relationships = [_rel("r1", "e1", "e1")]
        projection = build_canonical_projection(entities, relationships)
        assert projection.edges == ()
        assert len(projection.self_loop_diagnostics) == 1
        diag = projection.self_loop_diagnostics[0]
        assert diag.entity_id == "e1"
        assert diag.relationship_id == "r1"
        assert diag.reason == "self_loop_excluded_from_partition_graph"
        # The self-looping entity has no surviving edge: it is an isolate.
        assert projection.isolate_ids == ("e1",)

    def test_reciprocal_and_duplicate_rows_do_not_strengthen_edge(self) -> None:
        entities = [_entity("e1"), _entity("e2")]
        relationships = [
            _rel("r1", "e1", "e2"),
            _rel("r2", "e2", "e1"),  # reciprocal direction
            _rel("r3", "e1", "e2"),  # duplicate direction
        ]
        projection = build_canonical_projection(entities, relationships)
        assert len(projection.edges) == 1
        edge = projection.edges[0]
        assert edge.weight == 1.0
        assert edge.source_entity_id == "e1"
        assert edge.target_entity_id == "e2"
        # All three contribute provenance, never additional weight.
        assert edge.contributing_relationship_ids == ("r1", "r2", "r3")

    def test_sorted_endpoint_pair_independent_of_direction(self) -> None:
        entities = [_entity("b"), _entity("a")]
        relationships = [_rel("r1", "b", "a")]
        projection = build_canonical_projection(entities, relationships)
        edge = projection.edges[0]
        assert (edge.source_entity_id, edge.target_entity_id) == ("a", "b")

    def test_all_components_retained_no_largest_component_filter(self) -> None:
        # Two disjoint edges (two separate components) plus one isolate.
        entities = [_entity(e) for e in ("a", "b", "c", "d", "iso")]
        relationships = [_rel("r1", "a", "b"), _rel("r2", "c", "d")]
        projection = build_canonical_projection(entities, relationships)
        assert len(projection.edges) == 2
        assert projection.isolate_ids == ("iso",)

    def test_isolates_explicit_for_entities_with_no_surviving_edge(self) -> None:
        entities = [_entity("a"), _entity("b")]
        projection = build_canonical_projection(entities, [])
        assert projection.isolate_ids == ("a", "b")

    def test_relationship_with_endpoint_outside_entity_set_raises(self) -> None:
        """Fail-closed (defect #1): an unknown relationship endpoint is a
        referential-integrity violation of the input snapshot, never a
        silently-skipped row."""
        entities = [_entity("a")]
        relationships = [_rel("r1", "a", "ghost")]
        with pytest.raises(CanonicalProjectionError, match="unknown endpoint"):
            build_canonical_projection(entities, relationships)

    def test_relationship_with_both_endpoints_unknown_raises(self) -> None:
        entities = [_entity("a")]
        relationships = [_rel("r1", "ghost1", "ghost2")]
        with pytest.raises(CanonicalProjectionError, match="unknown endpoint"):
            build_canonical_projection(entities, relationships)

    def test_multiple_unknown_endpoint_relationships_all_reported(self) -> None:
        entities = [_entity("a")]
        relationships = [_rel("r1", "a", "ghost1"), _rel("r2", "ghost2", "a")]
        with pytest.raises(CanonicalProjectionError) as exc_info:
            build_canonical_projection(entities, relationships)
        assert "r1" in str(exc_info.value)
        assert "r2" in str(exc_info.value)

    def test_duplicate_relationship_id_raises_even_if_content_identical(self) -> None:
        entities = [_entity("a"), _entity("b")]
        relationships = [_rel("r1", "a", "b"), _rel("r1", "a", "b")]
        with pytest.raises(CanonicalProjectionError, match="unique relationship_id"):
            build_canonical_projection(entities, relationships)

    def test_duplicate_relationship_id_raises_when_conflicting(self) -> None:
        entities = [_entity("a"), _entity("b"), _entity("c")]
        relationships = [_rel("r1", "a", "b"), _rel("r1", "a", "c")]
        with pytest.raises(CanonicalProjectionError, match="unique relationship_id"):
            build_canonical_projection(entities, relationships)


# ---------------------------------------------------------------------------
# Completeness: empty input
# ---------------------------------------------------------------------------


class TestEmptyInput:
    def test_empty_entities_and_relationships(self) -> None:
        projection = build_canonical_projection([], [])
        assert projection.node_ids == ()
        assert projection.edges == ()
        assert projection.isolate_ids == ()
        assert projection.self_loop_diagnostics == ()
        assert projection.duplicate_entity_diagnostics == ()


# ---------------------------------------------------------------------------
# Permutation repeatability
# ---------------------------------------------------------------------------


class TestPermutationRepeatability:
    def test_input_graph_hash_stable_under_entity_and_relationship_permutation(self) -> None:
        entities = [_entity("a"), _entity("b"), _entity("c")]
        relationships = [_rel("r1", "a", "b"), _rel("r2", "b", "c")]

        projection_1 = build_canonical_projection(entities, relationships)
        projection_2 = build_canonical_projection(
            list(reversed(entities)), list(reversed(relationships))
        )
        assert projection_1.input_graph_hash == projection_2.input_graph_hash
        assert projection_1.node_ids == projection_2.node_ids
        assert projection_1.edges == projection_2.edges

    def test_input_graph_hash_binds_self_loops_and_relationship_ids(self) -> None:
        """Defect #5 regression: the fingerprint hash must bind the full
        original directed/typed relationship set (including self-loops and
        relationship_id), not merely the post-projection undirected
        topology — two inputs with identical undirected edges but different
        original rows must hash differently."""
        entities = [_entity("a"), _entity("b")]
        relationships_without_self_loop = [_rel("r1", "a", "b")]
        relationships_with_self_loop = [_rel("r1", "a", "b"), _rel("r2", "a", "a")]

        projection_without = build_canonical_projection(entities, relationships_without_self_loop)
        projection_with = build_canonical_projection(entities, relationships_with_self_loop)

        # Same undirected edge set (self-loop excluded from edges either way)...
        assert projection_without.edges == projection_with.edges
        # ...but the fingerprint hash must differ, since the self-loop row
        # is part of the retained original snapshot.
        assert projection_without.input_graph_hash != projection_with.input_graph_hash

    def test_input_graph_hash_changes_with_different_relationship_id(self) -> None:
        entities = [_entity("a"), _entity("b")]
        projection_1 = build_canonical_projection(entities, [_rel("r1", "a", "b")])
        projection_2 = build_canonical_projection(entities, [_rel("r_other", "a", "b")])
        assert projection_1.input_graph_hash != projection_2.input_graph_hash


# ---------------------------------------------------------------------------
# to_native_edge_list
# ---------------------------------------------------------------------------


class TestToNativeEdgeList:
    def test_isolates_excluded(self) -> None:
        entities = [_entity("a"), _entity("b"), _entity("iso")]
        relationships = [_rel("r1", "a", "b")]
        projection = build_canonical_projection(entities, relationships)
        edge_list = to_native_edge_list(projection)
        assert edge_list == [("a", "b", 1.0)]

    def test_empty_projection_yields_empty_edge_list(self) -> None:
        projection = build_canonical_projection([], [])
        assert to_native_edge_list(projection) == []
