from __future__ import annotations

import json
from pathlib import Path

import pytest

from fabric_kg_builder.domain.models import (
    DomainEntityTypeV2,
    DomainRelationshipTypeV2,
)
from fabric_kg_builder.enrichment.output_schema import (
    EntityOccurrenceAnchor,
    ExactRelationshipEvidence,
    LLMOutput,
)
from fabric_kg_builder.enrichment.orchestrator import (
    build_user_message,
    canonicalize_llm_output,
)
from fabric_kg_builder.enrichment.schema2_validation import (
    DIRECTION_MISMATCH,
    ENDPOINT_EVIDENCE_UNGROUNDED,
    ENDPOINT_UNRESOLVED,
    EVIDENCE_MISSING,
    EVIDENCE_QUOTE_MISMATCH,
    EVIDENCE_SOURCE_MISMATCH,
    EVIDENCE_SPAN_INVALID,
    SOURCE_TYPE_MISMATCH,
    UNKNOWN_RELATIONSHIP_TYPE,
    Schema2EnrichmentContext,
    Schema2WorkUnitInvariantError,
    apply_schema2_contract,
    assert_schema2_work_unit_invariants,
)
from fabric_kg_builder.model.ids import content_hash


_SOURCE_FILE_ID = "src:test"
_TEXT_UNIT_ID = "unit:test"
_SOURCE_TEXT = "Replacement event uses a Torx driver."
_LOCATOR = '{"page":1}'
_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "llm"
    / "schema2_exact_relationships.json"
)


def _entity(
    entity_id: str,
    name: str,
    *,
    parent: str | None = None,
) -> DomainEntityTypeV2:
    return DomainEntityTypeV2(
        id=entity_id,
        name=name,
        parent=parent,
        description=f"Approved {name} type.",
        source_evidence_ids=[f"proposal-evidence:{name.casefold()}"],
    )


def _context(*, endpoint_policy: str = "allow_subtypes") -> Schema2EnrichmentContext:
    support = _entity("entity-type:support-event", "SupportEvent")
    repair = _entity(
        "entity-type:repair-event",
        "RepairEvent",
        parent=support.id,
    )
    replacement = _entity(
        "entity-type:replacement-event",
        "ReplacementEvent",
        parent=repair.id,
    )
    tool = _entity("entity-type:tool", "Tool")
    relationship = DomainRelationshipTypeV2(
        id="relationship-type:requires-tool",
        predicate="requires_tool",
        description="A support event requires a tool.",
        source_types=[support.id],
        target_types=[tool.id],
        endpoint_policy=endpoint_policy,
        competency_question_ids=["cq:tools"],
        source_evidence_ids=["proposal-evidence:requires-tool"],
    )
    entities = [support, repair, replacement, tool]
    aliases = {
        alias.casefold(): item
        for item in entities
        for alias in (item.id, item.name)
    }
    return Schema2EnrichmentContext(
        contract_hash="contract:test",
        entities_by_alias=aliases,
        entity_definitions={item.id: item for item in entities},
        relationships_by_alias={
            relationship.id.casefold(): relationship,
            relationship.predicate.casefold(): relationship,
        },
        parent_by_id={item.id: item.parent for item in entities},
        max_relations_per_work_unit=25,
        allow_subtype_endpoints=True,
        prompt_payload={},
    )


def _payload(
    *,
    relation: str = "requires_tool",
    direction: str = "forward",
    source_type: str = "ReplacementEvent",
    evidence: dict | None | object = ...,
) -> LLMOutput:
    quote = "Replacement event uses a Torx driver."
    exact = {
        "text_unit_id": _TEXT_UNIT_ID,
        "span_start": 0,
        "span_end": len(quote),
        "quote": quote,
        "source_file_id": _SOURCE_FILE_ID,
        "source_content_hash": content_hash(_SOURCE_TEXT),
        "source_locator_json": _LOCATOR,
    }
    relationship: dict = {
        "source_id_hint": "event-1",
        "relation": relation,
        "target_id_hint": "tool-1",
        "direction": direction,
        "confidence": 0.9,
    }
    if evidence is ...:
        relationship["evidence"] = exact
    elif evidence is not None:
        relationship["evidence"] = evidence
    return LLMOutput.model_validate(
        {
            "source_file_id": _SOURCE_FILE_ID,
            "pass": "p3",
            "entities": [
                {
                    "id_hint": "event-1",
                    "type": source_type,
                    "label": "Replacement event",
                    "confidence": 0.9,
                },
                {
                    "id_hint": "tool-1",
                    "type": "Tool",
                    "label": "Torx driver",
                    "confidence": 0.9,
                },
            ],
            "relationships": [relationship],
        }
    )


def _apply(
    output: LLMOutput,
    *,
    context: Schema2EnrichmentContext | None = None,
    source_text: str = _SOURCE_TEXT,
    source_type: str = "document_span",
) -> LLMOutput:
    return apply_schema2_contract(
        output,
        context or _context(),
        source_file_id=_SOURCE_FILE_ID,
        text_unit_id=_TEXT_UNIT_ID,
        source_text=source_text,
        source_locator_json=_LOCATOR,
        source_type=source_type,
    )


def test_exact_span_is_asserted_and_mints_runner_evidence() -> None:
    validated = _apply(
        LLMOutput.model_validate(
            json.loads(_FIXTURE.read_text(encoding="utf-8"))
        )
    )
    relationship = validated.relationships[0]

    assert relationship.assertion_status == "asserted"
    assert relationship.verified_evidence_id
    assert relationship.evidence_id_hint == relationship.verified_evidence_id
    evidence = next(
        item
        for item in validated.evidence
        if item.id_hint == relationship.verified_evidence_id
    )
    assert evidence.runner_verified is True
    assert evidence.text == _SOURCE_TEXT
    assert evidence.span_start == 0
    assert evidence.span_end == len(_SOURCE_TEXT)
    repeated = _apply(
        LLMOutput.model_validate(
            json.loads(_FIXTURE.read_text(encoding="utf-8"))
        )
    )
    assert (
        repeated.relationships[0].verified_evidence_id
        == relationship.verified_evidence_id
    )


def test_missing_evidence_is_unresolved() -> None:
    relationship = _apply(_payload(evidence=None)).relationships[0]
    assert relationship.assertion_status == "unresolved"
    assert relationship.processing_status == "unresolved"
    assert relationship.rejection_reasons == [EVIDENCE_MISSING]


def test_model_authored_evidence_id_is_never_trusted() -> None:
    output = _payload()
    output.relationships[0] = output.relationships[0].model_copy(
        update={
            "evidence_id_hint": "evid:model-authored",
            "evidence_id_hints": ["evid:model-authored"],
        }
    )
    relationship = _apply(output).relationships[0]
    assert relationship.verified_evidence_id != "evid:model-authored"
    assert relationship.evidence_id_hints == [
        relationship.verified_evidence_id
    ]


def test_quote_mismatch_is_rejected() -> None:
    evidence = _payload().relationships[0].evidence.model_dump()
    evidence["quote"] = "Replacement event uses a hex driver."
    relationship = _apply(_payload(evidence=evidence)).relationships[0]
    assert relationship.assertion_status == "rejected"
    assert EVIDENCE_QUOTE_MISMATCH in relationship.rejection_reasons


def test_exact_unrelated_quote_cannot_assert_relationship() -> None:
    source_text = (
        "Replacement event uses a Torx driver. "
        "The maintenance window starts tomorrow."
    )
    quote = "The maintenance window starts tomorrow."
    evidence = {
        "text_unit_id": _TEXT_UNIT_ID,
        "span_start": source_text.index(quote),
        "span_end": source_text.index(quote) + len(quote),
        "quote": quote,
        "source_file_id": _SOURCE_FILE_ID,
        "source_content_hash": content_hash(source_text),
        "source_locator_json": _LOCATOR,
    }
    relationship = _apply(
        _payload(evidence=evidence),
        source_text=source_text,
    ).relationships[0]
    assert relationship.assertion_status == "rejected"
    assert ENDPOINT_EVIDENCE_UNGROUNDED in relationship.rejection_reasons
    assert relationship.verified_evidence_id is None


def test_explicit_entity_anchor_resolves_ambiguous_repeated_name() -> None:
    source_text = (
        "Replacement event follows Replacement event and uses a Torx driver."
    )
    output = _payload()
    first_start = source_text.index("Replacement event")
    target_start = source_text.index("Torx driver")
    output.entities[0] = output.entities[0].model_copy(
        update={
            "occurrence_anchors": [
                EntityOccurrenceAnchor(
                    text_unit_id=_TEXT_UNIT_ID,
                    span_start=first_start,
                    span_end=first_start + len("Replacement event"),
                    quote="Replacement event",
                )
            ]
        }
    )
    output.entities[1] = output.entities[1].model_copy(
        update={
            "occurrence_anchors": [
                EntityOccurrenceAnchor(
                    text_unit_id=_TEXT_UNIT_ID,
                    span_start=target_start,
                    span_end=target_start + len("Torx driver"),
                    quote="Torx driver",
                )
            ]
        }
    )
    output.relationships[0] = output.relationships[0].model_copy(
        update={
            "evidence": ExactRelationshipEvidence(
                text_unit_id=_TEXT_UNIT_ID,
                span_start=0,
                span_end=len(source_text),
                quote=source_text,
                source_file_id=_SOURCE_FILE_ID,
                source_content_hash=content_hash(source_text),
                source_locator_json=_LOCATOR,
            )
        }
    )
    relationship = _apply(
        output,
        source_text=source_text,
    ).relationships[0]
    assert relationship.assertion_status == "asserted"
    assert relationship.source_grounding_span_start == first_start
    assert relationship.target_grounding_span_start == target_start


def test_unknown_terms_use_discovery_lane() -> None:
    validated = _apply(
        _payload(
            relation="invented_predicate",
            source_type="InventedEvent",
            evidence=None,
        )
    )
    assert validated.entities[0].semantic_lane == "discovery"
    relationship = validated.relationships[0]
    assert relationship.semantic_lane == "discovery"
    assert relationship.processing_status == "discovery"
    assert relationship.rejection_reasons == [UNKNOWN_RELATIONSHIP_TYPE]


def test_direction_mismatch_is_rejected() -> None:
    relationship = _apply(_payload(direction="reverse")).relationships[0]
    assert relationship.assertion_status == "rejected"
    assert DIRECTION_MISMATCH in relationship.rejection_reasons


def test_unresolved_endpoint_is_rejected_and_retained_for_audit() -> None:
    output = _payload(evidence=None)
    output.relationships[0] = output.relationships[0].model_copy(
        update={"source_id_hint": "missing-event"}
    )
    validated = _apply(output)
    relationship = validated.relationships[0]
    records = canonicalize_llm_output(validated, _SOURCE_FILE_ID)

    assert relationship.assertion_status == "rejected"
    assert ENDPOINT_UNRESOLVED in relationship.rejection_reasons
    assert len(records.relationships) == 1
    assert records.relationships[0].source_entity_id.startswith(
        "unresolved-endpoint:"
    )


def test_transitive_subtype_records_deterministic_path() -> None:
    relationship = _apply(_payload()).relationships[0]
    assert relationship.source_inheritance_path == [
        "entity-type:replacement-event",
        "entity-type:repair-event",
        "entity-type:support-event",
    ]
    assert relationship.resolved_source_type_id == "entity-type:support-event"


def test_casefolded_endpoint_hint_keeps_asserted_canonical_ids() -> None:
    output = _payload()
    output.relationships[0] = output.relationships[0].model_copy(
        update={
            "source_id_hint": "Event-1",
            "target_id_hint": "TOOL-1",
        }
    )
    validated = _apply(output)
    relationship = validated.relationships[0]
    records = canonicalize_llm_output(validated, _SOURCE_FILE_ID)

    assert relationship.assertion_status == "asserted"
    assert records.relationships[0].source_entity_id == (
        relationship.resolved_source_entity_id
    )
    assert records.relationships[0].target_entity_id == (
        relationship.resolved_target_entity_id
    )


def test_exact_only_rejects_child_endpoint() -> None:
    relationship = _apply(
        _payload(),
        context=_context(endpoint_policy="exact"),
    ).relationships[0]
    assert relationship.assertion_status == "rejected"
    assert SOURCE_TYPE_MISMATCH in relationship.rejection_reasons


def test_source_identity_mismatch_is_rejected() -> None:
    evidence = _payload().relationships[0].evidence.model_dump()
    evidence["source_content_hash"] = "wrong"
    relationship = _apply(_payload(evidence=evidence)).relationships[0]
    assert relationship.assertion_status == "rejected"
    assert EVIDENCE_SOURCE_MISMATCH in relationship.rejection_reasons


def test_authoritative_source_type_changes_evidence_identity() -> None:
    document = _apply(_payload(), source_type="document_span")
    csv = _apply(_payload(), source_type="csv_row")
    document_relationship = document.relationships[0]
    csv_relationship = csv.relationships[0]

    assert document_relationship.verified_evidence_id
    assert csv_relationship.verified_evidence_id
    assert (
        document_relationship.verified_evidence_id
        != csv_relationship.verified_evidence_id
    )
    document_evidence = next(
        item
        for item in document.evidence
        if item.id_hint == document_relationship.verified_evidence_id
    )
    csv_evidence = next(
        item
        for item in csv.evidence
        if item.id_hint == csv_relationship.verified_evidence_id
    )
    assert document_evidence.source_type == "document_span"
    assert csv_evidence.source_type == "csv_row"


def test_schema2_prompt_carries_authoritative_source_type() -> None:
    prompt = build_user_message(
        None,
        _SOURCE_FILE_ID,
        _SOURCE_TEXT,
        "p3",
        schema2_context=_context(),
        text_unit_id=_TEXT_UNIT_ID,
        source_locator_json=_LOCATOR,
        source_type="csv_row",
    )
    assert '"source_type":"csv_row"' in prompt


def test_out_of_bounds_span_is_rejected_without_dropping_candidate() -> None:
    evidence = _payload().relationships[0].evidence.model_dump()
    evidence["span_end"] = len(_SOURCE_TEXT) + 1
    relationship = _apply(_payload(evidence=evidence)).relationships[0]
    assert relationship.assertion_status == "rejected"
    assert EVIDENCE_SPAN_INVALID in relationship.rejection_reasons


def test_asserted_without_verified_evidence_fails_work_unit() -> None:
    output = _payload(evidence=None)
    output.relationships[0] = output.relationships[0].model_copy(
        update={"assertion_status": "asserted"}
    )
    with pytest.raises(
        Schema2WorkUnitInvariantError,
        match="runner-verified evidence",
    ):
        assert_schema2_work_unit_invariants(output)


def test_reason_metadata_is_json_serializable() -> None:
    relationship = _apply(_payload(evidence=None)).relationships[0]
    assert json.loads(
        json.dumps({"rejection_reasons": relationship.rejection_reasons})
    ) == {"rejection_reasons": [EVIDENCE_MISSING]}
