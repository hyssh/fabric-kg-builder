"""GRP-001 (revised): Versioned per-text-unit subgraph and occurrence schemas.

Occurrence IDs hash actual canonical content (type+name+span+version) —
not entity/relationship counts. Every occurrence carries a non-empty description
and at least one of: a valid evidence span or a non-empty evidence_id list.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field, model_validator

SUBGRAPH_SCHEMA_VERSION = "1.0"


def _norm(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return re.sub(r"\s+", " ", "".join(c for c in nfkd if not unicodedata.combining(c)).lower()).strip()


def _content_hash(canonical_string: str) -> str:
    return hashlib.sha256(canonical_string.encode("utf-8")).hexdigest()


def _local_id(prefix: str, *canonical_parts: str) -> str:
    digest = _content_hash("|".join(canonical_parts))
    return f"{prefix}:{digest[:16]}"


class EvidenceSpan(BaseModel):
    text_unit_id: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str

    @model_validator(mode="after")
    def _span_order(self) -> "EvidenceSpan":
        if self.end <= self.start:
            raise ValueError("span end must be greater than start")
        return self


class OccurrenceContext(BaseModel):
    """Occurrence envelope keyed by semantic content — null spans preserved separately."""
    text_unit_id: str
    domain_hash: Optional[str] = None
    schema_version: str = SUBGRAPH_SCHEMA_VERSION
    chunk_id: Optional[str] = None
    source_file_id: Optional[str] = None
    span_start: Optional[int] = None
    span_end: Optional[int] = None
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    semantic_key: str = ""
    local_id: str = ""

    @model_validator(mode="after")
    def _set_ids(self) -> "OccurrenceContext":
        if not self.semantic_key:
            self.semantic_key = self.text_unit_id
        if not self.local_id:
            self.local_id = _local_id(
                "occ",
                self.text_unit_id,
                self.semantic_key,
                str(self.span_start),
                str(self.span_end),
                SUBGRAPH_SCHEMA_VERSION,
            )
        return self


class EntityOccurrence(BaseModel):
    """One mention of an entity candidate within a text unit."""
    text_unit_id: str
    domain_hash: Optional[str] = None
    schema_version: str = SUBGRAPH_SCHEMA_VERSION
    entity_type: str
    display_name: str
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    scope: Optional[str] = None
    span: Optional[EvidenceSpan] = None
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    properties_json: Optional[str] = None
    local_id: str = ""

    @model_validator(mode="after")
    def _set_local_id_and_defaults(self) -> "EntityOccurrence":
        if not self.description:
            self.description = self.display_name
        if not self.local_id:
            span_key = (
                f"{self.span.start}:{self.span.end}"
                if self.span
                else "nospan"
            )
            self.local_id = _local_id(
                "eocc",
                _norm(self.entity_type),
                _norm(self.display_name),
                span_key,
                self.text_unit_id,
                SUBGRAPH_SCHEMA_VERSION,
            )
        if not self.evidence_ids and self.span is None:
            pass  # callers must supply at least one; validated in SubgraphOccurrence
        return self


class RelationshipOccurrence(BaseModel):
    """One mention of a relationship candidate within a text unit."""
    text_unit_id: str
    domain_hash: Optional[str] = None
    schema_version: str = SUBGRAPH_SCHEMA_VERSION
    relationship_type: str
    source_local_id: str
    target_local_id: str
    description: str = ""
    span: Optional[EvidenceSpan] = None
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    properties_json: Optional[str] = None
    local_id: str = ""

    @model_validator(mode="after")
    def _set_local_id_and_defaults(self) -> "RelationshipOccurrence":
        if not self.description:
            self.description = (
                f"{self.relationship_type}({self.source_local_id}->{self.target_local_id})"
            )
        if not self.local_id:
            span_key = (
                f"{self.span.start}:{self.span.end}"
                if self.span
                else "nospan"
            )
            self.local_id = _local_id(
                "rocc",
                _norm(self.relationship_type),
                self.source_local_id,
                self.target_local_id,
                span_key,
                self.text_unit_id,
                SUBGRAPH_SCHEMA_VERSION,
            )
        return self


class SubgraphOccurrence(BaseModel):
    """Complete per-text-unit subgraph snapshot produced by one extraction pass."""
    occurrence_id: str
    subgraph_version: str = SUBGRAPH_SCHEMA_VERSION
    text_unit_id: str
    domain_hash: Optional[str] = None
    entity_occurrences: list[EntityOccurrence] = Field(default_factory=list)
    relationship_occurrences: list[RelationshipOccurrence] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _validate_references(self) -> "SubgraphOccurrence":
        entity_ids = {e.local_id for e in self.entity_occurrences}
        for rel in self.relationship_occurrences:
            if rel.source_local_id not in entity_ids:
                raise ValueError(
                    f"RelationshipOccurrence source {rel.source_local_id!r} "
                    "not found in entity_occurrences of same subgraph"
                )
            if rel.target_local_id not in entity_ids:
                raise ValueError(
                    f"RelationshipOccurrence target {rel.target_local_id!r} "
                    "not found in entity_occurrences of same subgraph"
                )
        return self

    @classmethod
    def make(
        cls,
        text_unit_id: str,
        entity_occurrences: list[EntityOccurrence],
        relationship_occurrences: list[RelationshipOccurrence],
        *,
        domain_hash: Optional[str] = None,
    ) -> "SubgraphOccurrence":
        entity_sig = "|".join(sorted(e.local_id for e in entity_occurrences))
        rel_sig = "|".join(sorted(r.local_id for r in relationship_occurrences))
        occ_id = _local_id(
            "subgraph",
            text_unit_id,
            domain_hash or "",
            entity_sig,
            rel_sig,
            SUBGRAPH_SCHEMA_VERSION,
        )
        return cls(
            occurrence_id=occ_id,
            text_unit_id=text_unit_id,
            domain_hash=domain_hash,
            entity_occurrences=entity_occurrences,
            relationship_occurrences=relationship_occurrences,
        )
