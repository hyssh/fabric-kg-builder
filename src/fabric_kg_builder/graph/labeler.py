"""GRP-009 (revised): Grounded community labels and summaries for all levels.

Fix #11:
- No filler prefixes (Group:, Root:)
- All levels use entity names/evidence from descendant members
- Parent clusters cite descendant evidence IDs
- Labels derived exclusively from member entity names/types/descriptions
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from fabric_kg_builder.model.schemas import ClusterMembershipRow, ClusterRow, EntityRow
from fabric_kg_builder.graph.summarizer import SummarizerProtocol, consolidate_description


def _member_entity_ids(cluster_id: str, memberships: list[ClusterMembershipRow]) -> list[str]:
    return [m.entity_id for m in memberships if m.cluster_id == cluster_id and m.entity_id]


def _descendant_entity_ids(
    cluster_id: str,
    cluster_map: dict[str, ClusterRow],
    memberships: list[ClusterMembershipRow],
) -> list[str]:
    """Gather all entity_ids from this cluster and all descendant clusters."""
    result: list[str] = _member_entity_ids(cluster_id, memberships)
    # Find child clusters (those whose parent_cluster_id == cluster_id)
    children = [c for c in cluster_map.values() if c.parent_cluster_id == cluster_id]
    for child in children:
        result.extend(_descendant_entity_ids(child.cluster_id, cluster_map, memberships))
    return result


def _collect_evidence_ids(
    entity_ids: list[str],
    memberships: list[ClusterMembershipRow],
    cluster_id: str,
) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for m in memberships:
        if m.cluster_id == cluster_id and m.entity_id in entity_ids:
            for eid in (m.evidence_ids or []):
                if eid not in seen:
                    seen.add(eid)
                    result.append(eid)
    return result


def label_communities(
    clusters: list[ClusterRow],
    entities: list[EntityRow],
    memberships: list[ClusterMembershipRow],
    *,
    summarizer: Optional[SummarizerProtocol] = None,
    max_summary_length: int = 300,
) -> list[ClusterRow]:
    """Assign grounded labels and summaries to each cluster for every level."""
    entity_map: dict[str, EntityRow] = {e.entity_id: e for e in entities}
    cluster_map: dict[str, ClusterRow] = {c.cluster_id: c for c in clusters}
    updated: list[ClusterRow] = []

    for cluster in clusters:
        # For leaf clusters: use direct members; for parent clusters: use descendants
        if cluster.level == 0:
            member_ids = _member_entity_ids(cluster.cluster_id, memberships)
        else:
            member_ids = _descendant_entity_ids(cluster.cluster_id, cluster_map, memberships)

        member_ids = list(dict.fromkeys(member_ids))  # deduplicate, preserve order
        member_entities = [entity_map[eid] for eid in member_ids if eid in entity_map]

        if not member_entities:
            updated.append(cluster)
            continue

        # Grounded label: top entity names sorted alphabetically + type annotation
        names = sorted(set(e.display_name for e in member_entities))
        type_summary = sorted(set(e.entity_type for e in member_entities))
        label = ", ".join(names[:4]) + ("…" if len(names) > 4 else "")
        if len(type_summary) == 1:
            label = f"{type_summary[0]}: {label}"

        # Grounded description from entity descriptions/names
        texts: list[str] = []
        for e in member_entities:
            if e.description and e.description != e.display_name:
                texts.append(e.description)
            else:
                texts.append(e.display_name)

        occurrence_ids = [eid for eid in member_ids]
        evidence_ids = _collect_evidence_ids(member_ids, memberships, cluster.cluster_id)

        description = consolidate_description(
            texts,
            summarizer=summarizer,
            occurrence_ids=occurrence_ids,
            evidence_ids=evidence_ids,
            max_length=max_summary_length,
        )

        updated_cluster = cluster.model_copy(
            update={"label": label, "description": description or cluster.description}
        )
        updated.append(updated_cluster)

    return updated
