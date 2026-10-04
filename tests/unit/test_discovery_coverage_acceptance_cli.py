"""Reviewed partial discovery never becomes complete evidence or implicit inference."""

import json

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.domain import discovery as core
from tests.unit.test_domain_discovery_cli import Model, _approved, _invoke, _paths


class PartialModel(Model):
    def __init__(self, *, fail_chunk=True):
        super().__init__()
        self.fail_chunk = fail_chunk

    def complete_json(self, **request):
        fields = request["json_schema"]["properties"]
        if "candidates" in fields and not self.observed and self.fail_chunk:
            self.calls.append(request)
            self.observed.append(json.loads(request["user"])["input"]["chunk"]["chunk_id"])
            raise ValueError("Synthetic missing response remains unknown")
        if "summary" in fields:
            data = json.loads(request["user"])["input"]
            if data["level"] == "corpus":
                self.calls.append(request)
                raise ValueError("Synthetic corpus summary unavailable")
        response = super().complete_json(**request)
        if "candidates" in fields and len(self.observed) == 2:
            response["candidates"][0]["anchors"][0]["quote"] = "Not present in the supplied source."
        return response


@pytest.fixture(scope="module")
def partial_discovery(tmp_path_factory):
    root = tmp_path_factory.getbasetemp() / "reviewed-partial"
    root.mkdir()
    source, intake, out, cache, args = _paths(root, count=25)
    for path in source.glob("*.html"):
        path.write_text("<html><body>" + "<p>A governed record describes a governed subject.</p>" * 4 + "</body></html>")
    model = PartialModel()
    _invoke([*args, "--live", "--max-calls", "400", "--max-chunk-chars", "128"], model=model)
    run = core.load_discovery(out)
    assert run.status == "partial"
    assert sum(item.response is not None for item in run.chunks) == 99
    assert len(run.chunks) == 100
    return source, intake, out, cache, model


def _accept_args(run, out):
    return [
        "domain", "accept-discovery", "--file", str(run),
        "--min-coverage", "0.99", "--actor", "offline-reviewer",
        "--rationale", "Proceed with reviewed partial evidence; unknown source remains pending.",
        "--out", str(out),
    ]


def test_coverage_acceptance_plan_is_read_only(partial_discovery, tmp_path, monkeypatch):
    from fabric_kg_builder.cli import domain_design_cmd

    _, _, run, _, _ = partial_discovery
    before = run.read_bytes()
    out = tmp_path / "acceptance.json"
    monkeypatch.setattr(
        domain_design_cmd, "_build_client",
        lambda *_: pytest.fail("Coverage review must never construct a model client"),
    )
    result = _invoke(_accept_args(run, out))
    assert result["status"] == "planned"
    assert result["model_calls"] == result["writes"] == 0
    assert not out.exists() and run.read_bytes() == before


def test_less_than_99_percent_is_not_rounded_up(tmp_path):
    _, _, run, _, args = _paths(tmp_path, count=99)
    _invoke([*args, "--live", "--max-calls", "400"], model=PartialModel())
    current = core.load_discovery(run)
    assert sum(item.response is not None for item in current.chunks) == 98
    out = tmp_path / "rejected.json"
    result = CliRunner().invoke(cli, [*_accept_args(run, out), "--accept"])
    assert result.exit_code != 0
    assert "coverage" in result.output.lower()
    assert not out.exists()


def test_all_chunks_observed_does_not_clear_summary_or_quarantine_gaps(tmp_path):
    source, intake, run, _, args = _paths(tmp_path, count=3)
    model = PartialModel(fail_chunk=False)
    _invoke([*args, "--live"], model=model)
    discovery = core.load_discovery(run)
    assert discovery.status == "partial" and discovery.full_corpus_design_ready is False
    assert all(item.response is not None for item in discovery.chunks)
    waiver = tmp_path / "acceptance.json"
    result = _invoke([*_accept_args(run, waiver), "--accept"])
    assert result["result"]["accounted_chunks"] == result["result"]["total_chunks"] == 3
    assert result["result"]["corpus_summary_missing"] is True
    assert result["result"]["grounding"]["quarantined_candidate_count"] > 0
    before = len(model.calls)
    draft = tmp_path / "draft.json"
    _invoke([
        "domain", "design", "--input", str(source), "--intake", str(intake),
        "--discovery", str(run), "--discovery-acceptance", str(waiver),
        "--out", str(draft), "--live",
    ], model=model)
    assert len(model.calls) == before + 1
    context = _invoke(["domain", "question-context", "--file", str(draft)])
    assert context["approval_status"] == "unapproved_design"
    assert context["discovery_coverage"]["unknown_chunk_count"] == 0
    assert context["discovery_coverage"]["status"] == "pending_review"
    assert context["discovery_coverage"]["quarantine_and_summary_gaps_cleared"] is False
    assert context["discovery_coverage"]["completeness_asserted"] is False
    assert "summary_failure_count" not in context["discovery_coverage"]


def test_explicit_acceptance_cannot_conflict_with_global_dry_run(partial_discovery, tmp_path):
    _, _, run, _, _ = partial_discovery
    out = tmp_path / "acceptance.json"
    result = CliRunner().invoke(cli, ["--dry-run", *_accept_args(run, out), "--accept"])
    assert result.exit_code == 2
    assert not out.exists()


@pytest.mark.parametrize("minimum", ["0.990000000000000000001", "0.98", "nan", "1.01"])
def test_minimum_threshold_is_exact_and_bounded(partial_discovery, tmp_path, minimum):
    _, _, run, _, _ = partial_discovery
    out = tmp_path / "acceptance.json"
    args = _accept_args(run, out)
    args[args.index("--min-coverage") + 1] = minimum
    result = CliRunner().invoke(cli, [*args, "--accept"])
    assert result.exit_code != 0
    assert not out.exists()


def test_default_design_still_rejects_partial_without_model_calls(partial_discovery, tmp_path):
    source, intake, run, _, model = partial_discovery
    before = len(model.calls)
    out = tmp_path / "draft.json"
    result = CliRunner().invoke(cli, [
        "domain", "design", "--input", str(source), "--intake", str(intake),
        "--discovery", str(run), "--out", str(out), "--live",
    ], obj={"_design_client": model})
    assert result.exit_code != 0
    assert len(model.calls) == before and not out.exists()


def test_acceptance_is_immutable_and_remains_bound_to_partial_run(partial_discovery, tmp_path):
    source, intake, run, _, model = partial_discovery
    before = run.read_bytes()
    out = tmp_path / "acceptance.json"
    accepted = _invoke([*_accept_args(run, out), "--accept"])
    assert accepted["status"] == "partial_accepted"
    assert accepted["discovery_status"] == "partial"
    assert accepted["ontology_approved"] is accepted["evidence_approved"] is False
    assert accepted["full_corpus_design_ready"] is False
    assert accepted["model_calls"] == 0 and accepted["writes"] == 1
    preserved = out.read_bytes()
    changed = _accept_args(run, out)
    changed[changed.index("--rationale") + 1] = "A different review cannot replace the first."
    result = CliRunner().invoke(cli, [*changed, "--accept"])
    assert result.exit_code != 0
    assert out.read_bytes() == preserved and run.read_bytes() == before
    draft = tmp_path / "draft.json"
    calls = len(model.calls)
    plan = _invoke([
        "domain", "design", "--input", str(source), "--intake", str(intake),
        "--discovery", str(run), "--coverage-acceptance", str(out), "--out", str(draft),
    ], model=model)
    assert plan["result"]["design_mode"] == "reviewed_partial_discovery"
    assert plan["result"]["full_corpus_design_ready"] is False
    assert not draft.exists() and len(model.calls) == calls


def test_wrong_run_acceptance_rejected_before_design_call(partial_discovery, tmp_path):
    from copy import deepcopy

    source, intake, run_path, _, model = partial_discovery
    waiver = tmp_path / "acceptance.json"
    _invoke([*_accept_args(run_path, waiver), "--accept"])
    run = core.load_discovery(run_path)
    values = {
        key: deepcopy(getattr(run, key))
        for key in type(run).model_fields if key != "artifact_hash"
    }
    values["issues"] = [*run.issues, "Distinct immutable snapshot for binding test"]
    different = core._seal(core.DiscoveryRun, **values)
    different_path = tmp_path / "different-run.json"
    core.save_discovery(different_path, different)
    calls = len(model.calls)
    draft = tmp_path / "draft.json"
    result = CliRunner().invoke(cli, [
        "domain", "design", "--input", str(source), "--intake", str(intake),
        "--discovery", str(different_path), "--discovery-acceptance", str(waiver),
        "--out", str(draft), "--live",
    ], obj={"_design_client": model})
    assert result.exit_code != 0
    assert "acceptance" in result.output.lower()
    assert len(model.calls) == calls and not draft.exists()


def test_approved_partial_reuse_keeps_missing_pending_without_model_calls(partial_discovery, tmp_path):
    from fabric_kg_builder.cli.domain_io import load_cli_domain_contract
    from fabric_kg_builder.domain.design import load_domain_design

    source, intake, run, _, model = partial_discovery
    original = run.read_bytes()
    waiver = tmp_path / "acceptance.json"
    _invoke([*_accept_args(run, waiver), "--accept"])
    l1, domain = _approved(
        tmp_path, source, intake, run, model, discovery_acceptance=waiver,
    )
    draft = load_domain_design(tmp_path / "draft.json")
    draft_context = _invoke(["domain", "question-context", "--file", str(tmp_path / "draft.json")])
    assert draft_context["approval_status"] == "unapproved_design"
    assert draft.discovery.status == "partial"
    assert draft.discovery.full_corpus_design_ready is False
    contract = load_cli_domain_contract(domain)
    assert contract.approval.status == "approved"
    assert contract.discovery_acceptance.acceptance_hash == draft.discovery_acceptance.acceptance_hash
    assert contract.discovery_run_hash == core.load_discovery(run).run_hash
    context = _invoke(["domain", "question-context", "--file", str(domain)])
    assert context["discovery_acceptance"]["acceptance_hash"] == draft.discovery_acceptance.acceptance_hash
    assert draft_context["discovery_acceptance"] == context["discovery_acceptance"]
    assert "run_issues" not in draft_context["discovery_acceptance"]
    assert "failed_summary_metadata" not in draft_context["discovery_acceptance"]
    assert context["execution_verified"] is False
    calls = len(model.calls)
    l2 = tmp_path / "l2"
    result = _invoke([
        "enrich", "--input", str(source), "--domain-file", str(domain),
        "--l1-state", str(l1), "--l2-state", str(l2),
        "--discovery", str(run), "--replay-only",
    ], model=model)
    assert result["status"] == "pending_review"
    assert result["discovery_status"] == "partial"
    assert result["targeted_model_calls"] == result["l2_model_calls"] == 0
    assert result["missing_chunks"] and result["pending_chunks"] >= 1
    assert result["original_quarantined_candidates"] > 0
    assert any("grounding_quarantined" in item["reasons"] for item in result["pending"])
    assert len(model.calls) == calls and run.read_bytes() == original
    authority = json.loads((l2 / "discovery-reuse-authority.json").read_text())
    assert authority["discovery_acceptance"]["acceptance_hash"] == draft.discovery_acceptance.acceptance_hash
    assert authority["pending_chunks"]
    leaves = [json.loads(path.read_text()) for path in (l2 / "checkpoint-leaves").glob("*.json")]
    assert any(
        dict(leaf["audit_reason_counts"]).get("discovery_coverage_waived_pending", 0)
        for leaf in leaves
    )
    from fabric_kg_builder.enrichment.schema2_validation_stage import run_l3
    from fabric_kg_builder.serving.lifecycle_projection import run_l4

    l3_root, l4_root = tmp_path / "l3", tmp_path / "l4"
    l3 = run_l3(state_root=l3_root, l2_state_root=l2, l1_state_root=l1, domain_path=domain)
    l4 = run_l4(l3, state_root=l4_root)
    sealed = _invoke([
        "domain", "question-context", "--l4-run", str(l4.run_root), "--l3-root", str(l3_root),
    ])
    assert sealed["discovery_acceptance"] == context["discovery_acceptance"]
    assert sealed["discovery_coverage"] == context["discovery_coverage"]
    assert sealed["discovery_coverage"]["status"] == "pending_review"
    assert sealed["discovery_coverage"]["unknown_chunk_count"] == 1
    assert sealed["discovery_coverage"]["reason_counts"] == {"discovery_coverage_waived_pending": 1}
    assert sealed["discovery_coverage"]["completeness_asserted"] is False
    assert sealed["discovery_coverage"]["quarantine_and_summary_gaps_cleared"] is False
    assert sealed["execution_verified"] is False
