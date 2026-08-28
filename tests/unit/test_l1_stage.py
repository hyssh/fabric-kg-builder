from __future__ import annotations

import json
from pathlib import Path

import pytest

from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.domain.stage import (
    L1ProposalSchemaRepairError,
    L1ZeroSupportedRoutesError,
    approve_persisted_l1_draft,
    dry_run_l1,
    finalize_l1_stage,
    preflight_l1_inputs,
    prepare_l1_stage,
    _normalize_question_route_shapes,
)


def _intake(domain: str = "records") -> dict:
    return {
        "business_goal": f"Support evidence-backed {domain} decisions.",
        "organization_context": "A generic organization.",
        "users": ["analyst"],
        "decisions": ["review governed records"],
        "desired_outcomes": ["trace exact evidence"],
        "in_scope": [domain],
        "out_of_scope": ["unreviewed automation"],
        "competency_questions": [
            f"What evidence supports {domain} question number {index}?"
            for index in range(1, 6)
        ],
    }


def _candidates(domain: str = "records") -> dict:
    question_ids = [f"cq:q{index}" for index in range(1, 6)]
    score_inputs = {
        "accepted_evidence_span_count": 0,
        "required_evidence_span_count": 0,
        "covered_competency_question_count": 5,
        "total_relevant_competency_question_count": 5,
        "ambiguity_conflict_count": 0,
        "classification_fit": "exact",
        "ip_governance_status": "eligible",
    }
    key_policy = {
        "authority": "user_approved",
        "namespace": domain,
        "key_mode": "stable_source_identity",
        "business_key_fields": [],
        "normalization_version": "1",
        "collision_behavior": "block",
        "missing_key_behavior": "unresolved",
        "type_independent": True,
    }
    sibling_policy = {
        "mode": "unresolved",
        "discriminator_property_id": None,
        "rationale": "Ambiguous sibling observations remain unresolved.",
    }

    def proposed_type(type_id: str, semantic_key: str, classification: str) -> dict:
        return {
            "type_id": type_id,
            "semantic_key": semantic_key,
            "display_name": semantic_key.replace("_", " ").title(),
            "description": f"A governed {semantic_key}.",
            "aliases": [],
            "classification": classification,
            "parent_type_id": None,
            "abstract": False,
            "identity_root_type_id": type_id,
            "identity_key_policy": {
                **key_policy,
                "namespace": f"{domain}.{semantic_key}",
            },
            "declared_properties": [],
            "declared_constraints": [],
            "sibling_classification_policy": sibling_policy,
            "generalization_basis": None,
            "evidence_span_ids": [],
            "competency_question_ids": question_ids,
            "governance_rationale": "Required by approved competency questions.",
            "tombstoned": False,
        }

    record_type_id = f"semantic-type:{domain}.record"
    subject_type_id = f"semantic-type:{domain}.subject"
    relationship_type_id = f"relationship-type:{domain}.record-subject"
    return {
        "contract_version": "1.0.0",
        "domain_boundary_candidates": [
            {
                "candidate_id": "candidate:boundary",
                "domain_name": domain.title(),
                "domain_description": f"Generic evidence-backed {domain}.",
                "subdomains": [],
                "in_scope": [domain],
                "out_of_scope": [],
                "evidence_span_ids": [],
                "competency_question_ids": question_ids,
                "governance_rationale": "User-approved scope.",
                "score_inputs": score_inputs,
            }
        ],
        "semantic_type_candidates": [
            {
                "candidate_id": "candidate:type-record",
                "proposed_type": proposed_type(
                    record_type_id, "record", "common"
                ),
                "score_inputs": score_inputs,
            },
            {
                "candidate_id": "candidate:type-subject",
                "proposed_type": proposed_type(
                    subject_type_id, "subject", "domain"
                ),
                "score_inputs": score_inputs,
            },
        ],
        "generalization_candidates": [],
        "relationship_candidates": [
            {
                "candidate_id": "candidate:relationship",
                "relationship_type_id": relationship_type_id,
                "predicate_id": f"predicate:{domain}.describes",
                "semantic_key": "describes",
                "inverse_of_candidate_id": None,
                "display_name": "describes",
                "description": "A record describes a governed subject.",
                "source_type_ids": [record_type_id],
                "target_type_ids": [subject_type_id],
                "endpoint_policy": "allow_subtypes",
                "competency_question_ids": question_ids,
                "evidence_span_ids": [],
                "governance_rationale": "Required by approved questions.",
                "identity_context_policy": "governed validity context",
                "score_inputs": score_inputs,
            }
        ],
        "completeness_candidates": [],
        "question_routes": [
            {
                "question_id": question_id,
                "start_type_id": record_type_id,
                "end_type_id": subject_type_id,
                "unsupported_reason": None,
            }
            for question_id in question_ids
        ],
        "external_reference_candidates": [],
        "assumptions": [],
        "warnings": [],
    }


def _preflight(tmp_path: Path, domain: str = "records"):
    source = tmp_path / "source"
    source.mkdir()
    (source / "records.txt").write_text(
        "A governed record describes a governed subject.",
        encoding="utf-8",
    )
    (source / "unsupported.bin").write_bytes(b"\x00\x01")
    return preflight_l1_inputs(
        source_path=source,
        intake_raw=_intake(domain),
        project_id=f"project:{domain}",
        run_id=f"run:{domain}",
        model_version="fixture/1.0.0",
        model_hash=canonical_sha256({"fixture": domain}),
    )


class _RepairingProposalClient:
    def __init__(self, *, half_route: bool = False) -> None:
        self.calls = 0
        self.half_route = half_route

    def complete_json(self, **kwargs):
        self.calls += 1
        if self.calls != 1:
            raise AssertionError("proposal client must not be retried")
        raw = _candidates("records")
        route = raw["question_routes"][0]
        route["start_type_id"] = (
            route["start_type_id"] if self.half_route else None
        )
        route["end_type_id"] = None
        route.pop("unsupported_reason", None)
        return raw


def test_proposal_route_reason_is_normalized_locally_without_retry(
    tmp_path: Path,
) -> None:
    client = _RepairingProposalClient()
    prepared = prepare_l1_stage(_preflight(tmp_path), client=client)
    assert client.calls == 1
    assert prepared.model_call_count == 1
    assert (
        prepared.candidates.question_routes[0].unsupported_reason
        == "unsupported_reason_missing"
    )


def test_half_defined_route_downgrades_without_regeneration(
    tmp_path: Path,
) -> None:
    client = _RepairingProposalClient(half_route=True)
    prepared = prepare_l1_stage(_preflight(tmp_path), client=client)
    route = prepared.candidates.question_routes[0]
    assert route.start_type_id is None
    assert route.end_type_id is None
    assert route.unsupported_reason == "route_endpoint_pair_half_defined"
    assert client.calls == 1


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ({"remove": "end_type_id"}, "route_endpoint_key_missing"),
        ({"end_type_id": None}, "route_endpoint_pair_half_defined"),
        ({"start_type_id": 42}, "route_endpoint_type_invalid"),
        (
            {
                "start_type_id": None,
                "end_type_id": None,
                "remove": "unsupported_reason",
            },
            "unsupported_reason_missing",
        ),
        (
            {
                "start_type_id": None,
                "end_type_id": None,
                "unsupported_reason": 42,
            },
            "unsupported_reason_type_invalid",
        ),
    ],
)
def test_raw_route_classification_uses_stable_codes(
    mutation: dict,
    expected_code: str,
) -> None:
    raw = _candidates("records")
    route = raw["question_routes"][0]
    removed = mutation.get("remove")
    if removed:
        route.pop(removed, None)
    route.update(
        {
            key: value
            for key, value in mutation.items()
            if key != "remove"
        }
    )
    normalized = _normalize_question_route_shapes(
        raw,
        trusted_question_ids=tuple(
            f"cq:q{index}" for index in range(1, 6)
        ),
    )
    assert (
        normalized["question_routes"][0]["unsupported_reason"]
        == expected_code
    )
    assert normalized["question_routes"][0]["start_type_id"] is None
    assert normalized["question_routes"][0]["end_type_id"] is None


def test_raw_route_unknown_question_id_fails_typed() -> None:
    raw = _candidates("records")
    raw["question_routes"][0]["question_id"] = "model-invented"
    with pytest.raises(
        L1ProposalSchemaRepairError,
        match="route_question_id_unknown",
    ):
        _normalize_question_route_shapes(
            raw,
            trusted_question_ids=tuple(
                f"cq:q{index}" for index in range(1, 6)
            ),
        )


def test_supported_route_reason_is_removed_without_endpoint_change() -> None:
    raw = _candidates("records")
    route = raw["question_routes"][0]
    endpoints = (route["start_type_id"], route["end_type_id"])
    route["unsupported_reason"] = "model-added reason"
    normalized = _normalize_question_route_shapes(
        raw,
        trusted_question_ids=tuple(
            f"cq:q{index}" for index in range(1, 6)
        ),
    )
    repaired = normalized["question_routes"][0]
    assert (repaired["start_type_id"], repaired["end_type_id"]) == endpoints
    assert repaired["unsupported_reason"] is None


class _ZeroRouteRepairClient:
    def __init__(self, *, keep_unsupported: bool = False) -> None:
        self.calls = 0
        self.keep_unsupported = keep_unsupported

    def complete_json(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raw = _candidates("records")
            for route in raw["question_routes"]:
                route["start_type_id"] = None
                route["end_type_id"] = None
                route.pop("unsupported_reason", None)
            return raw
        request = json.loads(kwargs["user"])
        routes = []
        for index, question in enumerate(
            request["ordered_competency_questions"]
        ):
            if index == 0 and not self.keep_unsupported:
                routes.append(
                    {
                        "question_id": question["question_id"],
                        "source_type_id": "semantic-type:records.record",
                        "target_type_id": "semantic-type:records.subject",
                        "unsupported_reason": None,
                    }
                )
            else:
                routes.append(
                    {
                        "question_id": question["question_id"],
                        "source_type_id": None,
                        "target_type_id": None,
                        "unsupported_reason": "No validated path.",
                    }
                )
        return {"question_routes": routes}


class _UnavailablePathRepairClient(_ZeroRouteRepairClient):
    def complete_json(self, **kwargs):
        if self.calls == 0:
            self.calls += 1
            raw = _candidates("records")
            raw["relationship_candidates"][0][
                "competency_question_ids"
            ] = []
            return raw
        return super().complete_json(**kwargs)


class _CandidateRegenerationClient:
    def __init__(self, *, second_valid: bool) -> None:
        self.calls = 0
        self.second_valid = second_valid

    def complete_json(self, **kwargs):
        self.calls += 1
        raw = _candidates("records")
        if self.calls == 1 or not self.second_valid:
            raw["relationship_candidates"] = []
            for route in raw["question_routes"]:
                route["start_type_id"] = None
                route["end_type_id"] = None
                route["unsupported_reason"] = "No candidate relationship."
        return raw


def test_zero_supported_routes_use_one_strict_route_only_repair(
    tmp_path: Path,
) -> None:
    client = _ZeroRouteRepairClient()
    prepared = prepare_l1_stage(_preflight(tmp_path), client=client)
    assert client.calls == 2
    assert prepared.model_call_count == 2
    assert prepared.candidates.question_routes[0].start_type_id == (
        "semantic-type:records.record"
    )


def test_zero_supported_route_repair_exhaustion_is_typed(
    tmp_path: Path,
) -> None:
    client = _ZeroRouteRepairClient(keep_unsupported=True)
    with pytest.raises(L1ZeroSupportedRoutesError) as captured:
        prepare_l1_stage(_preflight(tmp_path), client=client)
    assert captured.value.audit_payload.model_call_count == 2
    assert (
        captured.value.audit_payload.reason_code
        == "route_patch_zero_supported"
    )


def test_structurally_supported_but_unavailable_paths_trigger_repair(
    tmp_path: Path,
) -> None:
    client = _UnavailablePathRepairClient(keep_unsupported=True)
    with pytest.raises(L1ZeroSupportedRoutesError) as captured:
        prepare_l1_stage(_preflight(tmp_path), client=client)
    audit = captured.value.audit_payload
    assert audit.initial_route_codes == (
        "supported_path_unavailable",
    ) * 5
    assert audit.route_repair_attempted is True
    assert client.calls == 2


def test_insufficient_candidate_vocabulary_gets_one_full_regeneration(
    tmp_path: Path,
) -> None:
    client = _CandidateRegenerationClient(second_valid=True)
    prepared = prepare_l1_stage(_preflight(tmp_path), client=client)
    assert client.calls == 2
    assert prepared.model_call_count == 2
    assert prepared.candidates.relationship_candidates


def test_repeated_insufficient_candidate_vocabulary_is_typed(
    tmp_path: Path,
) -> None:
    client = _CandidateRegenerationClient(second_valid=False)
    with pytest.raises(L1ZeroSupportedRoutesError) as captured:
        prepare_l1_stage(_preflight(tmp_path), client=client)
    audit = captured.value.audit_payload
    assert audit.reason_code == "candidate_regeneration_insufficient"
    assert len(audit.candidate_attempt_hashes) == 2
    assert audit.candidate_attempt_relationship_counts == (0, 0)
    assert client.calls == 2


def test_review_summary_escapes_terminal_control_characters(
    tmp_path: Path,
) -> None:
    candidates = _candidates("records")
    candidates["semantic_type_candidates"][0]["proposed_type"][
        "display_name"
    ] = "\x1b[2JForged"
    prepared = prepare_l1_stage(
        _preflight(tmp_path),
        candidates=candidates,
    )
    assert "\x1b" not in prepared.summary
    assert "\\u001b[2JForged" in prepared.summary


def test_dry_run_inventories_complete_corpus_without_writes(tmp_path: Path) -> None:
    preflight = _preflight(tmp_path)
    state_root = tmp_path / ".fkg" / "l1"
    domain_path = tmp_path / "domain.yaml"

    result = dry_run_l1(
        preflight,
        state_root=state_root,
        domain_path=domain_path,
    )

    assert preflight.corpus.total_entry_count == 2
    assert result.receipt is None
    assert not state_root.exists()
    assert not domain_path.exists()


def test_approved_l1_stage_binds_complete_corpus_and_bounded_sample(
    tmp_path: Path,
) -> None:
    prepared = prepare_l1_stage(
        _preflight(tmp_path, "equipment"),
        candidates=_candidates("equipment"),
    )

    result = finalize_l1_stage(
        prepared,
        decision="approve",
        actor="reviewer@example.test",
        state_root=tmp_path / ".fkg" / "l1",
        domain_path=tmp_path / "domain.yaml",
    )

    assert result.status == "succeeded"
    assert result.receipt is not None and result.receipt.status == "succeeded"
    assert result.contract is not None
    assert result.contract.approval.status == "approved"
    assert prepared.sample_manifest.source_corpus_manifest_hash == (
        prepared.preflight.corpus.corpus_hash
    )
    assert {
        entry.source_file_id for entry in prepared.sample_manifest.entries
    } <= {
        entry.source_file_id for entry in prepared.preflight.corpus.entries
    }
    assert prepared.preflight.corpus.total_entry_count == 2


def test_noninteractive_stage_is_blocked_until_explicit_approval(
    tmp_path: Path,
) -> None:
    prepared = prepare_l1_stage(
        _preflight(tmp_path, "contracts"),
        candidates=_candidates("contracts"),
    )

    result = finalize_l1_stage(
        prepared,
        decision=None,
        actor=None,
        state_root=tmp_path / ".fkg" / "l1",
        domain_path=tmp_path / "domain.yaml",
    )

    assert result.status == "blocked"
    assert result.receipt is not None
    assert result.receipt.error_codes == ("L1_APPROVAL_REQUIRED",)
    assert result.contract is not None
    assert result.contract.approval.status == "draft"

    approved = approve_persisted_l1_draft(
        actor="automation-reviewer@example.test",
        state_root=tmp_path / ".fkg" / "l1",
        domain_path=tmp_path / "domain.yaml",
    )
    assert approved.status == "succeeded"
    assert approved.contract is not None
    assert approved.contract.approval.status == "approved"
