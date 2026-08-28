from __future__ import annotations

import json
from pathlib import Path

import yaml
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.domain.service import load_domain_contract
from tests.unit.test_l1_stage import (
    _ZeroRouteRepairClient,
    _candidates,
    _intake,
)


def _write_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "records.txt").write_text(
        "A governed record describes a governed subject.",
        encoding="utf-8",
    )
    intake_path = tmp_path / "intake.json"
    intake_path.write_text(json.dumps(_intake("records")), encoding="utf-8")
    candidates_path = tmp_path / "candidates.json"
    candidates_path.write_text(
        json.dumps(_candidates("records")),
        encoding="utf-8",
    )
    return source, intake_path, candidates_path


def test_simplified_yaml_and_json_intake_share_canonical_hash(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "records.txt").write_text("governed records", encoding="utf-8")
    raw = _intake("surface")
    yaml_path = tmp_path / "simplified.yaml"
    json_path = tmp_path / "simplified.json"
    yaml_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    json_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")

    outputs: list[str] = []
    for intake_path in (yaml_path, json_path):
        result = CliRunner().invoke(
            cli,
            [
                "init-domain",
                "--input",
                str(source),
                "--intake",
                str(intake_path),
                "--dry-run",
                "--force",
                "--project-id",
                "surface-024",
                "--state-dir",
                str(tmp_path / f"state-{intake_path.suffix[1:]}"),
                "--out",
                str(tmp_path / f"domain-{intake_path.suffix[1:]}.yaml"),
            ],
        )
        assert result.exit_code == 0, result.output
        outputs.append(result.output)
    assert all("writes=0" in output for output in outputs)

    from fabric_kg_builder.domain.stage import preflight_l1_inputs

    common = {
        "source_path": source,
        "project_id": "surface-024",
        "run_id": "run:canonical-intake-test",
        "model_version": "planned-model",
        "model_hash": "a" * 64,
    }
    yaml_preflight = preflight_l1_inputs(
        intake_raw=yaml.safe_load(yaml_path.read_text(encoding="utf-8")),
        **common,
    )
    json_preflight = preflight_l1_inputs(
        intake_raw=json.loads(json_path.read_text(encoding="utf-8")),
        **common,
    )
    assert (
        yaml_preflight.intake.intake_hash
        == json_preflight.intake.intake_hash
    )
    assert yaml_preflight.intake == json_preflight.intake


def test_schema2_intake_rejects_caller_authority_fields(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "records.txt").write_text("governed records", encoding="utf-8")
    raw = {**_intake("surface"), "intake_hash": "0" * 64}
    intake_path = tmp_path / "forged.json"
    intake_path.write_text(json.dumps(raw), encoding="utf-8")
    result = CliRunner().invoke(
        cli,
        [
            "init-domain",
            "--input",
            str(source),
            "--intake",
            str(intake_path),
            "--dry-run",
            "--force",
            "--project-id",
            "surface-024",
        ],
    )
    assert result.exit_code != 0
    assert "L1_STAGE_FAILED" in result.output


def test_schema_2_dry_run_makes_no_writes_or_remote_calls(tmp_path: Path) -> None:
    source, intake_path, _ = _write_inputs(tmp_path)
    runner = CliRunner()
    domain_path = tmp_path / "domain.yaml"
    state_root = tmp_path / ".fkg" / "l1"

    result = runner.invoke(
        cli,
        [
            "init-domain",
            "--input",
            str(source),
            "--intake",
            str(intake_path),
            "--dry-run",
            "--out",
            str(domain_path),
            "--state-dir",
            str(state_root),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "writes=0" in result.output
    assert not domain_path.exists()
    assert not state_root.exists()


def test_schema2_precondition_failure_replaces_current_audit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "records.txt").write_text("records", encoding="utf-8")
    state_root = tmp_path / ".fkg" / "l1"
    state_root.mkdir(parents=True)
    audit_path = state_root / "proposal-failure-audit.json"
    audit_path.write_text('{"error_code":"STALE"}', encoding="utf-8")
    result = CliRunner().invoke(
        cli,
        [
            "init-domain",
            "--input",
            str(source),
            "--dry-run",
            "--project-id",
            "surface-024",
            "--state-dir",
            str(state_root),
        ],
    )
    assert result.exit_code != 0
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["error_code"] == "L1_STAGE_FAILED"
    assert audit["failures"] == [
        {"path": "preflight.intake", "code": "intake_required"}
    ]


def test_schema_2_noninteractive_writes_blocked_draft(tmp_path: Path) -> None:
    source, intake_path, candidates_path = _write_inputs(tmp_path)
    runner = CliRunner()
    domain_path = tmp_path / "domain.yaml"
    state_root = tmp_path / ".fkg" / "l1"

    result = runner.invoke(
        cli,
        [
            "init-domain",
            "--input",
            str(source),
            "--intake",
            str(intake_path),
            "--candidates",
            str(candidates_path),
            "--non-interactive",
            "--out",
            str(domain_path),
            "--state-dir",
            str(state_root),
        ],
    )

    assert result.exit_code == 0, result.output
    receipt = json.loads((state_root / "stage-receipt.json").read_text())
    assert receipt["status"] == "blocked"
    assert receipt["error_codes"] == ["L1_APPROVAL_REQUIRED"]
    assert load_domain_contract(domain_path).approval.status == "draft"


def test_zero_route_failure_persists_sanitized_audit(
    tmp_path: Path,
) -> None:
    source, intake_path, _ = _write_inputs(tmp_path)
    state_root = tmp_path / ".fkg" / "l1"
    client = _ZeroRouteRepairClient(keep_unsupported=True)
    result = CliRunner().invoke(
        cli,
        [
            "init-domain",
            "--input",
            str(source),
            "--intake",
            str(intake_path),
            "--non-interactive",
            "--force",
            "--project-id",
            "surface-024",
            "--state-dir",
            str(state_root),
            "--out",
            str(tmp_path / "domain.yaml"),
        ],
        obj={
            "_foundry_client": client,
            "_foundry_model_version": "gpt-4-1",
        },
    )
    assert result.exit_code != 0
    assert "L1_ZERO_SUPPORTED_ROUTES" in result.output
    audit = json.loads(
        (state_root / "proposal-failure-audit.json").read_text(
            encoding="utf-8"
        )
    )
    assert (
        audit["zero_route_audit"]["reason_code"]
        == "route_patch_zero_supported"
    )
    assert audit["zero_route_audit"]["model_call_count"] == 2
    zero = audit["zero_route_audit"]
    assert zero["initial_supported_route_count"] == 0
    assert zero["supported_route_count"] == 0
    assert zero["route_repair_attempted"] is True
    assert zero["route_repair_result_code"] == "route_patch_zero_supported"
    assert len(zero["initial_route_codes"]) == 5
    assert zero["eligible_type_count"] == 2
    assert zero["eligible_relationship_count"] == 1
    assert len(zero["eligible_type_id_hash"]) == 64
    assert len(zero["eligible_relationship_id_hash"]) == 64
    assert "A governed record describes" not in json.dumps(audit)


def test_initial_proposal_validation_persists_path_specific_audit(
    tmp_path: Path,
) -> None:
    source, intake_path, _ = _write_inputs(tmp_path)
    state_root = tmp_path / ".fkg" / "l1"

    class UnknownQuestionRouteClient:
        def complete_json(self, **kwargs):
            raw = _candidates("records")
            raw["question_routes"][0]["question_id"] = "model-invented"
            return raw

    result = CliRunner().invoke(
        cli,
        [
            "init-domain",
            "--input",
            str(source),
            "--intake",
            str(intake_path),
            "--non-interactive",
            "--force",
            "--project-id",
            "surface-024",
            "--state-dir",
            str(state_root),
            "--out",
            str(tmp_path / "domain.yaml"),
        ],
        obj={
            "_foundry_client": UnknownQuestionRouteClient(),
            "_foundry_model_version": "gpt-4-1",
        },
    )
    assert result.exit_code != 0
    audit_path = state_root / "proposal-failure-audit.json"
    assert audit_path.exists()
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["failures"] == [
        {
            "path": "question_routes",
            "code": "route_question_id_unknown",
        }
    ]
    assert all("message" not in failure for failure in audit["failures"])


def test_schema_2_explicit_approval_requires_actor_and_seals_receipt(
    tmp_path: Path,
) -> None:
    source, intake_path, candidates_path = _write_inputs(tmp_path)
    runner = CliRunner()
    domain_path = tmp_path / "domain.yaml"
    state_root = tmp_path / ".fkg" / "l1"
    draft = runner.invoke(
        cli,
        [
            "init-domain",
            "--input",
            str(source),
            "--intake",
            str(intake_path),
            "--candidates",
            str(candidates_path),
            "--non-interactive",
            "--out",
            str(domain_path),
            "--state-dir",
            str(state_root),
        ],
    )
    assert draft.exit_code == 0, draft.output

    missing_actor = runner.invoke(
        cli,
        [
            "domain",
            "approve",
            "--file",
            str(domain_path),
            "--state-dir",
            str(state_root),
        ],
    )
    assert missing_actor.exit_code != 0

    approved = runner.invoke(
        cli,
        [
            "domain",
            "approve",
            "--file",
            str(domain_path),
            "--state-dir",
            str(state_root),
            "--approved-by",
            "automation-reviewer@example.test",
        ],
    )

    assert approved.exit_code == 0, approved.output
    assert load_domain_contract(domain_path).approval.status == "approved"
    receipt = json.loads((state_root / "stage-receipt.json").read_text())
    assert receipt["status"] == "succeeded"


def test_schema_2_interactive_uses_exact_one_summary_decision(
    tmp_path: Path,
) -> None:
    source, intake_path, candidates_path = _write_inputs(tmp_path)
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            "init-domain",
            "--input",
            str(source),
            "--intake",
            str(intake_path),
            "--candidates",
            str(candidates_path),
            "--interactive",
            "--out",
            str(tmp_path / "domain.yaml"),
            "--state-dir",
            str(tmp_path / ".fkg" / "l1"),
        ],
        input="approve\n",
        env={"FABRIC_KG_APPROVER": "interactive-reviewer@example.test"},
    )

    assert result.exit_code == 0, result.output
    assert result.output.count("L1 DOMAIN DESIGN SUMMARY") == 1
    assert "Decision" in result.output


def test_schema_1_compatibility_requires_explicit_flag(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "init-domain",
            "--legacy-schema-1",
            "--out",
            str(tmp_path / "domain.yaml"),
            "--profile-out",
            str(tmp_path / "profile.json"),
            "--approve",
        ],
    )

    assert result.exit_code == 0, result.output
    assert load_domain_contract(tmp_path / "domain.yaml").schema_version == "1.0"
