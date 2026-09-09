"""Offline public CLI checks; fixture questions are not real acceptance evidence."""

import json

from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.domain.design import generate_domain_design, save_design_artifact
from fabric_kg_builder.domain.question_routing import question_routing_context
from fabric_kg_builder.domain.service import compute_contract_hash, load_domain_contract
from tests.unit.test_domain_design import Client, _preflight, _sketch
from tests.unit.test_question_routing import _inputs, _mixed, _routing
from tests.unit.test_l1_stage import _intake


def _invoke(arguments, **kwargs):
    result = CliRunner().invoke(cli, arguments, **kwargs)
    assert result.exit_code == 0, (result.output, result.exception)
    return json.loads(result.output)


def _approved_design(tmp_path, *, routed=True):
    preflight = _inputs(tmp_path) if routed else _preflight(tmp_path)
    client = Client(_mixed() if routed else _sketch())
    draft = generate_domain_design(preflight, client=client)
    draft_path, evaluation_path = tmp_path / "draft.json", tmp_path / "evaluation.json"
    domain_path, state_root = tmp_path / "domain.yaml", tmp_path / ".fkg" / "l1"
    save_design_artifact(draft_path, draft)
    evaluated = _invoke([
        "domain", "evaluate-design", "--file", str(draft_path), "--out", str(evaluation_path),
    ])
    compiled = _invoke([
        "domain", "compile-design", "--file", str(draft_path),
        "--input", str(preflight.source_path), "--evaluation", str(evaluation_path),
        "--accept-evaluation-hash", evaluated["result"]["evaluation_hash"],
        "--out-state", str(state_root), "--out-domain", str(domain_path),
    ])
    approved = CliRunner().invoke(cli, [
        "domain", "approve", "--file", str(domain_path), "--state-dir", str(state_root),
        "--approved-by", "offline-routing-reviewer",
        "--project-id", preflight.base_identity.project_id, "--run-id", preflight.run_id,
        "--proposal-hash", compiled["result"]["proposal_hash"],
    ])
    assert approved.exit_code == 0, (approved.output, approved.exception)
    assert len(client.calls) == 1
    return preflight, draft, draft_path, domain_path, state_root


def test_design_plan_exposes_explicit_sql_context_without_calls_or_writes(tmp_path, monkeypatch):
    from fabric_kg_builder.cli import domain_design_cmd

    preflight = _inputs(tmp_path, explicit=_routing())
    intake = _intake()
    intake["competency_questions"] = [
        item.model_dump(mode="json") for item in preflight.intake.competency_questions
    ]
    intake_path, output = tmp_path / "intake.json", tmp_path / "never-written.json"
    intake_path.write_text(json.dumps(intake))
    monkeypatch.setattr(
        domain_design_cmd, "_build_client",
        lambda *args: (_ for _ in ()).throw(AssertionError("planning created a model client")),
    )
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    result = _invoke([
        "domain", "design", "--input", str(preflight.source_path),
        "--intake", str(intake_path), "--out", str(output),
    ])
    assert result["status"] == "planned"
    planned = result["result"]
    assert planned["question_routing_context"] == question_routing_context(preflight.intake)
    assert planned["unrouted_question_ids"] == [f"cq:q{i}" for i in range(1, 5)]
    assert planned["model_calls"] == planned["writes"] == 0
    assert planned["execution_verified"] is False
    assert before == {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}


def test_read_only_draft_and_approved_exports_preserve_proposed_sql_route(tmp_path, monkeypatch):
    _, draft, draft_path, domain_path, _ = _approved_design(tmp_path)
    from fabric_kg_builder.cli import domain_design_cmd

    monkeypatch.setattr(
        domain_design_cmd, "_build_client",
        lambda *args: (_ for _ in ()).throw(AssertionError("read-only export constructed a client")),
    )
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    draft_export = _invoke(["--dry-run", "domain", "question-context", "--file", str(draft_path)])
    approved_export = _invoke(["domain", "question-context", "--file", str(domain_path)])
    contract = load_domain_contract(domain_path)
    context = question_routing_context(contract)
    assert draft_export["question_routing_context"] == approved_export["question_routing_context"] == context
    assert draft_export["source_hash"] == draft.draft_hash
    assert approved_export["source_hash"] == compute_contract_hash(contract)
    assert draft_export["approval_status"] == "unapproved_design"
    assert approved_export["approval_status"] == "approved"
    question = context["questions"][0]
    assert question["question_id"] == "cq:q5" and question["business_critical"] is True
    assert question["routing"]["population"] == "Approved records"
    assert question["routing"]["physical_binding_state"] == "unresolved"
    for exported in (draft_export, approved_export):
        assert exported["execution_verified"] is False
        assert exported["unrouted_question_ids"] == [f"cq:q{i}" for i in range(1, 5)]
        assert exported["export_hash"] == canonical_sha256({
            key: value for key, value in exported.items() if key != "export_hash"
        })
    assert before == {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}


def test_legacy_draft_export_reports_unrouted_without_inventing_routes(tmp_path):
    draft = generate_domain_design(_preflight(tmp_path), client=Client(_sketch()))
    path = tmp_path / "draft.json"
    save_design_artifact(path, draft)
    exported = _invoke(["domain", "question-context", "--file", str(path)])
    assert exported["question_routing_context"] is None
    assert exported["unrouted_question_ids"] == [f"cq:q{i}" for i in range(1, 6)]
    schema = _invoke(["domain", "design-schema"])
    assert "QuestionRoutingContext" in schema["schemas"]


def test_question_context_rejects_malformed_or_unbound_input(tmp_path):
    path = tmp_path / "invalid.yaml"
    path.write_text("broken: [\n")
    malformed = CliRunner().invoke(cli, ["domain", "question-context", "--file", str(path)])
    assert malformed.exit_code == 1
    assert "Error:" in malformed.output
    missing_pair = CliRunner().invoke(cli, ["domain", "question-context", "--l4-run", str(tmp_path)])
    assert missing_pair.exit_code == 2
    assert "--l4-run and --l3-root" in missing_pair.output
