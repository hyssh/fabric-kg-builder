"""GRP-010 (revised): Graph validation.

Fix #10:
- validate_graph: cycle checks apply ONLY to configured hierarchical_relation_types
  (default: empty → natural graph cycles are allowed / not flagged)
- validate_hierarchy: dedicated function; checks parent cycles, invalid levels,
  orphan parents, coverage and evidence; VAL-038 gate
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from fabric_kg_builder.model.schemas import ClusterMembershipRow, ClusterRow, EntityRow, RelationshipRow

VAL_038 = "VAL-038"

# Default hierarchical relation types that should NOT form cycles
_DEFAULT_HIERARCHICAL_TYPES = frozenset({
    "parent_of", "part_of", "contains", "subsumes", "inherits_from", "derived_from",
})


@dataclass
class GraphValidationResult:
    has_cycles: bool = False
    cycle_examples: list[list[str]] = field(default_factory=list)
    coherence_issues: list[str] = field(default_factory=list)
    coverage_gaps: list[str] = field(default_factory=list)
    passed: bool = True

    def _recompute_passed(self) -> None:
        self.passed = not self.has_cycles and not self.coherence_issues


@dataclass
class HierarchyValidationResult:
    """VAL-038: hierarchy-specific validation."""
    gate: str = VAL_038
    parent_cycle_ids: list[str] = field(default_factory=list)
    invalid_level_ids: list[str] = field(default_factory=list)
    orphan_parent_ids: list[str] = field(default_factory=list)
    uncovered_entity_ids: list[str] = field(default_factory=list)
    empty_cluster_ids: list[str] = field(default_factory=list)
    membership_issues: list[str] = field(default_factory=list)
    passed: bool = True
    block_publication: bool = False

    def _recompute(self) -> None:
        critical = bool(
            self.parent_cycle_ids
            or self.invalid_level_ids
            or self.orphan_parent_ids
        )
        self.passed = not (
            critical
            or self.uncovered_entity_ids
            or self.membership_issues
        )
        self.block_publication = critical


_WHITE, _GREY, _BLACK = 0, 1, 2


def _find_directed_cycles(
    nodes: list[str],
    adj: dict[str, list[str]],
    *,
    max_examples: int = 3,
) -> list[list[str]]:
    colour = {n: _WHITE for n in nodes}
    path: list[str] = []
    cycles: list[list[str]] = []

    def dfs(node: str) -> None:
        if len(cycles) >= max_examples:
            return
        colour[node] = _GREY
        path.append(node)
        for neighbor in adj.get(node, []):
            if len(cycles) >= max_examples:
                break
            if colour.get(neighbor) == _GREY:
                idx = path.index(neighbor)
                cycles.append(path[idx:] + [neighbor])
            elif colour.get(neighbor) == _WHITE:
                dfs(neighbor)
        path.pop()
        colour[node] = _BLACK

    for node in nodes:
        if colour[node] == _WHITE:
            dfs(node)
    return cycles


def validate_graph(
    entities: list[EntityRow],
    relationships: list[RelationshipRow],
    *,
    expected_types: Optional[list[str]] = None,
    allow_self_loops: bool = False,
    hierarchical_relation_types: Optional[frozenset[str]] = None,
) -> GraphValidationResult:
    """Validate graph structure.

    Cycles are ONLY flagged for hierarchical_relation_types.
    Natural graph cycles (e.g. A->B->C->A for non-hierarchical edges)
    are allowed by default and never flagged.
    """
    if hierarchical_relation_types is None:
        hierarchical_relation_types = frozenset()  # no cycle checking by default

    entity_ids = {e.entity_id for e in entities}
    coherence_issues: list[str] = []
    coverage_gaps: list[str] = []

    # Build adjacency only for hierarchical relations
    hier_adj: dict[str, list[str]] = {eid: [] for eid in entity_ids}
    rel_sources: set[str] = set()
    rel_targets: set[str] = set()

    for rel in relationships:
        src, tgt = rel.source_entity_id, rel.target_entity_id
        if src not in entity_ids:
            coherence_issues.append(
                f"Dangling source entity {src!r} in relationship {rel.relationship_id!r}"
            )
        if tgt not in entity_ids:
            coherence_issues.append(
                f"Dangling target entity {tgt!r} in relationship {rel.relationship_id!r}"
            )
        if src == tgt and not allow_self_loops:
            coherence_issues.append(
                f"Self-loop on entity {src!r} via relationship {rel.relationship_id!r}"
            )
        if src in entity_ids:
            rel_sources.add(src)
            if rel.relationship_type in hierarchical_relation_types:
                hier_adj[src].append(tgt)
        if tgt in entity_ids:
            rel_targets.add(tgt)

    # Cycle detection only on hierarchical edges
    cycle_examples: list[list[str]] = []
    if hierarchical_relation_types:
        cycle_examples = _find_directed_cycles(list(entity_ids), hier_adj)

    connected = rel_sources | rel_targets
    isolated = [e.entity_id for e in entities if e.entity_id not in connected]
    if isolated:
        coverage_gaps.append(
            f"{len(isolated)} isolated entities: "
            + ", ".join(sorted(isolated)[:5])
            + ("…" if len(isolated) > 5 else "")
        )

    if expected_types:
        present = {e.entity_type for e in entities}
        for et in expected_types:
            if et not in present:
                coverage_gaps.append(f"Expected entity type {et!r} not found")

    result = GraphValidationResult(
        has_cycles=bool(cycle_examples),
        cycle_examples=cycle_examples,
        coherence_issues=coherence_issues,
        coverage_gaps=coverage_gaps,
    )
    result._recompute_passed()
    return result


def validate_hierarchy(
    clusters: list[ClusterRow],
    memberships: list[ClusterMembershipRow],
    entities: list[EntityRow],
    *,
    expected_levels: int = 3,
) -> HierarchyValidationResult:
    """VAL-038: Validate community hierarchy structure and coverage."""
    result = HierarchyValidationResult()
    cluster_map = {c.cluster_id: c for c in clusters}
    entity_ids = {e.entity_id for e in entities}

    # Check parent_cluster_id references exist
    for cluster in clusters:
        if cluster.parent_cluster_id and cluster.parent_cluster_id not in cluster_map:
            result.orphan_parent_ids.append(cluster.cluster_id)

    # Check valid level values
    max_level = expected_levels - 1
    for cluster in clusters:
        if cluster.level < 0 or cluster.level > max_level:
            result.invalid_level_ids.append(cluster.cluster_id)

    # Detect parent cycles in cluster hierarchy
    _WHITE_C, _GREY_C, _DONE_C = 0, 1, 2
    colour_c: dict[str, int] = {c.cluster_id: _WHITE_C for c in clusters}
    path: list[str] = []
    path_indexes: dict[str, int] = {}
    cycle_ids: set[str] = set()

    def _check_cycle(cid: str) -> None:
        colour_c[cid] = _GREY_C
        path_indexes[cid] = len(path)
        path.append(cid)
        c = cluster_map[cid]
        if c.parent_cluster_id and c.parent_cluster_id in colour_c:
            parent_id = c.parent_cluster_id
            if colour_c[parent_id] == _WHITE_C:
                _check_cycle(parent_id)
            elif colour_c[parent_id] == _GREY_C:
                cycle_ids.update(path[path_indexes[parent_id]:])
        path.pop()
        path_indexes.pop(cid)
        colour_c[cid] = _DONE_C

    for cid in list(cluster_map.keys()):
        if colour_c[cid] == _WHITE_C:
            _check_cycle(cid)
    result.parent_cycle_ids = sorted(cycle_ids)

    # Coverage: all entities must appear in at least one cluster membership
    covered_entities = {m.entity_id for m in memberships if m.entity_id is not None}
    uncovered = entity_ids - covered_entities
    result.uncovered_entity_ids = list(uncovered)

    # Check memberships: each must have exactly one of entity_id/relationship_id/claim_id set
    for m in memberships:
        targets = sum([
            1 if m.entity_id else 0,
            1 if m.relationship_id else 0,
            1 if m.claim_id else 0,
        ])
        if targets == 0:
            result.membership_issues.append(
                f"ClusterMembershipRow for cluster {m.cluster_id!r} has all-null targets"
            )
        elif targets > 1:
            result.membership_issues.append(
                f"ClusterMembershipRow for cluster {m.cluster_id!r} has multiple targets set"
            )
        if not m.rationale:
            result.membership_issues.append(
                f"ClusterMembershipRow for cluster {m.cluster_id!r} "
                f"(entity={m.entity_id!r}) missing rationale"
            )

    # Empty clusters
    cluster_member_counts: dict[str, int] = {c.cluster_id: 0 for c in clusters}
    for m in memberships:
        if m.cluster_id in cluster_member_counts:
            cluster_member_counts[m.cluster_id] += 1
    for cid, count in cluster_member_counts.items():
        cluster = cluster_map[cid]
        if count == 0 and cluster.level == 0:  # only leaf clusters must have members
            result.empty_cluster_ids.append(cid)

    result._recompute()
    return result
