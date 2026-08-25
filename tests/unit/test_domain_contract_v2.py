from __future__ import annotations

from pathlib import Path

from fabric_kg_builder.domain.guard import evaluate_domain_guard_status
from fabric_kg_builder.domain.models import DomainContractV2
from fabric_kg_builder.domain.review import run_deterministic_validation
from fabric_kg_builder.domain.service import (
    compute_contract_hash,
    load_domain_contract,
    save_domain_contract,
)
from fabric_kg_builder.domain.stage import finalize_l1_stage, prepare_l1_stage
from tests.unit.test_l1_stage import _candidates, _preflight


def _draft(tmp_path: Path) -> DomainContractV2:
    return prepare_l1_stage(
        _preflight(tmp_path, "equipment"),
        candidates=_candidates("equipment"),
    ).proposal.draft_contract


def test_loader_dispatches_final_schema_2_contract(tmp_path: Path) -> None:
    contract = _draft(tmp_path)
    path = tmp_path / "domain.yaml"
    save_domain_contract(contract, path)

    loaded = load_domain_contract(path)

    assert isinstance(loaded, DomainContractV2)
    assert loaded == contract


def test_schema_2_hash_excludes_approval_metadata(tmp_path: Path) -> None:
    prepared = prepare_l1_stage(
        _preflight(tmp_path, "contracts"),
        candidates=_candidates("contracts"),
    )
    approved = finalize_l1_stage(
        prepared,
        decision="approve",
        actor="reviewer@example.test",
        persist=False,
    ).contract

    assert approved is not None
    assert compute_contract_hash(approved) == compute_contract_hash(
        prepared.proposal.draft_contract
    )


def test_n_below_advisory_warns_without_padding(tmp_path: Path) -> None:
    contract = _draft(tmp_path)

    findings, _ = run_deterministic_validation(contract)

    assert contract.reasoning_policy.relationship_type_count == 1
    assert any(item.code == "DOM-103" and item.severity == "warning" for item in findings)


def test_schema_2_uses_c0_evidence_span_vocabulary(tmp_path: Path) -> None:
    schema = DomainContractV2.model_json_schema()
    rendered = str(schema)

    assert "evidence_span_ids" in rendered
    assert "ProposalEvidence" not in rendered
    assert "source_evidence_ids" not in rendered


def test_approved_schema_2_remains_fail_closed_for_enrichment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    prepared = prepare_l1_stage(
        _preflight(tmp_path, "records"),
        candidates=_candidates("records"),
    )
    state_root = tmp_path / ".fkg" / "l1"
    domain_path = tmp_path / "domain.yaml"
    finalize_l1_stage(
        prepared,
        decision="approve",
        actor="reviewer@example.test",
        state_root=state_root,
        domain_path=domain_path,
    )
    monkeypatch.chdir(tmp_path)

    status = evaluate_domain_guard_status(str(domain_path))

    assert status.ready_for_enrichment is False
    assert any("L2 schema-constrained extraction" in item for item in status.messages)


def test_schema_2_guard_accepts_custom_l1_state_directory(
    tmp_path: Path,
) -> None:
    prepared = prepare_l1_stage(
        _preflight(tmp_path, "custom-state"),
        candidates=_candidates("custom-state"),
    )
    state_root = tmp_path / "custom" / "l1-state"
    domain_path = tmp_path / "domain.yaml"
    finalize_l1_stage(
        prepared,
        decision="approve",
        actor="reviewer@example.test",
        state_root=state_root,
        domain_path=domain_path,
    )

    status = evaluate_domain_guard_status(
        str(domain_path),
        l1_state_root=state_root,
    )

    assert not any("missing or invalid" in item for item in status.messages)
    assert any("L2 schema-constrained extraction" in item for item in status.messages)
