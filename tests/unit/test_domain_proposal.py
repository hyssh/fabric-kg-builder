from __future__ import annotations

import pytest

from fabric_kg_builder.domain.proposal import ProposalArtifactError
from fabric_kg_builder.domain.stage import prepare_l1_stage
from tests.unit.test_l1_stage import _candidates, _preflight


def test_generic_structured_completeness_is_sealed_from_candidate_input(
    tmp_path,
) -> None:
    candidates = _candidates("collections")
    question_ids = [f"cq:q{index}" for index in range(1, 6)]
    record_type = "semantic-type:collections.record"
    subject_type = "semantic-type:collections.subject"
    relationship_type = "relationship-type:collections.record-subject"
    candidates["semantic_type_candidates"][1]["proposed_type"][
        "declared_properties"
    ] = [
        {
            "property_id": "property:collections.ordinal",
            "display_name": "Ordinal",
            "value_type": "integer",
            "required": True,
        }
    ]
    candidates["completeness_candidates"] = [
        {
            "candidate_id": "candidate:completeness",
            "proposed_requirement": {
                "requirement_id": "completeness-requirement:collections.members",
                "competency_question_ids": [
                    "cq:q1",
                    "cq:q2",
                    "cq:q3",
                    "cq:q4",
                    "cq:q5",
                ],
                "requirement_kind": "structured_fact_set",
                "scope_type_id": record_type,
                "scoped_subtype_id": None,
                "scoped_filter": None,
                "rationale": "Question q1 requires an ordered member collection.",
                "source_kind": "competency_question",
                "source_question_ids": ["cq:q1"],
                "governance_references": [],
                "evidence_span_ids": [],
                "coverage_status": "covered",
                "unsupported_reason": None,
                "required_roles": None,
                "structured_fact_set": {
                    "aggregate_type_id": record_type,
                    "membership_relationship_type_id": relationship_type,
                    "allowed_member_type_ids": [subject_type],
                    "member_role_ids": [],
                    "ordering_policy": {
                        "mode": "ordered",
                        "ordinal_property_id": "property:collections.ordinal",
                        "ordinal_value_type": "integer",
                        "direction": "ascending",
                        "unique_ordinals": True,
                        "contiguous": True,
                    },
                    "cardinality": {
                        "expected_count": 3,
                        "minimum_count": 3,
                        "maximum_count": 3,
                        "count_basis": "distinct_members_per_aggregate",
                        "source_kind": "competency_question",
                        "source_question_ids": ["cq:q1"],
                        "source_evidence_span_ids": [],
                        "reviewed_rationale": (
                            "The reviewed competency question explicitly requires three."
                        ),
                    },
                    "collection_identity_policy": {
                        "aggregate_identity_included": True,
                        "membership_relationship_included": True,
                        "member_identities_included": True,
                        "member_roles_included": False,
                        "ordinals_included": True,
                        "preserve_member_order": True,
                        "hash_algorithm": "sha256",
                    },
                    "membership_source_kind": "competency_question",
                    "membership_evidence_span_ids": [],
                    "membership_rationale": (
                        "The reviewed question requires collection membership."
                    ),
                },
            },
            "score_inputs": {
                "accepted_evidence_span_count": 0,
                "required_evidence_span_count": 0,
                "covered_competency_question_count": 1,
                "total_relevant_competency_question_count": 1,
                "ambiguity_conflict_count": 0,
                "classification_fit": "exact",
                "ip_governance_status": "eligible",
            },
        }
    ]

    prepared = prepare_l1_stage(
        _preflight(tmp_path, "collections"),
        candidates=candidates,
    )

    requirement = prepared.proposal.draft_contract.completeness_requirements[0]
    assert requirement.structured_fact_set is not None
    assert requirement.structured_fact_set.cardinality.expected_count == 3
    assert (
        prepared.proposal.draft_contract.reasoning_policy.relationship_type_count
        == 1
    )
    assert set(question_ids) == {
        item.question_id
        for item in prepared.proposal.draft_contract.completeness_question_coverage
    }


def test_every_candidate_is_preserved_in_deterministic_audit(tmp_path) -> None:
    candidates = _candidates("audit")
    candidates["external_reference_candidates"] = [
        {
            "candidate_id": "candidate:external",
            "source_uri": "https://example.test/vocabulary",
            "version": "1",
            "content_hash": "a" * 64,
            "retrieved_at_utc": "2025-01-01T00:00:00Z",
            "provenance": "User supplied metadata only.",
            "license_classification": "unclear",
            "allowed_use_decision": "license_unclear",
            "reviewer": None,
            "approval_reference": None,
            "semantic_target_ids": ["semantic-type:audit.record"],
            "evidence_span_ids": [],
            "rationale": "No legal approval was supplied.",
        }
    ]

    prepared = prepare_l1_stage(
        _preflight(tmp_path, "audit"),
        candidates=candidates,
    )

    audit = {
        item.candidate_id: item for item in prepared.proposal.candidate_audit
    }
    assert len(audit) == prepared.proposal.candidate_count
    assert audit["candidate:external"].disposition == "rejected"
    assert not prepared.proposal.draft_contract.approved_external_references


def test_unknown_evidence_id_is_rejected_even_on_unselected_candidate(
    tmp_path,
) -> None:
    candidates = _candidates("evidence")
    duplicate = dict(candidates["domain_boundary_candidates"][0])
    duplicate["candidate_id"] = "candidate:unsupported-boundary"
    duplicate["evidence_span_ids"] = ["evidence-span:not-locally-minted"]
    duplicate["score_inputs"] = {
        **duplicate["score_inputs"],
        "ip_governance_status": "rejected",
    }
    candidates["domain_boundary_candidates"].append(duplicate)

    with pytest.raises(ProposalArtifactError, match="invented evidence IDs"):
        prepare_l1_stage(
            _preflight(tmp_path, "evidence"),
            candidates=candidates,
        )
