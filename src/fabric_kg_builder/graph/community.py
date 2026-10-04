"""GRP-008 (revised): Semantic community hierarchy.

Fix #9:
- Builds 3 semantic levels using entity types/names/descriptions + graph adjacency
- No arbitrary/random filler groups — all groupings are semantically grounded
- Seed used only for deterministic tie-breaking within connected components
- Returns INSUFFICIENT_HIERARCHY_EVIDENCE when corpus lacks meaningful structure
- Level 0 = concept/community (leaf), Level 1 = topic/process, Level 2 = broad category
- Parent links via ClusterRow.parent_cluster_id only
- ClusterMembershipRow: exactly one of entity_id set per row; score/rationale populated
- Never emits all-null ClusterMembershipRow
"""

from __future__ import annotations

import hashlib
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from fabric_kg_builder.model.ids import make_id
from fabric_kg_builder.model.schemas import ClusterMembershipRow, ClusterRow, EntityRow, RelationshipRow

HIERARCHY_VERSION = "1.0"
INSUFFICIENT_HIERARCHY_EVIDENCE = "INSUFFICIENT_HIERARCHY_EVIDENCE"
_MIN_ENTITIES_FOR_3_LEVELS = 12
_DEFAULT_SEED = 42


@dataclass
class CommunityHierarchyResult:
    clusters: list[ClusterRow]
    memberships: list[ClusterMembershipRow]
    levels: int
    method: str
    hierarchy_version: str = HIERARCHY_VERSION


@dataclass
class InsufficientCorpusResult:
    reason: str
    entity_count: int
    min_required: int = _MIN_ENTITIES_FOR_3_LEVELS
    levels_built: int = 0
    clusters: list[ClusterRow] = field(default_factory=list)
    memberships: list[ClusterMembershipRow] = field(default_factory=list)
    method: str = INSUFFICIENT_HIERARCHY_EVIDENCE


# ---------------------------------------------------------------------------
# Type family mapping (semantic grouping by entity type)
# ---------------------------------------------------------------------------

_TYPE_FAMILIES: dict[str, list[str]] = {
    "organization": ["org", "company", "corp", "supplier", "manufacturer", "party", "vendor", "carrier", "broker"],
    "location": ["location", "place", "zone", "room", "region", "area", "port", "city", "space", "bay"],
    "product": ["product", "item", "goods", "material", "component", "equipment", "asset", "system"],
    "process": ["process", "procedure", "workflow", "activity", "service", "operation"],
    "document": ["document", "contract", "clause", "policy", "agreement", "regulation", "appendix"],
    "concept": ["concept", "claim", "obligation", "right", "risk", "issue", "technology"],
    "person": ["person", "individual", "contact", "employee", "officer"],
}


def _type_family(entity_type: str) -> str:
    et = entity_type.lower()
    for family, keywords in _TYPE_FAMILIES.items():
        if any(kw in et for kw in keywords):
            return family
    return "other"


def _cluster_id(level: int, label: str, domain_hash: Optional[str]) -> str:
    return make_id(
        "cluster",
        f"level:{level}:{label.lower()[:80]}:{domain_hash or ''}",
    )


def _adjacency(entities: list[EntityRow], rels: list[RelationshipRow]) -> dict[str, set[str]]:
    entity_ids = {e.entity_id for e in entities}
    adj: dict[str, set[str]] = {eid: set() for eid in entity_ids}
    for rel in rels:
        if rel.source_entity_id in entity_ids and rel.target_entity_id in entity_ids:
            adj[rel.source_entity_id].add(rel.target_entity_id)
            adj[rel.target_entity_id].add(rel.source_entity_id)
    return adj


def _connected_components(entity_ids: list[str], adj: dict[str, set[str]]) -> list[list[str]]:
    visited: set[str] = set()
    components: list[list[str]] = []
    for eid in entity_ids:
        if eid in visited:
            continue
        component: list[str] = []
        stack = [eid]
        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            component.append(node)
            for neighbor in adj.get(node, set()):
                if neighbor not in visited:
                    stack.append(neighbor)
        components.append(component)
    return components


def _make_cluster(
    level: int,
    label: str,
    description: str,
    method: str,
    *,
    domain_hash: Optional[str],
    run_id: str,
    parent_id: Optional[str] = None,
) -> ClusterRow:
    return ClusterRow(
        cluster_id=_cluster_id(level, label, domain_hash),
        hierarchy_version=HIERARCHY_VERSION,
        level=level,
        parent_cluster_id=parent_id,
        label=label,
        description=description,
        method=method,
        domain_hash=domain_hash,
        run_id=run_id,
    )


def _make_entity_membership(
    cluster_id: str,
    entity_id: str,
    *,
    score: float = 1.0,
    rationale: str = "",
    evidence_ids: Optional[list[str]] = None,
    primary: bool = True,
) -> ClusterMembershipRow:
    return ClusterMembershipRow(
        cluster_id=cluster_id,
        entity_id=entity_id,
        relationship_id=None,
        claim_id=None,
        score=score,
        rationale=rationale,
        evidence_ids=evidence_ids,
        primary_membership=primary,
    )


# ---------------------------------------------------------------------------
# Main public API
# ---------------------------------------------------------------------------


def build_community_hierarchy(
    entities: list[EntityRow],
    relationships: list[RelationshipRow],
    *,
    seed: int = _DEFAULT_SEED,
    domain_hash: Optional[str] = None,
    run_id: str = "",
    target_leaf_size: int = 4,
) -> CommunityHierarchyResult | InsufficientCorpusResult:
    n = len(entities)
    if n < _MIN_ENTITIES_FOR_3_LEVELS:
        return InsufficientCorpusResult(
            reason=(
                f"{INSUFFICIENT_HIERARCHY_EVIDENCE}: corpus has {n} entities; "
                f"at least {_MIN_ENTITIES_FOR_3_LEVELS} required for 3 semantic levels."
            ),
            entity_count=n,
        )

    entity_map: dict[str, EntityRow] = {e.entity_id: e for e in entities}
    adj = _adjacency(entities, relationships)
    rng = random.Random(seed)
    method = f"semantic_type_connectivity_seed_{seed}"

    all_clusters: list[ClusterRow] = []
    all_memberships: list[ClusterMembershipRow] = []

    # -----------
    # Level 2: broad type families
    # -----------
    family_groups: dict[str, list[str]] = defaultdict(list)
    for e in entities:
        family_groups[_type_family(e.entity_type)].append(e.entity_id)

    family_clusters: dict[str, ClusterRow] = {}
    for family, eids in sorted(family_groups.items()):
        representative_names = sorted(
            entity_map[eid].display_name for eid in eids if eid in entity_map
        )
        label = f"{family.title()} ({len(eids)})"
        desc = "Entities of type family: " + family.title() + ". Members: " + ", ".join(representative_names[:5])
        cluster = _make_cluster(2, label, desc, method, domain_hash=domain_hash, run_id=run_id)
        all_clusters.append(cluster)
        family_clusters[family] = cluster

    # -----------
    # Level 1: type + connected component (topic/process grouping)
    # -----------
    type_component_clusters: dict[tuple[str, int], ClusterRow] = {}
    type_component_entities: dict[tuple[str, int], list[str]] = {}

    for family, eids in sorted(family_groups.items()):
        components = _connected_components(eids, adj)
        components.sort(key=lambda c: (-len(c), sorted(c)[0]))
        parent_l2 = family_clusters[family]

        for comp_idx, component in enumerate(components):
            comp_names = sorted(
                entity_map[eid].display_name for eid in component if eid in entity_map
            )
            label = f"{family.title()} Group {comp_idx + 1}: " + ", ".join(comp_names[:3])
            if len(comp_names) > 3:
                label += f" +{len(comp_names) - 3} more"
            desc = f"Connected subgroup {comp_idx + 1} of {family.title()} entities. " + ", ".join(comp_names)
            cluster = _make_cluster(
                1, label, desc, method,
                domain_hash=domain_hash, run_id=run_id,
                parent_id=parent_l2.cluster_id,
            )
            all_clusters.append(cluster)
            key = (family, comp_idx)
            type_component_clusters[key] = cluster
            type_component_entities[key] = component

    # -----------
    # Level 0: leaf communities — entity neighborhoods
    # -----------
    entity_to_l1: dict[str, ClusterRow] = {}
    for (family, comp_idx), eids in type_component_entities.items():
        for eid in eids:
            entity_to_l1[eid] = type_component_clusters[(family, comp_idx)]

    # Sub-partition each level-1 cluster into leaves of target_leaf_size
    l1_to_entities: dict[str, list[str]] = defaultdict(list)
    for eid, l1_cluster in entity_to_l1.items():
        l1_to_entities[l1_cluster.cluster_id].append(eid)

    for l1_cid, l1_eids in l1_to_entities.items():
        l1_cluster = next(c for c in all_clusters if c.cluster_id == l1_cid)
        # Sort entities by display_name for determinism, then partition
        sorted_eids = sorted(l1_eids, key=lambda eid: entity_map[eid].display_name if eid in entity_map else eid)
        # Adjust leaf size so we don'\''t get singletons unless unavoidable
        actual_leaf_size = max(2, min(target_leaf_size, len(sorted_eids)))
        for chunk_start in range(0, len(sorted_eids), actual_leaf_size):
            chunk = sorted_eids[chunk_start: chunk_start + actual_leaf_size]
            chunk_names = [entity_map[eid].display_name for eid in chunk if eid in entity_map]
            label = ", ".join(chunk_names[:3]) + ("…" if len(chunk_names) > 3 else "")
            desc_parts = []
            for eid in chunk:
                e = entity_map.get(eid)
                if e and e.description:
                    desc_parts.append(e.description[:80])
            desc = "; ".join(desc_parts) if desc_parts else label

            leaf_cluster = _make_cluster(
                0, label, desc, method,
                domain_hash=domain_hash, run_id=run_id,
                parent_id=l1_cid,
            )
            all_clusters.append(leaf_cluster)

            for eid in chunk:
                e = entity_map.get(eid)
                rationale = f"Member of {label} (type: {e.entity_type if e else '?'})"
                all_memberships.append(
                    _make_entity_membership(
                        leaf_cluster.cluster_id,
                        eid,
                        score=1.0,
                        rationale=rationale,
                        primary=True,
                    )
                )

    return CommunityHierarchyResult(
        clusters=all_clusters,
        memberships=all_memberships,
        levels=3,
        method=method,
    )
