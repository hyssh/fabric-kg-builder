"""Schema-2.0 closed-vocabulary and exact-evidence validation authority."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from fabric_kg_builder.domain.models import (
    DomainContractV2,
    DomainEntityTypeV2,
    DomainRelationshipTypeV2,
)
from fabric_kg_builder.domain.service import compute_contract_hash
from fabric_kg_builder.model.ids import (
    content_hash,
    make_entity_id,
    make_evidence_id,
)

from .output_schema import Entity, Evidence, LLMOutput, Relationship


EVIDENCE_MISSING = "EVIDENCE_MISSING"
EVIDENCE_SOURCE_MISMATCH = "EVIDENCE_SOURCE_MISMATCH"
EVIDENCE_SPAN_INVALID = "EVIDENCE_SPAN_INVALID"
EVIDENCE_QUOTE_MISMATCH = "EVIDENCE_QUOTE_MISMATCH"
UNKNOWN_ENTITY_TYPE = "UNKNOWN_ENTITY_TYPE"
UNKNOWN_RELATIONSHIP_TYPE = "UNKNOWN_RELATIONSHIP_TYPE"
SOURCE_TYPE_MISMATCH = "SOURCE_TYPE_MISMATCH"
TARGET_TYPE_MISMATCH = "TARGET_TYPE_MISMATCH"
DIRECTION_MISMATCH = "DIRECTION_MISMATCH"
ENDPOINT_UNRESOLVED = "ENDPOINT_UNRESOLVED"
ENDPOINT_EVIDENCE_UNGROUNDED = "ENDPOINT_EVIDENCE_UNGROUNDED"

_REASON_ORDER = {
    UNKNOWN_RELATIONSHIP_TYPE: 0,
    ENDPOINT_UNRESOLVED: 1,
    ENDPOINT_EVIDENCE_UNGROUNDED: 2,
    SOURCE_TYPE_MISMATCH: 3,
    TARGET_TYPE_MISMATCH: 4,
    DIRECTION_MISMATCH: 5,
    EVIDENCE_SOURCE_MISMATCH: 6,
    EVIDENCE_SPAN_INVALID: 7,
    EVIDENCE_QUOTE_MISMATCH: 8,
    EVIDENCE_MISSING: 9,
}


class Schema2WorkUnitInvariantError(ValueError):
    """Raised when a schema-2 work unit cannot be checkpointed as successful."""


class Schema2MalformedRelationshipError(ValueError):
    """Raised when tolerant parsing cannot retain every schema-2 relationship."""


@dataclass(frozen=True)
class Schema2EnrichmentContext:
    """Immutable extraction authority compiled from an approved domain contract."""

    contract_hash: str
    entities_by_alias: dict[str, DomainEntityTypeV2]
    entity_definitions: dict[str, DomainEntityTypeV2]
    relationships_by_alias: dict[str, DomainRelationshipTypeV2]
    parent_by_id: dict[str, str | None]
    max_relations_per_work_unit: int
    allow_subtype_endpoints: bool
    prompt_payload: dict[str, Any]
    proposal_hash: str = ""
    source_profile_hash: str = ""
    prompt_version: str = ""
    model_version: str = ""


def build_schema2_enrichment_context(
    contract: DomainContractV2,
) -> Schema2EnrichmentContext:
    """Compile deterministic closed-vocabulary lookups from schema 2.0."""
    entities_by_alias: dict[str, DomainEntityTypeV2] = {}
    entity_definitions = {
        definition.id: definition
        for definition in contract.candidate_model.entity_types
    }
    for definition in contract.candidate_model.entity_types:
        for alias in (definition.id, definition.name):
            key = alias.strip().casefold()
            existing = entities_by_alias.get(key)
            if existing is not None and existing.id != definition.id:
                raise ValueError(f"Ambiguous schema-2 entity alias: {alias!r}")
            entities_by_alias[key] = definition

    relationships_by_alias: dict[str, DomainRelationshipTypeV2] = {}
    for definition in contract.candidate_model.relationship_types:
        for alias in (definition.id, definition.predicate):
            key = alias.strip().casefold()
            existing = relationships_by_alias.get(key)
            if existing is not None and existing.id != definition.id:
                raise ValueError(f"Ambiguous schema-2 relationship alias: {alias!r}")
            relationships_by_alias[key] = definition

    contract_hash = compute_contract_hash(contract)
    prompt_payload = {
        "schema_version": "2.0",
        "contract_hash": contract_hash,
        "entity_types": [
            {
                "id": item.id,
                "name": item.name,
                "parent": item.parent,
            }
            for item in contract.candidate_model.entity_types
        ],
        "relationship_types": [
            {
                "id": item.id,
                "predicate": item.predicate,
                "source_types": item.source_types,
                "target_types": item.target_types,
                "direction": item.direction,
                "endpoint_policy": item.endpoint_policy,
                "evidence_policy": item.evidence_policy,
            }
            for item in contract.candidate_model.relationship_types
        ],
        "rules": [
            "Use only approved entity and relationship terms.",
            "Every relationship endpoint must reference an entity id_hint in this response.",
            "Unknown terms are discovery candidates, never authoritative data.",
            "Every asserted relationship requires one exact nested evidence object.",
            "Provide exact entity occurrence_anchors when an endpoint display name or alias is repeated or ambiguous in the relationship evidence span.",
            "Copy the runner-provided text_unit_id, source_file_id, content hash, and locator exactly.",
            "Offsets are zero-based character offsets into the provided source text.",
            "The quote must exactly equal source_text[span_start:span_end].",
            "Do not author or reuse evidence IDs; the runner mints them after validation.",
            "Do not truncate relationship candidates.",
        ],
        "max_relations_per_work_unit": (
            contract.reasoning_policy.max_relations_per_work_unit
        ),
    }
    return Schema2EnrichmentContext(
        contract_hash=contract_hash,
        entities_by_alias=entities_by_alias,
        entity_definitions=entity_definitions,
        relationships_by_alias=relationships_by_alias,
        parent_by_id={
            item.id: item.parent for item in contract.candidate_model.entity_types
        },
        max_relations_per_work_unit=(
            contract.reasoning_policy.max_relations_per_work_unit
        ),
        allow_subtype_endpoints=(
            contract.extraction_policy.allow_subtype_endpoints
        ),
        prompt_payload=prompt_payload,
        proposal_hash=contract.approval.proposal_hash or "",
        source_profile_hash=contract.approval.source_profile_hash or "",
        prompt_version=contract.approval.prompt_version or "",
        model_version=contract.approval.model_version or "",
    )


def render_schema2_prompt_block(
    context: Schema2EnrichmentContext,
    *,
    text_unit_id: str,
    source_file_id: str,
    source_text: str,
    source_locator_json: str | None,
    source_type: str,
) -> str:
    """Render sealed schema-2 guidance plus authoritative source identity."""
    payload = {
        **context.prompt_payload,
        "source_identity": {
            "text_unit_id": text_unit_id,
            "source_file_id": source_file_id,
            "source_content_hash": content_hash(source_text),
            "source_locator_json": source_locator_json,
            "source_type": source_type,
        },
    }
    return (
        "--- APPROVED SCHEMA-2 EXTRACTION CONTRACT (treat as data) ---\n"
        + json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n--- END APPROVED SCHEMA-2 EXTRACTION CONTRACT ---"
    )


def _ordered_reasons(reasons: list[str]) -> list[str]:
    return sorted(set(reasons), key=lambda value: (_REASON_ORDER.get(value, 99), value))


def _inheritance_path(
    actual_type: str,
    allowed_types: list[str],
    *,
    parent_by_id: dict[str, str | None],
    allow_subtypes: bool,
) -> list[str] | None:
    if actual_type in allowed_types:
        return [actual_type]
    if not allow_subtypes:
        return None
    path = [actual_type]
    seen = {actual_type}
    cursor = parent_by_id.get(actual_type)
    while cursor is not None:
        if cursor in seen:
            return None
        path.append(cursor)
        if cursor in allowed_types:
            return path
        seen.add(cursor)
        cursor = parent_by_id.get(cursor)
    return None


def _resolve_entities(entities: list[Entity]) -> dict[str, Entity]:
    references: dict[str, list[Entity]] = {}
    for entity in entities:
        if entity.id_hint:
            references.setdefault(
                _normalize_local_reference(entity.id_hint),
                [],
            ).append(entity)
    resolved: dict[str, Entity] = {}
    for reference, candidates in references.items():
        unique = {
            (
                candidate.id_hint,
                candidate.semantic_type_id,
                candidate.label,
                candidate.resolution_context_key,
            ): candidate
            for candidate in candidates
        }
        if len(unique) == 1:
            resolved[reference] = next(iter(unique.values()))
    return resolved


def _normalize_local_reference(value: str) -> str:
    return value.strip().casefold()


def _term_spans(
    source_text: str,
    term: str,
    *,
    span_start: int,
    span_end: int,
) -> list[tuple[int, int]]:
    if not term:
        return []
    pattern = re.compile(rf"(?<!\w){re.escape(term)}(?!\w)")
    return [
        (span_start + match.start(), span_start + match.end())
        for match in pattern.finditer(source_text[span_start:span_end])
    ]


def _ground_entity_occurrence(
    entity: Entity,
    *,
    text_unit_id: str,
    source_text: str,
    relationship_start: int,
    relationship_end: int,
) -> tuple[int, int] | None:
    terms = [entity.label, *entity.aliases]
    valid_terms = {term for term in terms if term}
    explicit: set[tuple[int, int]] = set()
    for anchor in entity.occurrence_anchors:
        start = anchor.span_start
        end = anchor.span_end
        if (
            anchor.text_unit_id != text_unit_id
            or not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < relationship_start
            or end > relationship_end
            or end <= start
            or end > len(source_text)
            or not anchor.quote
            or anchor.quote not in valid_terms
            or source_text[start:end] != anchor.quote
            or (start, end)
            not in _term_spans(
                source_text,
                anchor.quote,
                span_start=relationship_start,
                span_end=relationship_end,
            )
        ):
            continue
        explicit.add((start, end))
    if len(explicit) == 1:
        return next(iter(explicit))
    if len(explicit) > 1:
        return None

    label_spans = _term_spans(
        source_text,
        entity.label,
        span_start=relationship_start,
        span_end=relationship_end,
    )
    if len(label_spans) == 1:
        return label_spans[0]
    if len(label_spans) > 1:
        return None

    alias_spans = {
        span
        for alias in entity.aliases
        for span in _term_spans(
            source_text,
            alias,
            span_start=relationship_start,
            span_end=relationship_end,
        )
    }
    return next(iter(alias_spans)) if len(alias_spans) == 1 else None


def apply_schema2_contract(
    output: LLMOutput,
    context: Schema2EnrichmentContext,
    *,
    source_file_id: str,
    text_unit_id: str,
    source_text: str,
    source_locator_json: str | None,
    source_type: str,
) -> LLMOutput:
    """Classify every schema-2 candidate and mint evidence after exact validation."""
    normalized_entities: list[Entity] = []
    for entity in output.entities:
        definition = context.entities_by_alias.get(entity.type.strip().casefold())
        if definition is None:
            normalized_entities.append(
                entity.model_copy(
                    update={
                        "observed_type": entity.type,
                        "semantic_type_id": None,
                        "semantic_lane": "discovery",
                        "assertion_state": "unresolved",
                        "review_status": "needs_review",
                        "audit_reasons": [UNKNOWN_ENTITY_TYPE],
                    }
                )
            )
        else:
            normalized_entities.append(
                entity.model_copy(
                    update={
                        "type": definition.name,
                        "observed_type": entity.type,
                        "semantic_type_id": definition.id,
                        "semantic_lane": "authoritative",
                        "assertion_state": "asserted",
                        "review_status": "approved",
                        "audit_reasons": [],
                    }
                )
            )

    entities_by_reference = _resolve_entities(normalized_entities)
    verified_evidence: dict[str, Evidence] = {}
    normalized_relationships: list[Relationship] = []
    source_hash = content_hash(source_text)

    for relationship in output.relationships:
        definition = context.relationships_by_alias.get(
            relationship.relation.strip().casefold()
        )
        source = entities_by_reference.get(
            _normalize_local_reference(relationship.source_id_hint)
        )
        target = entities_by_reference.get(
            _normalize_local_reference(relationship.target_id_hint)
        )
        reasons: list[str] = []
        source_path: list[str] = []
        target_path: list[str] = []

        if definition is None:
            reasons.append(UNKNOWN_RELATIONSHIP_TYPE)
        if source is None or target is None:
            reasons.append(ENDPOINT_UNRESOLVED)

        if definition is not None and source is not None and target is not None:
            use_subtypes = (
                context.allow_subtype_endpoints
                and definition.endpoint_policy == "allow_subtypes"
            )
            actual_source_type = source.semantic_type_id or ""
            actual_target_type = target.semantic_type_id or ""
            source_path = (
                _inheritance_path(
                    actual_source_type,
                    definition.source_types,
                    parent_by_id=context.parent_by_id,
                    allow_subtypes=use_subtypes,
                )
                or []
            )
            target_path = (
                _inheritance_path(
                    actual_target_type,
                    definition.target_types,
                    parent_by_id=context.parent_by_id,
                    allow_subtypes=use_subtypes,
                )
                or []
            )
            if not source_path:
                reasons.append(SOURCE_TYPE_MISMATCH)
            if not target_path:
                reasons.append(TARGET_TYPE_MISMATCH)
            if relationship.direction != "forward":
                reasons.append(DIRECTION_MISMATCH)

        evidence = relationship.evidence
        evidence_id: str | None = None
        source_grounding: tuple[int, int] | None = None
        target_grounding: tuple[int, int] | None = None
        structural_failure = any(
            reason
            in {
                UNKNOWN_RELATIONSHIP_TYPE,
                ENDPOINT_UNRESOLVED,
                SOURCE_TYPE_MISMATCH,
                TARGET_TYPE_MISMATCH,
                DIRECTION_MISMATCH,
            }
            for reason in reasons
        )
        if evidence is None:
            if not structural_failure:
                reasons.append(EVIDENCE_MISSING)
        else:
            if (
                evidence.text_unit_id != text_unit_id
                or evidence.source_file_id != source_file_id
                or evidence.source_content_hash != source_hash
                or evidence.source_locator_json != source_locator_json
            ):
                reasons.append(EVIDENCE_SOURCE_MISMATCH)
            start = evidence.span_start
            end = evidence.span_end
            if (
                not isinstance(start, int)
                or isinstance(start, bool)
                or not isinstance(end, int)
                or isinstance(end, bool)
                or start < 0
                or end <= start
                or end > len(source_text)
                or not evidence.quote
            ):
                reasons.append(EVIDENCE_SPAN_INVALID)
            elif source_text[start:end] != evidence.quote:
                reasons.append(EVIDENCE_QUOTE_MISMATCH)
            else:
                if source is not None and target is not None:
                    source_grounding = _ground_entity_occurrence(
                        source,
                        text_unit_id=text_unit_id,
                        source_text=source_text,
                        relationship_start=start,
                        relationship_end=end,
                    )
                    target_grounding = _ground_entity_occurrence(
                        target,
                        text_unit_id=text_unit_id,
                        source_text=source_text,
                        relationship_start=start,
                        relationship_end=end,
                    )
                    if (
                        source_grounding is None
                        or target_grounding is None
                        or (
                            source is not target
                            and source_grounding == target_grounding
                        )
                    ):
                        reasons.append(ENDPOINT_EVIDENCE_UNGROUNDED)

        reasons = _ordered_reasons(reasons)
        if definition is None:
            semantic_lane = "discovery"
            processing_status = "discovery"
            assertion_status = "unresolved"
        elif reasons == [EVIDENCE_MISSING]:
            semantic_lane = "authoritative"
            processing_status = "unresolved"
            assertion_status = "unresolved"
        elif reasons:
            semantic_lane = "authoritative"
            processing_status = "rejected"
            assertion_status = "rejected"
        else:
            assert evidence is not None
            assert evidence.span_start is not None
            assert evidence.span_end is not None
            context_key = json.dumps(
                {
                    "text_unit_id": text_unit_id,
                    "span_start": evidence.span_start,
                    "span_end": evidence.span_end,
                    "source_content_hash": source_hash,
                    "source_locator_json": source_locator_json,
                    "source_type": source_type,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            evidence_id = make_evidence_id(
                source_file_id,
                source_type,
                context_key,
                content_hash(evidence.quote),
            )
            verified_evidence[evidence_id] = Evidence(
                id_hint=evidence_id,
                source_type=source_type,
                text=evidence.quote,
                text_unit_id=text_unit_id,
                span_start=evidence.span_start,
                span_end=evidence.span_end,
                source_content_hash=source_hash,
                source_locator_json=source_locator_json,
                runner_verified=True,
            )
            semantic_lane = "authoritative"
            processing_status = "accepted"
            assertion_status = "asserted"

        normalized_relationships.append(
            relationship.model_copy(
                update={
                    "relation": (
                        definition.predicate
                        if definition is not None
                        else relationship.relation
                    ),
                    "observed_relation": relationship.relation,
                    "semantic_relationship_id": (
                        definition.id if definition is not None else None
                    ),
                    "semantic_lane": semantic_lane,
                    "assertion_status": assertion_status,
                    "review_status": (
                        "approved"
                        if assertion_status == "asserted"
                        else "needs_review"
                    ),
                    "processing_status": processing_status,
                    "rejection_reasons": reasons,
                    "source_semantic_type_id": (
                        source.semantic_type_id if source is not None else None
                    ),
                    "target_semantic_type_id": (
                        target.semantic_type_id if target is not None else None
                    ),
                    "resolved_source_type_id": (
                        source_path[-1] if source_path else None
                    ),
                    "resolved_target_type_id": (
                        target_path[-1] if target_path else None
                    ),
                    "source_inheritance_path": source_path,
                    "target_inheritance_path": target_path,
                    "validation_authority": "schema2",
                    "resolved_source_entity_id": (
                        make_entity_id(
                            source.type,
                            source.label,
                            source.resolution_context_key,
                        )
                        if source is not None
                        else None
                    ),
                    "resolved_target_entity_id": (
                        make_entity_id(
                            target.type,
                            target.label,
                            target.resolution_context_key,
                        )
                        if target is not None
                        else None
                    ),
                    "source_grounding_span_start": (
                        source_grounding[0] if source_grounding else None
                    ),
                    "source_grounding_span_end": (
                        source_grounding[1] if source_grounding else None
                    ),
                    "target_grounding_span_start": (
                        target_grounding[0] if target_grounding else None
                    ),
                    "target_grounding_span_end": (
                        target_grounding[1] if target_grounding else None
                    ),
                    "verified_evidence_id": evidence_id,
                    "evidence_id_hint": evidence_id,
                    "evidence_id_hints": [evidence_id] if evidence_id else [],
                    "source_span_ids": [evidence_id] if evidence_id else [],
                    "description_evidence_id_hints": (
                        [evidence_id]
                        if evidence_id and relationship.description
                        else []
                    ),
                }
            )
        )

    validated = output.model_copy(
        update={
            "semantic_contract_hash": context.contract_hash,
            "proposal_hash": context.proposal_hash,
            "source_profile_hash": context.source_profile_hash,
            "prompt_version": context.prompt_version,
            "model_version": context.model_version,
            "entities": normalized_entities,
            "relationships": normalized_relationships,
            "evidence": [
                *output.evidence,
                *[
                    verified_evidence[key]
                    for key in sorted(verified_evidence)
                ],
            ],
        }
    )
    assert_schema2_work_unit_invariants(validated)
    return validated


def assert_schema2_work_unit_invariants(output: LLMOutput) -> None:
    """Reject checkpoint success for an asserted relationship without evidence."""
    for relationship in output.relationships:
        if relationship.assertion_status != "asserted":
            continue
        if (
            not relationship.verified_evidence_id
            or relationship.evidence_id_hint
            != relationship.verified_evidence_id
            or relationship.verified_evidence_id
            not in relationship.evidence_id_hints
            or not relationship.resolved_source_entity_id
            or not relationship.resolved_target_entity_id
            or relationship.source_grounding_span_start is None
            or relationship.source_grounding_span_end is None
            or relationship.target_grounding_span_start is None
            or relationship.target_grounding_span_end is None
        ):
            raise Schema2WorkUnitInvariantError(
                "Schema-2 asserted relationship lacks runner-verified evidence "
                "or endpoint grounding."
            )
