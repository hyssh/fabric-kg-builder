"""GRP-001 Fix #1: LLM subgraph extraction contract.

System/user prompt boundary — approved domain context in system role,
untrusted source text as user role. Pydantic-validates all local IDs,
source/target refs, spans against source text length, descriptions,
confidence, and evidence.
"""
from __future__ import annotations

import json
from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field, model_validator

from fabric_kg_builder.graph.occurrence import (
    EntityOccurrence,
    EvidenceSpan,
    RelationshipOccurrence,
    SubgraphOccurrence,
    SUBGRAPH_SCHEMA_VERSION,
)


# ---------------------------------------------------------------------------
# Request / context
# ---------------------------------------------------------------------------


class SubgraphExtractionRequest(BaseModel):
    """All context needed for one extraction call. Source text is untrusted."""

    text_unit_id: str
    source_text: str
    domain_summary: str
    domain_hash: Optional[str] = None
    competency_questions: list[str] = Field(default_factory=list)
    allowed_entity_types: list[str] = Field(default_factory=list)
    allowed_relationship_types: list[str] = Field(default_factory=list)
    observed_types: list[str] = Field(default_factory=list)
    source_locator_json: Optional[str] = None


# ---------------------------------------------------------------------------
# LLM response schema (Pydantic-validated before use)
# ---------------------------------------------------------------------------


class _LLMEntityOccurrence(BaseModel):
    local_id: str = Field(min_length=1, max_length=64)
    entity_type: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    description: str = Field(default="", min_length=0)
    aliases: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    span_start: Optional[int] = Field(default=None, ge=0)
    span_end: Optional[int] = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_span(self) -> "_LLMEntityOccurrence":
        if self.span_start is not None and self.span_end is not None:
            if self.span_end <= self.span_start:
                raise ValueError("span_end must be > span_start")
        elif (self.span_start is None) != (self.span_end is None):
            raise ValueError("span_start and span_end must both be set or both be null")
        return self


class _LLMRelationshipOccurrence(BaseModel):
    local_id: str = Field(min_length=1, max_length=64)
    relationship_type: str = Field(min_length=1)
    source_local_id: str = Field(min_length=1)
    target_local_id: str = Field(min_length=1)
    description: str = Field(default="")
    confidence: float = Field(ge=0.0, le=1.0)
    span_start: Optional[int] = Field(default=None, ge=0)
    span_end: Optional[int] = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_span(self) -> "_LLMRelationshipOccurrence":
        if self.span_start is not None and self.span_end is not None:
            if self.span_end <= self.span_start:
                raise ValueError("span_end must be > span_start")
        return self


class _LLMExtractionResponse(BaseModel):
    entity_occurrences: list[_LLMEntityOccurrence] = Field(default_factory=list)
    relationship_occurrences: list[_LLMRelationshipOccurrence] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_refs_and_ids(self) -> "_LLMExtractionResponse":
        entity_ids = {e.local_id for e in self.entity_occurrences}
        # Unique entity local_ids
        if len(entity_ids) != len(self.entity_occurrences):
            raise ValueError("Duplicate entity local_ids in LLM extraction response")
        for rel in self.relationship_occurrences:
            if rel.source_local_id not in entity_ids:
                raise ValueError(
                    f"Relationship source {rel.source_local_id!r} not in entity_occurrences"
                )
            if rel.target_local_id not in entity_ids:
                raise ValueError(
                    f"Relationship target {rel.target_local_id!r} not in entity_occurrences"
                )
        rel_ids = {r.local_id for r in self.relationship_occurrences}
        if len(rel_ids) != len(self.relationship_occurrences):
            raise ValueError("Duplicate relationship local_ids in LLM extraction response")
        return self


def _validate_spans_in_text(
    response: _LLMExtractionResponse,
    source_text: str,
) -> None:
    text_len = len(source_text)
    for e in response.entity_occurrences:
        if e.span_end is not None and e.span_end > text_len:
            raise ValueError(
                f"Entity {e.local_id!r} span_end {e.span_end} exceeds source text length {text_len}"
            )
    for r in response.relationship_occurrences:
        if r.span_end is not None and r.span_end > text_len:
            raise ValueError(
                f"Relationship {r.local_id!r} span_end {r.span_end} exceeds source text length {text_len}"
            )


# ---------------------------------------------------------------------------
# System prompt (domain context — not untrusted data)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_TEMPLATE = """\
You are a knowledge graph extraction engine.
Extract entity and relationship occurrences from the provided source text.

DOMAIN CONTEXT (approved, not from user input):
{domain_summary}

COMPETENCY QUESTIONS:
{competency_questions}

ALLOWED ENTITY TYPES: {allowed_entity_types}
ALLOWED RELATIONSHIP TYPES: {allowed_relationship_types}

RULES:
1. Only extract facts explicitly stated in the source text.
2. Every entity local_id must be a short alphanumeric string unique within this response.
3. Every relationship must reference entity local_ids present in this response.
4. Provide span_start/span_end (character offsets) when the text is directly quoted.
5. Descriptions must be concise and grounded in the source text.
6. Output ONLY valid JSON matching the schema:
   {{
     "entity_occurrences": [...],
     "relationship_occurrences": [...]
   }}
"""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ExtractionProtocol(Protocol):
    def extract(self, request: SubgraphExtractionRequest) -> SubgraphOccurrence:
        ...


# ---------------------------------------------------------------------------
# LLM extractor
# ---------------------------------------------------------------------------


class LLMExtractionClient:
    """Wraps an injected LLM client; validates all output before use."""

    def __init__(self, client: object, *, model: str = "gpt-4o") -> None:
        self._client = client
        self._model = model

    def extract(self, request: SubgraphExtractionRequest) -> SubgraphOccurrence:
        system_content = _SYSTEM_PROMPT_TEMPLATE.format(
            domain_summary=request.domain_summary,
            competency_questions="\n".join(
                f"- {q}" for q in request.competency_questions
            ) or "None specified",
            allowed_entity_types=", ".join(request.allowed_entity_types) or "Any",
            allowed_relationship_types=", ".join(request.allowed_relationship_types) or "Any",
        )
        # Source text goes in user role (untrusted)
        user_content = json.dumps(
            {
                "text_unit_id": request.text_unit_id,
                "source_text": request.source_text,
                "source_locator": request.source_locator_json,
            },
            ensure_ascii=False,
        )
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content
        validated = _LLMExtractionResponse.model_validate_json(raw)
        _validate_spans_in_text(validated, request.source_text)
        return _to_subgraph(validated, request)


def _to_subgraph(
    response: _LLMExtractionResponse,
    request: SubgraphExtractionRequest,
) -> SubgraphOccurrence:
    entity_occs: list[EntityOccurrence] = []
    for e in response.entity_occurrences:
        span = None
        if e.span_start is not None and e.span_end is not None:
            span = EvidenceSpan(
                text_unit_id=request.text_unit_id,
                start=e.span_start,
                end=e.span_end,
                text=request.source_text[e.span_start : e.span_end],
            )
        entity_occs.append(
            EntityOccurrence(
                local_id=e.local_id,
                text_unit_id=request.text_unit_id,
                domain_hash=request.domain_hash,
                entity_type=e.entity_type,
                display_name=e.display_name,
                description=e.description or e.display_name,
                aliases=e.aliases,
                span=span,
                confidence=e.confidence,
            )
        )
    rel_occs: list[RelationshipOccurrence] = []
    for r in response.relationship_occurrences:
        span = None
        if r.span_start is not None and r.span_end is not None:
            span = EvidenceSpan(
                text_unit_id=request.text_unit_id,
                start=r.span_start,
                end=r.span_end,
                text=request.source_text[r.span_start : r.span_end],
            )
        rel_occs.append(
            RelationshipOccurrence(
                local_id=r.local_id,
                text_unit_id=request.text_unit_id,
                domain_hash=request.domain_hash,
                relationship_type=r.relationship_type,
                source_local_id=r.source_local_id,
                target_local_id=r.target_local_id,
                description=r.description,
                span=span,
                confidence=r.confidence,
            )
        )
    return SubgraphOccurrence.make(
        request.text_unit_id,
        entity_occs,
        rel_occs,
        domain_hash=request.domain_hash,
    )
