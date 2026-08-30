"""Dependency-free L3 evidence, hierarchy, identity, and completeness rules."""

from __future__ import annotations

import itertools
import unicodedata
from datetime import datetime, timezone

import pytest

from fabric_kg_builder.contracts.adapters import (
    DESIGN_VERIFIER_NAME,
    TrustedL1DesignEvidenceManifestContext,
    adapt_evidence_span_v1_0_to_v1_1,
)
from fabric_kg_builder.contracts.base import (
    EvidencePurposePromotionError,
    canonical_sha256,
    deterministic_contract_id,
)
from fabric_kg_builder.contracts.evidence import (
    EvidenceSpan,
    EvidenceSpanV1_1,
    SourceUnit,
)
from fabric_kg_builder.contracts.extraction import (
    ExtractionAuthorityReferences,
    RequiredMemberOrderingPolicyV1_1,
    RequiredMemberReferenceV1_1,
)
from fabric_kg_builder.contracts.identity import (
    CanonicalIdentityEnvelope,
    ImmutableSourceLocator,
)
from fabric_kg_builder.contracts.lifecycle import (
    AssertionState,
    CandidateLifecycleRecord,
)
from fabric_kg_builder.domain.hierarchy import build_type_hierarchy_closure
from fabric_kg_builder.domain.models import (
    CardinalityExpectationV2,
    CollectionIdentityPolicyV2,
    CompletenessRequirementV2,
    DomainConstraintV2,
    DomainContractV2,
    DomainEntityTypeV2,
    DomainPropertyV2,
    DomainRelationshipTypeV2,
    GeneralizationBasisV2,
    IdentityKeyPolicyV2,
    OrderingPolicyV2,
    RelationshipIdentityPolicyV2,
    SiblingClassificationPolicyV2,
    StructuredFactSetV2,
)
from fabric_kg_builder.domain.service import load_domain_contract
from fabric_kg_builder.enrichment.schema2_evidence import (
    L3_EXTRACTION_PURPOSE,
    L3_EXTRACTION_PURPOSE_VERSION,
    L3_EXTRACTION_VERIFIER_NAME,
    L3_EXTRACTION_VERIFIER_VERSION,
    L3_SUPPORTED_EVIDENCE_UNIT_KINDS,
    NON_ASSERTABLE_WITNESS_KINDS,
    STABLE_REASON_CODES,
    AdjacencyPolicy,
    CompiledHierarchy,
    EndpointGroundingRequest,
    L3StageError,
    ProposedOccurrenceAnchor,
    VerifiedMember,
    append_current_transition,
    assert_type_independent_identity_inputs,
    classify_state,
    compile_hierarchy,
    compile_parent_closure,
    derived_stable_source_identity,
    deterministic_ancestor_path,
    evaluate_inherited_constraints,
    exact_occurrences,
    ground_endpoints,
    is_minted_contract_id,
    normalize_business_key,
    property_attribution_reasons,
    recompute_entity_id,
    recompute_observation_entity_id,
    recompute_relationship_id,
    relationship_direction_reasons,
    require_extraction_evidence,
    resolve_direction,
    resolve_identity_witness,
    resolve_most_specific_classification,
    sorted_reasons,
    unprovable_assertion_reasons,
    validate_adjacency_edges,
    validate_property_observation,
    validate_required_member_proposal,
    verify_and_mint_extraction_span,
)
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures built without any repository domain contract
# ---------------------------------------------------------------------------


def _identity(**overrides) -> CanonicalIdentityEnvelope:
    values = {
        "contract_kind": "c0.source_unit",
        "project_id": "project:l3-tests",
        "asset_id": "asset:test",
        "asset_version_id": "asset-version:test",
        "run_id": "run:l3-tests",
        "source_file_id": "source-file:test",
        "source_unit_id": None,
        "content_hash": "a" * 64,
        "domain_schema_version": "2.0",
        "domain_contract_hash": "e" * 64,
        "semantic_contract_hash": None,
        "canonical_schema_version": "2.0.0",
        "prompt_version": None,
        "prompt_hash": None,
        "model_version": None,
        "model_hash": None,
        "extractor_name": "l3-tests",
        "extractor_version": "1.0.0",
        "parent_artifact_ids": (),
        "parent_record_ids": (),
        "immutable_locator": None,
    }
    values.update(overrides)
    return CanonicalIdentityEnvelope(**values)


def _unit(text: str, *, unit_kind: str = "paragraph", ordinal: int = 0) -> SourceUnit:
    locator = ImmutableSourceLocator.from_authority(
        blob_uri="https://storage.example/source",
        blob_version_id="v1",
        char_start=0,
        char_end=max(1, len(text)),
    )
    return SourceUnit.mint(
        identity=_identity(),
        unit_kind=unit_kind,
        text=text,
        ordinal=ordinal,
        locator=locator,
    )


def _policy(namespace: str = "l3") -> IdentityKeyPolicyV2:
    return IdentityKeyPolicyV2(
        authority="user_approved",
        namespace=namespace,
        key_mode="stable_source_identity",
        business_key_fields=[],
        normalization_version="1",
        collision_behavior="block",
        missing_key_behavior="unresolved",
    )


def _business_policy(namespace: str = "l3") -> IdentityKeyPolicyV2:
    return IdentityKeyPolicyV2(
        authority="user_approved",
        namespace=namespace,
        key_mode="business_key",
        business_key_fields=["serial"],
        normalization_version="1",
        collision_behavior="block",
        missing_key_behavior="unresolved",
    )


def _entity(
    type_id: str,
    *,
    parent: str | None = None,
    root: str | None = None,
    abstract: bool = False,
    policy: IdentityKeyPolicyV2 | None = None,
    properties: tuple[DomainPropertyV2, ...] = (),
    constraints: tuple[DomainConstraintV2, ...] = (),
) -> DomainEntityTypeV2:
    return DomainEntityTypeV2(
        type_id=type_id,
        semantic_key=type_id.rsplit(".", 1)[-1],
        display_name=type_id.rsplit(".", 1)[-1].replace("-", " ").title(),
        description=f"A governed {type_id}.",
        classification="domain",
        parent_type_id=parent,
        abstract=abstract,
        identity_root_type_id=root or type_id,
        identity_key_policy=policy if parent is None else None,
        declared_properties=list(properties),
        declared_constraints=list(constraints),
        sibling_classification_policy=SiblingClassificationPolicyV2(
            mode="unresolved",
            rationale="Competing siblings stay unresolved.",
        ),
        generalization_basis=(
            None
            if parent is None
            else GeneralizationBasisV2(governance_rationale="Approved generalization.")
        ),
        governance_rationale="Approved by governance.",
    )


def _relationship(
    relationship_type_id: str,
    *,
    sources: tuple[str, ...],
    targets: tuple[str, ...],
    endpoint_policy: str = "allow_subtypes",
) -> DomainRelationshipTypeV2:
    return DomainRelationshipTypeV2(
        relationship_type_id=relationship_type_id,
        predicate_id=f"predicate:{relationship_type_id.split(':', 1)[-1]}",
        display_name=relationship_type_id.rsplit(".", 1)[-1],
        description="An approved relationship.",
        source_type_ids=list(sources),
        target_type_ids=list(targets),
        endpoint_policy=endpoint_policy,
        identity_policy=RelationshipIdentityPolicyV2(
            context_policy="governed validity context"
        ),
        governance_rationale="Approved by governance.",
    )


def _compiled(
    entities: tuple[DomainEntityTypeV2, ...],
    relationships: tuple[DomainRelationshipTypeV2, ...],
    *,
    requirements: tuple[CompletenessRequirementV2, ...] = (),
) -> CompiledHierarchy:
    """Build a CompiledHierarchy from synthetic types without a full contract."""

    closure = build_type_hierarchy_closure(entities, relationships)
    parent_by_type = {item.type_id: item.parent_type_id for item in entities}
    ancestors = compile_parent_closure(parent_by_type)
    property_by_id = {
        item.property_id: item
        for entity in entities
        for item in entity.declared_properties
    }
    constraint_by_id = {
        item.constraint_id: item
        for entity in entities
        for item in entity.declared_constraints
    }
    entity_by_id = {item.type_id: item for item in entities}
    identity_policy_by_type = {}
    for entity in entities:
        root = entity_by_id[entity.identity_root_type_id]
        assert root.identity_key_policy is not None
        identity_policy_by_type[entity.type_id] = root.identity_key_policy
    return CompiledHierarchy(
        domain_contract_hash="e" * 64,
        hierarchy_hash=closure.hierarchy_hash,
        identity_policy_hash="f" * 64,
        completeness_requirement_hash="1" * 64,
        external_reference_decision_hash="2" * 64,
        parent_by_type=parent_by_type,
        ancestors_by_type=ancestors,
        descendants_by_type={
            key: tuple(value) for key, value in closure.descendants_by_type.items()
        },
        depth_by_type={key: len(value) for key, value in ancestors.items()},
        abstract_type_ids=frozenset(
            item.type_id for item in entities if item.abstract
        ),
        effective_property_ids_by_type={
            key: tuple(value)
            for key, value in closure.effective_property_ids_by_type.items()
        },
        effective_constraint_ids_by_type={
            key: tuple(value)
            for key, value in closure.effective_constraint_ids_by_type.items()
        },
        entity_by_id=entity_by_id,
        relationship_by_id={
            item.relationship_type_id: item for item in relationships
        },
        property_by_id=property_by_id,
        constraint_by_id=constraint_by_id,
        identity_policy_by_type=identity_policy_by_type,
        compatible_source_type_ids={
            key: frozenset(value)
            for key, value in (
                closure.compatible_source_type_ids_by_relationship.items()
            )
        },
        compatible_target_type_ids={
            key: frozenset(value)
            for key, value in (
                closure.compatible_target_type_ids_by_relationship.items()
            )
        },
        requirement_by_id={
            item.requirement_id: item for item in requirements
        },
    )


def _vehicle_hierarchy() -> CompiledHierarchy:
    entities = (
        _entity("semantic-type:x.thing", abstract=True, policy=_policy("x.thing")),
        _entity(
            "semantic-type:x.vehicle",
            parent="semantic-type:x.thing",
            root="semantic-type:x.thing",
        ),
        _entity(
            "semantic-type:x.truck",
            parent="semantic-type:x.vehicle",
            root="semantic-type:x.thing",
        ),
        _entity("semantic-type:x.depot", policy=_policy("x.depot")),
    )
    relationships = (
        _relationship(
            "relationship-type:x.serves",
            sources=("semantic-type:x.depot",),
            targets=("semantic-type:x.vehicle",),
        ),
        _relationship(
            "relationship-type:x.exact-serves",
            sources=("semantic-type:x.depot",),
            targets=("semantic-type:x.vehicle",),
            endpoint_policy="exact",
        ),
    )
    return _compiled(entities, relationships)


# ---------------------------------------------------------------------------
# Exact Unicode evidence verification and minting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Facility A contains Pump 1.",
        "Aparelho é medido — 12 °C, 30 %.",
        "設備Aはポンプ1を含みます。",
        "e\u0301quipement compose\u0301",
        "emoji 👩‍🔬 and 𝔘𝔫𝔦𝔠𝔬𝔡𝔢 tail",
        "\u0041\u030a ring above",
    ],
)
def test_exact_unicode_spans_mint_and_replay_the_same_c0_identity(text: str) -> None:
    unit = _unit(text)
    for start in range(0, unit.codepoint_count):
        for end in range(start + 1, min(start + 4, unit.codepoint_count) + 1):
            anchor = ProposedOccurrenceAnchor(
                span_start=start,
                span_end=end,
                quote=unit.text[start:end],
            )
            first = verify_and_mint_extraction_span(
                source_unit=unit,
                anchor=anchor,
                verified_at_utc=_NOW,
            )
            second = verify_and_mint_extraction_span(
                source_unit=unit,
                anchor=anchor,
                verified_at_utc=_NOW,
            )
            assert first.verified and second.verified
            assert first.span.evidence_span_id == second.span.evidence_span_id
            assert first.span.quote == unit.text[start:end]
            assert first.span.span_end - first.span.span_start == end - start
            assert unicodedata.normalize("NFC", first.span.quote) == first.span.quote


@pytest.mark.parametrize(
    ("start", "end", "quote", "expected"),
    [
        (-1, 3, "abc", "EVIDENCE_SPAN_INVALID"),
        (0, 0, "", "EVIDENCE_SPAN_INVALID"),
        (5, 3, "abc", "EVIDENCE_SPAN_INVALID"),
        (0, 999, "abc", "EVIDENCE_SPAN_INVALID"),
        (0, 3, "zzz", "EVIDENCE_QUOTE_MISMATCH"),
    ],
)
def test_invalid_ranges_and_quotes_never_mint(
    start: int,
    end: int,
    quote: str,
    expected: str,
) -> None:
    unit = _unit("Facility A contains Pump 1.")

    outcome = verify_and_mint_extraction_span(
        source_unit=unit,
        anchor=ProposedOccurrenceAnchor(
            span_start=start,
            span_end=end,
            quote=quote,
        ),
        verified_at_utc=_NOW,
    )

    assert outcome.span is None
    assert expected in outcome.reason_codes


def test_shifted_anchor_offsets_are_relocated_deterministically() -> None:
    unit = _unit("Facility A contains Pump 1.")

    outcome = verify_and_mint_extraction_span(
        source_unit=unit,
        # Model reports the right quote at the wrong offsets.
        anchor=ProposedOccurrenceAnchor(span_start=14, span_end=20, quote="Pump 1"),
        verified_at_utc=_NOW,
    )

    assert outcome.span is not None
    assert outcome.span.span_start == 20
    assert outcome.span.span_end == 26
    assert outcome.span.quote == "Pump 1"
    assert unit.text[outcome.span.span_start : outcome.span.span_end] == "Pump 1"
    assert "EVIDENCE_ANCHOR_RELOCATED" in outcome.reason_codes


def test_relocation_repairs_inconsistent_model_span_arithmetic() -> None:
    unit = _unit("Facility A contains Pump 1.")

    # span_end - span_start (9) disagrees with len(quote) (8): unmatchable as-is.
    outcome = verify_and_mint_extraction_span(
        source_unit=unit,
        anchor=ProposedOccurrenceAnchor(span_start=1, span_end=10, quote="Facility"),
        verified_at_utc=_NOW,
    )

    assert outcome.span is not None
    assert (outcome.span.span_start, outcome.span.span_end) == (0, 8)
    assert "EVIDENCE_ANCHOR_RELOCATED" in outcome.reason_codes


def test_ambiguous_quote_is_never_relocated_by_guessing() -> None:
    unit = _unit("Pump 1 feeds Pump 1.")

    outcome = verify_and_mint_extraction_span(
        source_unit=unit,
        anchor=ProposedOccurrenceAnchor(span_start=2, span_end=8, quote="Pump 1"),
        verified_at_utc=_NOW,
    )

    assert outcome.span is None
    assert "EVIDENCE_QUOTE_MISMATCH" in outcome.reason_codes
    assert "EVIDENCE_ANCHOR_RELOCATED" not in outcome.reason_codes


def test_exact_anchor_is_minted_without_a_relocation_reason() -> None:
    unit = _unit("Facility A contains Pump 1.")

    outcome = verify_and_mint_extraction_span(
        source_unit=unit,
        anchor=ProposedOccurrenceAnchor(span_start=0, span_end=8, quote="Facility"),
        verified_at_utc=_NOW,
    )

    assert outcome.span is not None
    assert "EVIDENCE_ANCHOR_RELOCATED" not in outcome.reason_codes


def test_quote_absent_from_source_text_is_still_rejected() -> None:
    unit = _unit("Facility A contains Pump 1.")

    outcome = verify_and_mint_extraction_span(
        source_unit=unit,
        anchor=ProposedOccurrenceAnchor(span_start=0, span_end=7, quote="Turbine"),
        verified_at_utc=_NOW,
    )

    assert outcome.span is None
    assert "EVIDENCE_QUOTE_MISMATCH" in outcome.reason_codes


def test_missing_anchor_is_unresolved_and_source_drift_is_rejected() -> None:
    unit = _unit("Facility A contains Pump 1.")

    missing = verify_and_mint_extraction_span(
        source_unit=unit,
        anchor=None,
        verified_at_utc=_NOW,
    )
    drifted = verify_and_mint_extraction_span(
        source_unit=unit,
        anchor=ProposedOccurrenceAnchor(span_start=0, span_end=8, quote="Facility"),
        expected_source_text_hash="9" * 64,
        verified_at_utc=_NOW,
    )

    assert missing.reason_codes == ("EVIDENCE_MISSING",)
    assert classify_state(missing.reason_codes) is AssertionState.UNRESOLVED
    assert drifted.span is None
    assert drifted.reason_codes == ("EVIDENCE_SOURCE_MISMATCH",)
    assert classify_state(drifted.reason_codes) is AssertionState.REJECTED


def test_model_authored_evidence_ids_are_ignored_but_recorded() -> None:
    unit = _unit("Facility A contains Pump 1.")

    outcome = verify_and_mint_extraction_span(
        source_unit=unit,
        anchor=ProposedOccurrenceAnchor(
            span_start=0,
            span_end=8,
            quote="Facility",
            model_authored_evidence_id="evidence-span:" + "0" * 32,
        ),
        verified_at_utc=_NOW,
    )

    assert outcome.verified
    assert outcome.reason_codes == ("MODEL_EVIDENCE_ID_IGNORED",)
    assert outcome.ignored_model_evidence_id != outcome.span.evidence_span_id
    assert classify_state(outcome.reason_codes) is AssertionState.ASSERTED


def test_verifier_identity_changes_the_evidence_id_deterministically() -> None:
    unit = _unit("Facility A contains Pump 1.")
    anchor = ProposedOccurrenceAnchor(span_start=0, span_end=8, quote="Facility")

    base = verify_and_mint_extraction_span(
        source_unit=unit,
        anchor=anchor,
        verified_at_utc=_NOW,
    ).span
    other_version = verify_and_mint_extraction_span(
        source_unit=unit,
        anchor=anchor,
        verifier_version="2.0.0",
        verified_at_utc=_NOW,
    ).span
    other_purpose_version = verify_and_mint_extraction_span(
        source_unit=unit,
        anchor=anchor,
        verifier_purpose_version="1.1.0",
        verified_at_utc=_NOW,
    ).span

    ids = {
        base.evidence_span_id,
        other_version.evidence_span_id,
        other_purpose_version.evidence_span_id,
    }
    assert len(ids) == 3
    assert base.purpose == L3_EXTRACTION_PURPOSE
    assert base.verifier_purpose_version == L3_EXTRACTION_PURPOSE_VERSION
    assert base.verifier_name == L3_EXTRACTION_VERIFIER_NAME
    assert base.verifier_version == L3_EXTRACTION_VERIFIER_VERSION
    assert base.identity.contract_version == "1.1.0"


def test_design_purpose_evidence_can_never_assert_an_extraction_candidate() -> None:
    unit = _unit("Facility A contains Pump 1.")
    legacy = EvidenceSpan.mint_verified(
        source_unit=unit,
        span_start=0,
        span_end=8,
        verifier_name=DESIGN_VERIFIER_NAME,
        verifier_version="1.0.0",
        verified_at_utc=_NOW,
    )
    trusted = TrustedL1DesignEvidenceManifestContext(
        manifest_contract_kind="l1.design_sample_manifest",
        manifest_contract_version="1.0.0",
        design_sample_manifest_id="design-sample-manifest:test",
        design_sample_manifest_hash="b" * 64,
        evidence_span_ids=(legacy.evidence_span_id,),
    )
    adapted = adapt_evidence_span_v1_0_to_v1_1(
        legacy,
        source_unit=unit,
        trusted_manifest=trusted,
        purpose="domain_design",
        verifier_purpose_version="1.0.0",
    )

    with pytest.raises(L3StageError) as legacy_error:
        require_extraction_evidence(legacy)
    with pytest.raises(L3StageError) as adapted_error:
        require_extraction_evidence(adapted)
    with pytest.raises(EvidencePurposePromotionError):
        adapt_evidence_span_v1_0_to_v1_1(
            legacy,
            source_unit=unit,
            trusted_manifest=trusted,
            purpose="extraction_assertion",
            verifier_purpose_version="1.0.0",
        )

    assert legacy_error.value.code == "L3_CONTRACT_VERSION_UNSUPPORTED"
    assert adapted_error.value.code == "L3_EVIDENCE_PURPOSE_INVALID"


def test_unsupported_purpose_policy_version_blocks_the_leaf() -> None:
    unit = _unit("Facility A contains Pump 1.")
    span = EvidenceSpanV1_1.mint_verified(
        source_unit=unit,
        span_start=0,
        span_end=8,
        verifier_name=L3_EXTRACTION_VERIFIER_NAME,
        verifier_version=L3_EXTRACTION_VERIFIER_VERSION,
        purpose="extraction_assertion",
        verifier_purpose_version="9.9.9",
        verified_at_utc=_NOW,
    )

    with pytest.raises(L3StageError) as excinfo:
        require_extraction_evidence(span)

    assert excinfo.value.code == "L3_EVIDENCE_PURPOSE_VERSION_UNSUPPORTED"


def test_unsupported_modalities_are_unsupported_not_a_generic_fallback() -> None:
    assert "visual_description" not in L3_SUPPORTED_EVIDENCE_UNIT_KINDS
    assert "transcript" not in L3_SUPPORTED_EVIDENCE_UNIT_KINDS
    assert (
        classify_state(("EVIDENCE_MODALITY_UNSUPPORTED",))
        is AssertionState.UNSUPPORTED
    )


# ---------------------------------------------------------------------------
# Endpoint occurrence grounding
# ---------------------------------------------------------------------------


def test_both_endpoints_must_ground_inside_the_relationship_span() -> None:
    text = "Depot D serves Truck T today."
    outcome = ground_endpoints(
        source_text=text,
        span_start=0,
        span_end=len(text),
        requests=(
            EndpointGroundingRequest(
                endpoint_id="entity:source",
                role="source",
                terms=("Depot D",),
            ),
            EndpointGroundingRequest(
                endpoint_id="entity:target",
                role="target",
                terms=("Truck T",),
            ),
        ),
    )
    missing = ground_endpoints(
        source_text=text,
        span_start=0,
        span_end=8,
        requests=(
            EndpointGroundingRequest(
                endpoint_id="entity:source",
                role="source",
                terms=("Depot D",),
            ),
            EndpointGroundingRequest(
                endpoint_id="entity:target",
                role="target",
                terms=("Truck T",),
            ),
        ),
    )

    assert outcome.grounded
    assert [item.endpoint_id for item in outcome.occurrences] == [
        "entity:source",
        "entity:target",
    ]
    assert not missing.grounded
    assert missing.reason_codes == ("ENDPOINT_EVIDENCE_UNGROUNDED",)


def test_repeated_endpoint_names_require_one_exact_occurrence_anchor() -> None:
    text = "Pump 1 replaced Pump 1 in Bay 2."
    ambiguous = ground_endpoints(
        source_text=text,
        span_start=0,
        span_end=len(text),
        requests=(
            EndpointGroundingRequest(
                endpoint_id="entity:a",
                role="source",
                terms=("Pump 1",),
            ),
            EndpointGroundingRequest(
                endpoint_id="entity:b",
                role="target",
                terms=("Bay 2",),
            ),
        ),
    )
    anchored = ground_endpoints(
        source_text=text,
        span_start=0,
        span_end=len(text),
        requests=(
            EndpointGroundingRequest(
                endpoint_id="entity:a",
                role="source",
                anchor=ProposedOccurrenceAnchor(
                    span_start=0,
                    span_end=6,
                    quote="Pump 1",
                ),
            ),
            EndpointGroundingRequest(
                endpoint_id="entity:b",
                role="target",
                terms=("Bay 2",),
            ),
        ),
    )

    assert not ambiguous.grounded
    assert anchored.grounded
    assert anchored.occurrences[0].span_start == 0


def test_one_span_cannot_ground_two_endpoints_to_the_same_occurrence() -> None:
    text = "Pump 1 relates to itself."

    outcome = ground_endpoints(
        source_text=text,
        span_start=0,
        span_end=len(text),
        requests=(
            EndpointGroundingRequest(
                endpoint_id="entity:a",
                role="source",
                terms=("Pump 1",),
            ),
            EndpointGroundingRequest(
                endpoint_id="entity:b",
                role="target",
                terms=("Pump 1",),
            ),
        ),
    )

    assert outcome.reason_codes == ("ENDPOINT_EVIDENCE_UNGROUNDED",)


def test_exact_occurrences_counts_overlapping_unicode_matches() -> None:
    assert exact_occurrences("aaa", "aa") == ((0, 2), (1, 3))
    assert exact_occurrences("設備設備", "設備") == ((0, 2), (2, 4))
    assert exact_occurrences("abc", "") == ()


@pytest.mark.parametrize(
    ("direction", "expected"),
    [
        ("source_to_target", ()),
        ("reverse", ("DIRECTION_MISMATCH",)),
        ("unknown", ("DIRECTION_MISMATCH",)),
        (None, ("DIRECTION_MISMATCH",)),
    ],
)
def test_only_the_approved_forward_direction_is_accepted(
    direction: str | None,
    expected: tuple[str, ...],
) -> None:
    assert resolve_direction(direction) == expected


# ---------------------------------------------------------------------------
# Hierarchy closure, inheritance, and endpoint compatibility
# ---------------------------------------------------------------------------


def test_multi_level_subtype_endpoints_record_deterministic_paths() -> None:
    hierarchy = _vehicle_hierarchy()

    direct = hierarchy.endpoint_outcome(
        "relationship-type:x.serves",
        "semantic-type:x.vehicle",
        role="target",
    )
    transitive = hierarchy.endpoint_outcome(
        "relationship-type:x.serves",
        "semantic-type:x.truck",
        role="target",
    )
    incompatible = hierarchy.endpoint_outcome(
        "relationship-type:x.serves",
        "semantic-type:x.depot",
        role="target",
    )

    assert direct.compatible and direct.inheritance_path == ("semantic-type:x.vehicle",)
    assert transitive.compatible
    assert transitive.inheritance_path == (
        "semantic-type:x.truck",
        "semantic-type:x.vehicle",
    )
    assert not incompatible.compatible
    assert incompatible.reason_codes == ("TARGET_TYPE_MISMATCH",)


def test_exact_endpoint_policy_rejects_child_types() -> None:
    hierarchy = _vehicle_hierarchy()

    exact = hierarchy.endpoint_outcome(
        "relationship-type:x.exact-serves",
        "semantic-type:x.truck",
        role="target",
    )

    assert not exact.compatible
    assert exact.reason_codes == ("TARGET_TYPE_MISMATCH",)


def test_abstract_types_can_never_be_instantiated_directly() -> None:
    hierarchy = _vehicle_hierarchy()

    assert evaluate_inherited_constraints(
        "semantic-type:x.thing",
        hierarchy,
    ) == ("ABSTRACT_TYPE_INSTANTIATION",)
    assert evaluate_inherited_constraints("semantic-type:x.truck", hierarchy) == ()
    assert evaluate_inherited_constraints("semantic-type:x.missing", hierarchy) == (
        "HIERARCHY_CONCEPT_MISSING",
    )


def test_root_to_leaf_properties_and_constraints_form_one_conjunction() -> None:
    required = DomainPropertyV2(
        property_id="property:x.serial",
        display_name="Serial",
        value_type="string",
        required=True,
    )
    optional = DomainPropertyV2(
        property_id="property:x.axles",
        display_name="Axles",
        value_type="integer",
    )
    entities = (
        _entity(
            "semantic-type:x.root",
            policy=_policy("x.root"),
            properties=(required,),
            constraints=(
                DomainConstraintV2(
                    constraint_id="constraint:x.serial-present",
                    expression="serial is present",
                ),
            ),
        ),
        _entity(
            "semantic-type:x.leaf",
            parent="semantic-type:x.root",
            root="semantic-type:x.root",
            properties=(optional,),
        ),
    )
    hierarchy = _compiled(
        entities,
        (
            _relationship(
                "relationship-type:x.link",
                sources=("semantic-type:x.root",),
                targets=("semantic-type:x.leaf",),
            ),
        ),
    )

    assert hierarchy.effective_property_ids("semantic-type:x.leaf") == (
        "property:x.axles",
        "property:x.serial",
    )
    assert evaluate_inherited_constraints(
        "semantic-type:x.leaf",
        hierarchy,
        observed_property_ids=("property:x.serial",),
    ) == ()
    assert evaluate_inherited_constraints(
        "semantic-type:x.leaf",
        hierarchy,
        observed_property_ids=(),
    ) == ("INHERITED_CONSTRAINT_VIOLATION",)
    assert evaluate_inherited_constraints(
        "semantic-type:x.leaf",
        hierarchy,
        observed_property_ids=("property:x.serial", "property:x.foreign"),
    ) == ("INHERITED_PROPERTY_INVALID",)


def test_property_observations_validate_id_value_and_sibling_state() -> None:
    hierarchy = _compiled(
        (
            _entity(
                "semantic-type:x.root",
                policy=_policy("x.root"),
                properties=(
                    DomainPropertyV2(
                        property_id="property:x.axles",
                        display_name="Axles",
                        value_type="integer",
                    ),
                ),
            ),
            _entity("semantic-type:x.other", policy=_policy("x.other")),
        ),
        (
            _relationship(
                "relationship-type:x.link",
                sources=("semantic-type:x.root",),
                targets=("semantic-type:x.other",),
            ),
        ),
    )

    assert validate_property_observation(
        hierarchy=hierarchy,
        owner_type_id="semantic-type:x.root",
        property_id="property:x.axles",
        value=4,
        value_available=True,
    ) == ()
    assert validate_property_observation(
        hierarchy=hierarchy,
        owner_type_id="semantic-type:x.root",
        property_id="property:x.axles",
        value="four",
        value_available=True,
    ) == ("PROPERTY_VALUE_INVALID",)
    assert validate_property_observation(
        hierarchy=hierarchy,
        owner_type_id="semantic-type:x.other",
        property_id="property:x.axles",
    ) == ("INHERITED_PROPERTY_INVALID",)
    assert validate_property_observation(
        hierarchy=hierarchy,
        owner_type_id="semantic-type:x.root",
        property_id=None,
    ) == ("UNKNOWN_PROPERTY",)
    assert validate_property_observation(
        hierarchy=hierarchy,
        owner_type_id="semantic-type:x.root",
        property_id="property:x.axles",
        owner_classification_unresolved=True,
    ) == ("AMBIGUOUS_SIBLING_CLASSIFICATION",)


@pytest.mark.parametrize("permutation", list(itertools.permutations(range(4))))
def test_generated_acyclic_hierarchies_are_insertion_order_independent(
    permutation: tuple[int, ...],
) -> None:
    edges = [
        ("t:0", None),
        ("t:1", "t:0"),
        ("t:2", "t:1"),
        ("t:3", "t:1"),
    ]
    ordered = {edges[index][0]: edges[index][1] for index in permutation}

    closure = compile_parent_closure(ordered)

    assert closure["t:3"] == ("t:1", "t:0")
    assert deterministic_ancestor_path("t:2", "t:0", ordered) == ("t:2", "t:1", "t:0")
    assert deterministic_ancestor_path("t:2", "t:3", ordered) == ()


@pytest.mark.parametrize(
    "parents",
    [
        {"a": "b", "b": "a"},
        {"a": "a"},
        {"a": "b", "b": "c", "c": "a"},
    ],
)
def test_generated_cyclic_hierarchies_terminate_and_fail_closed(parents) -> None:
    with pytest.raises(L3StageError) as excinfo:
        compile_parent_closure(parents)

    assert excinfo.value.code == "L3_HIERARCHY_INVALID"
    assert excinfo.value.reason_codes == ("SUBTYPE_HIERARCHY_CYCLE",)


def test_unknown_parents_fail_closed_with_a_missing_concept_reason() -> None:
    with pytest.raises(L3StageError) as excinfo:
        compile_parent_closure({"a": "missing"})

    assert excinfo.value.reason_codes == ("HIERARCHY_CONCEPT_MISSING",)


@pytest.mark.parametrize(
    "observed",
    list(
        itertools.permutations(
            ("semantic-type:x.truck", "semantic-type:x.depot")
        )
    ),
)
def test_competing_siblings_stay_unresolved_in_any_order(observed) -> None:
    hierarchy = _vehicle_hierarchy()

    resolution = resolve_most_specific_classification(observed, hierarchy)

    assert resolution.ambiguous
    assert resolution.most_specific_type_id is None
    assert resolution.reason_codes == ("AMBIGUOUS_SIBLING_CLASSIFICATION",)
    assert classify_state(resolution.reason_codes) is AssertionState.UNRESOLVED


@pytest.mark.parametrize(
    "observed",
    list(
        itertools.permutations(
            ("semantic-type:x.truck", "semantic-type:x.vehicle")
        )
    ),
)
def test_the_most_specific_concrete_classification_wins(observed) -> None:
    hierarchy = _vehicle_hierarchy()

    resolution = resolve_most_specific_classification(observed, hierarchy)

    assert not resolution.ambiguous
    assert resolution.most_specific_type_id == "semantic-type:x.truck"


def test_hierarchy_depth_is_reported_independently_of_k() -> None:
    hierarchy = _vehicle_hierarchy()

    assert hierarchy.hierarchy_depth == 3
    assert hierarchy.depth_by_type["semantic-type:x.truck"] == 2
    assert hierarchy.depth_by_type["semantic-type:x.depot"] == 0


def test_compile_hierarchy_accepts_the_approved_contract_and_blocks_drift() -> None:
    contract = load_domain_contract(
        _ROOT / "examples/domains/facility-maintenance-v2.domain.yaml"
    )
    assert isinstance(contract, DomainContractV2)

    compiled = compile_hierarchy(contract)

    assert compiled.hierarchy_hash == contract.hierarchy_closure.hierarchy_hash
    assert compiled.identity_policy_hash == contract.identity_policy_hash
    for field, code in (
        ("identity_policy_hash", "L3_IDENTITY_POLICY_HASH_MISMATCH"),
        ("completeness_requirement_hash", "L3_COMPLETENESS_HASH_MISMATCH"),
        (
            "external_reference_decision_hash",
            "L3_EXTERNAL_REFERENCE_DECISION_HASH_MISMATCH",
        ),
    ):
        drifted = DomainContractV2.model_construct(
            **{**contract.__dict__, field: "0" * 64}
        )
        with pytest.raises(L3StageError) as excinfo:
            compile_hierarchy(drifted)
        assert excinfo.value.code == code


# ---------------------------------------------------------------------------
# Stable identity recomputation
# ---------------------------------------------------------------------------


def test_entity_identity_is_type_independent_and_reclassification_safe() -> None:
    policy = _policy("x.thing")
    witness = derived_stable_source_identity(
        source_unit_id="source-unit:abc",
        local_reference="Truck-7",
    )

    first = recompute_entity_id(
        project_id="project:x",
        policy=policy,
        stable_source_identity=witness,
    )
    second = recompute_entity_id(
        project_id="project:x",
        policy=policy,
        stable_source_identity=witness,
    )

    assert first == second
    assert is_minted_contract_id(first, "entity")
    assert witness == "source-unit:abc:truck-7"
    # No classification, ancestor path, or hierarchy depth may seed identity.
    with pytest.raises(L3StageError) as excinfo:
        assert_type_independent_identity_inputs(
            {"project_id": "project:x", "semantic_type_id": "semantic-type:x.truck"}
        )
    assert excinfo.value.reason_codes == ("IDENTITY_POLICY_VIOLATION",)


def test_business_key_identity_normalizes_deterministically() -> None:
    policy = _business_policy("x.thing")

    first = recompute_entity_id(
        project_id="project:x",
        policy=policy,
        normalized_business_key=normalize_business_key({"serial": "  AB   12 "}),
    )
    second = recompute_entity_id(
        project_id="project:x",
        policy=policy,
        normalized_business_key=normalize_business_key({"serial": "ab 12"}),
    )
    assert first == second

    with pytest.raises(L3StageError) as excinfo:
        recompute_entity_id(
            project_id="project:x",
            policy=policy,
            normalized_business_key={"other": "value"},
        )
    assert excinfo.value.code == "L3_IDENTITY_POLICY_HASH_MISMATCH"


def test_relationship_identity_survives_endpoint_reclassification() -> None:
    hierarchy = _vehicle_hierarchy()
    seed = {
        "predicate_id": "predicate:x.serves",
        "source_entity_id": "entity:" + "1" * 32,
        "target_entity_id": "entity:" + "2" * 32,
        "governed_context": {
            "approved_context": "ctx",
            "direction": "source_to_target",
        },
    }

    # Endpoint classification changes cannot reach the relationship seed at all.
    ids = {
        recompute_relationship_id(**seed)
        for _ in hierarchy.entity_by_id
    }
    swapped = recompute_relationship_id(
        **{
            **seed,
            "source_entity_id": seed["target_entity_id"],
            "target_entity_id": seed["source_entity_id"],
        }
    )

    assert len(ids) == 1
    baseline = ids.pop()
    assert baseline != swapped
    assert is_minted_contract_id(baseline, "relationship")
    with pytest.raises(L3StageError):
        recompute_relationship_id(
            predicate_id="predicate:x.serves",
            source_entity_id="entity:" + "1" * 32,
            target_entity_id="entity:" + "2" * 32,
            governed_context={"source_type_id": "semantic-type:x.truck"},
        )


def test_minted_id_shape_detects_foreign_identifiers() -> None:
    assert is_minted_contract_id(
        deterministic_contract_id("entity", {"a": 1}),
        "entity",
    )
    assert not is_minted_contract_id("entity:not-hex", "entity")
    assert not is_minted_contract_id("relationship:" + "0" * 32, "entity")


# ---------------------------------------------------------------------------
# Append-only lifecycle classification
# ---------------------------------------------------------------------------


def _initial(candidate_kind: str = "entity") -> CandidateLifecycleRecord:
    identity = _identity(
        contract_kind="c0.candidate_lifecycle_record",
        asset_id=None,
        asset_version_id=None,
        source_file_id=None,
        source_unit_id=None,
        content_hash=None,
    )
    return CandidateLifecycleRecord.seal(
        identity=identity,
        lifecycle_record_id=deterministic_contract_id(
            "candidate-lifecycle",
            {
                "candidate_id": "entity-candidate:1",
                "candidate_version_id": "candidate-version:1",
                "sequence": 0,
            },
        ),
        candidate_id="entity-candidate:1",
        candidate_version_id="candidate-version:1",
        candidate_kind=candidate_kind,
        sequence=0,
        prior_lifecycle_record_id=None,
        from_state=None,
        to_state=AssertionState.PROPOSED,
        reason_codes=(),
        evidence_span_ids=(),
        governance_justification_id=None,
        resolved_source_entity_id=None,
        resolved_target_entity_id=None,
        source_inheritance_path=(),
        target_inheritance_path=(),
        validator_name="l2-proposal-only",
        validator_version="1.1.0",
        occurred_at_utc=_NOW,
    )


@pytest.mark.parametrize(
    ("reasons", "expected"),
    [
        ((), AssertionState.ASSERTED),
        (("MODEL_EVIDENCE_ID_IGNORED",), AssertionState.ASSERTED),
        (("EVIDENCE_MISSING",), AssertionState.UNRESOLVED),
        (("AMBIGUOUS_SIBLING_CLASSIFICATION",), AssertionState.UNRESOLVED),
        (("UNKNOWN_RELATIONSHIP_TYPE",), AssertionState.DISCOVERY),
        (
            ("UNKNOWN_ENTITY_TYPE", "EVIDENCE_MISSING"),
            AssertionState.DISCOVERY,
        ),
        (("EVIDENCE_MODALITY_UNSUPPORTED",), AssertionState.UNSUPPORTED),
        (
            ("EVIDENCE_MODALITY_UNSUPPORTED", "UNKNOWN_ENTITY_TYPE"),
            AssertionState.UNSUPPORTED,
        ),
        (("DIRECTION_MISMATCH",), AssertionState.REJECTED),
        (
            ("EVIDENCE_QUOTE_MISMATCH", "UNKNOWN_ENTITY_TYPE"),
            AssertionState.REJECTED,
        ),
    ],
)
def test_reason_codes_map_to_one_deterministic_state(
    reasons: tuple[str, ...],
    expected: AssertionState,
) -> None:
    assert classify_state(reasons) is expected
    assert set(reasons) <= STABLE_REASON_CODES


def test_unstable_reason_codes_are_prohibited() -> None:
    with pytest.raises(L3StageError) as excinfo:
        classify_state(("SOMETHING_ELSE",))

    assert excinfo.value.code == "L3_VALIDATION_RESULT_INCOMPLETE"


def test_one_current_transition_is_appended_to_the_proposed_event() -> None:
    prior = _initial()
    identity = prior.identity

    appended = append_current_transition(
        prior,
        identity=identity,
        to_state=AssertionState.ASSERTED,
        reason_codes=("MODEL_EVIDENCE_ID_IGNORED",),
        evidence_span_ids=("evidence-span:" + "0" * 32,),
        resolved_source_entity_id=None,
        resolved_target_entity_id=None,
        occurred_at_utc=_NOW,
    )

    assert appended.sequence == 1
    assert appended.prior_lifecycle_record_id == prior.lifecycle_record_id
    assert appended.from_state is AssertionState.PROPOSED
    assert appended.to_state is AssertionState.ASSERTED
    assert appended.reason_codes == ("MODEL_EVIDENCE_ID_IGNORED",)
    assert appended.transition_hash != prior.transition_hash

    with pytest.raises(L3StageError) as terminal:
        append_current_transition(
            appended,
            identity=identity,
            to_state=AssertionState.REJECTED,
            occurred_at_utc=_NOW,
        )
    assert terminal.value.code == "L3_LIFECYCLE_CHAIN_INVALID"

    with pytest.raises(L3StageError) as unevidenced:
        append_current_transition(
            prior,
            identity=identity,
            to_state=AssertionState.ASSERTED,
            occurred_at_utc=_NOW,
        )
    assert unevidenced.value.code == "L3_VALIDATION_RESULT_INCOMPLETE"


# ---------------------------------------------------------------------------
# Generic structured fact-set completeness
# ---------------------------------------------------------------------------


def _authority() -> ExtractionAuthorityReferences:
    return ExtractionAuthorityReferences(
        source_corpus_manifest_id="source-corpus-manifest:test",
        source_corpus_manifest_hash="3" * 64,
        source_unit_manifest_id="artifact-manifest:source-units",
        source_unit_manifest_hash="4" * 64,
        domain_contract_hash="e" * 64,
        completeness_requirement_id="completeness-requirement:x.set",
        completeness_requirement_hash="5" * 64,
        hierarchy_hash="6" * 64,
        identity_policy_hash="7" * 64,
    )


def _requirement(
    *,
    ordered: bool,
    roles: tuple[str, ...],
    cardinality: CardinalityExpectationV2 | None = None,
) -> CompletenessRequirementV2:
    ordering = (
        OrderingPolicyV2(
            mode="ordered",
            ordinal_property_id="property:x.order",
            ordinal_value_type="integer",
            direction="ascending",
            unique_ordinals=True,
            contiguous=True,
        )
        if ordered
        else OrderingPolicyV2(mode="unordered")
    )
    return CompletenessRequirementV2(
        requirement_id="completeness-requirement:x.set",
        competency_question_ids=["cq:q1"],
        requirement_kind="structured_fact_set",
        scope_type_id="semantic-type:x.depot",
        rationale="Approved question requires complete membership.",
        source_kind="competency_question",
        source_question_ids=["cq:q1"],
        coverage_status="covered",
        structured_fact_set=StructuredFactSetV2(
            aggregate_type_id="semantic-type:x.depot",
            membership_relationship_type_id="relationship-type:x.serves",
            allowed_member_type_ids=["semantic-type:x.vehicle"],
            member_role_ids=list(roles),
            ordering_policy=ordering,
            cardinality=cardinality,
            collection_identity_policy=CollectionIdentityPolicyV2(
                member_roles_included=bool(roles),
                ordinals_included=ordered,
                preserve_member_order=ordered,
            ),
            membership_source_kind="competency_question",
            membership_rationale="The approved question governs membership.",
        ),
    )


def _member(
    index: int,
    *,
    role: str | None = None,
    order: int | None = None,
    type_id: str = "semantic-type:x.vehicle",
) -> RequiredMemberReferenceV1_1:
    return RequiredMemberReferenceV1_1.seal(
        member_canonical_id=f"entity:{index:032d}",
        member_semantic_type_id=type_id,
        member_role_id=role,
        member_order=order,
        candidate_id=f"entity-candidate:{index}",
        supporting_evidence_span_ids=(),
    )


def _verified(
    member: RequiredMemberReferenceV1_1,
    *,
    member_state: AssertionState = AssertionState.ASSERTED,
    membership_state: AssertionState = AssertionState.ASSERTED,
    evidence: tuple[str, ...] = ("evidence-span:" + "1" * 32,),
    type_id: str | None = None,
    role: str | None = ...,
    order: int | None = ...,
) -> VerifiedMember:
    return VerifiedMember(
        member_canonical_id=member.member_canonical_id,
        member_semantic_type_id=type_id or member.member_semantic_type_id,
        member_role_id=member.member_role_id if role is ... else role,
        member_order=member.member_order if order is ... else order,
        candidate_id=member.candidate_id,
        member_state=member_state,
        membership_state=membership_state,
        membership_evidence_span_ids=evidence,
        member_evidence_span_ids=evidence,
    )


def _validate(
    *,
    requirement: CompletenessRequirementV2,
    members: tuple[RequiredMemberReferenceV1_1, ...],
    verified: tuple[VerifiedMember, ...],
    expected: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
    collection_hash: str | None = None,
    adjacency_policy: AdjacencyPolicy | None = None,
    verified_edges: tuple[tuple[str, str], ...] = (),
    approved_evidence: tuple[str, ...] = ("evidence-span:" + "8" * 32,),
    prohibited_evidence: tuple[str, ...] = (),
):
    fact_set = requirement.structured_fact_set
    assert fact_set is not None
    ordering = RequiredMemberOrderingPolicyV1_1(
        mode=fact_set.ordering_policy.mode,
        ordinal_property_id=fact_set.ordering_policy.ordinal_property_id,
        ordinal_value_type=fact_set.ordering_policy.ordinal_value_type,
        direction=fact_set.ordering_policy.direction,
        unique_ordinals=fact_set.ordering_policy.unique_ordinals,
        contiguous=fact_set.ordering_policy.contiguous,
        member_order_encoding=(
            "zero_based_contiguous"
            if fact_set.ordering_policy.mode == "ordered"
            else None
        ),
    )
    from fabric_kg_builder.contracts.extraction import (
        authoritative_collection_hash_v1_1,
    )

    hierarchy = _vehicle_hierarchy()
    exact_hash = collection_hash or authoritative_collection_hash_v1_1(
        authority=_authority(),
        scope_canonical_id="entity:" + "9" * 32,
        membership_semantic_relationship_id="relationship-type:x.serves",
        ordering_policy=ordering,
        expected_cardinality=expected,
        minimum_cardinality=minimum,
        maximum_cardinality=maximum,
        required_role_ids=tuple(fact_set.member_role_ids),
        members=members,
    )
    return validate_required_member_proposal(
        proposal_id="required-member-set-proposal:test",
        requirement=requirement,
        scope_canonical_id="entity:" + "9" * 32,
        ordering_policy=ordering,
        required_role_ids=tuple(fact_set.member_role_ids),
        expected_cardinality=expected,
        minimum_cardinality=minimum,
        maximum_cardinality=maximum,
        proposal_members=members,
        verified_members=verified,
        hierarchy=hierarchy,
        authority=_authority(),
        membership_semantic_relationship_id="relationship-type:x.serves",
        proposal_collection_hash=exact_hash,
        approved_cardinality_evidence_ids=approved_evidence,
        prohibited_cardinality_evidence_ids=prohibited_evidence,
        adjacency_policy=adjacency_policy,
        verified_adjacency_edges=verified_edges,
    )


def test_unordered_roleless_collection_is_complete_and_hash_stable() -> None:
    requirement = _requirement(ordered=False, roles=())
    members = (_member(1), _member(2))

    outcome = _validate(
        requirement=requirement,
        members=members,
        verified=tuple(_verified(item) for item in members),
    )

    assert outcome.completeness_state == "complete"
    assert outcome.reason_codes == ()
    assert outcome.verified_member_count == 2
    assert outcome.recomputed_collection_hash == outcome.proposal_collection_hash


def test_ordered_role_bearing_collection_verifies_roles_and_ordinals() -> None:
    requirement = _requirement(ordered=True, roles=("role:x.primary", "role:x.backup"))
    members = (
        _member(1, role="role:x.primary", order=0),
        _member(2, role="role:x.backup", order=1),
    )

    outcome = _validate(
        requirement=requirement,
        members=members,
        verified=tuple(_verified(item) for item in members),
    )

    assert outcome.completeness_state == "complete"
    assert outcome.role_coverage == (("role:x.backup", 1), ("role:x.primary", 1))


@pytest.mark.parametrize(
    ("scenario", "reason"),
    [
        ("missing_member", "REQUIRED_MEMBER_MISSING"),
        ("missing_membership_evidence", "MEMBERSHIP_EVIDENCE_INVALID"),
        ("member_type_mismatch", "MEMBER_TYPE_OR_ROLE_MISMATCH"),
        ("missing_role", "REQUIRED_ROLE_MISSING"),
        ("cardinality_bound", "CARDINALITY_BOUND_VIOLATION"),
        ("collection_hash", "COLLECTION_HASH_MISMATCH"),
    ],
)
def test_broken_obligations_keep_the_collection_unresolved(
    scenario: str,
    reason: str,
) -> None:
    if scenario == "missing_role":
        requirement = _requirement(
            ordered=False,
            roles=("role:x.primary", "role:x.backup"),
        )
        members = (
            _member(1, role="role:x.primary"),
            _member(2, role="role:x.primary"),
        )
        verified = tuple(_verified(item) for item in members)
    elif scenario == "cardinality_bound":
        requirement = _requirement(
            ordered=False,
            roles=(),
            cardinality=CardinalityExpectationV2(
                expected_count=3,
                source_kind="competency_question",
                source_question_ids=["cq:q1"],
                reviewed_rationale="Three members are approved.",
            ),
        )
        members = (_member(1), _member(2))
        verified = tuple(_verified(item) for item in members)
    else:
        requirement = _requirement(ordered=False, roles=())
        members = (_member(1), _member(2))
        if scenario == "missing_member":
            verified = (_verified(members[0]),)
        elif scenario == "missing_membership_evidence":
            verified = (
                _verified(members[0]),
                _verified(members[1], evidence=()),
            )
        elif scenario == "member_type_mismatch":
            verified = (
                _verified(members[0]),
                _verified(members[1], type_id="semantic-type:x.depot"),
            )
        else:
            verified = tuple(_verified(item) for item in members)

    outcome = _validate(
        requirement=requirement,
        members=members,
        verified=verified,
        expected=3 if scenario == "cardinality_bound" else None,
        collection_hash="0" * 64 if scenario == "collection_hash" else None,
    )

    assert outcome.completeness_state == "unresolved"
    assert reason in outcome.reason_codes


def test_unspecified_counts_are_never_inferred() -> None:
    requirement = _requirement(ordered=False, roles=())
    members = (_member(1),)

    outcome = _validate(
        requirement=requirement,
        members=members,
        verified=(_verified(members[0]),),
        expected=1,
    )

    assert "CARDINALITY_EVIDENCE_INVALID" in outcome.reason_codes


def test_source_evidenced_counts_require_approved_evidence_ids() -> None:
    requirement = _requirement(
        ordered=False,
        roles=(),
        cardinality=CardinalityExpectationV2(
            expected_count=1,
            source_kind="source_evidence",
            source_evidence_span_ids=["evidence-span:" + "7" * 32],
        ),
    )
    members = (_member(1),)

    outcome = _validate(
        requirement=requirement,
        members=members,
        verified=(_verified(members[0]),),
        expected=1,
    )

    assert outcome.reason_codes == ("CARDINALITY_EVIDENCE_INVALID",)


@pytest.mark.parametrize(
    ("edges", "reason"),
    [
        ((("m:0", "m:1"), ("m:1", "m:2")), None),
        ((("m:0", "m:1"),), "ADJACENCY_EDGE_MISSING"),
        (
            (("m:0", "m:1"), ("m:1", "m:2"), ("m:0", "m:2")),
            "ADJACENCY_EDGE_INVALID",
        ),
        ((("m:0", "m:1"), ("m:1", "m:2"), ("m:2", "m:0")), "ADJACENCY_EDGE_INVALID"),
    ],
)
def test_generated_adjacency_graphs_terminate_deterministically(edges, reason) -> None:
    reasons = validate_adjacency_edges(
        policy=AdjacencyPolicy(relationship_type_id="relationship-type:x.next"),
        ordered_member_ids=("m:0", "m:1", "m:2"),
        verified_edges=edges,
    )

    if reason is None:
        assert reasons == ()
    else:
        assert reason in reasons


def test_adjacency_is_never_invented_from_ordinal_order() -> None:
    assert validate_adjacency_edges(
        policy=None,
        ordered_member_ids=("m:0", "m:1"),
        verified_edges=(),
    ) == ()


@pytest.mark.parametrize("order", list(itertools.permutations(range(3))))
def test_member_permutations_produce_one_identity_set_and_hash(order) -> None:
    requirement = _requirement(ordered=False, roles=())
    members = tuple(_member(index + 1) for index in range(3))
    shuffled = tuple(members[index] for index in order)

    outcome = _validate(
        requirement=requirement,
        members=members,
        verified=tuple(_verified(item) for item in shuffled),
    )

    assert outcome.completeness_state == "complete"
    assert outcome.verified_member_ids == tuple(
        sorted(item.member_canonical_id for item in members)
    )
    assert outcome.recomputed_collection_hash == outcome.proposal_collection_hash


def test_sorted_reasons_is_deterministic_and_unique() -> None:
    assert sorted_reasons(["B", "A", "A"]) == ("A", "B")
    assert canonical_sha256(sorted_reasons(["A", "B"])) == canonical_sha256(
        sorted_reasons(["B", "A"])
    )


# ---------------------------------------------------------------------------
# Fail-closed limits of the frozen L2 carrier
# ---------------------------------------------------------------------------


def test_an_unprovable_fact_is_unsupported_and_never_asserted() -> None:
    assert unprovable_assertion_reasons(()) == ("EVIDENCE_MODALITY_UNSUPPORTED",)
    assert classify_state(unprovable_assertion_reasons(())) is (
        AssertionState.UNSUPPORTED
    )


@pytest.mark.parametrize(
    ("blocking", "expected_state"),
    [
        ((), AssertionState.UNSUPPORTED),
        (("EVIDENCE_MISSING",), AssertionState.UNRESOLVED),
        (("ENDPOINT_UNRESOLVED",), AssertionState.UNRESOLVED),
        (("UNKNOWN_RELATIONSHIP_TYPE",), AssertionState.DISCOVERY),
        (("DIRECTION_MISMATCH",), AssertionState.REJECTED),
        (("EVIDENCE_MODALITY_UNSUPPORTED",), AssertionState.UNSUPPORTED),
    ],
)
def test_an_unprovable_fact_never_overwrites_a_more_precise_reason(
    blocking,
    expected_state,
) -> None:
    reasons = set(blocking) | set(unprovable_assertion_reasons(blocking))

    assert classify_state(reasons) is expected_state
    assert set(blocking) <= reasons


@pytest.mark.parametrize("direction", ["source_to_target", "reverse", "unknown", None])
def test_direction_is_never_asserted_without_a_persisted_token(direction) -> None:
    # The frozen L2 carrier persists no direction token, so every proposal is
    # locally indistinguishable and none of them may assert.
    unpersisted = relationship_direction_reasons(
        proposed_direction=direction,
        direction_persisted=False,
        blocking_reason_codes=(),
    )
    assert unpersisted == ("EVIDENCE_MODALITY_UNSUPPORTED",)
    assert classify_state(unpersisted) is AssertionState.UNSUPPORTED

    # The same rule activates unchanged when a later carrier persists the token.
    persisted = relationship_direction_reasons(
        proposed_direction=direction,
        direction_persisted=True,
    )
    if direction == "source_to_target":
        assert persisted == ()
        assert classify_state(persisted) is AssertionState.ASSERTED
    else:
        assert persisted == ("DIRECTION_MISMATCH",)
        assert classify_state(persisted) is AssertionState.REJECTED


@pytest.mark.parametrize(
    ("owner", "value"),
    [(False, False), (False, True), (True, False)],
)
def test_property_attribution_is_never_claimed_without_owner_and_value(
    owner: bool,
    value: bool,
) -> None:
    reasons = property_attribution_reasons(
        owner_attribution_persisted=owner,
        value_persisted=value,
        blocking_reason_codes=(),
    )

    assert reasons == ("EVIDENCE_MODALITY_UNSUPPORTED",)
    assert classify_state(reasons) is AssertionState.UNSUPPORTED
    assert property_attribution_reasons(
        owner_attribution_persisted=True,
        value_persisted=True,
        blocking_reason_codes=(),
    ) == ()


# ---------------------------------------------------------------------------
# Identity witness recomputation
# ---------------------------------------------------------------------------


def _witness_hierarchy() -> CompiledHierarchy:
    return _compiled(
        (
            _entity("semantic-type:x.depot", policy=_policy("x.depot")),
            _entity("semantic-type:x.keyed", policy=_business_policy("x.keyed")),
        ),
        (),
    )


def test_identity_witness_recomputes_a_derivable_stable_source_identity() -> None:
    hierarchy = _witness_hierarchy()
    policy = hierarchy.identity_policy_by_type["semantic-type:x.depot"]
    derived = derived_stable_source_identity(
        source_unit_id="source-unit:1",
        local_reference="Depot-1",
    )
    entity_id = recompute_entity_id(
        project_id="project:l3-tests",
        policy=policy,
        stable_source_identity=derived,
    )

    outcome = resolve_identity_witness(
        semantic_id=entity_id,
        approved_semantic_id="semantic-type:x.depot",
        source_unit_id="source-unit:1",
        local_reference="Depot-1",
        hierarchy=hierarchy,
        project_id="project:l3-tests",
    )

    assert outcome.recomputed is True
    assert outcome.witness_kind == "derived_source_identity"
    assert outcome.reason_codes == ()
    assert outcome.witness_kind not in NON_ASSERTABLE_WITNESS_KINDS


def test_identity_witness_recomputes_a_persisted_business_key() -> None:
    hierarchy = _witness_hierarchy()
    policy = hierarchy.identity_policy_by_type["semantic-type:x.keyed"]
    entity_id = recompute_entity_id(
        project_id="project:l3-tests",
        policy=policy,
        normalized_business_key={"serial": "sn-1"},
    )

    outcome = resolve_identity_witness(
        semantic_id=entity_id,
        approved_semantic_id="semantic-type:x.keyed",
        source_unit_id="source-unit:1",
        local_reference="Keyed-1",
        hierarchy=hierarchy,
        project_id="project:l3-tests",
        normalized_business_key={"serial": "sn-1"},
    )

    assert outcome.recomputed is True
    assert outcome.witness_kind == "persisted_business_key"
    assert outcome.reason_codes == ()
    assert outcome.witness_kind not in NON_ASSERTABLE_WITNESS_KINDS


def test_a_business_key_that_does_not_reproduce_the_id_is_rejected() -> None:
    hierarchy = _witness_hierarchy()
    policy = hierarchy.identity_policy_by_type["semantic-type:x.keyed"]
    entity_id = recompute_entity_id(
        project_id="project:l3-tests",
        policy=policy,
        normalized_business_key={"serial": "sn-1"},
    )

    outcome = resolve_identity_witness(
        semantic_id=entity_id,
        approved_semantic_id="semantic-type:x.keyed",
        source_unit_id="source-unit:1",
        local_reference="Keyed-1",
        hierarchy=hierarchy,
        project_id="project:l3-tests",
        normalized_business_key={"serial": "sn-2"},
    )

    assert outcome.recomputed is False
    assert outcome.witness_kind == "opaque_business_key"
    assert outcome.reason_codes == ("IDENTITY_POLICY_VIOLATION",)
    assert classify_state(outcome.reason_codes) is not AssertionState.ASSERTED


def test_a_business_key_entity_without_a_persisted_key_stays_unresolved() -> None:
    hierarchy = _witness_hierarchy()
    policy = hierarchy.identity_policy_by_type["semantic-type:x.keyed"]
    entity_id = recompute_entity_id(
        project_id="project:l3-tests",
        policy=policy,
        normalized_business_key={"serial": "sn-1"},
    )

    outcome = resolve_identity_witness(
        semantic_id=entity_id,
        approved_semantic_id="semantic-type:x.keyed",
        source_unit_id="source-unit:1",
        local_reference="Keyed-1",
        hierarchy=hierarchy,
        project_id="project:l3-tests",
    )

    assert outcome.recomputed is False
    assert outcome.witness_kind == "business_key_witness_unavailable"
    assert outcome.reason_codes == ("IDENTITY_WITNESS_UNAVAILABLE",)


def test_identity_witness_recomputes_an_unapproved_observation_identity() -> None:
    hierarchy = _witness_hierarchy()
    derived = derived_stable_source_identity(
        source_unit_id="source-unit:1",
        local_reference="thing-1",
    )
    observation_id = recompute_observation_entity_id(
        project_id="project:l3-tests",
        identity_value={"stable_source_identity": derived},
    )

    outcome = resolve_identity_witness(
        semantic_id=observation_id,
        approved_semantic_id=None,
        source_unit_id="source-unit:1",
        local_reference="thing-1",
        hierarchy=hierarchy,
        project_id="project:l3-tests",
    )

    assert outcome.recomputed is True
    assert outcome.witness_kind == "derived_observation_identity"
    assert outcome.reason_codes == ()


@pytest.mark.parametrize(
    ("semantic_id", "approved", "local_reference", "witness_kind", "reason"),
    [
        (
            "entity:not-a-minted-id",
            "semantic-type:x.depot",
            "Depot-1",
            "invalid_shape",
            "SEMANTIC_ID_MISMATCH",
        ),
        (
            "entity:" + "a" * 32,
            "semantic-type:x.depot",
            None,
            "witness_unavailable",
            "IDENTITY_WITNESS_UNAVAILABLE",
        ),
        (
            "entity:" + "a" * 32,
            "semantic-type:x.keyed",
            "Keyed-1",
            "business_key_witness_unavailable",
            "IDENTITY_WITNESS_UNAVAILABLE",
        ),
        (
            "entity:" + "a" * 32,
            "semantic-type:x.missing",
            "Depot-1",
            "policy_missing",
            "HIERARCHY_CONCEPT_MISSING",
        ),
        (
            "entity:" + "a" * 32,
            "semantic-type:x.depot",
            "Depot-1",
            "opaque_source_identity",
            "IDENTITY_POLICY_VIOLATION",
        ),
        (
            "entity:" + "a" * 32,
            None,
            "Depot-1",
            "opaque_observation_identity",
            "IDENTITY_POLICY_VIOLATION",
        ),
    ],
)
def test_an_unprovable_identity_witness_can_never_assert(
    semantic_id: str,
    approved: str | None,
    local_reference: str | None,
    witness_kind: str,
    reason: str,
) -> None:
    outcome = resolve_identity_witness(
        semantic_id=semantic_id,
        approved_semantic_id=approved,
        source_unit_id="source-unit:1",
        local_reference=local_reference,
        hierarchy=_witness_hierarchy(),
        project_id="project:l3-tests",
    )

    assert outcome.recomputed is False
    assert outcome.witness_kind == witness_kind
    assert outcome.witness_kind in NON_ASSERTABLE_WITNESS_KINDS
    assert outcome.reason_codes == (reason,)
    assert classify_state(outcome.reason_codes) is not AssertionState.ASSERTED


def test_a_model_controlled_identity_seed_is_never_reproduced() -> None:
    hierarchy = _witness_hierarchy()
    policy = hierarchy.identity_policy_by_type["semantic-type:x.depot"]
    # Two distinct local references collapse onto one ID when the model, rather
    # than the sealed policy, controls the stable_source_identity seed.
    collided = recompute_entity_id(
        project_id="project:l3-tests",
        policy=policy,
        stable_source_identity="model-controlled-seed",
    )

    outcomes = [
        resolve_identity_witness(
            semantic_id=collided,
            approved_semantic_id="semantic-type:x.depot",
            source_unit_id="source-unit:1",
            local_reference=local_reference,
            hierarchy=hierarchy,
            project_id="project:l3-tests",
        )
        for local_reference in ("Depot-1", "Depot-2")
    ]

    for outcome in outcomes:
        assert outcome.recomputed is False
        assert outcome.witness_kind == "opaque_source_identity"
        assert classify_state(outcome.reason_codes) is AssertionState.REJECTED


def test_design_sample_evidence_can_never_prove_an_extraction_count() -> None:
    design_evidence_id = "evidence-span:" + "3" * 32
    requirement = _requirement(
        ordered=False,
        roles=(),
        cardinality=CardinalityExpectationV2(
            expected_count=1,
            source_kind="source_evidence",
            source_evidence_span_ids=[design_evidence_id],
        ),
    )
    members = (_member(1),)

    prohibited = _validate(
        requirement=requirement,
        members=members,
        verified=(_verified(members[0]),),
        expected=1,
        # A design ID stays invalid even if it were mistakenly approved.
        approved_evidence=(design_evidence_id,),
        prohibited_evidence=(design_evidence_id,),
    )
    minted_only = _validate(
        requirement=requirement,
        members=members,
        verified=(_verified(members[0]),),
        expected=1,
        approved_evidence=(design_evidence_id,),
        prohibited_evidence=("evidence-span:" + "4" * 32,),
    )

    assert prohibited.reason_codes == ("CARDINALITY_EVIDENCE_INVALID",)
    assert prohibited.completeness_state == "unresolved"
    assert minted_only.reason_codes == ()
    assert minted_only.completeness_state == "complete"
