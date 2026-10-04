from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.contracts.base import canonical_json
from fabric_kg_builder.domain.assessment import (
    AssessmentReport,
    Decision,
    assess_documents,
    review_assessment,
)
from fabric_kg_builder.domain.service import save_domain_contract
from tests.unit.test_schema2_extraction import _domain


class Client:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or {"findings": []}

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def source(tmp_path):
    folder = tmp_path / "source"
    folder.mkdir()
    (folder / "first.html").write_text(
        "<p>Record A requires approval before publication.</p>", encoding="utf-8"
    )
    (folder / "second.html").write_text(
        "<p>Except for archived records, a review is mandatory.</p>", encoding="utf-8"
    )
    return folder


def test_assessment_visits_multiple_files_and_preserves_explicit_budget(tmp_path):
    folder = source(tmp_path)
    client = Client()
    report = assess_documents(folder, _domain(), client=client, max_calls=1)
    assert len(report.files) == 2
    assert len(report.windows) == 2
    assert len(client.calls) == 1
    assert report.model_calls == 1
    assert report.coverage == "partial"
    assert [w.status for w in report.windows].count("deferred") == 1
    assert "findings" in client.calls[0]["json_schema"]["properties"]
    assert client.calls[0]["max_attempts"] == 1


def test_assessment_dry_run_makes_no_calls_or_writes(tmp_path):
    folder = source(tmp_path)
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    client = Client()
    report = assess_documents(folder, _domain(), client=client, dry_run=True)
    assert not client.calls
    assert report.coverage == "partial"
    assert before == sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))


def test_finding_offsets_are_minted_from_exact_quote(tmp_path):
    folder = source(tmp_path)
    plan = assess_documents(folder, _domain(), dry_run=True)
    target = next(w for w in plan.windows if "archived" in w.text)
    response = {
        "findings": [{
            "category": "ontology_gap", "summary": "Preserve the exception.",
            "suggested_action": "refine_scope", "quote": "Except for archived records",
            "question_ids": [], "semantic_ids": [],
        }]
    }
    report = assess_documents(
        folder, _domain(), responses={
            w.window_id: response if w.window_id == target.window_id else {"findings": []}
            for w in plan.windows
        },
    )
    assert report.coverage == "complete"
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert target.text[
        finding.span_start-target.span_start:finding.span_end-target.span_start
    ] == finding.quote
    assert report.model_calls == 0
    assert AssessmentReport.model_validate_json(
        canonical_json(report.model_dump(mode="json"))
    ) == report


@pytest.mark.parametrize("change", [
    {"quote": "fabricated quotation"},
    {"question_ids": ["cq:invented"]},
    {"semantic_ids": ["semantic-type:invented"]},
])
def test_untrusted_model_findings_are_rejected(tmp_path, change):
    folder = source(tmp_path)
    plan = assess_documents(folder, _domain(), dry_run=True)
    target = plan.windows[0]
    finding = {
        "category": "extraction_risk", "summary": "Needs review",
        "suggested_action": "review_source", "quote": target.text,
        "question_ids": [], "semantic_ids": [],
        **change,
    }
    with pytest.raises(ValueError):
        assess_documents(
            folder, _domain(), responses={target.window_id: {"findings": [finding]}},
        )


def test_checkpoint_reuses_only_matching_inputs(tmp_path):
    folder = source(tmp_path)
    client = Client()
    first = assess_documents(folder, _domain(), client=client, max_calls=1)
    resumed = assess_documents(
        folder, _domain(), client=client, max_calls=1, checkpoint=first
    )
    assert resumed.coverage == "complete"
    assert resumed.model_calls == 1
    (folder / "first.html").write_text("<p>Changed content</p>", encoding="utf-8")
    with pytest.raises(ValueError, match="different assessment inputs"):
        assess_documents(folder, _domain(), client=client, checkpoint=resumed)


def test_review_requires_all_decisions_and_never_modifies_domain(tmp_path):
    folder = source(tmp_path)
    contract = _domain()
    before = contract.model_dump(mode="json")
    plan = assess_documents(folder, contract, dry_run=True)
    report = assess_documents(folder, contract, responses={
        w.window_id: {"findings": [{
            "category": "ontology_gap", "summary": "Review applicability.",
            "suggested_action": "refine_scope", "quote": w.text,
            "question_ids": [], "semantic_ids": [],
        }]} for w in plan.windows
    })
    with pytest.raises(ValueError, match="every finding"):
        review_assessment(report, actor="reviewer", decisions=())
    decisions = tuple(
        Decision(finding_id=f.finding_id, disposition="accepted", rationale="Supported.")
        for f in report.findings
    )
    review, request = review_assessment(report, actor="reviewer", decisions=decisions)
    assert request.accepted_findings
    assert request.automatic_schema_mutation is False
    assert request.review_hash == review.review_hash
    assert before == contract.model_dump(mode="json")


def test_cli_offline_assessment_and_review(tmp_path):
    folder = source(tmp_path)
    domain = tmp_path / "domain.yaml"
    save_domain_contract(_domain(), domain)
    before = domain.read_bytes()
    runner = CliRunner()
    out = tmp_path / "assessment.json"
    args = ["domain", "assess", "--file", str(domain), "--input", str(folder), "--out", str(out)]
    planned = runner.invoke(cli, args)
    assert planned.exit_code == 0, planned.output
    assert not out.exists()
    windows = json.loads(planned.output)["planned_windows"]
    responses = tmp_path / "responses.json"
    responses.write_text(json.dumps({
        w["window_id"]: {"findings": []} for w in windows
    }), encoding="utf-8")
    result = runner.invoke(cli, [*args, "--responses", str(responses)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "complete"
    decisions = tmp_path / "decisions.json"
    decisions.write_text("[]", encoding="utf-8")
    reviewed = runner.invoke(cli, [
        "domain", "review-assessment", "--file", str(domain),
        "--assessment", str(out), "--decisions", str(decisions),
        "--actor", "reviewer", "--out", str(tmp_path / "review.json"),
        "--revision-out", str(tmp_path / "revision.json"),
    ])
    assert reviewed.exit_code == 0, reviewed.output
    assert domain.read_bytes() == before
    assert not json.loads(reviewed.output)["approved"]
    refused = runner.invoke(cli, [*args, "--responses", str(responses)])
    assert refused.exit_code != 0


def test_unsupported_files_and_prompt_budget_are_visible(tmp_path):
    folder = source(tmp_path)
    (folder / "unknown.bin").write_bytes(b"\x00\x01")
    client = Client()
    report = assess_documents(
        folder, _domain(), client=client, max_prompt_chars=256,
    )
    assert report.coverage == "partial"
    assert any(f.status == "unsupported" for f in report.files)
    assert all(w.reason == "prompt_budget" for w in report.windows)
    assert not client.calls


def test_global_dry_run_cannot_enable_live_calls(tmp_path):
    folder = source(tmp_path)
    domain = tmp_path / "domain.yaml"
    save_domain_contract(_domain(), domain)
    result = CliRunner().invoke(cli, [
        "--dry-run", "domain", "assess", "--file", str(domain),
        "--input", str(folder), "--live",
    ])
    assert result.exit_code != 0
    assert "cannot be combined" in result.output


def test_response_cache_replays_without_spending_calls_and_rejects_tampering(tmp_path):
    folder = source(tmp_path)
    cache = tmp_path / "responses"
    client = Client()
    first = assess_documents(
        folder, _domain(), client=client, response_cache=cache,
    )
    assert first.model_calls == 2
    replay = assess_documents(
        folder, _domain(), client=client, response_cache=cache, max_calls=0,
    )
    assert replay.coverage == "complete"
    assert replay.model_calls == 0
    assert len(client.calls) == 2
    cached = next(cache.glob("*.json"))
    payload = json.loads(cached.read_text())
    payload["response"] = {"findings": [{"tampered": True}]}
    cached.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="cached response hash"):
        assess_documents(folder, _domain(), client=client, response_cache=cache)


def test_cli_revision_creates_separate_unapproved_l1_and_preserves_parent(tmp_path):
    from fabric_kg_builder.contracts.base import canonical_sha256
    from fabric_kg_builder.domain.assessment import save_new_artifact
    from fabric_kg_builder.domain.stage import (
        finalize_l1_stage, preflight_l1_inputs, prepare_l1_stage,
    )
    from fabric_kg_builder.domain.service import compute_contract_hash, load_domain_contract
    from tests.unit.test_l1_stage import _candidates, _intake

    folder = source(tmp_path)
    candidates = _candidates("revision")
    preflight = preflight_l1_inputs(
        source_path=folder, intake_raw=_intake("revision"),
        project_id="project:revision", run_id="run:parent",
        model_version="fixture", model_hash=canonical_sha256("fixture"),
    )
    parent_state = tmp_path / "parent"
    parent_domain = parent_state / "domain.yaml"
    prepared = prepare_l1_stage(preflight, candidates=candidates)
    finalize_l1_stage(
        prepared, decision=None, actor=None,
        state_root=parent_state, domain_path=parent_domain,
    )
    contract = load_domain_contract(parent_domain)
    plan = assess_documents(folder, contract, dry_run=True)
    report = assess_documents(folder, contract, responses={
        w.window_id: {"findings": [{
            "category": "ontology_gap", "summary": "Preserve archival applicability.",
            "suggested_action": "refine_scope", "quote": w.text,
            "question_ids": [], "semantic_ids": [],
        }]} for w in plan.windows
    })
    review, request = review_assessment(
        report, actor="reviewer", decisions=tuple(
            Decision(finding_id=f.finding_id, disposition="accepted", rationale="Evidence supports review.")
            for f in report.findings
        ),
    )
    report_path = tmp_path / "assessment.json"
    review_path = tmp_path / "review.json"
    request_path = tmp_path / "request.json"
    for path, record in [(report_path, report), (review_path, review), (request_path, request)]:
        save_new_artifact(path, record)
    candidates["semantic_type_candidates"][0]["proposed_type"]["description"] = (
        "A governed record with explicitly reviewed archival applicability."
    )
    fixture = tmp_path / "candidates.json"
    fixture.write_text(json.dumps(candidates), encoding="utf-8")
    prior = {str(p.relative_to(parent_state)): p.read_bytes() for p in parent_state.rglob("*") if p.is_file()}
    out_state = tmp_path / "child"
    args = [
        "domain", "revise", "--parent-state", str(parent_state),
        "--input", str(folder), "--assessment", str(report_path),
        "--review", str(review_path), "--request", str(request_path),
        "--out-state", str(out_state),
    ]
    runner = CliRunner()
    planned = runner.invoke(cli, args)
    assert planned.exit_code == 0, planned.output
    assert not out_state.exists()
    revised = runner.invoke(cli, [*args, "--candidates", str(fixture)])
    assert revised.exit_code == 0, revised.output
    result = json.loads(revised.output)
    assert result["status"] == "blocked"
    assert result["approved"] is False
    child = load_domain_contract(out_state / "domain.yaml")
    assert child.approval.status == "draft"
    assert compute_contract_hash(child) != compute_contract_hash(contract)
    assert prior == {str(p.relative_to(parent_state)): p.read_bytes() for p in parent_state.rglob("*") if p.is_file()}
    again = runner.invoke(cli, [*args, "--candidates", str(fixture)])
    assert again.exit_code != 0


def test_cli_denied_inference_is_actionable_and_not_retried(tmp_path):
    import httpx
    from openai import AuthenticationError

    class Denied:
        calls = 0

        def execution_identity(self):
            return {"model": "denied-fixture"}

        def complete_json(self, **kwargs):
            self.calls += 1
            raise AuthenticationError(
                "not authorized",
                response=httpx.Response(
                    401, request=httpx.Request("POST", "https://example.test")
                ),
                body=None,
            )

    folder = source(tmp_path)
    domain = tmp_path / "domain.yaml"
    save_domain_contract(_domain(), domain)
    client = Denied()
    out = tmp_path / "report.json"
    result = CliRunner().invoke(cli, [
        "domain", "assess", "--file", str(domain), "--input", str(folder),
        "--live", "--out", str(out),
    ], obj={"_assessment_client": client})
    assert result.exit_code != 0
    assert "MODEL_ACCESS_DENIED" in result.output
    assert "Traceback" not in result.output
    assert client.calls == 1
    assert not out.exists()
