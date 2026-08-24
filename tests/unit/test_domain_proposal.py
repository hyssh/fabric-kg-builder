from __future__ import annotations

import copy
import hashlib
import json
import sys
import types
from pathlib import Path

import pytest
from pydantic import ValidationError

from fabric_kg_builder.domain.proposal import (
    DOMAIN_PROPOSAL_SYSTEM_PROMPT,
    DomainIntake,
    DomainProposal,
    DomainProposalCandidates,
    ProposalQuestionRoute,
    ProposalScore,
    RelationshipCandidate,
    compute_proposal_hash,
    domain_proposal_candidates_json_schema,
    domain_proposal_json_schema,
    generate_domain_proposal,
    load_domain_intake,
    load_domain_proposal,
)
from fabric_kg_builder.domain.selection import (
    ProposalSelectionError,
    _selection_key,
    merge_relationship_candidates,
    select_relationship_vocabulary,
)

pytestmark = pytest.mark.unit

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "domain_proposals"
_PROFILE_HASH_EXCLUDED_KEYS = frozenset(
    {"approved", "approved_at_utc", "approved_by", "inspected_at_utc", "profile_hash"}
)


def _load_json(name: str) -> dict:
    return json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))


class FakeSourceProfile(dict):
    @property
    def representative_samples(self) -> list[dict]:
        return self["representative_samples"]


class FakeFoundryClient:
    def __init__(self, response: dict) -> None:
        self._response = response
        self.calls: list[dict] = []

    def execution_identity(self) -> dict[str, str]:
        return {
            "provider": "fake-foundry",
            "deployment": "gpt-test-domain",
            "api_version": "2026-08-01-preview",
        }

    def complete_json(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return copy.deepcopy(self._response)



def _canonicalize_profile_hash_value(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _canonicalize_profile_hash_value(val)
            for key, val in sorted(value.items())
            if key not in _PROFILE_HASH_EXCLUDED_KEYS
        }
    if isinstance(value, list):
        return [_canonicalize_profile_hash_value(item) for item in value]
    return value



def _compute_source_profile_hash(profile: FakeSourceProfile | dict[str, object]) -> str:
    canonical = _canonicalize_profile_hash_value(dict(profile))
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()



def _sample_row(
    sample_id: str,
    *,
    sample_kind: str,
    source_file_id: str,
    citation: str,
    locator: dict[str, object],
    excerpt: str,
) -> dict[str, object]:
    return {
        "id": sample_id,
        "sample_kind": sample_kind,
        "source_file_id": source_file_id,
        "citation": citation,
        "locator": locator,
        "excerpt": excerpt,
        "content_hash": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
    }



def _make_source_profile() -> FakeSourceProfile:
    return FakeSourceProfile(
        {
            "schema_version": "test-1.0",
            "source_hash": "abc123",
            "domain_description": "Facility maintenance source profile for proposal tests.",
            "observed": {
                "total_file_count": 5,
                "format_counts": {"csv": 1, "markdown": 2, "pdf": 2},
                "total_bytes": 20480,
                "date_range": ["2024", "2026"],
                "csv_column_names": ["asset_id", "facility", "status"],
            },
            "inferred": {
                "document_categories": ["equipment schedules", "maintenance records"],
                "entity_candidates": ["Equipment", "Facility", "Work Order"],
                "extraction_risks": [],
            },
            "representative_samples": [
                _sample_row(
                    "sample:facility-layout",
                    sample_kind="heading",
                    source_file_id="source-file:facility-layout",
                    citation="docs/facility-layout.pdf#page=1",
                    locator={"page": 1, "section": "Facility summary"},
                    excerpt="Building A contains air handler AHU-4 and pump P-220 in the central plant.",
                ),
                _sample_row(
                    "sample:equipment-register",
                    sample_kind="table",
                    source_file_id="source-file:equipment-register",
                    citation="data/equipment-register.csv",
                    locator={"row": 14},
                    excerpt="AHU-4, Building A, HVAC, Active",
                ),
                _sample_row(
                    "sample:work-order-log",
                    sample_kind="text",
                    source_file_id="source-file:work-order-log",
                    citation="logs/work-orders.md#wo-17",
                    locator={"line": 17},
                    excerpt="WO-17 applies to AHU-4 after a vibration alarm in Building A.",
                ),
                _sample_row(
                    "sample:maintenance-log",
                    sample_kind="text",
                    source_file_id="source-file:maintenance-log",
                    citation="logs/maintenance-history.md#ahu-4",
                    locator={"line": 44},
                    excerpt="Maintenance history lists WO-12 and WO-17 against AHU-4.",
                ),
                _sample_row(
                    "sample:service-manual",
                    sample_kind="visual_description",
                    source_file_id="source-file:service-manual",
                    citation="manuals/ahu-4-service.pdf#page=3",
                    locator={"page": 3},
                    excerpt="The service manual labels AHU-4 as installed in Building A.",
                ),
            ],
            "approved": False,
            "profile_hash": "stale",
        }
    )



def _install_inspector_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    package = types.ModuleType("fabric_kg_builder.sources")
    inspector = types.ModuleType("fabric_kg_builder.sources.inspector")
    inspector.compute_source_profile_hash = _compute_source_profile_hash
    package.inspector = inspector
    monkeypatch.setitem(sys.modules, "fabric_kg_builder.sources", package)
    monkeypatch.setitem(sys.modules, "fabric_kg_builder.sources.inspector", inspector)



def _relationship(
    relationship_id: str,
    *,
    predicate: str,
    semantic_key: str,
    source_types: list[str],
    target_types: list[str],
    competency_question_ids: list[str],
    source_evidence_ids: list[str] | None = None,
    inverse_of: str | None = None,
    governance_rule: str | None = None,
    score: float = 1.0,
) -> RelationshipCandidate:
    return RelationshipCandidate(
        id=relationship_id,
        predicate=predicate,
        semantic_key=semantic_key,
        inverse_of=inverse_of,
        description=f"{relationship_id} description",
        source_types=source_types,
        target_types=target_types,
        competency_question_ids=competency_question_ids,
        source_evidence_ids=source_evidence_ids or [],
        governance_rule=governance_rule,
        scores=ProposalScore(
            coverage_score=score,
            source_support_score=score,
            reuse_score=0.0,
            clarity_score=0.0,
            risk_penalty=0.0,
            redundancy_penalty=0.0,
        ),
    )



def _route(question_id: str, start_type: str | None, end_type: str | None, unsupported_reason: str | None = None) -> ProposalQuestionRoute:
    return ProposalQuestionRoute(
        question_id=question_id,
        start_type=start_type,
        end_type=end_type,
        unsupported_reason=unsupported_reason,
    )



def test_proposal_models_require_schema_2_and_forbid_unknown_keys() -> None:
    candidates_payload = _load_json("facility_maintenance_candidates.json")
    candidates_payload["schema_version"] = "1.9"
    with pytest.raises(ValidationError):
        DomainProposalCandidates.model_validate(candidates_payload)

    candidates_payload = _load_json("facility_maintenance_candidates.json")
    candidates_payload["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        DomainProposalCandidates.model_validate(candidates_payload)

    proposal_payload = _load_json("facility_maintenance_proposal.json")
    proposal_payload["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        DomainProposal.model_validate(proposal_payload)


def test_intake_yaml_json_and_generated_proposal_schema_are_stable() -> None:
    json_intake = load_domain_intake(
        _FIXTURE_DIR / "facility_maintenance_intake.json"
    )
    yaml_intake = load_domain_intake(
        _FIXTURE_DIR / "facility_maintenance_intake.yaml"
    )
    assert yaml_intake == json_intake

    artifact_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "fabric_kg_builder"
        / "domain"
        / "domain-proposal.schema.json"
    )
    assert json.loads(artifact_path.read_text(encoding="utf-8")) == (
        domain_proposal_json_schema()
    )



def test_compute_proposal_hash_is_canonical_for_golden_fixture() -> None:
    proposal = load_domain_proposal(_FIXTURE_DIR / "facility_maintenance_proposal.json")
    assert compute_proposal_hash(proposal) == proposal.proposal_hash

    payload = _load_json("facility_maintenance_proposal.json")
    payload["proposal_hash"] = "0" * 64
    reordered = json.loads(json.dumps(payload, sort_keys=True))
    reparsed = DomainProposal.model_validate(reordered)
    assert compute_proposal_hash(reparsed) == proposal.proposal_hash



def test_merge_relationship_candidates_merges_duplicates_and_inverse_endpoints() -> None:
    candidates = DomainProposalCandidates.model_validate(
        _load_json("facility_maintenance_candidates.json")
    )

    merged, aliases, groups = merge_relationship_candidates(candidates.relationship_types)

    assert [item.id for item in merged] == [
        "relationship-type:contains",
        "relationship-type:work-order-for",
    ]
    contains = merged[0]
    assert contains.description == (
        "A facility contains installed equipment documented in the equipment register."
    )
    assert contains.source_types == ["entity-type:facility"]
    assert contains.target_types == ["entity-type:equipment"]
    assert contains.competency_question_ids == [
        "cq:equipment-location",
        "cq:facility-equipment",
        "cq:facility-work-orders",
    ]
    assert contains.source_evidence_ids == [
        "proposal-evidence:1c84374ee25f7c6a3c8d5e20",
        "proposal-evidence:229584004727433c22a0e8ed",
        "proposal-evidence:cd4d73b167a9e3bd01c2a499",
    ]
    assert aliases["relationship-type:installed-in"] == "relationship-type:contains"
    assert groups["relationship-type:contains"] == (
        "relationship-type:contains",
        "relationship-type:contains-register",
        "relationship-type:installed-in",
    )


def test_merge_relationship_candidates_preserves_conflicting_endpoint_policies() -> None:
    question_id = "cq:facility-equipment"
    allow_subtypes = _relationship(
        "relationship-type:contains-flexible",
        predicate="contains_flexible",
        semantic_key="contains",
        source_types=["entity-type:facility"],
        target_types=["entity-type:equipment"],
        competency_question_ids=[question_id],
        source_evidence_ids=["proposal-evidence:flexible"],
    )
    exact = _relationship(
        "relationship-type:contains-exact",
        predicate="contains_exact",
        semantic_key="contains",
        source_types=["entity-type:facility"],
        target_types=["entity-type:equipment"],
        competency_question_ids=[question_id],
        source_evidence_ids=["proposal-evidence:exact"],
    ).model_copy(update={"endpoint_policy": "exact"})

    merged, aliases, groups = merge_relationship_candidates(
        [allow_subtypes, exact]
    )

    assert [item.id for item in merged] == [
        "relationship-type:contains-exact",
        "relationship-type:contains-flexible",
    ]
    assert aliases == {
        "relationship-type:contains-exact": "relationship-type:contains-exact",
        "relationship-type:contains-flexible": "relationship-type:contains-flexible",
    }
    assert all(len(group) == 1 for group in groups.values())


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf"), -0.01, 100.01],
)
def test_proposal_scores_reject_nonfinite_or_unreasonable_values(
    value: float,
) -> None:
    with pytest.raises(ValidationError):
        ProposalScore(coverage_score=value)


def test_selection_score_key_uses_stable_ids_and_fsum() -> None:
    candidates = {
        item.id: item
        for item in [
            _relationship(
                "relationship-type:a",
                predicate="a",
                semantic_key="a",
                source_types=["entity-type:facility"],
                target_types=["entity-type:equipment"],
                competency_question_ids=["cq:q"],
                source_evidence_ids=["proposal-evidence:a"],
                score=0.1,
            ),
            _relationship(
                "relationship-type:b",
                predicate="b",
                semantic_key="b",
                source_types=["entity-type:facility"],
                target_types=["entity-type:equipment"],
                competency_question_ids=["cq:q"],
                source_evidence_ids=["proposal-evidence:b"],
                score=0.2,
            ),
            _relationship(
                "relationship-type:c",
                predicate="c",
                semantic_key="c",
                source_types=["entity-type:facility"],
                target_types=["entity-type:equipment"],
                competency_question_ids=["cq:q"],
                source_evidence_ids=["proposal-evidence:c"],
                score=0.3,
            ),
        ]
    }
    first = frozenset(
        [
            "relationship-type:c",
            "relationship-type:a",
            "relationship-type:b",
        ]
    )
    second = frozenset(
        [
            "relationship-type:b",
            "relationship-type:c",
            "relationship-type:a",
        ]
    )

    assert _selection_key(first, candidates) == _selection_key(second, candidates)
    assert _selection_key(first, candidates)[2] == tuple(sorted(first))



def test_select_relationship_vocabulary_uses_exact_minimum_and_deterministic_tie_breaking() -> None:
    qid = "cq:facility-equipment"
    rel_a = _relationship(
        "relationship-type:a-connects",
        predicate="a_connects",
        semantic_key="a_connects",
        source_types=["entity-type:facility"],
        target_types=["entity-type:equipment"],
        competency_question_ids=[qid],
        source_evidence_ids=["proposal-evidence:a"],
        score=1.0,
    )
    rel_b = _relationship(
        "relationship-type:b-connects",
        predicate="b_connects",
        semantic_key="b_connects",
        source_types=["entity-type:facility"],
        target_types=["entity-type:equipment"],
        competency_question_ids=[qid],
        source_evidence_ids=["proposal-evidence:b"],
        score=1.0,
    )

    result = select_relationship_vocabulary(
        [rel_b, rel_a],
        [_route(qid, "entity-type:facility", "entity-type:equipment")],
        critical_question_ids={qid},
    )

    assert [item.id for item in result.relationships] == ["relationship-type:a-connects"]
    assert result.question_plans[0].required_path[0].relationship_type == "relationship-type:a-connects"



def test_selection_respects_question_scope_and_directional_shortest_paths() -> None:
    facility_orders = "cq:facility-work-orders"
    equipment_location = "cq:equipment-location"
    shortcut_question = "cq:facility-equipment"
    contains = _relationship(
        "relationship-type:contains",
        predicate="contains",
        semantic_key="contains",
        source_types=["entity-type:facility"],
        target_types=["entity-type:equipment"],
        competency_question_ids=[facility_orders, equipment_location],
        source_evidence_ids=["proposal-evidence:contains"],
        score=2.0,
    )
    work_order_for = _relationship(
        "relationship-type:work-order-for",
        predicate="work_order_for",
        semantic_key="work_order_for",
        source_types=["entity-type:work-order"],
        target_types=["entity-type:equipment"],
        competency_question_ids=[facility_orders],
        source_evidence_ids=["proposal-evidence:wo"],
        score=2.0,
    )
    shortcut = _relationship(
        "relationship-type:facility-work-order-shortcut",
        predicate="facility_work_order_shortcut",
        semantic_key="facility_work_order_shortcut",
        source_types=["entity-type:facility"],
        target_types=["entity-type:work-order"],
        competency_question_ids=[shortcut_question],
        source_evidence_ids=["proposal-evidence:shortcut"],
        score=5.0,
    )

    result = select_relationship_vocabulary(
        [contains, work_order_for, shortcut],
        [
            _route(facility_orders, "entity-type:facility", "entity-type:work-order"),
            _route(equipment_location, "entity-type:equipment", "entity-type:facility"),
        ],
        critical_question_ids={facility_orders, equipment_location},
    )

    assert [item.id for item in result.relationships] == [
        "relationship-type:contains",
        "relationship-type:work-order-for",
    ]
    facility_plan = next(plan for plan in result.question_plans if plan.question_id == facility_orders)
    assert [(step.relationship_type, step.traversal) for step in facility_plan.required_path] == [
        ("relationship-type:contains", "forward"),
        ("relationship-type:work-order-for", "reverse"),
    ]
    equipment_plan = next(plan for plan in result.question_plans if plan.question_id == equipment_location)
    assert [(step.relationship_type, step.traversal) for step in equipment_plan.required_path] == [
        ("relationship-type:contains", "reverse"),
    ]
    assert result.max_hops == 2
    assert result.max_hops_rationale is None



def test_selection_does_not_pad_below_advisory_n_and_keeps_unsupported_critical_questions_visible() -> None:
    candidates = DomainProposalCandidates.model_validate(
        _load_json("facility_maintenance_candidates.json")
    )
    intake = DomainIntake.model_validate(_load_json("facility_maintenance_intake.json"))

    result = select_relationship_vocabulary(
        candidates.relationship_types,
        candidates.question_routes,
        critical_question_ids={item.id for item in intake.competency_questions if item.business_critical},
    )

    assert [item.id for item in result.relationships] == [
        "relationship-type:contains",
        "relationship-type:work-order-for",
    ]
    assert result.relationship_type_count_rationale is None
    unsupported = next(
        plan
        for plan in result.question_plans
        if plan.question_id == "cq:vendor-response-commitment"
    )
    assert unsupported.covered is False
    assert unsupported.unsupported_reason == (
        "Representative samples do not cite vendor response-time commitments for specific equipment assets."
    )



def test_selection_enforces_hard_n_max_of_24() -> None:
    question_id = "cq:limit-check"
    relationships = [
        _relationship(
            f"relationship-type:limit-{index:02d}",
            predicate=f"limit_{index:02d}",
            semantic_key=f"limit_{index:02d}",
            source_types=["entity-type:a"],
            target_types=["entity-type:b"],
            competency_question_ids=[question_id],
            governance_rule=f"Governance rule {index}",
            score=1.0,
        )
        for index in range(25)
    ]

    with pytest.raises(ProposalSelectionError, match=r"\[DOM-103\].*N=25"):
        select_relationship_vocabulary(
            relationships,
            [_route(question_id, "entity-type:a", "entity-type:b")],
            critical_question_ids={question_id},
        )



def test_selection_derives_k4_and_requires_evidence_on_every_hop() -> None:
    question_id = "cq:campus-root-cause"
    relationships = [
        _relationship(
            "relationship-type:facility-has-line",
            predicate="facility_has_line",
            semantic_key="facility_has_line",
            source_types=["entity-type:facility"],
            target_types=["entity-type:line"],
            competency_question_ids=[question_id],
            source_evidence_ids=["proposal-evidence:line"],
            score=2.0,
        ),
        _relationship(
            "relationship-type:line-feeds-zone",
            predicate="line_feeds_zone",
            semantic_key="line_feeds_zone",
            source_types=["entity-type:line"],
            target_types=["entity-type:zone"],
            competency_question_ids=[question_id],
            source_evidence_ids=["proposal-evidence:zone"],
            score=2.0,
        ),
        _relationship(
            "relationship-type:zone-has-asset",
            predicate="zone_has_asset",
            semantic_key="zone_has_asset",
            source_types=["entity-type:zone"],
            target_types=["entity-type:asset"],
            competency_question_ids=[question_id],
            source_evidence_ids=["proposal-evidence:asset"],
            score=2.0,
        ),
        _relationship(
            "relationship-type:asset-has-alarm",
            predicate="asset_has_alarm",
            semantic_key="asset_has_alarm",
            source_types=["entity-type:asset"],
            target_types=["entity-type:alarm"],
            competency_question_ids=[question_id],
            source_evidence_ids=["proposal-evidence:alarm"],
            score=2.0,
        ),
    ]

    result = select_relationship_vocabulary(
        relationships,
        [_route(question_id, "entity-type:facility", "entity-type:alarm")],
        critical_question_ids={question_id},
    )

    assert result.max_hops == 4
    assert result.max_hops_rationale == (
        "K=4 is required by the cited shortest path(s) for cq:campus-root-cause."
    )

    missing_evidence = relationships[:2] + [
        _relationship(
            "relationship-type:zone-has-asset",
            predicate="zone_has_asset",
            semantic_key="zone_has_asset",
            source_types=["entity-type:zone"],
            target_types=["entity-type:asset"],
            competency_question_ids=[question_id],
            governance_rule="Manually curated pending evidence.",
            score=2.0,
        ),
        relationships[3],
    ]
    with pytest.raises(ProposalSelectionError, match=r"\[DOM-105\].*source evidence"):
        select_relationship_vocabulary(
            missing_evidence,
            [_route(question_id, "entity-type:facility", "entity-type:alarm")],
            critical_question_ids={question_id},
        )



def test_generate_domain_proposal_matches_golden_candidate_to_selected_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_inspector_stub(monkeypatch)
    intake = DomainIntake.model_validate(_load_json("facility_maintenance_intake.json"))
    source_profile = _make_source_profile()
    client = FakeFoundryClient(_load_json("facility_maintenance_candidates.json"))

    proposal = generate_domain_proposal(
        intake,
        source_profile,
        client=client,
        model_version="gpt-test-domain-1",
    )

    golden = load_domain_proposal(_FIXTURE_DIR / "facility_maintenance_proposal.json")
    assert proposal.model_dump(mode="json") == golden.model_dump(mode="json")
    assert proposal.contract.reasoning_policy.relationship_type_count == 2
    assert proposal.contract.reasoning_policy.max_hops == 2
    unsupported = next(
        plan
        for plan in proposal.contract.question_plans
        if plan.question_id == "cq:vendor-response-commitment"
    )
    assert unsupported.covered is False
    assert proposal.relationship_merge_groups == {
        "relationship-type:contains": [
            "relationship-type:contains",
            "relationship-type:contains-register",
            "relationship-type:installed-in",
        ],
        "relationship-type:work-order-for": [
            "relationship-type:work-order-for",
            "relationship-type:work-order-targets",
        ],
    }
    assert len(client.calls) == 1
    assert client.calls[0]["system"] == DOMAIN_PROPOSAL_SYSTEM_PROMPT
    assert client.calls[0]["json_schema"] == domain_proposal_candidates_json_schema()
