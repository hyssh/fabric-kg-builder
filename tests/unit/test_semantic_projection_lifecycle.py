from __future__ import annotations

import copy
import json
import os
import random
import subprocess
import sys
from pathlib import Path

import yaml

from fabric_kg_builder.domain.models import ApprovalMetadataV2, DomainContractV2
from fabric_kg_builder.domain.service import compute_contract_hash
from fabric_kg_builder.serving.semantic_projection import (
    SemanticProjectionResult,
    build_semantic_projection,
)


_CONTRACT_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "domains"
    / "facility-maintenance-v2.yaml"
)


def _contract() -> DomainContractV2:
    contract = DomainContractV2.model_validate(
        yaml.safe_load(_CONTRACT_FIXTURE.read_text(encoding="utf-8"))
    )
    contract_hash = compute_contract_hash(contract)
    approval = ApprovalMetadataV2(
        status="approved",
        approved_by="owner@example.com",
        approved_at_utc="2026-08-24T17:00:00Z",
        contract_hash=contract_hash,
        proposal_hash="proposal:test",
        source_profile_hash="profile:test",
        prompt_hash="prompt:test",
        prompt_version="2",
        model_version="test-model",
        model_hash="model:test",
    )
    return contract.model_copy(update={"approval": approval})


def _evidence(evidence_id: str = "evidence:1") -> dict:
    return {
        "evidence_id": evidence_id,
        "source_file_id": "source:1",
        "source_type": "document_span",
        "text": "Building A contains AHU-4.",
        "runner_verified": True,
    }


def _entity(
    entity_id: str,
    type_id: str,
    name: str,
    *,
    published: bool = True,
    evidence_ids: list[str] | None = None,
) -> dict:
    contract = _contract()
    definition = next(
        item
        for item in contract.candidate_model.entity_types
        if item.id == type_id
    )
    return {
        "entity_id": entity_id,
        "entity_type": definition.name,
        "display_name": name,
        "canonical_key": f"{definition.name.casefold()}:{name.casefold()}",
        "aliases": [name, name],
        "evidence_ids": evidence_ids if evidence_ids is not None else ["evidence:1"],
        "assertion_state": "asserted" if published else "unresolved",
        "semantic_lane": "authoritative" if published else "discovery",
        "semantic_type_id": type_id,
        "review_status": "approved" if published else "needs_review",
        "semantic_contract_hash": contract.approval.contract_hash,
        "properties_json": {
            "semantic_contract_hash": contract.approval.contract_hash,
            "semantic_lane": "authoritative" if published else "discovery",
            "semantic_type_id": type_id if published else None,
            "review_status": "approved" if published else "needs_review",
        },
    }


def _relationship(
    relationship_id: str,
    *,
    state: str = "asserted",
    source_id: str = "entity:facility",
    target_id: str = "entity:equipment",
    evidence_id: str | None = "evidence:1",
    lane: str = "authoritative",
    reasons: list[str] | None = None,
) -> dict:
    contract = _contract()
    row = {
        "relationship_id": relationship_id,
        "relationship_type": "contains",
        "source_entity_id": source_id,
        "target_entity_id": target_id,
        "semantic_relationship_id": "relationship-type:contains",
        "assertion_state": state,
        "processing_status": (
            "accepted" if state == "asserted" else state
        ),
        "semantic_lane": lane,
        "semantic_contract_hash": contract.approval.contract_hash,
        "reason_codes": reasons or [],
        "resolved_source_type_id": "entity-type:facility",
        "resolved_target_type_id": "entity-type:equipment",
        "source_inheritance_path": ["entity-type:facility"],
        "target_inheritance_path": ["entity-type:equipment"],
        "validation_authority": "schema2",
        "direction": "forward",
        "review_status": "approved" if state == "asserted" else "needs_review",
        "properties_json": {
            "assertion_status": state,
            "semantic_lane": lane,
            "semantic_relationship_id": "relationship-type:contains",
            "semantic_contract_hash": contract.approval.contract_hash,
            "validation_authority": "schema2",
            "direction": "forward",
        },
    }
    if evidence_id is not None:
        row["evidence_id"] = evidence_id
        row["evidence_ids"] = [evidence_id]
    return row


def _entities() -> list[dict]:
    return [
        _entity(
            "entity:facility",
            "entity-type:facility",
            "Building A",
        ),
        _entity(
            "entity:equipment",
            "entity-type:equipment",
            "AHU-4",
        ),
    ]


def _project(relationships: list[dict]) -> SemanticProjectionResult:
    result = build_semantic_projection(
        _entities(),
        relationships,
        [_evidence()],
        schema2_contract=_contract(),
    )
    assert isinstance(result, SemanticProjectionResult)
    return result


def test_mixed_lifecycle_reconciles_every_occurrence_without_loss() -> None:
    rows = [
        _relationship("rel:asserted"),
        _relationship("rel:unresolved", state="unresolved", evidence_id=None),
        _relationship("rel:rejected", state="rejected", reasons=["BAD_QUOTE"]),
        _relationship(
            "rel:discovery",
            state="unresolved",
            lane="discovery",
            evidence_id=None,
        ),
        _relationship(
            "rel:endpoint",
            state="unresolved",
            source_id="unresolved-endpoint:1",
            evidence_id=None,
        ),
    ]

    result = _project(rows)

    assert result.receipt["status"] == "succeeded"
    assert result.receipt["terminal_counts"] == {
        "asserted": 1,
        "unresolved": 1,
        "rejected": 1,
        "discovery": 1,
        "deduplicated": 0,
        "endpoint_unresolved": 1,
        "endpoint_unpublished": 0,
    }
    assert len(result.receipt["reconciliation_records"]) == len(rows)
    assert sum(result.receipt["terminal_counts"].values()) == len(rows)
    assert len(result.semantic_relationships) == 1
    assert len(result.claims) == len(result.claim_evidence) == 1


def test_legacy_unverified_normalizes_to_unresolved_and_never_serves() -> None:
    result = _project([_relationship("rel:legacy", state="unverified")])

    audit = result.audit_relationships[0]
    assert audit["assertion_state"] == "unresolved"
    assert audit["terminal_bucket"] == "unresolved"
    assert "LEGACY_UNVERIFIED_STATE" in audit["reason_codes"]
    assert result.semantic_relationships == []
    assert result.receipt["reason_counts"]["LEGACY_UNVERIFIED_STATE"] == 1


def test_dedup_selects_asserted_winner_and_accounts_per_occurrence() -> None:
    unresolved = _relationship(
        "rel:duplicate",
        state="unresolved",
        evidence_id=None,
        reasons=["NO_EVIDENCE"],
    )
    asserted = _relationship("rel:duplicate")
    rejected = _relationship(
        "rel:duplicate",
        state="rejected",
        evidence_id=None,
        reasons=["BAD_QUOTE"],
    )

    result = _project([rejected, asserted, unresolved])

    assert result.audit_relationships[0]["assertion_state"] == "asserted"
    assert result.audit_relationships[0]["deduplicated_occurrence_count"] == 2
    assert result.audit_relationships[0]["reason_codes"] == [
        "BAD_QUOTE",
        "NO_EVIDENCE",
    ]
    assert result.receipt["terminal_counts"]["asserted"] == 1
    assert result.receipt["terminal_counts"]["deduplicated"] == 2
    assert result.receipt["dedup_counts"]["deduplicated_occurrences"] == 2


def test_invalid_asserted_dedup_loser_fails_with_occurrence_reasons() -> None:
    valid = _relationship("rel:duplicate")
    invalid_loser = _relationship("rel:duplicate", evidence_id=None)

    result = _project([invalid_loser, valid])

    loser = next(
        record
        for record in result.receipt["reconciliation_records"]
        if record["bucket"] == "deduplicated"
    )
    assert "EVIDENCE_MISSING" in loser["reason_codes"]
    assert result.audit_relationships[0]["assertion_state"] == "asserted"
    assert "EVIDENCE_MISSING" in result.audit_relationships[0]["reason_codes"]
    assert result.receipt["status"] == "failed"
    assert (
        f"{loser['occurrence_key']}:EVIDENCE_MISSING"
        in result.receipt["invariant_results"]["SEM-102"]["violations"]
    )
    assert result.semantic_entities == []
    assert result.semantic_relationships == []
    assert result.claims == []
    assert result.claim_evidence == []


def test_asserted_relationship_with_unpublished_endpoint_is_not_served() -> None:
    entities = _entities()
    entities[1] = _entity(
        "entity:equipment",
        "entity-type:equipment",
        "AHU-4",
        published=False,
    )
    result = build_semantic_projection(
        entities,
        [_relationship("rel:unpublished")],
        [_evidence()],
        schema2_contract=_contract(),
    )
    assert isinstance(result, SemanticProjectionResult)

    assert result.receipt["status"] == "failed"
    assert result.receipt["terminal_counts"]["endpoint_unpublished"] == 1
    assert result.audit_relationships[0]["assertion_state"] == "asserted"
    assert result.semantic_relationships == []
    assert result.semantic_entities == []
    assert result.claims == []
    assert result.claim_evidence == []
    assert result.receipt["candidate_serving_counts"]["semantic_entities"] == 1
    assert result.receipt["invariant_results"]["SEM-103"]["passed"] is False
    assert result.receipt["invariants"] == [
        {
            "gate": gate,
            "passed": result.receipt["invariant_results"][gate]["passed"],
            "details": result.receipt["invariant_results"][gate]["violations"],
        }
        for gate in ("SEM-100", "SEM-101", "SEM-102", "SEM-103", "SEM-104")
    ]


def test_unresolved_raw_entity_is_excluded_without_failing_valid_projection() -> None:
    entities = _entities()
    unresolved = _entity(
        "entity:unresolved",
        "entity-type:equipment",
        "Unresolved equipment",
        published=False,
    )
    unresolved["semantic_lane"] = "authoritative"
    unresolved["properties_json"] = None
    entities.append(unresolved)
    result = build_semantic_projection(
        entities,
        [_relationship("rel:valid")],
        [_evidence()],
        schema2_contract=_contract(),
    )
    assert isinstance(result, SemanticProjectionResult)
    assert result.receipt["status"] == "succeeded"
    assert {row["entity_id"] for row in result.semantic_entities} == {
        "entity:facility",
        "entity:equipment",
    }
    assert len(result.claims) == len(result.claim_evidence) == 1


def test_hard_invariant_failure_returns_empty_atomic_serving_output() -> None:
    result = _project([
        _relationship("rel:invalid-asserted", evidence_id=None)
    ])

    assert result.receipt["status"] == "failed"
    assert result.audit_relationships[0]["assertion_state"] == "asserted"
    assert result.receipt["terminal_counts"]["asserted"] == 1
    assert result.receipt["candidate_serving_counts"]["semantic_entities"] == 2
    assert result.semantic_entities == []
    assert result.semantic_relationships == []
    assert result.claims == []
    assert result.claim_evidence == []
    assert result.receipt["invariant_results"]["SEM-102"]["passed"] is False


def test_projection_and_hashes_are_deterministic_across_input_shuffle() -> None:
    relationships = [
        _relationship("rel:a"),
        _relationship("rel:b", state="unresolved", evidence_id=None),
        _relationship("rel:b", state="rejected", evidence_id=None),
    ]
    entities = _entities()
    evidence = [_evidence("evidence:unused"), _evidence()]
    baseline = build_semantic_projection(
        entities,
        relationships,
        evidence,
        schema2_contract=_contract(),
    )
    assert isinstance(baseline, SemanticProjectionResult)

    shuffled_entities = copy.deepcopy(entities)
    shuffled_relationships = copy.deepcopy(relationships)
    shuffled_evidence = copy.deepcopy(evidence)
    random.Random(42).shuffle(shuffled_entities)
    random.Random(43).shuffle(shuffled_relationships)
    random.Random(44).shuffle(shuffled_evidence)
    shuffled = build_semantic_projection(
        shuffled_entities,
        shuffled_relationships,
        shuffled_evidence,
        schema2_contract=_contract(),
    )
    assert isinstance(shuffled, SemanticProjectionResult)

    assert baseline.receipt == shuffled.receipt
    assert baseline.as_dict() == shuffled.as_dict()


def test_conflicting_entity_authority_is_order_independent_and_fails() -> None:
    entities = _entities()
    conflicting = copy.deepcopy(entities[0])
    conflicting["semantic_contract_hash"] = "stale-contract"
    conflicting["properties_json"]["semantic_contract_hash"] = "stale-contract"
    entities.append(conflicting)

    baseline = build_semantic_projection(
        entities,
        [_relationship("rel:valid")],
        [_evidence()],
        schema2_contract=_contract(),
    )
    shuffled = build_semantic_projection(
        list(reversed(copy.deepcopy(entities))),
        [_relationship("rel:valid")],
        [_evidence()],
        schema2_contract=_contract(),
    )
    assert isinstance(baseline, SemanticProjectionResult)
    assert isinstance(shuffled, SemanticProjectionResult)

    assert baseline.receipt == shuffled.receipt
    assert baseline.as_dict() == shuffled.as_dict()
    assert baseline.receipt["status"] == "failed"
    assert baseline.receipt["entity_reconciliation_counts"] == {
        "input_occurrences": 3,
        "entity_groups": 2,
        "selected_occurrences": 2,
        "deduplicated_occurrences": 1,
        "authority_conflicts": 1,
    }
    records = baseline.receipt["entity_reconciliation_records"]
    assert len(records) == 3
    stale = next(
        record
        for record in records
        if "STALE_CONTRACT_HASH" in record["reason_codes"]
    )
    assert stale["selected"] is False
    assert baseline.receipt["invariant_results"]["SEM-100"]["passed"] is False
    assert baseline.semantic_entities == []
    assert baseline.semantic_relationships == []
    assert baseline.claims == []
    assert baseline.claim_evidence == []


def test_projection_receipt_is_stable_across_python_hash_seeds() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    script = """
import copy
import json
from tests.unit.test_semantic_projection_lifecycle import (
    _contract, _entities, _evidence, _relationship,
)
from fabric_kg_builder.serving.semantic_projection import build_semantic_projection
entities = _entities()
conflicting = copy.deepcopy(entities[0])
conflicting["semantic_contract_hash"] = "stale-contract"
conflicting["properties_json"]["semantic_contract_hash"] = "stale-contract"
entities.append(conflicting)
result = build_semantic_projection(
    entities,
    [
        _relationship("rel:a"),
        _relationship("rel:b", state="unresolved", evidence_id=None),
        _relationship("rel:b", state="rejected", evidence_id=None),
    ],
    [_evidence()],
    schema2_contract=_contract(),
)
print(json.dumps(result.receipt, sort_keys=True, separators=(",", ":")))
"""
    outputs = []
    for seed in ("1", "777"):
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = seed
        env["PYTHONPATH"] = os.pathsep.join(
            [str(repo_root / "src"), str(repo_root)]
        )
        outputs.append(
            subprocess.check_output(
                [sys.executable, "-c", script],
                cwd=repo_root,
                env=env,
                text=True,
            ).strip()
        )
    assert outputs[0] == outputs[1]
    assert json.loads(outputs[0])["status"] == "failed"


def test_schema1_call_keeps_original_dict_shape_and_behavior() -> None:
    entities = [{
        "entity_id": "legacy:1",
        "entity_type": "facility",
        "display_name": "Legacy Building",
        "canonical_key": "facility:legacy-building",
    }]

    result = build_semantic_projection(entities, [], [])

    assert type(result) is dict
    assert set(result) == {
        "semantic_entities",
        "semantic_relationships",
        "claims",
        "claim_evidence",
    }
    assert result["semantic_entities"][0]["entity_type"] == "Facility"
