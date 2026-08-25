from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.domain.service import load_domain_contract
from tests.unit.test_l1_stage import _candidates, _intake


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
