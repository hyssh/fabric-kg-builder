from __future__ import annotations

import json

from pathlib import Path

import yaml
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.domain.service import load_domain_contract
from tests.unit.test_l1_stage import (
    _SchemaRegenerationClient,
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
    assert len(audit["failures"]) == 1
    failure = audit["failures"][0]
    assert failure["path"] == "preflight.intake"
    assert failure["code"] == "intake_required"
    assert "--intake is required" in failure["detail"]


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
        == "route_patch_critical_coverage_incomplete"
    )
    assert audit["zero_route_audit"]["model_call_count"] == 2
    zero = audit["zero_route_audit"]
    assert zero["initial_supported_route_count"] == 0
    assert zero["supported_route_count"] == 0
    assert zero["route_repair_attempted"] is True
    assert (
        zero["route_repair_result_code"]
        == "route_patch_critical_coverage_incomplete"
    )
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


def test_repeated_provider_validation_failure_persists_two_attempts(
    tmp_path: Path,
) -> None:
    source, intake_path, _ = _write_inputs(tmp_path)
    state_root = tmp_path / ".fkg" / "l1"
    client = _SchemaRegenerationClient(second_valid=False)
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
    audit = json.loads(
        (state_root / "proposal-failure-audit.json").read_text(
            encoding="utf-8"
        )
    )
    assert audit["attempt_count"] == 2
    assert len(audit["candidate_attempts"]) == 2
    assert all(
        failure["path"] and failure["code"] != "value_error"
        for attempt in audit["candidate_attempts"]
        for failure in attempt["failures"]
    )


def test_schema_2_explicit_approval_requires_actor_and_seals_receipt(
    tmp_path: Path,
) -> None:
    source, intake_path, candidates_path = _write_inputs(tmp_path)
    runner = CliRunner()
    domain_path = tmp_path / "domain.yaml"
    state_root = tmp_path / ".fkg" / "l1"
    state_root.mkdir(parents=True)
    (state_root / "domain-approval-context.json").write_text(
        '{"stale":true}',
        encoding="utf-8",
    )
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
    assert not (state_root / "domain-approval-context.json").exists()
    source_unit_dir = state_root / "design-samples" / "source-units"
    source_unit_dir.mkdir(parents=True, exist_ok=True)
    stale_unit = source_unit_dir / "stale.json"
    stale_unit.write_text('{"stale":true}', encoding="utf-8")
    regenerated = runner.invoke(
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
            "--force",
            "--out",
            str(domain_path),
            "--state-dir",
            str(state_root),
        ],
    )
    assert regenerated.exit_code == 0, regenerated.output
    assert not stale_unit.exists()
    assert "[init-domain] approval-anchors project_id=" in draft.output
    assert " run_id=" in draft.output
    assert " proposal_hash=" in draft.output
    identity = json.loads(
        (state_root / "domain-design-context.json").read_text(
            encoding="utf-8"
        )
    )["identity"]
    proposal_hash = json.loads(
        (state_root / "domain-proposal.json").read_text(encoding="utf-8")
    )["proposal_hash"]

    mismatched_path = tmp_path / "different-domain.yaml"
    mismatched = yaml.safe_load(domain_path.read_text(encoding="utf-8"))
    mismatched["domain"]["description"] = "A different reviewed contract."
    mismatched_path.write_text(
        yaml.safe_dump(mismatched, sort_keys=False),
        encoding="utf-8",
    )
    mismatch = runner.invoke(
        cli,
        [
            "domain",
            "approve",
            "--file",
            str(mismatched_path),
            "--state-dir",
            str(state_root),
            "--approved-by",
            "automation-reviewer@example.test",
            "--project-id",
            identity["project_id"],
            "--run-id",
            identity["run_id"],
            "--proposal-hash",
            proposal_hash,
        ],
    )
    assert mismatch.exit_code != 0
    assert "does not match the persisted L1 draft" in mismatch.output

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
            "--project-id",
            identity["project_id"],
            "--run-id",
            identity["run_id"],
            "--proposal-hash",
            proposal_hash,
        ],
    )

    assert approved.exit_code == 0, approved.output
    assert load_domain_contract(domain_path).approval.status == "approved"
    receipt = json.loads((state_root / "stage-receipt.json").read_text())
    assert receipt["status"] == "succeeded"
    replay = runner.invoke(
        cli,
        [
            "domain",
            "approve",
            "--file",
            str(domain_path),
            "--state-dir",
            str(state_root),
            "--approved-by",
            "different-reviewer@example.test",
            "--project-id",
            identity["project_id"],
            "--run-id",
            identity["run_id"],
            "--proposal-hash",
            proposal_hash,
        ],
    )
    assert replay.exit_code != 0


def test_schema_2_approval_rejects_cross_run_proposal_replay(
    tmp_path: Path,
) -> None:
    source, intake_path, candidates_path = _write_inputs(tmp_path)
    runner = CliRunner()
    states = [tmp_path / "state-a", tmp_path / "state-b"]
    domains = [tmp_path / "domain-a.yaml", tmp_path / "domain-b.yaml"]
    for index, state_root in enumerate(states):
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
                "--project-id",
                f"surface-{index}",
                "--out",
                str(domains[index]),
                "--state-dir",
                str(state_root),
            ],
        )
        assert result.exit_code == 0, result.output
    expected_identity = json.loads(
        (states[0] / "domain-design-context.json").read_text(
            encoding="utf-8"
        )
    )["identity"]
    expected_proposal_hash = json.loads(
        (states[0] / "domain-proposal.json").read_text(encoding="utf-8")
    )["proposal_hash"]
    (states[0] / "domain-proposal.json").write_bytes(
        (states[1] / "domain-proposal.json").read_bytes()
    )

    approval = runner.invoke(
        cli,
        [
            "domain",
            "approve",
            "--file",
            str(domains[0]),
            "--state-dir",
            str(states[0]),
            "--approved-by",
            "automation-reviewer@example.test",
            "--project-id",
            expected_identity["project_id"],
            "--run-id",
            expected_identity["run_id"],
            "--proposal-hash",
            expected_proposal_hash,
        ],
    )

    assert approval.exit_code != 0
    assert "cross-artifact binding mismatch" in approval.output


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


def test_sanitized_failure_detail_preserves_cause_without_secrets() -> None:
    """Operators need the real cause; audits must never carry credentials."""
    from fabric_kg_builder.cli.init_domain_cmd import (
        _sanitize_failure_detail,
    )

    detail = _sanitize_failure_detail(
        EnvironmentError(
            "AZURE_OPENAI_ENDPOINT is not set; api_key=sk-secret-value "
            "Authorization: Bearer eyJhbGciOi.payload.signature"
        )
    )

    assert "AZURE_OPENAI_ENDPOINT is not set" in detail
    assert detail.startswith("OSError:")
    assert "sk-secret-value" not in detail
    assert "eyJhbGciOi.payload.signature" not in detail
    assert "[redacted]" in detail


def test_sanitized_failure_detail_is_bounded() -> None:
    """A pathological provider message cannot flood the audit record."""
    from fabric_kg_builder.cli.init_domain_cmd import (
        _sanitize_failure_detail,
    )

    detail = _sanitize_failure_detail(RuntimeError("x" * 5_000))

    assert len(detail) <= 520


def test_early_failure_audit_records_detail(tmp_path) -> None:
    """The persisted audit must carry the sanitized cause, not just a code."""
    import json as _json

    from fabric_kg_builder.cli.init_domain_cmd import (
        _persist_early_l1_failure_audit,
    )

    audit_path = _persist_early_l1_failure_audit(
        state_root=tmp_path / ".fkg" / "l1",
        project_id="project:test",
        run_id="run:test",
        path="proposal.provider",
        code="client_construction_failed",
        detail="EnvironmentError: AZURE_OPENAI_ENDPOINT is not set",
    )

    payload = _json.loads(audit_path.read_text(encoding="utf-8"))
    failure = payload["failures"][0]
    assert failure["code"] == "client_construction_failed"
    assert failure["detail"] == (
        "EnvironmentError: AZURE_OPENAI_ENDPOINT is not set"
    )


def test_schema2_preconditions_surface_actionable_reasons(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "records.txt").write_text("records", encoding="utf-8")
    runner = CliRunner()

    cases = [
        (["init-domain"], "input_required", "--input"),
        (
            [
                "init-domain",
                "--input",
                str(source),
                "--interactive",
                "--non-interactive",
            ],
            "mode_conflict",
            "mutually exclusive",
        ),
        (
            ["init-domain", "--input", str(source), "--approve"],
            "approve_not_supported",
            "schema-1-only",
        ),
        (
            ["init-domain", "--input", str(tmp_path / "missing")],
            "source_not_found",
            "does not exist",
        ),
    ]

    for index, (args, code, needle) in enumerate(cases):
        state_root = tmp_path / f"state-{index}"
        result = runner.invoke(cli, [*args, "--state-dir", str(state_root)])
        assert result.exit_code != 0, result.output
        assert needle in result.output, result.output
        audit = json.loads(
            (state_root / "proposal-failure-audit.json").read_text(
                encoding="utf-8"
            )
        )
        failure = audit["failures"][0]
        assert failure["code"] == code
        assert needle in failure["detail"]


def test_sanitize_failure_detail_redacts_quoted_and_connection_secrets() -> None:
    from fabric_kg_builder.cli.init_domain_cmd import _sanitize_failure_detail

    leaky = [
        'api_key: "sk-live-ABC123"',
        "'password': 'hunter2'",
        "Endpoint=sb://x;SharedAccessKey=abc123def=;",
        "DefaultEndpointsProtocol=https;AccountKey=Zm9vYmFy==;",
        "https://user:s3cr3t@host/db",
        "token eyJhbGciOiJIUzI1NiJ9.eyJvaWQiOiJ4In0.sig1234",
    ]
    for message in leaky:
        detail = _sanitize_failure_detail(ValueError(message))
        assert "[redacted]" in detail
        for secret in (
            "sk-live-ABC123",
            "hunter2",
            "abc123def",
            "Zm9vYmFy",
            "s3cr3t",
            "eyJvaWQiOiJ4In0",
        ):
            assert secret not in detail

    # Actionable, non-credential causes must survive verbatim.
    kept = _sanitize_failure_detail(
        ValueError("missing environment variable AZURE_OPENAI_ENDPOINT")
    )
    assert kept.endswith("AZURE_OPENAI_ENDPOINT")
