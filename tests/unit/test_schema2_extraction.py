from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.contracts.evidence import SourceUnit
from fabric_kg_builder.contracts.extraction import ExtractionAuthorityReferences
from fabric_kg_builder.contracts.identity import (
    CanonicalIdentityEnvelope,
    ImmutableSourceLocator,
)
from fabric_kg_builder.domain.hierarchy import build_type_hierarchy_closure
from fabric_kg_builder.domain.models import (
    CardinalityExpectationV2,
    CollectionIdentityPolicyV2,
    CompletenessRequirementV2,
    DomainContractV2,
    GeneralizationBasisV2,
    OrderingPolicyV2,
    StructuredFactSetV2,
)
from fabric_kg_builder.domain.service import load_domain_contract
from fabric_kg_builder.enrichment.schema2_extraction import (
    build_candidate_batch,
    build_required_member_set_proposals,
    compile_closed_vocabulary,
    derive_collection_member_fragments,
    extraction_leaf_from_dict,
    extraction_leaf_to_dict,
)
from fabric_kg_builder.enrichment.schema2_sources import L2StageError

_ROOT = Path(__file__).resolve().parents[2]


def _domain() -> DomainContractV2:
    contract = load_domain_contract(
        _ROOT / "examples/domains/facility-maintenance-v2.domain.yaml"
    )
    assert isinstance(contract, DomainContractV2)
    return contract


def _identity() -> CanonicalIdentityEnvelope:
    return CanonicalIdentityEnvelope(
        contract_kind="c0.source_unit",
        project_id="project:l2-tests",
        asset_id="asset:test",
        asset_version_id="asset-version:test",
        run_id="run:l2-tests",
        source_file_id="source-file:test",
        source_unit_id=None,
        content_hash="a" * 64,
        domain_schema_version="2.0",
        domain_contract_hash="e" * 64,
        semantic_contract_hash=None,
        canonical_schema_version="2.0.0",
        prompt_version="l2-closed-vocabulary-v1",
        prompt_hash="c" * 64,
        model_version="test-model",
        model_hash="d" * 64,
        extractor_name="l2-schema-constrained",
        extractor_version="1.0.0",
        parent_artifact_ids=(),
        parent_record_ids=(),
        immutable_locator=None,
    )


def _source_unit() -> SourceUnit:
    text = "Facility A contains Pump 1 in service."
    locator = ImmutableSourceLocator.from_authority(
        blob_uri="https://storage.example/source",
        blob_version_id="v1",
        char_start=0,
        char_end=len(text),
    )
    return SourceUnit.mint(
        identity=_identity(),
        unit_kind="paragraph",
        text=text,
        ordinal=0,
        locator=locator,
    )


def _authority(domain: DomainContractV2) -> ExtractionAuthorityReferences:
    requirement_id = (
        domain.completeness_requirements[0].requirement_id
        if domain.completeness_requirements
        else "completeness-requirement:none"
    )
    return ExtractionAuthorityReferences(
        source_corpus_manifest_id="source-corpus-manifest:test",
        source_corpus_manifest_hash="3" * 64,
        source_unit_manifest_id="artifact-manifest:source-units",
        source_unit_manifest_hash="4" * 64,
        domain_contract_hash="e" * 64,
        completeness_requirement_id=requirement_id,
        completeness_requirement_hash=domain.completeness_requirement_hash,
        hierarchy_hash=domain.hierarchy_closure.hierarchy_hash,
        identity_policy_hash=domain.identity_policy_hash,
    )


def _response() -> list[dict]:
    return json.loads(
        (
            _ROOT / "tests/fixtures/llm/schema2_candidate_proposals.json"
        ).read_text(encoding="utf-8")
    )["candidates"]


def _build(domain: DomainContractV2, response: list[dict]):
    return build_candidate_batch(
        response,
        vocabulary=compile_closed_vocabulary(domain),
        contract=domain,
        authority=_authority(domain),
        base_identity=_identity(),
        source_unit_id=_source_unit().source_unit_id,
        work_unit_id="l2-work-unit:test",
        classifier_version="1.0.0",
        prompt_hash="f" * 64,
        model_hash="1" * 64,
        extractor_name="l2-schema-constrained",
        extractor_version="1.0.0",
        occurred_at_utc=datetime(2026, 6, 24, 12, tzinfo=timezone.utc),
    )


def test_closed_vocabulary_unknowns_are_audited_not_mutated() -> None:
    domain = _domain()
    vocabulary = compile_closed_vocabulary(domain)

    result = _build(domain, _response())

    assert result.batch.retained_candidate_count == 4
    assert len(result.lifecycle_records) == 4
    assert dict(result.audit_reason_counts)["UNKNOWN_ENTITY_TYPE"] == 1
    assert set(vocabulary.entities_by_id) == {
        item.type_id for item in domain.candidate_model.entity_types
    }
    assert "inventedmachinekind" not in vocabulary.entities_by_alias


def test_distinct_same_anchor_observations_are_conflict_accounted() -> None:
    response = _response()
    duplicate = dict(response[0])
    duplicate["label"] = f"{duplicate['label']} alternate"
    result = _build(_domain(), [response[0], duplicate])

    candidate_ids = [
        item.candidate_id for item in result.batch.candidates
    ]
    assert len(candidate_ids) == 1
    assert dict(result.audit_reason_counts)[
        "CANDIDATE_PAYLOAD_CONFLICT"
    ] == 1
    assert any(
        item.reason_codes == ("CANDIDATE_PAYLOAD_CONFLICT",)
        for item in result.batch.candidate_dispositions
    )


def test_a_business_key_entity_persists_the_key_that_minted_its_id() -> None:
    from fabric_kg_builder.enrichment.schema2_evidence import recompute_entity_id

    domain = _domain()
    result = _build(domain, [_response()[0]])
    record = result.proposed_candidates[0]

    assert record.normalized_business_key is not None
    persisted = dict(record.normalized_business_key)
    policy = next(
        item.identity_key_policy
        for item in domain.candidate_model.entity_types
        if item.type_id == record.approved_semantic_id
    )

    assert recompute_entity_id(
        project_id=_identity().project_id,
        policy=policy,
        normalized_business_key=persisted,
    ) == record.semantic_id


def test_the_persisted_business_key_survives_a_checkpoint_round_trip() -> None:
    leaf = _build(_domain(), [_response()[0]])
    restored = extraction_leaf_from_dict(extraction_leaf_to_dict(leaf))

    assert (
        restored.proposed_candidates[0].normalized_business_key
        == leaf.proposed_candidates[0].normalized_business_key
    )
    assert restored.proposed_candidates[0].normalized_business_key is not None


def test_invalid_business_key_is_downgraded_for_rereview() -> None:
    response = _response()
    entity = dict(response[0])
    entity["identity_key"] = {"wrong_key": "facility-a"}
    result = _build(_domain(), [entity])

    record = result.proposed_candidates[0]
    assert record.approved_semantic_id is None
    reasons = dict(result.audit_reason_counts)
    assert reasons["IDENTITY_POLICY_MISMATCH"] == 1
    assert reasons["DOMAIN_REREVIEW_REQUESTED"] == 1


def test_blank_business_key_is_downgraded_for_rereview() -> None:
    response = _response()
    entity = dict(response[0])
    entity["identity_key"] = {"facility_id": "   "}
    result = _build(_domain(), [entity])

    record = result.proposed_candidates[0]
    assert record.approved_semantic_id is None


def test_legacy_identity_mismatch_flag_is_removed_on_checkpoint_load() -> None:
    leaf = _build(_domain(), [_response()[0]])
    payload = extraction_leaf_to_dict(leaf)
    payload["proposed_candidates"][0][
        "identity_policy_mismatch"
    ] = False

    loaded = extraction_leaf_from_dict(payload)

    assert not hasattr(
        loaded.proposed_candidates[0], "identity_policy_mismatch"
    )


def test_candidates_are_proposed_only_and_do_not_mint_evidence() -> None:
    domain = _domain()
    result = _build(domain, _response())

    assert all(record.from_state is None for record in result.lifecycle_records)
    assert all(record.to_state.value == "proposed" for record in result.lifecycle_records)
    assert all(record.evidence_span_ids == () for record in result.lifecycle_records)
    assert all(record.resolved_source_entity_id is None for record in result.lifecycle_records)
    assert all(record.resolved_target_entity_id is None for record in result.lifecycle_records)
    relationship = next(
        item
        for item in result.proposed_candidates
        if item.candidate_kind == "relationship"
    )
    assert relationship.proposed_anchor is not None
    assert (
        relationship.proposed_anchor.model_authored_evidence_id
        == "model-evidence-must-not-be-trusted"
    )
    assert "verification_status" not in relationship.proposed_anchor.model_dump()


def test_stable_ids_do_not_depend_on_labels_or_type_alias_spelling() -> None:
    domain = _domain()
    base = _response()
    changed = json.loads(json.dumps(base))
    changed[0]["observed_type"] = "semantic-type:facility-maintenance.facility"
    changed[0]["label"] = "Renamed display label"
    changed[0]["aliases"] = ["Different Alias"]

    original = _build(domain, base)
    renamed = _build(domain, changed)
    original_entities = {
        item.approved_semantic_id: item.candidate_id
        for item in original.proposed_candidates
        if item.candidate_kind == "entity"
    }
    renamed_entities = {
        item.approved_semantic_id: item.candidate_id
        for item in renamed.proposed_candidates
        if item.candidate_kind == "entity"
    }
    assert original_entities == renamed_entities
    assert {
        item.candidate_id
        for item in original.proposed_candidates
        if item.candidate_kind == "relationship"
    } == {
        item.candidate_id
        for item in renamed.proposed_candidates
        if item.candidate_kind == "relationship"
    }


def test_reclassification_changes_version_not_type_independent_identity() -> None:
    domain = _domain()
    equipment = next(
        item
        for item in domain.candidate_model.entity_types
        if item.display_name == "Equipment"
    )
    pump = equipment.model_copy(
        update={
            "type_id": "semantic-type:facility-maintenance.pump",
            "semantic_key": "facility-maintenance.pump",
            "display_name": "Pump",
            "aliases": ["Centrifugal Pump"],
            "classification": "domain_specialization",
            "parent_type_id": equipment.type_id,
            "identity_key_policy": None,
            "generalization_basis": GeneralizationBasisV2(
                governance_rationale="Approved specialization for reclassification test."
            ),
        }
    )
    candidate_model = domain.candidate_model.model_copy(
        update={"entity_types": [*domain.candidate_model.entity_types, pump]}
    )
    hierarchy = build_type_hierarchy_closure(
        candidate_model.entity_types,
        candidate_model.relationship_types,
    )
    specialized = domain.model_copy(
        update={
            "candidate_model": candidate_model,
            "hierarchy_closure": hierarchy,
        }
    )
    base = _response()
    child = json.loads(json.dumps(base))
    child[1]["observed_type"] = "Pump"

    root_result = _build(specialized, base)
    child_result = _build(specialized, child)
    root_entity = next(
        item
        for item in root_result.proposed_candidates
        if item.local_reference == "equipment-1"
    )
    child_entity = next(
        item
        for item in child_result.proposed_candidates
        if item.local_reference == "equipment-1"
    )

    assert root_entity.candidate_id == child_entity.candidate_id
    assert root_entity.classification_version_id != child_entity.classification_version_id
    assert root_entity.candidate_version_id != child_entity.candidate_version_id


def test_deduplication_accounts_every_input_candidate_exactly_once() -> None:
    domain = _domain()
    response = _response()
    duplicate = json.loads(json.dumps(response[0]))
    result = _build(domain, [*response, duplicate])

    dispositions = result.batch.candidate_dispositions
    assert result.batch.input_candidate_count == len(response) + 1
    assert len(dispositions) == result.batch.input_candidate_count
    assert len({item.input_candidate_id for item in dispositions}) == len(dispositions)
    assert result.batch.deduplicated_input_count == 1
    assert {item.disposition for item in dispositions} == {"retained", "deduplicated"}
    assert all(item.reason_codes == () for item in dispositions)


@pytest.mark.parametrize(
    "fixture_name",
    [
        "schema2_structured_collection_manufacturing.json",
        "schema2_structured_collection_clinical.json",
        "schema2_structured_collection_logistics.json",
    ],
)
def test_structured_fact_sets_are_domain_neutral_and_do_not_invent_semantics(
    fixture_name: str,
) -> None:
    raw = json.loads(
        (_ROOT / "tests/fixtures/llm" / fixture_name).read_text(encoding="utf-8")
    )
    base = _domain()
    facility = next(
        item
        for item in base.candidate_model.entity_types
        if item.display_name == "Facility"
    )
    equipment = next(
        item
        for item in base.candidate_model.entity_types
        if item.display_name == "Equipment"
    )
    contains = next(
        item
        for item in base.candidate_model.relationship_types
        if item.display_name == "contains"
    )
    ordered = raw["ordering"] == "ordered"
    ordering = (
        OrderingPolicyV2(
            mode="ordered",
            ordinal_property_id="property:test.member-order",
            ordinal_value_type="integer",
            direction="ascending",
            unique_ordinals=True,
            contiguous=True,
        )
        if ordered
        else OrderingPolicyV2(mode="unordered")
    )
    requirement = CompletenessRequirementV2(
        requirement_id=raw["requirement_id"],
        competency_question_ids=[base.competency_questions[0].id],
        requirement_kind="structured_fact_set",
        scope_type_id=facility.type_id,
        rationale="Approved domain-neutral collection semantics.",
        source_kind="governance_rule",
        governance_references=[f"governance:{raw['domain']}"],
        coverage_status="covered",
        structured_fact_set=StructuredFactSetV2(
            aggregate_type_id=facility.type_id,
            membership_relationship_type_id=contains.relationship_type_id,
            allowed_member_type_ids=[equipment.type_id],
            member_role_ids=[raw["member_role_id"]],
            ordering_policy=ordering,
            cardinality=CardinalityExpectationV2(
                expected_count=1,
                minimum_count=1,
                maximum_count=3,
                source_kind="governance_rule",
                reviewed_rationale="Approved exact collection bounds.",
            ),
            collection_identity_policy=CollectionIdentityPolicyV2(
                member_roles_included=True,
                ordinals_included=ordered,
                preserve_member_order=ordered,
            ),
            membership_source_kind="governance_rule",
            membership_rationale="Approved membership semantics.",
        ),
    )
    fields = {
        name: getattr(base, name) for name in DomainContractV2.model_fields
    }
    fields.update(
        completeness_requirements=[requirement],
        completeness_requirement_hash=canonical_sha256(
            [requirement.model_dump(mode="json")]
        ),
    )
    contract = DomainContractV2.model_construct(**fields)
    response = _response()
    raw_membership = next(
        item for item in response if item["candidate_kind"] == "relationship"
    )
    raw_membership["member_role_id"] = raw["member_role_id"]
    raw_membership["member_order"] = 0 if ordered else None
    leaf = _build(contract, response)
    fragments = derive_collection_member_fragments((leaf,), contract=contract)
    assert len(fragments) == 1
    fragment = fragments[0]

    views = build_required_member_set_proposals(
        fragments,
        leaves=(leaf,),
        contract=contract,
        authority_factory=lambda _requirement: _authority(contract),
        base_identity=_identity(),
    )

    assert len(views) == 1
    view = views[0]
    assert view.requirement_id == raw["requirement_id"]
    assert (
        view.proposal.expected_cardinality,
        view.proposal.minimum_cardinality,
        view.proposal.maximum_cardinality,
    ) == (1, 1, 3)
    assert view.proposal.identity.contract_version == "1.1.0"
    assert view.proposal.ordering_policy.mode == raw["ordering"]
    assert view.proposal.required_role_ids == (raw["member_role_id"],)
    assert view.proposal.members[0].member_role_id == raw["member_role_id"]
    assert view.proposal.members[0].member_order == (0 if ordered else None)
    assert view.unresolved_reasons == ()
    assert view.proposal.members[0].supporting_evidence_span_ids == ()
    assert view.proposal.proposal_hash == canonical_sha256(
        view.proposal.model_dump(mode="json", exclude={"proposal_hash"})
    )
    assert "role:unspecified" not in view.proposal.model_dump_json()
    assert not hasattr(view, "expected_count")

    if ordered:
        with pytest.raises(L2StageError, match="observed unique contiguous"):
            build_required_member_set_proposals(
                (replace(fragment, member_order=None),),
                leaves=(leaf,),
                contract=contract,
                authority_factory=lambda _requirement: _authority(contract),
                base_identity=_identity(),
            )

    second_entity = json.loads(
        json.dumps(
            next(
                item
                for item in response
                if item["candidate_kind"] == "entity"
                and item["local_id"] == "equipment-1"
            )
        )
    )
    second_entity.update(
        local_id="equipment-2",
        label="Pump 2",
        identity_key={"equipment_id": "pump-2"},
    )
    second_membership = json.loads(json.dumps(raw_membership))
    second_membership["target_local_id"] = "equipment-2"
    if ordered:
        second_membership["member_order"] = 1
    excess_leaf = _build(
        contract,
        [*response, second_entity, second_membership],
    )
    excess_fragments = derive_collection_member_fragments(
        (excess_leaf,),
        contract=contract,
    )
    excess_views = build_required_member_set_proposals(
        excess_fragments,
        leaves=(excess_leaf,),
        contract=contract,
        authority_factory=lambda _requirement: _authority(contract),
        base_identity=_identity(),
    )
    assert excess_views[0].unresolved_reasons == (
        "EXPECTED_MEMBER_COUNT_MISMATCH",
    )

    empty_leaf = _build(
        contract,
        [item for item in response if item["candidate_kind"] != "relationship"],
    )
    with pytest.raises(L2StageError, match="no required-member observations"):
        build_required_member_set_proposals(
            (),
            leaves=(empty_leaf,),
            contract=contract,
            authority_factory=lambda _requirement: _authority(contract),
            base_identity=_identity(),
        )


def test_malformed_response_is_rejected_atomically() -> None:
    raw = _response()
    raw[0]["unapproved_field"] = "must fail"

    with pytest.raises(L2StageError, match="candidate response is not wholly valid"):
        _build(_domain(), raw)
