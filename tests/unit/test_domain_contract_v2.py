"""Contract tests for additive schema-2.0 domain foundations."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner
from pydantic import ValidationError

from fabric_kg_builder.cli import cli
from fabric_kg_builder.domain import (
    DOMAIN_SCHEMA_VERSION,
    DOMAIN_SCHEMA_V2_VERSION,
    DomainContract,
    DomainContractV2,
    compute_contract_hash,
    default_domain_contract,
    domain_contract_json_schema,
    evaluate_domain_guard_status,
    load_domain_contract,
    run_deterministic_validation,
)


_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "domains"
    / "facility-maintenance-v2.yaml"
)


def _payload() -> dict:
    return yaml.safe_load(_FIXTURE.read_text(encoding="utf-8"))


def _contract() -> DomainContractV2:
    return DomainContractV2.model_validate(_payload())


def _set_path(
    payload: dict,
    path: tuple[str | int, ...],
    value: object,
) -> None:
    target: object = payload
    for key in path[:-1]:
        target = target[key]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]


def test_schema_versions_are_explicit() -> None:
    assert DOMAIN_SCHEMA_VERSION == "1.0"
    assert DOMAIN_SCHEMA_V2_VERSION == "2.0"


def test_default_contract_remains_schema_1() -> None:
    contract = default_domain_contract()
    assert type(contract) is DomainContract
    assert contract.schema_version == "1.0"


def test_versionless_legacy_contract_still_loads_as_schema_1(
    tmp_path: Path,
) -> None:
    payload = default_domain_contract().model_dump(mode="json")
    payload.pop("schema_version")
    path = tmp_path / "domain.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    contract = load_domain_contract(path)
    assert type(contract) is DomainContract
    assert contract.schema_version == "1.0"


def test_loader_dispatches_schema_2() -> None:
    contract = load_domain_contract(_FIXTURE)
    assert type(contract) is DomainContractV2
    assert contract.reasoning_policy.max_hops == 2


def test_schema_2_rejects_unknown_keys() -> None:
    payload = _payload()
    payload["unknown"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        DomainContractV2.model_validate(payload)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("reasoning_policy", "relationship_type_count"), "2"),
        (("question_plans", 0, "hop_count"), "1"),
        (("question_plans", 0, "covered"), "true"),
    ],
)
def test_schema_2_rejects_scalar_type_coercion(
    path: tuple[str | int, ...],
    value: object,
) -> None:
    payload = _payload()
    _set_path(payload, path, value)
    with pytest.raises(ValidationError):
        DomainContractV2.model_validate(payload)


@pytest.mark.parametrize(
    "path",
    [
        ("domain", "name"),
        ("business", "organization_context"),
        ("business", "users", 0),
        ("problem", "statement"),
        ("terminology", "canonical_terms", 0, "definition"),
        ("constraints", "temporal", 0),
        ("examples", "positive", 0, "text"),
        ("candidate_model", "entity_types", 0, "description"),
        ("candidate_model", "relationship_types", 0, "description"),
        ("competency_questions", 0, "question"),
    ],
)
def test_schema_2_rejects_whitespace_only_required_text(
    path: tuple[str | int, ...],
) -> None:
    payload = _payload()
    _set_path(payload, path, "   ")
    with pytest.raises(ValidationError):
        DomainContractV2.model_validate(payload)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("constraints", "temporal"), "Preserve effective dates."),
        (("constraints", "safety"), None),
        (("business", "users"), "facility manager"),
    ],
)
def test_schema_2_does_not_inherit_schema_1_list_coercion(
    path: tuple[str | int, ...],
    value: object,
) -> None:
    payload = _payload()
    _set_path(payload, path, value)
    with pytest.raises(ValidationError):
        DomainContractV2.model_validate(payload)


def test_schema_2_requires_explicit_version() -> None:
    payload = _payload()
    payload.pop("schema_version")
    with pytest.raises(ValidationError, match="schema_version"):
        DomainContractV2.model_validate(payload)


def test_schema_artifact_is_version_discriminated_and_strict() -> None:
    schema = domain_contract_json_schema()
    assert schema["discriminator"]["propertyName"] == "schema_version"
    assert set(schema["discriminator"]["mapping"]) == {"1.0", "2.0"}
    assert len(schema["oneOf"]) == 2
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert "schema_version" in schema["$defs"]["DomainContractV2"]["required"]
    artifact_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "fabric_kg_builder"
        / "domain"
        / "domain.schema.json"
    )
    assert json.loads(artifact_path.read_text(encoding="utf-8")) == schema


def test_schema_2_hash_is_deterministic_and_approval_independent() -> None:
    contract = _contract()
    first = compute_contract_hash(contract)
    reordered = DomainContractV2.model_validate(
        json.loads(json.dumps(_payload(), sort_keys=True))
    )
    assert compute_contract_hash(reordered) == first
    approved_metadata = contract.approval.model_copy(
        update={"notes": ["Approval metadata must not affect semantic identity."]}
    )
    assert compute_contract_hash(
        contract.model_copy(update={"approval": approved_metadata})
    ) == first


def test_schema_1_hash_behavior_remains_approval_independent() -> None:
    contract = default_domain_contract()
    original = compute_contract_hash(contract)
    contract.approval.notes = ["existing schema-1 behavior"]
    assert compute_contract_hash(contract) == original


def test_n_below_advisory_warns_without_padding() -> None:
    contract = _contract()
    findings, _ = run_deterministic_validation(contract)
    assert len(contract.candidate_model.relationship_types) == 2
    assert any(
        item.code == "DOM-103"
        and item.severity == "warning"
        and "Do not pad" in item.message
        for item in findings
    )


def test_n_hard_max_is_24() -> None:
    payload = _payload()
    payload["reasoning_policy"]["relationship_type_count"] = 25
    with pytest.raises(ValidationError, match="DOM-103"):
        DomainContractV2.model_validate(payload)


def test_n_above_20_requires_rationale() -> None:
    payload = _payload()
    relationships = copy.deepcopy(
        payload["candidate_model"]["relationship_types"]
    )
    template = relationships[0]
    for index in range(19):
        relationship = copy.deepcopy(template)
        relationship["id"] = f"relationship-type:relation-{index}"
        relationship["predicate"] = f"relation_{index}"
        relationships.append(relationship)
    payload["candidate_model"]["relationship_types"] = relationships
    payload["reasoning_policy"]["relationship_type_count"] = 21
    with pytest.raises(ValidationError, match="require a rationale"):
        DomainContractV2.model_validate(payload)
    payload["reasoning_policy"]["relationship_type_count_rationale"] = (
        "Twenty-one distinct predicates are required by the cited governance rules."
    )
    DomainContractV2.model_validate(payload)


def test_k_must_equal_maximum_shortest_covered_path() -> None:
    payload = _payload()
    payload["reasoning_policy"]["max_hops"] = 3
    with pytest.raises(ValidationError, match="maximum shortest covered question path"):
        DomainContractV2.model_validate(payload)


def test_shortest_path_uses_question_scoped_relationship_graph() -> None:
    payload = _payload()
    payload["candidate_model"]["relationship_types"].append(
        {
            "id": "relationship-type:facility-work-order-shortcut",
            "predicate": "facility_work_order_shortcut",
            "description": "A relationship approved for a different question.",
            "source_types": ["entity-type:facility"],
            "target_types": ["entity-type:work-order"],
            "competency_question_ids": ["cq:equipment-location"],
            "source_evidence_ids": ["proposal-evidence:shortcut"],
        }
    )
    payload["reasoning_policy"]["relationship_type_count"] = 3
    contract = DomainContractV2.model_validate(payload)
    assert contract.reasoning_policy.max_hops == 2


def test_question_plan_relationship_must_support_that_question() -> None:
    payload = _payload()
    contains = payload["candidate_model"]["relationship_types"][0]
    contains["competency_question_ids"].remove("cq:facility-work-orders")
    with pytest.raises(ValidationError, match="not approved for the competency question"):
        DomainContractV2.model_validate(payload)


def test_k_4_requires_cited_rationale() -> None:
    payload = _payload()
    long_plan = payload["question_plans"][3]
    payload["candidate_model"]["entity_types"].extend([
        {
            "id": "entity-type:inspection",
            "name": "Inspection",
            "parent": None,
            "description": "A maintenance inspection.",
            "source_evidence_ids": ["proposal-evidence:inspection"],
        },
        {
            "id": "entity-type:finding",
            "name": "Finding",
            "parent": None,
            "description": "An inspection finding.",
            "source_evidence_ids": ["proposal-evidence:finding"],
        },
    ])
    payload["candidate_model"]["relationship_types"].extend([
        {
            "id": "relationship-type:requires-inspection",
            "predicate": "requires_inspection",
            "description": "A work order requires an inspection.",
            "source_types": ["entity-type:work-order"],
            "target_types": ["entity-type:inspection"],
            "competency_question_ids": ["cq:facility-work-orders"],
            "source_evidence_ids": ["proposal-evidence:requires-inspection"],
        },
        {
            "id": "relationship-type:produces-finding",
            "predicate": "produces_finding",
            "description": "An inspection produces a finding.",
            "source_types": ["entity-type:inspection"],
            "target_types": ["entity-type:finding"],
            "competency_question_ids": ["cq:facility-work-orders"],
            "source_evidence_ids": ["proposal-evidence:produces-finding"],
        },
    ])
    payload["reasoning_policy"]["relationship_type_count"] = 4
    long_plan["required_path"] = [
        long_plan["required_path"][0],
        long_plan["required_path"][1],
        {
            "from_type": "entity-type:work-order",
            "relationship_type": "relationship-type:requires-inspection",
            "to_type": "entity-type:inspection",
            "traversal": "forward",
        },
        {
            "from_type": "entity-type:inspection",
            "relationship_type": "relationship-type:produces-finding",
            "to_type": "entity-type:finding",
            "traversal": "forward",
        },
    ]
    long_plan["hop_count"] = 4
    payload["reasoning_policy"]["max_hops"] = 4
    with pytest.raises(ValidationError, match="K=4 requires"):
        DomainContractV2.model_validate(payload)
    payload["reasoning_policy"]["max_hops_rationale"] = (
        "Question cq:facility-work-orders requires the cited four-edge path."
    )
    DomainContractV2.model_validate(payload)


def test_per_hop_direction_is_validated() -> None:
    payload = _payload()
    payload["question_plans"][0]["required_path"][0]["traversal"] = "forward"
    with pytest.raises(ValidationError, match="direction mismatch"):
        DomainContractV2.model_validate(payload)


def test_question_plan_cannot_exceed_k() -> None:
    payload = _payload()
    payload["reasoning_policy"]["max_hops"] = 1
    with pytest.raises(ValidationError, match="exceeds approved K=1"):
        DomainContractV2.model_validate(payload)


def test_non_shortest_question_plan_is_rejected() -> None:
    payload = _payload()
    plan = payload["question_plans"][3]
    plan["required_path"] = [
        plan["required_path"][0],
        {
            "from_type": "entity-type:equipment",
            "relationship_type": "relationship-type:contains",
            "to_type": "entity-type:facility",
            "traversal": "reverse",
        },
        plan["required_path"][0],
        plan["required_path"][1],
    ]
    plan["hop_count"] = 4
    payload["reasoning_policy"]["max_hops"] = 4
    payload["reasoning_policy"]["max_hops_rationale"] = "A cited but invalid detour."
    with pytest.raises(ValidationError, match="shortest path of 2"):
        DomainContractV2.model_validate(payload)


def test_extracted_entity_type_requires_proposal_evidence() -> None:
    payload = _payload()
    entity = payload["candidate_model"]["entity_types"][0]
    entity["source_evidence_ids"] = []
    with pytest.raises(ValidationError, match="entity types require proposal source"):
        DomainContractV2.model_validate(payload)
    entity["business_defined"] = True
    DomainContractV2.model_validate(payload)


def test_relationship_requires_evidence_or_governance_justification() -> None:
    payload = _payload()
    relationship = payload["candidate_model"]["relationship_types"][0]
    relationship["source_evidence_ids"] = []
    with pytest.raises(ValidationError, match="require proposal source evidence"):
        DomainContractV2.model_validate(payload)
    relationship["governance_rule"] = (
        "The approved asset-governance policy requires this relationship."
    )
    DomainContractV2.model_validate(payload)


def test_publication_excluded_states_have_one_canonical_hash() -> None:
    baseline = _contract()
    payload = _payload()
    payload["publication_policy"]["excluded_states"] = [
        "rejected",
        "unresolved",
        "rejected",
    ]
    canonicalized = DomainContractV2.model_validate(payload)
    assert canonicalized.publication_policy.excluded_states == [
        "unresolved",
        "rejected",
    ]
    assert compute_contract_hash(canonicalized) == compute_contract_hash(baseline)


def test_business_critical_unsupported_question_is_deterministic_error() -> None:
    payload = _payload()
    plan = payload["question_plans"][0]
    plan.update(
        required_path=[],
        hop_count=0,
        covered=False,
        unsupported_reason="Representative sources do not support a path.",
    )
    contract = DomainContractV2.model_validate(payload)
    findings, coverage = run_deterministic_validation(contract)
    assert any(
        item.code == "DOM-104" and item.severity == "error"
        for item in findings
    )
    assert any(not item.supported for item in coverage)


def test_schema_2_foundation_does_not_activate_enrichment(tmp_path: Path) -> None:
    domain_path = tmp_path / "domain.yaml"
    domain_path.write_text(_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    status = evaluate_domain_guard_status(str(domain_path))
    assert status.ready_for_enrichment is False
    assert any("not enabled in the schema foundation layer" in item for item in status.messages)


def test_domain_validate_accepts_valid_schema_2_contract() -> None:
    result = CliRunner().invoke(cli, ["domain", "validate", "--file", str(_FIXTURE)])
    assert result.exit_code == 0, result.output
    assert "DOM-103" in result.output
    assert "passed deterministic checks" in result.output


@pytest.mark.parametrize("command", ["review", "approve"])
def test_schema_2_cannot_enter_schema_1_approval_flow(command: str) -> None:
    result = CliRunner().invoke(
        cli,
        ["domain", command, "--file", str(_FIXTURE)],
    )
    assert result.exit_code != 0
    assert "not enabled in the schema foundation layer" in result.output
