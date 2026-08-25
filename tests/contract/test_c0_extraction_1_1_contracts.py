"""Additive C0.Extraction 1.1 member carrier contracts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from fabric_kg_builder.contracts import (
    CanonicalIdentityEnvelope,
    ExtractionAuthorityReferences,
    ExtractionCandidateBatch,
    RequiredMemberManifest,
    RequiredMemberManifestV1_1,
    RequiredMemberMigrationError,
    RequiredMemberOrderingPolicyV1_1,
    RequiredMemberReference,
    RequiredMemberReferenceV1_1,
    RequiredMemberSetProposal,
    RequiredMemberSetProposalV1_1,
    TrustedRequiredMemberPolicyContextV1_1,
    adapt_required_member_manifest_v1_0_to_v1_1,
    adapt_required_member_set_proposal_v1_0_to_v1_1,
    canonical_json,
    canonical_sha256,
    negotiate_contract,
    parse_contract,
    write_registered_schemas,
)
from fabric_kg_builder.contracts.extraction import (
    RequiredMemberManifestIdentityV1_1,
    RequiredMemberSetProposalIdentityV1_1,
)

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
    version: str,
    *,
    domain_schema_version: str = "2.0",
) -> (
    CanonicalIdentityEnvelope
    | RequiredMemberSetProposalIdentityV1_1
    | RequiredMemberManifestIdentityV1_1
):
    values = {
        "contract_kind": kind,
        "contract_version": version,
        "project_id": "project-c0-1-1",
        "asset_id": None,
        "asset_version_id": None,
        "run_id": "run-c0-1-1",
        "source_file_id": None,
        "source_unit_id": None,
        "content_hash": None,
        "domain_schema_version": domain_schema_version,
        "domain_contract_hash": HASH_B,
        "semantic_contract_hash": None,
        "canonical_schema_version": "2.0",
        "prompt_version": None,
        "prompt_hash": None,
        "model_version": None,
        "model_hash": None,
        "extractor_name": "fixture-extractor",
        "extractor_version": "1.1.0",
        "parent_artifact_ids": ("artifact-input",),
        "parent_record_ids": (),
        "immutable_locator": None,
    }
    if kind == "c0.required_member_set_proposal" and version == "1.1.0":
        return RequiredMemberSetProposalIdentityV1_1.model_validate(values)
    if kind == "c0.required_member_manifest" and version == "1.1.0":
        return RequiredMemberManifestIdentityV1_1.model_validate(values)
    return CanonicalIdentityEnvelope.model_validate(values)


def authority() -> ExtractionAuthorityReferences:
    return ExtractionAuthorityReferences(
        source_corpus_manifest_id="manifest:source-corpus",
        source_corpus_manifest_hash=HASH_A,
        source_unit_manifest_id="manifest:source-units",
        source_unit_manifest_hash=HASH_F,
        domain_contract_hash=HASH_B,
        completeness_requirement_id="completeness-requirement:member-set",
        completeness_requirement_hash=HASH_C,
        hierarchy_hash=HASH_D,
        identity_policy_hash=HASH_E,
    )


def unordered_policy() -> RequiredMemberOrderingPolicyV1_1:
    return RequiredMemberOrderingPolicyV1_1(mode="unordered")


def ordered_policy() -> RequiredMemberOrderingPolicyV1_1:
    return RequiredMemberOrderingPolicyV1_1(
        mode="ordered",
        ordinal_property_id="property:member-position",
        ordinal_value_type="integer",
        direction="ascending",
        unique_ordinals=True,
        contiguous=True,
        member_order_encoding="zero_based_contiguous",
    )


def member_v1_1(
    member_id: str,
    candidate_id: str,
    *,
    semantic_type_id: str = "semantic-type:member",
    role_id: str | None = None,
    order: int | None = None,
) -> RequiredMemberReferenceV1_1:
    return RequiredMemberReferenceV1_1.seal(
        member_canonical_id=member_id,
        member_semantic_type_id=semantic_type_id,
        member_role_id=role_id,
        member_order=order,
        candidate_id=candidate_id,
        supporting_evidence_span_ids=(),
    )


def proposal_v1_1(
    *,
    ordering_policy: RequiredMemberOrderingPolicyV1_1 | None = None,
    required_role_ids: tuple[str, ...] = (),
    members: tuple[RequiredMemberReferenceV1_1, ...] | None = None,
    expected_cardinality: int | None = None,
    minimum_cardinality: int | None = None,
    maximum_cardinality: int | None = None,
) -> RequiredMemberSetProposalV1_1:
    policy = ordering_policy or unordered_policy()
    source_members = members or (
        member_v1_1(
            "canonical-member:beta",
            "candidate:beta",
            semantic_type_id="semantic-type:beta",
        ),
        member_v1_1(
            "canonical-member:alpha",
            "candidate:alpha",
            semantic_type_id="semantic-type:alpha",
        ),
    )
    return RequiredMemberSetProposalV1_1.seal(
        identity=identity("c0.required_member_set_proposal", "1.1.0"),
        required_member_set_proposal_id="proposal:c0-1-1",
        extraction_candidate_batch_id="batch:c0-1-1",
        extraction_candidate_batch_hash=HASH_A,
        authority=authority(),
        scope_canonical_id="canonical-scope:c0-1-1",
        membership_semantic_relationship_id="relationship:contains",
        ordering_policy=policy,
        expected_cardinality=expected_cardinality,
        minimum_cardinality=minimum_cardinality,
        maximum_cardinality=maximum_cardinality,
        required_role_ids=required_role_ids,
        members=source_members,
    )


def manifest_v1_1(
    proposal: RequiredMemberSetProposalV1_1,
) -> RequiredMemberManifestV1_1:
    return RequiredMemberManifestV1_1.seal_from_proposal(
        proposal,
        identity=identity("c0.required_member_manifest", "1.1.0"),
        required_member_manifest_id="manifest:c0-1-1",
        validator_name="local-deterministic-validator",
        validator_version="1.1.0",
        sealed_at_utc=NOW,
    )


def legacy_member(
    member_id: str,
    candidate_id: str,
    *,
    role_id: str,
    order: int,
    minimum: int = 2,
    maximum: int | None = 2,
) -> RequiredMemberReference:
    return RequiredMemberReference(
        member_canonical_id=member_id,
        member_semantic_type_id="semantic-type:member",
        member_role_id=role_id,
        member_order=order,
        minimum_cardinality=minimum,
        maximum_cardinality=maximum,
        candidate_id=candidate_id,
        supporting_evidence_span_ids=(),
    )


def legacy_proposal(
    *,
    members: tuple[RequiredMemberReference, ...] | None = None,
) -> RequiredMemberSetProposal:
    return RequiredMemberSetProposal.seal(
        identity=identity("c0.required_member_set_proposal", "1.0.0"),
        required_member_set_proposal_id="proposal:legacy",
        extraction_candidate_batch_id="batch:legacy",
        extraction_candidate_batch_hash=HASH_A,
        authority=authority(),
        scope_canonical_id="canonical-scope:legacy",
        membership_semantic_relationship_id="relationship:contains",
        members=members
        or (
            legacy_member(
                "canonical-member:origin",
                "candidate:origin",
                role_id="role:origin",
                order=0,
            ),
            legacy_member(
                "canonical-member:destination",
                "candidate:destination",
                role_id="role:destination",
                order=1,
            ),
        ),
    )


def trusted_legacy_policy(
    *,
    expected: int | None = 2,
    minimum: int | None = 2,
    maximum: int | None = 2,
) -> TrustedRequiredMemberPolicyContextV1_1:
    return TrustedRequiredMemberPolicyContextV1_1(
        domain_contract_hash=HASH_B,
        completeness_requirement_id="completeness-requirement:member-set",
        completeness_requirement_hash=HASH_C,
        hierarchy_hash=HASH_D,
        identity_policy_hash=HASH_E,
        ordering_policy=ordered_policy(),
        expected_cardinality=expected,
        minimum_cardinality=minimum,
        maximum_cardinality=maximum,
        required_role_ids=("role:origin", "role:destination"),
    )


@pytest.mark.contract
def test_1_1_registry_is_additive_and_candidate_batch_stays_1_0() -> None:
    assert (
        negotiate_contract("c0.required_member_set_proposal", "1.0.0")
        is RequiredMemberSetProposal
    )
    assert (
        negotiate_contract("c0.required_member_set_proposal", "1.1.0")
        is RequiredMemberSetProposalV1_1
    )
    assert (
        negotiate_contract("c0.required_member_manifest", "1.0.0")
        is RequiredMemberManifest
    )
    assert (
        negotiate_contract("c0.required_member_manifest", "1.1.0")
        is RequiredMemberManifestV1_1
    )
    assert (
        negotiate_contract("c0.extraction_candidate_batch", "1.0.0")
        is ExtractionCandidateBatch
    )
    with pytest.raises(ValueError):
        negotiate_contract("c0.extraction_candidate_batch", "1.1.0")


@pytest.mark.contract
def test_roleless_unordered_members_canonicalize_without_invention() -> None:
    proposal = proposal_v1_1(minimum_cardinality=1, maximum_cardinality=4)
    assert [item.member_canonical_id for item in proposal.members] == [
        "canonical-member:alpha",
        "canonical-member:beta",
    ]
    assert all(item.member_role_id is None for item in proposal.members)
    assert all(item.member_order is None for item in proposal.members)
    assert proposal.required_role_ids == ()
    assert proposal.ordered_member_tuple_hash is None

    schema = json.dumps(RequiredMemberReferenceV1_1.model_json_schema())
    assert '"member_role_id"' in schema
    assert '"member_order"' in schema
    assert '"default": null' in schema
    assert "minimum_cardinality" not in schema
    assert "maximum_cardinality" not in schema


@pytest.mark.contract
def test_role_and_order_policy_dimensions_are_independent() -> None:
    ordered_roleless = proposal_v1_1(
        ordering_policy=ordered_policy(),
        members=(
            member_v1_1(
                "canonical-member:alpha",
                "candidate:alpha",
                order=0,
            ),
        ),
    )
    assert ordered_roleless.members[0].member_order == 0
    assert ordered_roleless.members[0].member_role_id is None

    unordered_role_bearing = proposal_v1_1(
        required_role_ids=("role:subject",),
        members=(
            member_v1_1(
                "canonical-member:subject",
                "candidate:subject",
                role_id="role:subject",
            ),
        ),
    )
    assert unordered_role_bearing.members[0].member_order is None
    assert unordered_role_bearing.members[0].member_role_id == "role:subject"

    defaults = RequiredMemberReferenceV1_1.seal(
        member_canonical_id="canonical-member:defaulted-optionals",
        member_semantic_type_id="semantic-type:member",
        candidate_id="candidate:defaulted-optionals",
    )
    assert defaults.member_role_id is None
    assert defaults.member_order is None
    assert defaults.supporting_evidence_span_ids == ()

    evidence_ids = (
        "evidence-span:" + "2" * 32,
        "evidence-span:" + "1" * 32,
    )
    canonical_evidence = RequiredMemberReferenceV1_1.seal(
        member_canonical_id="canonical-member:evidence",
        member_semantic_type_id="semantic-type:member",
        candidate_id="candidate:evidence",
        supporting_evidence_span_ids=evidence_ids,
    )
    assert canonical_evidence.supporting_evidence_span_ids == tuple(
        sorted(evidence_ids)
    )


@pytest.mark.contract
def test_role_bearing_ordered_members_and_hashes_are_deterministic() -> None:
    members = (
        member_v1_1(
            "canonical-member:destination",
            "candidate:destination",
            role_id="role:destination",
            order=1,
        ),
        member_v1_1(
            "canonical-member:origin",
            "candidate:origin",
            role_id="role:origin",
            order=0,
        ),
    )
    proposal = proposal_v1_1(
        ordering_policy=ordered_policy(),
        required_role_ids=("role:origin", "role:destination"),
        members=members,
        expected_cardinality=2,
        minimum_cardinality=2,
        maximum_cardinality=2,
    )
    assert [item.member_order for item in proposal.members] == [0, 1]
    assert proposal.ordered_member_tuple_hash == canonical_sha256(
        [
            (item.member_order, item.member_canonical_id, item.member_hash)
            for item in proposal.members
        ]
    )
    assert proposal.member_set_hash == canonical_sha256(
        sorted((item.member_canonical_id, item.member_hash) for item in members)
    )
    assert proposal_v1_1(
        ordering_policy=ordered_policy(),
        required_role_ids=("role:destination", "role:origin"),
        members=tuple(reversed(members)),
        expected_cardinality=2,
        minimum_cardinality=2,
        maximum_cardinality=2,
    ).authoritative_collection_hash == proposal.authoritative_collection_hash


@pytest.mark.contract
def test_collection_cardinality_preserves_exact_optional_bounds() -> None:
    proposal = proposal_v1_1(
        expected_cardinality=3,
        minimum_cardinality=1,
        maximum_cardinality=5,
    )
    assert (
        proposal.expected_cardinality,
        proposal.minimum_cardinality,
        proposal.maximum_cardinality,
    ) == (3, 1, 5)

    for updates in (
        {"minimum_cardinality": 4, "maximum_cardinality": 3},
        {
            "expected_cardinality": 0,
            "minimum_cardinality": 1,
            "maximum_cardinality": 3,
        },
        {
            "expected_cardinality": 4,
            "minimum_cardinality": 1,
            "maximum_cardinality": 3,
        },
    ):
        with pytest.raises(ValidationError):
            proposal_v1_1(**updates)


@pytest.mark.contract
def test_ordered_members_reject_missing_duplicate_and_gapped_orders() -> None:
    role_ids = ("role:origin", "role:destination")
    cases = (
        (
            member_v1_1(
                "canonical-member:origin",
                "candidate:origin",
                role_id="role:origin",
            ),
            member_v1_1(
                "canonical-member:destination",
                "candidate:destination",
                role_id="role:destination",
                order=1,
            ),
        ),
        (
            member_v1_1(
                "canonical-member:origin",
                "candidate:origin",
                role_id="role:origin",
                order=0,
            ),
            member_v1_1(
                "canonical-member:destination",
                "candidate:destination",
                role_id="role:destination",
                order=0,
            ),
        ),
        (
            member_v1_1(
                "canonical-member:origin",
                "candidate:origin",
                role_id="role:origin",
                order=0,
            ),
            member_v1_1(
                "canonical-member:destination",
                "candidate:destination",
                role_id="role:destination",
                order=2,
            ),
        ),
    )
    for members in cases:
        with pytest.raises(ValidationError, match="member_order"):
            proposal_v1_1(
                ordering_policy=ordered_policy(),
                required_role_ids=role_ids,
                members=members,
            )


@pytest.mark.contract
def test_roles_must_be_approved_and_sentinels_are_prohibited() -> None:
    with pytest.raises(ValidationError, match="not approved"):
        proposal_v1_1(
            ordering_policy=ordered_policy(),
            required_role_ids=("role:origin",),
            members=(
                member_v1_1(
                    "canonical-member:origin",
                    "candidate:origin",
                    role_id="role:invented",
                    order=0,
                ),
            ),
        )
    with pytest.raises(ValidationError, match="sentinel"):
        member_v1_1(
            "canonical-member:unknown",
            "candidate:unknown",
            role_id="role:unspecified",
            order=0,
        )


@pytest.mark.contract
def test_collection_hash_changes_with_policy_or_authority() -> None:
    base = proposal_v1_1(minimum_cardinality=1, maximum_cardinality=4)
    changed_bound = proposal_v1_1(minimum_cardinality=2, maximum_cardinality=4)
    assert (
        base.authoritative_collection_hash
        != changed_bound.authoritative_collection_hash
    )

    payload = base.model_dump(mode="python", exclude={"proposal_hash"})
    payload["hierarchy_hash"] = HASH_A
    payload["authoritative_collection_hash"] = HASH_A
    payload["proposal_hash"] = canonical_sha256(payload)
    with pytest.raises(ValidationError, match="authoritative_collection_hash"):
        RequiredMemberSetProposalV1_1.model_validate(payload)


@pytest.mark.contract
def test_manifest_seals_exact_proposal_and_enforces_cardinality() -> None:
    source = proposal_v1_1(
        expected_cardinality=2,
        minimum_cardinality=1,
        maximum_cardinality=3,
    )
    sealed = manifest_v1_1(source)
    sealed.validate_against_proposal(source)
    assert sealed.authoritative_collection_hash == source.authoritative_collection_hash
    assert sealed.members == source.members
    assert sealed.ordering_policy == source.ordering_policy

    different = proposal_v1_1(
        expected_cardinality=2,
        minimum_cardinality=2,
        maximum_cardinality=3,
    )
    with pytest.raises(ValueError, match="proposal reference"):
        sealed.validate_against_proposal(different)

    incomplete = proposal_v1_1(
        expected_cardinality=3,
        minimum_cardinality=1,
        maximum_cardinality=4,
    )
    with pytest.raises(ValidationError, match="expected_cardinality"):
        manifest_v1_1(incomplete)


@pytest.mark.contract
def test_1_0_manifest_golden_and_hash_remain_exact() -> None:
    parsed = parse_contract(
        (FIXTURES / "valid" / "required-member-manifest-media.json").read_text(
            encoding="utf-8"
        )
    )
    assert isinstance(parsed, RequiredMemberManifest)
    expected_json = (
        FIXTURES / "golden" / "required-member-manifest.canonical.json"
    ).read_text(encoding="utf-8").rstrip("\n")
    expected_hash = (
        FIXTURES / "golden" / "required-member-manifest.sha256"
    ).read_text(encoding="utf-8").strip()
    assert canonical_json(parsed) == expected_json
    assert canonical_sha256(parsed) == expected_hash


@pytest.mark.contract
def test_legacy_adapter_accepts_only_unambiguous_ordered_role_policy() -> None:
    legacy = legacy_proposal()
    adapted = adapt_required_member_set_proposal_v1_0_to_v1_1(
        legacy,
        trusted_policy=trusted_legacy_policy(),
    )
    assert adapted.identity.contract_version == "1.1.0"
    assert adapted.expected_cardinality == 2
    assert adapted.minimum_cardinality == 2
    assert adapted.maximum_cardinality == 2
    assert adapted.required_role_ids == ("role:destination", "role:origin")

    sentinel = legacy_proposal(
        members=(
            legacy_member(
                "canonical-member:origin",
                "candidate:origin",
                role_id="role:unspecified",
                order=0,
            ),
            legacy_member(
                "canonical-member:destination",
                "candidate:destination",
                role_id="role:destination",
                order=1,
            ),
        )
    )
    with pytest.raises(RequiredMemberMigrationError, match="sentinel"):
        adapt_required_member_set_proposal_v1_0_to_v1_1(
            sentinel,
            trusted_policy=trusted_legacy_policy(),
        )

    gapped = legacy_proposal(
        members=(
            legacy_member(
                "canonical-member:origin",
                "candidate:origin",
                role_id="role:origin",
                order=0,
            ),
            legacy_member(
                "canonical-member:destination",
                "candidate:destination",
                role_id="role:destination",
                order=2,
            ),
        )
    )
    with pytest.raises(RequiredMemberMigrationError, match="contiguous"):
        adapt_required_member_set_proposal_v1_0_to_v1_1(
            gapped,
            trusted_policy=trusted_legacy_policy(),
        )

    inconsistent = legacy_proposal(
        members=(
            legacy_member(
                "canonical-member:origin",
                "candidate:origin",
                role_id="role:origin",
                order=0,
                minimum=1,
            ),
            legacy_member(
                "canonical-member:destination",
                "candidate:destination",
                role_id="role:destination",
                order=1,
                minimum=2,
            ),
        )
    )
    with pytest.raises(RequiredMemberMigrationError, match="inconsistent"):
        adapt_required_member_set_proposal_v1_0_to_v1_1(
            inconsistent,
            trusted_policy=trusted_legacy_policy(),
        )


@pytest.mark.contract
def test_legacy_adapter_refuses_to_recover_discarded_expected_count() -> None:
    legacy = legacy_proposal(
        members=(
            legacy_member(
                "canonical-member:origin",
                "candidate:origin",
                role_id="role:origin",
                order=0,
                minimum=1,
                maximum=3,
            ),
            legacy_member(
                "canonical-member:destination",
                "candidate:destination",
                role_id="role:destination",
                order=1,
                minimum=1,
                maximum=3,
            ),
        )
    )
    with pytest.raises(RequiredMemberMigrationError, match="expected cardinality"):
        adapt_required_member_set_proposal_v1_0_to_v1_1(
            legacy,
            trusted_policy=trusted_legacy_policy(
                expected=2,
                minimum=1,
                maximum=3,
            ),
        )


@pytest.mark.contract
def test_1_1_requires_domain_v2_authority() -> None:
    identity_payload = identity(
        "c0.required_member_set_proposal",
        "1.1.0",
    ).model_dump(mode="python")
    missing = dict(identity_payload)
    missing.pop("domain_schema_version")
    with pytest.raises(ValidationError, match="domain_schema_version"):
        RequiredMemberSetProposalV1_1.seal(
            identity=missing,
            required_member_set_proposal_id="proposal:missing-domain-schema",
            extraction_candidate_batch_id="batch:missing-domain-schema",
            extraction_candidate_batch_hash=HASH_A,
            authority=authority(),
            scope_canonical_id="canonical-scope:missing-domain-schema",
            membership_semantic_relationship_id="relationship:contains",
            ordering_policy=unordered_policy(),
            members=(),
        )

    domain_v1_identity = dict(identity_payload)
    domain_v1_identity["domain_schema_version"] = "1.0"
    with pytest.raises(ValidationError, match="domain_schema_version"):
        RequiredMemberSetProposalV1_1.seal(
            identity=domain_v1_identity,
            required_member_set_proposal_id="proposal:domain-v1",
            extraction_candidate_batch_id="batch:domain-v1",
            extraction_candidate_batch_hash=HASH_A,
            authority=authority(),
            scope_canonical_id="canonical-scope:domain-v1",
            membership_semantic_relationship_id="relationship:contains",
            ordering_policy=unordered_policy(),
            members=(),
        )

    domain_v1_batch = ExtractionCandidateBatch.seal(
        identity=identity(
            "c0.extraction_candidate_batch",
            "1.0.0",
            domain_schema_version="1.0",
        ),
        extraction_candidate_batch_id="batch:domain-v1",
        authority=authority(),
        input_candidate_count=0,
        retained_candidate_count=0,
        deduplicated_input_count=0,
        candidates=(),
        candidate_dispositions=(),
    )
    domain_v2_proposal = RequiredMemberSetProposalV1_1.seal(
        identity=identity("c0.required_member_set_proposal", "1.1.0"),
        required_member_set_proposal_id="proposal:domain-v2",
        extraction_candidate_batch_id=domain_v1_batch.extraction_candidate_batch_id,
        extraction_candidate_batch_hash=domain_v1_batch.batch_hash,
        authority=authority(),
        scope_canonical_id="canonical-scope:domain-v2",
        membership_semantic_relationship_id="relationship:contains",
        ordering_policy=unordered_policy(),
        members=(),
    )
    with pytest.raises(ValueError, match="Domain schema authority"):
        domain_v2_proposal.validate_against_batch(domain_v1_batch)

    legacy = legacy_proposal()
    legacy_payload = legacy.model_dump(mode="python", exclude={"proposal_hash"})
    legacy_payload["identity"]["domain_schema_version"] = "1.0"
    legacy_payload["proposal_hash"] = canonical_sha256(legacy_payload)
    domain_v1 = RequiredMemberSetProposal.model_validate(legacy_payload)
    with pytest.raises(RequiredMemberMigrationError, match="DomainContractV2"):
        adapt_required_member_set_proposal_v1_0_to_v1_1(
            domain_v1,
            trusted_policy=trusted_legacy_policy(),
        )


@pytest.mark.contract
def test_legacy_manifest_adapter_requires_exact_legacy_seal() -> None:
    legacy = legacy_proposal()
    legacy_manifest = RequiredMemberManifest.seal_from_proposal(
        legacy,
        identity=identity("c0.required_member_manifest", "1.0.0"),
        required_member_manifest_id="manifest:legacy",
        validator_name="local-deterministic-validator",
        validator_version="1.0.0",
        sealed_at_utc=NOW,
    )
    adapted_proposal = adapt_required_member_set_proposal_v1_0_to_v1_1(
        legacy,
        trusted_policy=trusted_legacy_policy(),
    )
    adapted_manifest = adapt_required_member_manifest_v1_0_to_v1_1(
        legacy_manifest,
        legacy_proposal=legacy,
        trusted_policy=trusted_legacy_policy(),
    )
    adapted_manifest.validate_against_proposal(adapted_proposal)
    assert adapted_manifest.identity.contract_version == "1.1.0"


@pytest.mark.contract
@pytest.mark.parametrize(
    ("fixture_name", "expected_type"),
    [
        (
            "required-member-set-proposal-v1.1-research.json",
            RequiredMemberSetProposalV1_1,
        ),
        (
            "required-member-manifest-v1.1-logistics.json",
            RequiredMemberManifestV1_1,
        ),
    ],
)
def test_multiple_domain_1_1_fixtures(
    fixture_name: str,
    expected_type: type,
) -> None:
    parsed = parse_contract(
        (FIXTURES / "valid" / fixture_name).read_text(encoding="utf-8")
    )
    assert isinstance(parsed, expected_type)
    assert parse_contract(canonical_json(parsed)) == parsed


@pytest.mark.contract
def test_1_1_manifest_golden_is_canonical_and_stable() -> None:
    parsed = parse_contract(
        (
            FIXTURES
            / "valid"
            / "required-member-manifest-v1.1-logistics.json"
        ).read_text(encoding="utf-8")
    )
    expected_json = (
        FIXTURES / "golden" / "required-member-manifest-1.1.canonical.json"
    ).read_text(encoding="utf-8").rstrip("\n")
    expected_hash = (
        FIXTURES / "golden" / "required-member-manifest-1.1.sha256"
    ).read_text(encoding="utf-8").strip()
    assert canonical_json(parsed) == expected_json
    assert canonical_sha256(parsed) == expected_hash


@pytest.mark.contract
def test_schema_writer_and_version_negotiation_include_both_carriers(
    tmp_path: Path,
) -> None:
    hashes = write_registered_schemas(tmp_path)
    assert (
        hashes["c0.required_member_set_proposal"]
        == hashes["c0.required_member_set_proposal@1.0.0"]
    )
    assert (
        hashes["c0.required_member_manifest"]
        == hashes["c0.required_member_manifest@1.0.0"]
    )
    for kind in (
        "c0.required_member_set_proposal",
        "c0.required_member_manifest",
    ):
        assert f"{kind}@1.1.0" in hashes
        path = tmp_path / f"{kind.replace('.', '-')}-1.1.0.schema.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["contract_version"] == "1.1.0"
        identity_schema = next(
            definition
            for name, definition in payload["schema"]["$defs"].items()
            if name.endswith("IdentityV1_1")
        )
        assert (
            identity_schema["properties"]["contract_version"]["const"]
            == "1.1.0"
        )
