from __future__ import annotations

import pytest

from fabric_kg_builder.domain.hierarchy import (
    build_type_hierarchy_closure,
    stable_entity_identity_inputs,
    stable_relationship_identity_inputs,
)
from fabric_kg_builder.domain.models import (
    DomainEntityTypeV2,
    DomainRelationshipTypeV2,
)
from fabric_kg_builder.domain.proposal import (
    DomainProposalCandidatesV2,
    normalize_candidate_scores,
)
from tests.unit.test_l1_stage import _candidates


def _models():
    candidates = _candidates("education")
    child = candidates["semantic_type_candidates"][1]["proposed_type"]
    child["parent_type_id"] = "semantic-type:education.record"
    child["identity_root_type_id"] = "semantic-type:education.record"
    child["identity_key_policy"] = None
    child["generalization_basis"] = {
        "competency_question_ids": ["cq:q1"],
        "evidence_span_ids": [],
        "governance_rationale": "Reviewed specialization semantics.",
    }
    validated = DomainProposalCandidatesV2.model_validate(
        normalize_candidate_scores(candidates)
    )
    entities = [
        item.proposed_type for item in validated.semantic_type_candidates
    ]
    relationship_candidate = validated.relationship_candidates[0]
    relationship = DomainRelationshipTypeV2(
        relationship_type_id=relationship_candidate.relationship_type_id,
        predicate_id=relationship_candidate.predicate_id,
        display_name=relationship_candidate.display_name,
        description=relationship_candidate.description,
        source_type_ids=list(relationship_candidate.source_type_ids),
        target_type_ids=list(relationship_candidate.target_type_ids),
        endpoint_policy=relationship_candidate.endpoint_policy,
        identity_policy={
            "context_policy": relationship_candidate.identity_context_policy
        },
        competency_question_ids=list(
            relationship_candidate.competency_question_ids
        ),
        governance_rationale=relationship_candidate.governance_rationale,
        evidence_span_ids=[],
    )
    return entities, relationship


def test_hierarchy_closure_is_deterministic_and_inherits_identity_root() -> None:
    entities, relationship = _models()

    closure = build_type_hierarchy_closure(entities, [relationship])

    assert closure.ancestors_by_type["semantic-type:education.subject"] == [
        "semantic-type:education.record"
    ]
    assert "semantic-type:education.subject" in (
        closure.compatible_source_type_ids_by_relationship[
            relationship.relationship_type_id
        ]
    )
    assert closure == build_type_hierarchy_closure(reversed(entities), [relationship])


def test_hierarchy_cycle_is_rejected() -> None:
    entities, relationship = _models()
    root = entities[0].model_copy(
        update={
            "parent_type_id": entities[1].type_id,
            "identity_root_type_id": entities[1].type_id,
            "identity_key_policy": None,
            "generalization_basis": {
                "competency_question_ids": ["cq:q1"],
                "evidence_span_ids": [],
                "governance_rationale": "Invalid cycle fixture.",
            },
        }
    )

    with pytest.raises(ValueError, match="cycle"):
        build_type_hierarchy_closure([root, entities[1]], [relationship])


def test_entity_identity_survives_reclassification() -> None:
    entities, _ = _models()
    policy = entities[0].identity_key_policy
    before = stable_entity_identity_inputs(
        project_id="project:test",
        policy=policy,
        stable_source_identity="asset-version:123",
    )
    after = stable_entity_identity_inputs(
        project_id="project:test",
        policy=policy,
        stable_source_identity="asset-version:123",
    )

    assert before == after
    assert "type_id" not in before
    assert "display_name" not in before


def test_relationship_identity_excludes_endpoint_type_labels() -> None:
    values = stable_relationship_identity_inputs(
        predicate_id="predicate:governs",
        source_entity_id="entity:a",
        target_entity_id="entity:b",
        governed_context={"valid_from": "2025-01-01"},
    )

    assert set(values) == {
        "predicate_id",
        "source_entity_id",
        "target_entity_id",
        "governed_context",
    }
