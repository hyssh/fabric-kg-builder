"""Canonical projection builder for the v2 derived community hierarchy.

Transforms the directed, typed ``RelationshipRow`` set into an undirected,
weight-bearing partition graph suitable for a hierarchical community
provider, while retaining full provenance back to the original rows and
never mutating them.

Rules enforced here:

* Canonical nodes must be unique; a duplicate ``entity_id`` in the input is
  reported as a diagnostic (and raises, since silently deduplicating here
  would hide a precondition violation upstream).
* Self-loops (same canonical entity on both ends of a relationship) are
  excluded from the partition edge list with a recorded diagnostic reason —
  never silently dropped.
* The undirected graph uses sorted-endpoint-pair keys with default
  weight = 1 per distinct pair. Reciprocal/duplicate directed rows between
  the same pair do not increase weight; their relationship ids are recorded
  as additional provenance on the single resulting edge.
* All connected components are retained (no largest-connected-component
  filtering); isolates (entities with no surviving edge) are retained as
  explicit node ids for the caller to turn into singleton terminals.
* Edges are returned in canonical sorted order so that permutations of the
  input produce byte-identical edge lists (required for the native
  provider's permutation-repeatability contract).
"""

from __future__ import annotations

from collections import Counter, defaultdict

from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.graph.community_contracts import (
    CanonicalProjection,
    DuplicateEntityDiagnostic,
    DuplicateRelationshipIdDiagnostic,
    ProjectionEdge,
    SelfLoopDiagnostic,
    UnknownRelationshipEndpointDiagnostic,
)
from fabric_kg_builder.model.schemas import EntityRow, RelationshipRow


class CanonicalProjectionError(ValueError):
    """Raised when the input violates a canonical-projection precondition
    (currently: duplicate ``entity_id`` values) that must not be silently
    repaired."""


def build_canonical_projection(
    entities: list[EntityRow],
    relationships: list[RelationshipRow],
) -> CanonicalProjection:
    """Build the canonical undirected projection graph.

    Raises ``CanonicalProjectionError`` if ``entities`` contains duplicate
    ``entity_id`` values (a precondition violation, not something this
    function silently repairs).
    """
    entity_id_counts = Counter(e.entity_id for e in entities)
    duplicates = tuple(
        DuplicateEntityDiagnostic(entity_id=eid, occurrence_count=count)
        for eid, count in sorted(entity_id_counts.items())
        if count > 1
    )
    if duplicates:
        raise CanonicalProjectionError(
            "Canonical projection requires unique entity_id values; found "
            f"{len(duplicates)} duplicate id(s): "
            f"{', '.join(d.entity_id for d in duplicates)}"
        )

    node_ids = tuple(sorted(entity_id_counts))
    known_ids = set(node_ids)

    relationship_id_counts = Counter(r.relationship_id for r in relationships)
    relationship_groups: dict[str, list[RelationshipRow]] = defaultdict(list)
    for rel in relationships:
        relationship_groups[rel.relationship_id].append(rel)
    duplicate_relationship_diagnostics = tuple(
        DuplicateRelationshipIdDiagnostic(
            relationship_id=rel_id,
            occurrence_count=count,
            conflicting=len(
                {
                    (r.source_entity_id, r.target_entity_id, r.relationship_type)
                    for r in relationship_groups[rel_id]
                }
            )
            > 1,
        )
        for rel_id, count in sorted(relationship_id_counts.items())
        if count > 1
    )
    if duplicate_relationship_diagnostics:
        raise CanonicalProjectionError(
            "Canonical projection requires unique relationship_id values; "
            f"found {len(duplicate_relationship_diagnostics)} duplicate id(s): "
            f"{', '.join(d.relationship_id for d in duplicate_relationship_diagnostics)}"
        )

    self_loop_diagnostics: list[SelfLoopDiagnostic] = []
    unknown_endpoint_diagnostics: list[UnknownRelationshipEndpointDiagnostic] = []
    # Aggregate by canonical sorted pair key; never let reciprocal/duplicate
    # rows between the same pair increase weight.
    pair_relationship_ids: dict[tuple[str, str], list[str]] = defaultdict(list)

    for rel in relationships:
        source = rel.source_entity_id
        target = rel.target_entity_id
        unknown_source = source not in known_ids
        unknown_target = target not in known_ids
        if unknown_source or unknown_target:
            # Endpoint outside the supplied canonical entity set is a
            # referential-integrity violation of the input snapshot, not a
            # condition to silently skip past. Collected (not raised
            # immediately) so every violation in this batch is reported.
            unknown_endpoint_diagnostics.append(
                UnknownRelationshipEndpointDiagnostic(
                    relationship_id=rel.relationship_id,
                    relationship_type=rel.relationship_type,
                    source_entity_id=source,
                    target_entity_id=target,
                    unknown_source=unknown_source,
                    unknown_target=unknown_target,
                )
            )
            continue
        if source == target:
            self_loop_diagnostics.append(
                SelfLoopDiagnostic(
                    entity_id=source,
                    relationship_id=rel.relationship_id,
                    relationship_type=rel.relationship_type,
                )
            )
            continue
        pair = (source, target) if source < target else (target, source)
        pair_relationship_ids[pair].append(rel.relationship_id)

    if unknown_endpoint_diagnostics:
        raise CanonicalProjectionError(
            "Canonical projection requires every relationship endpoint to "
            f"resolve to a known entity_id; found {len(unknown_endpoint_diagnostics)} "
            "relationship(s) with an unknown endpoint: "
            + ", ".join(
                f"{d.relationship_id} (source={d.source_entity_id!r} unknown="
                f"{d.unknown_source}, target={d.target_entity_id!r} unknown={d.unknown_target})"
                for d in unknown_endpoint_diagnostics
            )
        )

    edges = tuple(
        ProjectionEdge(
            source_entity_id=pair[0],
            target_entity_id=pair[1],
            weight=1.0,
            contributing_relationship_ids=tuple(sorted(rel_ids)),
        )
        for pair, rel_ids in sorted(pair_relationship_ids.items())
    )

    nodes_with_edges = {n for e in edges for n in (e.source_entity_id, e.target_entity_id)}
    isolate_ids = tuple(sorted(known_ids - nodes_with_edges))

    input_graph_hash = _compute_input_graph_hash(entities, relationships)

    return CanonicalProjection(
        node_ids=node_ids,
        edges=edges,
        self_loop_diagnostics=tuple(self_loop_diagnostics),
        duplicate_entity_diagnostics=duplicates,
        isolate_ids=isolate_ids,
        input_graph_hash=input_graph_hash,
    )


def _compute_input_graph_hash(
    entities: list[EntityRow], relationships: list[RelationshipRow]
) -> str:
    """Hash the complete original canonical-source snapshot.

    This binds the *execution fingerprint* to the full retained-source
    input: every original directed, typed relationship row (including
    self-loops and the id that distinguishes otherwise-identical pairs),
    not merely the post-projection undirected partition topology. Two
    inputs that project to the same undirected graph but differ in their
    original directed rows (different relationship ids, different self-loop
    rows, different per-row content) must — and do — hash differently here.

    Built over a canonically-encoded (sorted-key JSON, NFC-normalized)
    structured payload via ``contracts.base.canonical_sha256`` rather than a
    delimiter-joined string, so no field value can be crafted to produce an
    ambiguous/colliding payload. Entities and relationships are each sorted
    by their stable id before hashing, so the result is insensitive to
    input list order (required for the permutation-repeatability contract).
    """
    entity_payload = [
        {
            "entity_id": e.entity_id,
            "domain_hash": e.domain_hash,
            "content_hash": e.content_hash,
        }
        for e in sorted(entities, key=lambda e: e.entity_id)
    ]
    relationship_payload = [
        {
            "relationship_id": r.relationship_id,
            "relationship_type": r.relationship_type,
            "source_entity_id": r.source_entity_id,
            "target_entity_id": r.target_entity_id,
            "is_self_loop": r.source_entity_id == r.target_entity_id,
            "content_hash": r.content_hash,
        }
        for r in sorted(relationships, key=lambda r: r.relationship_id)
    ]
    return canonical_sha256({"entities": entity_payload, "relationships": relationship_payload})


def to_native_edge_list(projection: CanonicalProjection) -> list[tuple[str, str, float]]:
    """Canonical, sorted edge-tuple list for the native provider call.

    Isolates are intentionally excluded — a provider has no edges to
    partition them on; callers must handle ``projection.isolate_ids`` as
    explicit singleton terminals (see community_hierarchy_v2.py).
    """
    return [
        (edge.source_entity_id, edge.target_entity_id, edge.weight)
        for edge in sorted(
            projection.edges, key=lambda e: (e.source_entity_id, e.target_entity_id)
        )
    ]
