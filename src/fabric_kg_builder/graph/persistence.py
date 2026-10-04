"""GRP-003 (revised): Canonical graph extraction result / JSON persistence.

Fix #3:
- GraphExtractionResult: entity_occurrences, relationship_occurrences,
  merged_entities (occurrence_ids+descriptions), merged_relationships, claims, hierarchy
- to_json() / from_json() serialization
- MergedEntityRecord: entity_id, canonical_entity, occurrence_ids, descriptions, all_evidence_ids
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from pydantic import BaseModel, Field

from fabric_kg_builder.graph.occurrence import EntityOccurrence, RelationshipOccurrence


@dataclass
class MergedEntityRecord:
    entity_id: str
    display_name: str
    entity_type: str
    description: str
    occurrence_ids: list[str]
    descriptions: list[str]
    all_evidence_ids: list[str]
    aliases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "display_name": self.display_name,
            "entity_type": self.entity_type,
            "description": self.description,
            "occurrence_ids": self.occurrence_ids,
            "descriptions": self.descriptions,
            "all_evidence_ids": self.all_evidence_ids,
            "aliases": self.aliases,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MergedEntityRecord":
        return cls(
            entity_id=d["entity_id"],
            display_name=d["display_name"],
            entity_type=d["entity_type"],
            description=d.get("description", ""),
            occurrence_ids=d.get("occurrence_ids", []),
            descriptions=d.get("descriptions", []),
            all_evidence_ids=d.get("all_evidence_ids", []),
            aliases=d.get("aliases", []),
        )


@dataclass
class MergedRelationshipRecord:
    relationship_id: str
    source_entity_id: str
    target_entity_id: str
    relationship_type: str
    description: str
    occurrence_ids: list[str]
    descriptions: list[str]
    all_evidence_ids: list[str]

    def to_dict(self) -> dict:
        return {
            "relationship_id": self.relationship_id,
            "source_entity_id": self.source_entity_id,
            "target_entity_id": self.target_entity_id,
            "relationship_type": self.relationship_type,
            "description": self.description,
            "occurrence_ids": self.occurrence_ids,
            "descriptions": self.descriptions,
            "all_evidence_ids": self.all_evidence_ids,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MergedRelationshipRecord":
        return cls(
            relationship_id=d["relationship_id"],
            source_entity_id=d["source_entity_id"],
            target_entity_id=d["target_entity_id"],
            relationship_type=d["relationship_type"],
            description=d.get("description", ""),
            occurrence_ids=d.get("occurrence_ids", []),
            descriptions=d.get("descriptions", []),
            all_evidence_ids=d.get("all_evidence_ids", []),
        )


@dataclass
class GraphExtractionResult:
    """Complete result of graph extraction pipeline for a set of chunks."""
    source_id: str
    domain_hash: Optional[str]
    entity_occurrences: list[EntityOccurrence]
    relationship_occurrences: list[RelationshipOccurrence]
    merged_entities: list[MergedEntityRecord]
    merged_relationships: list[MergedRelationshipRecord]
    claims: list[dict]
    hierarchy_clusters: list[dict]
    hierarchy_memberships: list[dict]
    claim_evidence: list[dict] = field(default_factory=list)
    claim_contradictions: list[tuple[str, str]] = field(default_factory=list)
    extraction_version: str = "1.0"

    def to_json(self) -> str:
        def _occ_to_dict(o: EntityOccurrence | RelationshipOccurrence) -> dict:
            d = o.model_dump()
            # Convert datetime fields to ISO strings
            for k, v in d.items():
                if hasattr(v, "isoformat"):
                    d[k] = v.isoformat()
            return d

        return json.dumps(
            {
                "source_id": self.source_id,
                "domain_hash": self.domain_hash,
                "extraction_version": self.extraction_version,
                "entity_occurrences": [_occ_to_dict(o) for o in self.entity_occurrences],
                "relationship_occurrences": [_occ_to_dict(r) for r in self.relationship_occurrences],
                "merged_entities": [e.to_dict() for e in self.merged_entities],
                "merged_relationships": [r.to_dict() for r in self.merged_relationships],
                "claims": self.claims,
                "claim_evidence": self.claim_evidence,
                "claim_contradictions": self.claim_contradictions,
                "hierarchy_clusters": self.hierarchy_clusters,
                "hierarchy_memberships": self.hierarchy_memberships,
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, data: str) -> "GraphExtractionResult":
        obj = json.loads(data)
        entity_occurrences = [
            EntityOccurrence.model_validate(e)
            for e in obj.get("entity_occurrences", [])
        ]
        rel_occurrences = [
            RelationshipOccurrence.model_validate(r)
            for r in obj.get("relationship_occurrences", [])
        ]
        return cls(
            source_id=obj["source_id"],
            domain_hash=obj.get("domain_hash"),
            extraction_version=obj.get("extraction_version", "1.0"),
            entity_occurrences=entity_occurrences,
            relationship_occurrences=rel_occurrences,
            merged_entities=[MergedEntityRecord.from_dict(e) for e in obj.get("merged_entities", [])],
            merged_relationships=[MergedRelationshipRecord.from_dict(r) for r in obj.get("merged_relationships", [])],
            claims=obj.get("claims", []),
            claim_evidence=obj.get("claim_evidence", []),
            claim_contradictions=[
                tuple(pair) for pair in obj.get("claim_contradictions", [])
            ],
            hierarchy_clusters=obj.get("hierarchy_clusters", []),
            hierarchy_memberships=obj.get("hierarchy_memberships", []),
        )
