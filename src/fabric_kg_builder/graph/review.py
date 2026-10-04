"""GRP-013 (revised): Versioned review export and decision replay.

Fix #12:
- ReplayResult: entities, canonical_id_map, merged_aliases, merged_descriptions,
  affected_occurrence_ids
- replay_decisions() returns ReplayResult, merges aliases+descriptions
- Idempotent — replaying same decisions on already-merged state produces identical result
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from fabric_kg_builder.model.schemas import EntityRow, EvidenceRow, RelationshipRow


@dataclass
class ResolutionDecision:
    entity_a_id: str
    entity_b_id: str
    decision: str  # "SAME" | "DIFFERENT" | "REVIEW"
    reviewer: str = "auto"
    reviewed_at: Optional[str] = None
    rationale: str = ""
    canonical_id: Optional[str] = None  # preferred canonical ID when SAME


@dataclass
class ReviewExport:
    """Versioned snapshot of resolution decisions for replay."""
    export_version: str
    exported_at: str
    decisions: list[ResolutionDecision]
    domain_hash: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "export_version": self.export_version,
                "exported_at": self.exported_at,
                "domain_hash": self.domain_hash,
                "decisions": [
                    {
                        "entity_a_id": d.entity_a_id,
                        "entity_b_id": d.entity_b_id,
                        "decision": d.decision,
                        "reviewer": d.reviewer,
                        "reviewed_at": d.reviewed_at,
                        "rationale": d.rationale,
                        "canonical_id": d.canonical_id,
                    }
                    for d in self.decisions
                ],
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, data: str) -> "ReviewExport":
        obj = json.loads(data)
        decisions = [
            ResolutionDecision(**d) for d in obj.get("decisions", [])
        ]
        return cls(
            export_version=obj["export_version"],
            exported_at=obj["exported_at"],
            decisions=decisions,
            domain_hash=obj.get("domain_hash"),
        )


def export_review(
    entities: list[EntityRow],
    candidates: list[ResolutionDecision],
    *,
    export_version: str = "1.0",
    domain_hash: Optional[str] = None,
) -> ReviewExport:
    return ReviewExport(
        export_version=export_version,
        exported_at=datetime.now(timezone.utc).isoformat(),
        decisions=candidates,
        domain_hash=domain_hash,
    )


@dataclass
class ReplayResult:
    """Result of replaying review decisions."""
    entities: list[EntityRow]
    canonical_id_map: dict[str, str]  # old_id → canonical_id
    merged_aliases: dict[str, list[str]]  # canonical_id → all aliases
    merged_descriptions: dict[str, list[str]]  # canonical_id → all descriptions
    affected_occurrence_ids: list[str]


def _merge_aliases(a: Optional[str], b: Optional[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in [a, b]:
        if not raw:
            continue
        try:
            items = json.loads(raw) if raw.startswith("[") else [raw]
        except Exception:
            items = [raw]
        for item in items:
            if item and item not in seen:
                seen.add(item)
                result.append(item)
    return result


def _get_aliases(entity: EntityRow) -> list[str]:
    if not entity.aliases:
        return []
    if isinstance(entity.aliases, list):
        return [str(a) for a in entity.aliases]
    # Fallback: try JSON parse if stored as string (legacy)
    try:
        loaded = json.loads(entity.aliases)  # type: ignore[arg-type]
        if isinstance(loaded, list):
            return [str(a) for a in loaded]
        return [str(loaded)]
    except Exception:
        return [str(entity.aliases)]


def replay_decisions(
    entities: list[EntityRow],
    decisions: list[ResolutionDecision],
) -> ReplayResult:
    """Apply SAME decisions, merging aliases/descriptions.

    Idempotent: if entities are already merged, re-running produces same result.
    """
    entity_map: dict[str, EntityRow] = {e.entity_id: e for e in entities}
    canonical_id_map: dict[str, str] = {}
    merged_aliases: dict[str, list[str]] = {}
    merged_descriptions: dict[str, list[str]] = {}
    affected_occurrence_ids: list[str] = []

    # Initialize canonical IDs as self-references
    for e in entities:
        canonical_id_map[e.entity_id] = e.entity_id
        merged_aliases[e.entity_id] = _get_aliases(e)
        merged_descriptions[e.entity_id] = [e.description] if e.description else []

    def _canonical(eid: str) -> str:
        while canonical_id_map.get(eid, eid) != eid:
            eid = canonical_id_map[eid]
        return eid

    for decision in decisions:
        if decision.decision != "SAME":
            continue
        can_a = _canonical(decision.entity_a_id)
        can_b = _canonical(decision.entity_b_id)
        if can_a == can_b:
            continue  # already merged — idempotent

        # Prefer explicit canonical_id if set, else prefer lower lexicographic entity_id
        if decision.canonical_id and decision.canonical_id in {can_a, can_b}:
            keep = decision.canonical_id
            drop = can_b if keep == can_a else can_a
        else:
            keep, drop = (can_a, can_b) if can_a <= can_b else (can_b, can_a)

        # Merge aliases
        keep_aliases = merged_aliases.get(keep, [])
        drop_aliases = merged_aliases.get(drop, [])
        seen: set[str] = set(keep_aliases)
        for a in drop_aliases:
            if a not in seen:
                seen.add(a)
                keep_aliases.append(a)
        # Also add the drop entity display_name as an alias on keep
        drop_entity = entity_map.get(drop)
        if drop_entity and drop_entity.display_name not in seen:
            keep_aliases.append(drop_entity.display_name)
        merged_aliases[keep] = keep_aliases

        # Merge descriptions
        keep_descs = merged_descriptions.get(keep, [])
        drop_descs = merged_descriptions.get(drop, [])
        seen_desc: set[str] = set(keep_descs)
        for d in drop_descs:
            if d not in seen_desc:
                seen_desc.add(d)
                keep_descs.append(d)
        merged_descriptions[keep] = keep_descs

        # Update canonical map
        canonical_id_map[drop] = keep
        affected_occurrence_ids.append(drop)

    # Build final entity list: one entry per canonical ID, with merged data
    canonical_entities: dict[str, EntityRow] = {}
    for eid, entity in entity_map.items():
        can = _canonical(eid)
        if can not in canonical_entities:
            canonical_entities[can] = entity_map[can]

    # Update aliases on merged entities
    result_entities: list[EntityRow] = []
    for can_id, entity in canonical_entities.items():
        aliases = merged_aliases.get(can_id, [])
        all_descriptions = merged_descriptions.get(can_id, [])
        canonical_desc = all_descriptions[0] if all_descriptions else entity.description
        updated = entity.model_copy(update={
            "aliases": aliases if aliases else entity.aliases,
            "description": canonical_desc,
        })
        result_entities.append(updated)

    # Remove duplicates from canonical_id_map (pointing to self)
    final_map = {k: v for k, v in canonical_id_map.items() if v != k}

    return ReplayResult(
        entities=result_entities,
        canonical_id_map=final_map,
        merged_aliases={k: v for k, v in merged_aliases.items() if k in canonical_entities},
        merged_descriptions={k: v for k, v in merged_descriptions.items() if k in canonical_entities},
        affected_occurrence_ids=list(dict.fromkeys(affected_occurrence_ids)),
    )
