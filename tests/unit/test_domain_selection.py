from __future__ import annotations

import pytest

from fabric_kg_builder.domain.proposal import (
    DomainProposalCandidatesV2,
    normalize_candidate_scores,
)
from fabric_kg_builder.domain.selection import (
    ProposalSelectionError,
    merge_relationship_candidates,
    select_relationship_vocabulary,
)
from tests.unit.test_l1_stage import _candidates


def _validated(domain: str = "records") -> DomainProposalCandidatesV2:
    return DomainProposalCandidatesV2.model_validate(
        normalize_candidate_scores(_candidates(domain))
    )


def test_duplicate_merge_uses_smallest_stable_relationship_id() -> None:
    candidates = _candidates()
    duplicate = dict(candidates["relationship_candidates"][0])
    duplicate["candidate_id"] = "candidate:duplicate"
    duplicate["relationship_type_id"] = "relationship-type:records.aaa"
    candidates["relationship_candidates"].append(duplicate)
    validated = DomainProposalCandidatesV2.model_validate(
        normalize_candidate_scores(candidates)
    )

    merged, aliases, groups = merge_relationship_candidates(
        validated.relationship_candidates
    )

    assert len(merged) == 1
    assert merged[0].relationship_type_id == "relationship-type:records.aaa"
    assert aliases["candidate:relationship"] == "candidate:duplicate"
    assert groups["candidate:duplicate"] == (
        "candidate:duplicate",
        "candidate:relationship",
    )


def test_selector_uses_minimum_question_path_union_without_padding() -> None:
    candidates = _validated()

    result = select_relationship_vocabulary(
        candidates.relationship_candidates,
        candidates.question_routes,
        critical_question_ids={item.question_id for item in candidates.question_routes},
    )

    assert len(result.relationships) == 1
    assert result.max_hops == 1
    assert all(item.covered for item in result.question_plans)


def test_required_role_relationship_is_selected_off_shortest_path() -> None:
    candidates = _candidates()
    mandatory = dict(candidates["relationship_candidates"][0])
    mandatory["candidate_id"] = "candidate:mandatory"
    mandatory["relationship_type_id"] = "relationship-type:records.mandatory"
    mandatory["predicate_id"] = "predicate:records.mandatory"
    mandatory["semantic_key"] = "mandatory"
    mandatory["competency_question_ids"] = []
    candidates["relationship_candidates"].append(mandatory)
    validated = DomainProposalCandidatesV2.model_validate(
        normalize_candidate_scores(candidates)
    )

    result = select_relationship_vocabulary(
        validated.relationship_candidates,
        validated.question_routes,
        critical_question_ids=set(),
        required_relationship_type_ids={"relationship-type:records.mandatory"},
    )

    assert {
        item.relationship_type_id for item in result.relationships
    } == {
        "relationship-type:records.record-subject",
        "relationship-type:records.mandatory",
    }


def test_k4_requires_exact_evidence_on_every_hop() -> None:
    candidates = _validated()
    relationships = [
        candidates.relationship_candidates[0].model_copy(
            update={
                "candidate_id": f"candidate:r{index}",
                "relationship_type_id": f"relationship-type:records.r{index}",
                "predicate_id": f"predicate:records.r{index}",
                "source_type_ids": (f"semantic-type:records.t{index}",),
                "target_type_ids": (f"semantic-type:records.t{index + 1}",),
                "evidence_span_ids": (),
            }
        )
        for index in range(4)
    ]
    route = candidates.question_routes[0].model_copy(
        update={
            "start_type_id": "semantic-type:records.t0",
            "end_type_id": "semantic-type:records.t4",
        }
    )

    with pytest.raises(ProposalSelectionError, match="per-hop evidence"):
        select_relationship_vocabulary(
            relationships,
            [route],
            critical_question_ids={route.question_id},
        )
