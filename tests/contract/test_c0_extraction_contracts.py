"""C0.Extraction carrier contracts and deterministic L3 seals."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from fabric_kg_builder.contracts import (
    AssertionState,
    CandidateAccountingDisposition,
    CandidateLifecycleRecord,
    CanonicalIdentityEnvelope,
    EvidenceSpan,
    ExtractionAuthorityReferences,
    ExtractionCandidateBatch,
    ExtractionCandidateReference,
    ImmutableSourceLocator,
    REGISTERED_CONTRACTS,
    RequiredMemberManifest,
    RequiredMemberReference,
    RequiredMemberSetProposal,
    SourceUnit,
    StageReceipt,
    authoritative_collection_hash,
    canonical_json,
    canonical_sha256,
    negotiate_contract,
    parse_contract,
)
from fabric_kg_builder.contracts.base import CONTRACT_VERSION

FIXTURES = Path(__file__).parent.parent / "fixtures" / "contracts"
NOW = datetime(2026, 8, 24, 20, 0, tzinfo=timezone.utc)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
HASH_F = "f" * 64


def identity(
    kind: str,
    *,
    source_file_id: str | None = None,
    source_unit_id: str | None = None,
    content_hash: str | None = None,
    locator: ImmutableSourceLocator | None = None,
) -> CanonicalIdentityEnvelope:
    source_derived = source_file_id is not None
    return CanonicalIdentityEnvelope(
        contract_kind=kind,
        contract_version=CONTRACT_VERSION,
        project_id="project-fixture",
        asset_id="asset-fixture" if source_derived else None,
        asset_version_id="asset-version-fixture" if source_derived else None,
        run_id="run-fixture",
        source_file_id=source_file_id,
        source_unit_id=source_unit_id,
        content_hash=content_hash,
        domain_schema_version="2.0",
        domain_contract_hash=HASH_B,
        semantic_contract_hash=None,
        canonical_schema_version="2.0",
        prompt_version=None,
        prompt_hash=None,
        model_version=None,
        model_hash=None,
        extractor_name="fixture-extractor",
        extractor_version="1.0.0",
        parent_artifact_ids=("artifact-input",),
        parent_record_ids=(),
        immutable_locator=locator,
    )


def authority(
    *,
    domain: str = HASH_B,
    completeness: str = HASH_C,
    hierarchy: str = HASH_D,
    identity_policy: str = HASH_E,
) -> ExtractionAuthorityReferences:
    return ExtractionAuthorityReferences(
        source_corpus_manifest_id="manifest:source-corpus",
        source_corpus_manifest_hash=HASH_A,
        source_unit_manifest_id="manifest:source-units",
        source_unit_manifest_hash=HASH_F,
        domain_contract_hash=domain,
        completeness_requirement_id="requirement:approved-membership",
        completeness_requirement_hash=completeness,
        hierarchy_hash=hierarchy,
        identity_policy_hash=identity_policy,
    )


def accounting(
    input_id: str,
    *,
    retained_id: str | None = None,
    dedup_target: str | None = None,
) -> CandidateAccountingDisposition:
    return CandidateAccountingDisposition(
        identity=identity("c0.candidate_accounting_disposition"),
        input_candidate_id=input_id,
        disposition="retained" if retained_id else "deduplicated",
        retained_candidate_id=retained_id,
        deduplicated_into_candidate_id=dedup_target,
        current_state=AssertionState.PROPOSED if retained_id else None,
        reason_codes=(),
    )


def candidate(
    candidate_id: str,
    order: int,
    *,
    evidence_span_ids: tuple[str, ...] = (),
) -> ExtractionCandidateReference:
    return ExtractionCandidateReference(
        candidate_id=candidate_id,
        candidate_version_id=f"{candidate_id}:version-1",
        candidate_kind="entity",
        semantic_type_id=f"semantic-type:generic-{order}",
        lifecycle_record_id=f"lifecycle:{candidate_id}",
        evidence_span_ids=evidence_span_ids,
    )


def batch(
    *,
    refs: ExtractionAuthorityReferences | None = None,
    evidence_span_ids: tuple[str, ...] = (),
) -> ExtractionCandidateBatch:
    candidates = (
        candidate("candidate:alpha", 0, evidence_span_ids=evidence_span_ids),
        candidate("candidate:beta", 1, evidence_span_ids=evidence_span_ids),
    )
    return ExtractionCandidateBatch.seal(
        identity=identity("c0.extraction_candidate_batch"),
        extraction_candidate_batch_id="batch:fixture",
        authority=refs or authority(),
        input_candidate_count=3,
        retained_candidate_count=2,
        deduplicated_input_count=1,
        candidates=candidates,
        candidate_dispositions=(
            accounting("input:alpha", retained_id="candidate:alpha"),
            accounting("input:beta", retained_id="candidate:beta"),
            accounting("input:duplicate", dedup_target="candidate:alpha"),
        ),
    )


def member(
    member_id: str,
    order: int,
    candidate_id: str,
    *,
    member_type: str = "semantic-type:generic-member",
    role: str = "member-role:required",
    minimum_cardinality: int = 1,
    maximum_cardinality: int | None = 1,
    evidence_span_ids: tuple[str, ...] = (),
) -> RequiredMemberReference:
    return RequiredMemberReference(
        member_canonical_id=member_id,
        member_semantic_type_id=member_type,
        member_role_id=role,
        member_order=order,
        minimum_cardinality=minimum_cardinality,
        maximum_cardinality=maximum_cardinality,
        candidate_id=candidate_id,
        supporting_evidence_span_ids=evidence_span_ids,
    )


def proposal(
    source_batch: ExtractionCandidateBatch | None = None,
    *,
    members: tuple[RequiredMemberReference, ...] | None = None,
) -> RequiredMemberSetProposal:
    source_batch = source_batch or batch()
    return RequiredMemberSetProposal.seal(
        identity=identity("c0.required_member_set_proposal"),
        required_member_set_proposal_id="proposal:fixture",
        extraction_candidate_batch_id=source_batch.extraction_candidate_batch_id,
        extraction_candidate_batch_hash=source_batch.batch_hash,
        authority=source_batch.authority,
        scope_canonical_id="canonical-scope:fixture",
        membership_semantic_relationship_id="semantic-relationship:contains",
        members=members
        or (
            member(
                "canonical-member:alpha",
                0,
                "candidate:alpha",
                member_type="semantic-type:generic-0",
            ),
            member(
                "canonical-member:beta",
                1,
                "candidate:beta",
                member_type="semantic-type:generic-1",
                role="member-role:secondary",
                minimum_cardinality=0,
                maximum_cardinality=None,
            ),
        ),
    )


def manifest(source_proposal: RequiredMemberSetProposal | None = None):
    source_proposal = source_proposal or proposal()
    return RequiredMemberManifest.seal_from_proposal(
        source_proposal,
        identity=identity("c0.required_member_manifest"),
        required_member_manifest_id="manifest:required-members",
        validator_name="local-deterministic-validator",
        validator_version="1.0.0",
        sealed_at_utc=NOW,
    )


@pytest.mark.contract
def test_registry_contains_exact_extraction_contracts_at_1_0_0() -> None:
    expected = {
        "c0.extraction_candidate_batch": ExtractionCandidateBatch,
        "c0.required_member_set_proposal": RequiredMemberSetProposal,
        "c0.required_member_manifest": RequiredMemberManifest,
    }
    for kind, model in expected.items():
        assert REGISTERED_CONTRACTS[kind] is model
        assert negotiate_contract(kind, "1.0.0") is model
        with pytest.raises(ValueError):
            negotiate_contract(kind, "1.0.1")
    assert not any(
        "completeness_requirement" in kind or "hierarchy_identity" in kind
        for kind in REGISTERED_CONTRACTS
    )


@pytest.mark.contract
def test_authority_references_are_direct_strict_carrier_fields() -> None:
    expected = set(ExtractionAuthorityReferences.model_fields)
    for model in (
        ExtractionCandidateBatch,
        RequiredMemberSetProposal,
        RequiredMemberManifest,
    ):
        assert expected.issubset(model.model_fields)
        assert "authority" not in model.model_fields
        assert model.model_json_schema()["additionalProperties"] is False


@pytest.mark.contract
@pytest.mark.parametrize(
    ("fixture_name", "expected_type"),
    [
        ("extraction-candidate-batch-clinical.json", ExtractionCandidateBatch),
        ("required-member-set-proposal-supply.json", RequiredMemberSetProposal),
        ("required-member-manifest-media.json", RequiredMemberManifest),
    ],
)
def test_multiple_domain_valid_fixtures(
    fixture_name: str,
    expected_type: type,
) -> None:
    parsed = parse_contract(
        (FIXTURES / "valid" / fixture_name).read_text(encoding="utf-8")
    )
    assert isinstance(parsed, expected_type)
    assert parse_contract(canonical_json(parsed)) == parsed


@pytest.mark.contract
@pytest.mark.parametrize(
    "fixture_name",
    [
        "extraction-unknown-field.json",
        "extraction-wrong-version.json",
        "required-member-duplicate-id.json",
        "required-member-missing-id.json",
        "required-member-collection-hash.json",
    ],
)
def test_extraction_invalid_fixtures_fail_closed(fixture_name: str) -> None:
    with pytest.raises((ValidationError, ValueError)):
        parse_contract(
            (FIXTURES / "invalid" / fixture_name).read_text(encoding="utf-8")
        )


@pytest.mark.contract
def test_candidate_accounting_is_core_compatible_and_mutually_exclusive() -> None:
    sealed = batch()
    assert sealed.input_candidate_count == 3
    assert sealed.retained_candidate_count == 2
    assert sealed.deduplicated_input_count == 1
    assert {item.candidate_id for item in sealed.candidates} == {
        "candidate:alpha",
        "candidate:beta",
    }
    invalid = sealed.model_dump(mode="python")
    invalid["candidate_dispositions"][0]["deduplicated_into_candidate_id"] = (
        "candidate:beta"
    )
    with pytest.raises(ValidationError):
        ExtractionCandidateBatch.model_validate(invalid)


@pytest.mark.contract
def test_candidate_batch_sorts_set_like_carriers_and_seals_hashes() -> None:
    sealed = batch()
    assert tuple(item.candidate_id for item in sealed.candidates) == (
        "candidate:alpha",
        "candidate:beta",
    )
    assert sealed.candidate_id_set_hash == canonical_sha256(
        ["candidate:alpha", "candidate:beta"]
    )
    assert sealed.batch_hash == canonical_sha256(
        sealed.model_dump(mode="json", exclude={"batch_hash"})
    )
    with pytest.raises(ValidationError, match="candidate_id_set_hash"):
        sealed.model_copy(update={"candidate_id_set_hash": HASH_A})


@pytest.mark.contract
def test_proposal_cannot_broaden_or_reinterpret_batch_authority() -> None:
    source_batch = batch()
    source_proposal = proposal(source_batch)
    source_proposal.validate_against_batch(source_batch)

    changed = source_proposal.model_dump(mode="python", exclude={"proposal_hash"})
    changed["hierarchy_hash"] = HASH_A
    changed["proposal_hash"] = canonical_sha256(changed)
    reinterpreted = RequiredMemberSetProposal.model_validate(changed)
    with pytest.raises(ValueError, match="authority"):
        reinterpreted.validate_against_batch(source_batch)


@pytest.mark.contract
def test_proposal_cannot_relabel_candidate_semantic_type() -> None:
    source_batch = batch()
    relabeled = proposal(
        source_batch,
        members=(
            member(
                "canonical-member:alpha",
                0,
                "candidate:alpha",
                member_type="semantic-type:reinterpreted",
            ),
        ),
    )
    with pytest.raises(ValueError, match="semantic type"):
        relabeled.validate_against_batch(source_batch)


@pytest.mark.contract
def test_member_roles_order_and_cardinality_are_domain_neutral_data() -> None:
    source_proposal = proposal()
    assert [item.member_order for item in source_proposal.members] == [0, 1]
    assert [
        (item.minimum_cardinality, item.maximum_cardinality)
        for item in source_proposal.members
    ] == [(1, 1), (0, None)]
    assert {item.member_role_id for item in source_proposal.members} == {
        "member-role:required",
        "member-role:secondary",
    }
    production_schema = json.dumps(RequiredMemberReference.model_json_schema())
    for forbidden in ("Procedure", "Step", "Tool", "School"):
        assert forbidden not in production_schema


@pytest.mark.contract
def test_duplicate_missing_or_out_of_order_members_fail() -> None:
    duplicate = (
        member("canonical-member:same", 0, "candidate:alpha"),
        member("canonical-member:same", 1, "candidate:beta"),
    )
    with pytest.raises(ValidationError, match="member_canonical_id"):
        proposal(members=duplicate)
    unordered = (
        member("canonical-member:beta", 1, "candidate:beta"),
        member("canonical-member:alpha", 0, "candidate:alpha"),
    )
    with pytest.raises(ValidationError, match="ascending member_order"):
        proposal(members=unordered)
    missing = member(
        "canonical-member:alpha",
        0,
        "candidate:alpha",
    ).model_dump(mode="python")
    missing.pop("member_canonical_id")
    with pytest.raises(ValidationError):
        RequiredMemberReference.model_validate(missing)
    invalid_bounds = member(
        "canonical-member:alpha",
        0,
        "candidate:alpha",
    ).model_dump(mode="python")
    invalid_bounds["minimum_cardinality"] = 2
    invalid_bounds["maximum_cardinality"] = 1
    with pytest.raises(ValidationError, match="maximum_cardinality"):
        RequiredMemberReference.model_validate(invalid_bounds)


@pytest.mark.contract
def test_l3_manifest_is_deterministic_except_operational_timestamp() -> None:
    source_proposal = proposal()
    first = manifest(source_proposal)
    second = RequiredMemberManifest.seal_from_proposal(
        source_proposal,
        identity=identity("c0.required_member_manifest"),
        required_member_manifest_id=first.required_member_manifest_id,
        validator_name=first.validator_name,
        validator_version=first.validator_version,
        sealed_at_utc=datetime(2026, 8, 24, 21, 0, tzinfo=timezone.utc),
    )
    assert (
        first.authoritative_collection_hash
        == second.authoritative_collection_hash
    )
    assert first.manifest_hash == second.manifest_hash
    first.validate_against_proposal(source_proposal)


@pytest.mark.contract
def test_l3_manifest_factory_rejects_cross_scope_identity() -> None:
    source_proposal = proposal()
    unrelated_identity = identity("c0.required_member_manifest").model_copy(
        update={"project_id": "another-project"}
    )
    with pytest.raises(ValueError, match="identity scope"):
        RequiredMemberManifest.seal_from_proposal(
            source_proposal,
            identity=unrelated_identity,
            required_member_manifest_id="manifest:cross-scope",
            validator_name="local-deterministic-validator",
            validator_version="1.0.0",
            sealed_at_utc=NOW,
        )


@pytest.mark.contract
def test_manifest_rejects_membership_or_policy_reinterpretation() -> None:
    source_proposal = proposal()
    sealed = manifest(source_proposal)
    changed = sealed.model_dump(mode="python", exclude={"manifest_hash"})
    changed["members"][0]["maximum_cardinality"] = 2
    changed["authoritative_collection_hash"] = authoritative_collection_hash(
        authority=ExtractionAuthorityReferences.model_validate(
            {
                field: changed[field]
                for field in ExtractionAuthorityReferences.model_fields
            }
        ),
        scope_canonical_id=changed["scope_canonical_id"],
        membership_semantic_relationship_id=(
            changed["membership_semantic_relationship_id"]
        ),
        members=tuple(
            RequiredMemberReference.model_validate(item)
            for item in changed["members"]
        ),
    )
    semantic = copy.deepcopy(changed)
    semantic.pop("sealed_at_utc")
    changed["manifest_hash"] = canonical_sha256(semantic)
    reinterpreted = RequiredMemberManifest.model_validate(changed)
    with pytest.raises(ValueError, match="reinterprets"):
        reinterpreted.validate_against_proposal(source_proposal)


def verified_evidence() -> EvidenceSpan:
    locator_values = {
        "locator_version": "1.0",
        "blob_uri": "https://storage.example.test/source/unit.txt",
        "blob_version_id": "version-1",
        "source_uri": None,
        "page": 0,
        "sheet": None,
        "slide": None,
        "section_path": ("section",),
        "cell_range": None,
        "char_start": None,
        "char_end": None,
        "polygon": None,
        "sheet_zone": None,
        "tile_id": None,
        "coordinate_system": None,
        "transform": None,
        "native_layer_id": None,
        "native_object_id": None,
    }
    locator = ImmutableSourceLocator(
        **locator_values,
        locator_hash=canonical_sha256(locator_values),
    )
    unit = SourceUnit.mint(
        identity=identity(
            "c0.source_unit",
            source_file_id="source-file:fixture",
            source_unit_id="replaced-by-mint",
            content_hash=HASH_A,
            locator=locator,
        ),
        unit_kind="paragraph",
        text="Neutral source evidence.",
        ordinal=0,
        locator=locator,
    )
    return EvidenceSpan.mint_verified(
        source_unit=unit,
        span_start=0,
        span_end=7,
        verifier_name="local-verifier",
        verifier_version="1.0.0",
        verified_at_utc=NOW,
    )


@pytest.mark.contract
def test_references_resolve_only_to_core_lifecycle_and_evidence() -> None:
    evidence = verified_evidence()
    lifecycle = CandidateLifecycleRecord.seal(
        identity=identity("c0.candidate_lifecycle_record"),
        lifecycle_record_id="lifecycle:candidate:alpha",
        candidate_id="candidate:alpha",
        candidate_version_id="candidate:alpha:version-1",
        candidate_kind="entity",
        sequence=0,
        prior_lifecycle_record_id=None,
        from_state=None,
        to_state=AssertionState.PROPOSED,
        reason_codes=(),
        evidence_span_ids=(evidence.evidence_span_id,),
        governance_justification_id=None,
        resolved_source_entity_id=None,
        resolved_target_entity_id=None,
        source_inheritance_path=(),
        target_inheritance_path=(),
        validator_name="local-validator",
        validator_version="1.0.0",
        occurred_at_utc=NOW,
    )
    source_batch = ExtractionCandidateBatch.seal(
        identity=identity("c0.extraction_candidate_batch"),
        extraction_candidate_batch_id="batch:core-reference",
        authority=authority(),
        input_candidate_count=1,
        retained_candidate_count=1,
        deduplicated_input_count=0,
        candidates=(
            candidate(
                "candidate:alpha",
                0,
                evidence_span_ids=(evidence.evidence_span_id,),
            ),
        ),
        candidate_dispositions=(
            accounting("input:alpha", retained_id="candidate:alpha"),
        ),
    )
    source_batch.validate_core_references(
        lifecycle_records=(lifecycle,),
        evidence_spans=(evidence,),
    )
    with pytest.raises(ValueError, match="evidence reference does not resolve"):
        source_batch.validate_core_references(
            lifecycle_records=(lifecycle,),
            evidence_spans=(),
        )


@pytest.mark.contract
def test_proposal_member_evidence_must_come_from_candidate() -> None:
    evidence_id = "evidence-span:" + "1" * 32
    source_batch = batch(evidence_span_ids=(evidence_id,))
    valid = proposal(
        source_batch,
        members=(
            member(
                "canonical-member:alpha",
                0,
                "candidate:alpha",
                member_type="semantic-type:generic-0",
                evidence_span_ids=(evidence_id,),
            ),
        ),
    )
    valid.validate_against_batch(source_batch)
    unrelated = "evidence-span:" + "2" * 32
    invalid = proposal(
        source_batch,
        members=(
            member(
                "canonical-member:alpha",
                0,
                "candidate:alpha",
                member_type="semantic-type:generic-0",
                evidence_span_ids=(unrelated,),
            ),
        ),
    )
    with pytest.raises(ValueError, match="not carried"):
        invalid.validate_against_batch(source_batch)


@pytest.mark.contract
def test_receipt_accepts_exact_extraction_versions_without_new_stage_behavior() -> None:
    accepted = {
        "c0.extraction_candidate_batch": "==1.0.0",
        "c0.required_member_set_proposal": "==1.0.0",
        "c0.required_member_manifest": "==1.0.0",
    }
    values = {
        "identity": identity("c0.stage_receipt"),
        "stage_receipt_id": "receipt:l3-validation",
        "stage_id": "L3",
        "stage_name": "Evidence Validation",
        "stage_contract_version": CONTRACT_VERSION,
        "status": "succeeded",
        "input_manifest_id": "manifest:l2-output",
        "input_manifest_hash": HASH_A,
        "output_manifest_id": "manifest:l3-output",
        "output_manifest_hash": HASH_B,
        "skip_key": HASH_C,
        "accepted_contract_versions": accepted,
        "resource_metrics_id": "metrics:l3-validation",
        "resource_metrics_hash": HASH_D,
        "attempt_count": 1,
        "remote_operation_refs": (),
        "error_codes": (),
        "started_at_utc": NOW,
        "completed_at_utc": NOW,
    }
    semantic_values = dict(values)
    semantic_values.pop("started_at_utc")
    semantic_values.pop("completed_at_utc")
    receipt = StageReceipt(
        **values,
        receipt_hash=canonical_sha256(semantic_values),
    )
    assert dict(receipt.accepted_contract_versions) == accepted


@pytest.mark.contract
def test_extraction_golden_canonical_json_and_hash() -> None:
    parsed = parse_contract(
        (
            FIXTURES / "valid" / "required-member-manifest-media.json"
        ).read_text(encoding="utf-8")
    )
    expected_json = (
        FIXTURES / "golden" / "required-member-manifest.canonical.json"
    ).read_text(encoding="utf-8").rstrip("\n")
    expected_hash = (
        FIXTURES / "golden" / "required-member-manifest.sha256"
    ).read_text(encoding="utf-8").strip()
    assert canonical_json(parsed) == expected_json
    assert canonical_sha256(parsed) == expected_hash
