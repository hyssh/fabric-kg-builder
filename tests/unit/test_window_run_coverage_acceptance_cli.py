"""Genuine 99/100 processing coverage never becomes complete or semantic approval."""

import json
from decimal import Decimal

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
from fabric_kg_builder.domain.discovery import prepare_discovery_corpus
from fabric_kg_builder.domain.service import load_domain_contract
from fabric_kg_builder.domain.stage import preflight_l1_inputs
from fabric_kg_builder.domain.window_run import load_windowed_run
from fabric_kg_builder.domain.window_run_acceptance import (
    load_window_run_acceptance, preview_window_run_partial, validate_window_run_acceptance,
)
from fabric_kg_builder.sources.preparation import indexed_corpus_reader
from tests.unit.test_domain_discovery_cli import _invoke, _paths
from tests.unit.test_window_run_approved_cli import IntegratedModel, _approve_integrated, _replay_args, _review


class CoverageModel(IntegratedModel):
    def complete_json(self, **request):
        if "schema_proposals" in request["json_schema"]["properties"]:
            payload = json.loads(request["user"])["input"]
            if payload["schema"]["concepts"]:
                self.calls.append(request)
                return {"candidates": [], "schema_proposals": [], "pending": [], "working_context": {}}
        return super().complete_json(**request)


def _run(prepared, intake, windows, calls):
    return _invoke([
        "domain", "window-run", "--prepared", str(prepared), "--intake", str(intake),
        "--out-state", str(windows), "--window-size", "1", "--concurrency", "1",
        "--max-calls", str(calls), "--max-repair-calls", "0", "--max-tokens", "10000000", "--live",
    ], model=CoverageModel())


@pytest.fixture(scope="module")
def partial_case(tmp_path_factory):
    root = tmp_path_factory.mktemp("integrated-partial-coverage")
    source, intake, _, _, _ = _paths(root, count=1)
    source_file = next(source.glob("*.html"))
    source_file.write_text("".join(
        f"<p>A governed record describes a governed subject at 80.5 volts. Section {index:03d}.</p>"
        for index in range(100)
    ))
    preflight = preflight_l1_inputs(
        source_path=source, intake_raw=json.loads(intake.read_text()),
        project_id="project:records", run_id="run:100-source-units",
        model_version="offline", model_hash=canonical_sha256({"offline": True}),
    )
    prepared = prepare_discovery_corpus(
        preflight, reader=indexed_corpus_reader(preflight.corpus, source, project_id="project:records"),
    )
    assert len(prepared.source_units) == 100
    prepared_path = root / "prepared.json"
    prepared_path.write_text(canonical_json(prepared))
    windows = root / "windows"
    result = _run(prepared_path, intake, windows, 99)
    run = load_windowed_run(windows)
    assert result["status"] == run.state == "partial"
    assert result["model_calls"] == 99
    assert run.cursor == len(run.chunks) == 99 and len(run.chunk_plan) == 100
    return root, source, intake, prepared_path, windows


def _accept(windows, out, *, accept=False, minimum="0.99", dry_run=False):
    return CliRunner().invoke(cli, [
        *(["--dry-run"] if dry_run else []), "domain", "accept-window-run-partial",
        "--window-run", str(windows), "--out", str(out), "--actor", "coverage-reviewer",
        "--rationale", "Accept exactly 99 processed chunks; retain the last chunk and all quarantine as unresolved.",
        "--min-chunk-coverage", minimum, *(["--accept"] if accept else []),
    ])


def test_exact_partial_acceptance_approved_public_replay_preserves_one_gap(partial_case, tmp_path, monkeypatch):
    from fabric_kg_builder.cli import domain_design_cmd
    from fabric_kg_builder.domain.design import load_domain_design
    from fabric_kg_builder.enrichment.schema2_validation_stage import run_l3
    from fabric_kg_builder.serving.lifecycle_projection import run_l4

    _, source, intake, _, windows = partial_case
    before = {path: path.read_bytes() for path in windows.rglob("*.json")}
    default = CliRunner().invoke(cli, [
        "domain", "design", "--window-run", str(windows), "--input", str(source),
        "--intake", str(intake), "--out", str(tmp_path / "unaccepted.json"), "--live",
    ], obj={"_design_client": CoverageModel()})
    assert default.exit_code != 0 and "WINDOW_RUN_INCOMPLETE" in default.output
    assert not (tmp_path / "unaccepted.json").exists()
    waiver = tmp_path / "waiver.json"
    preview = _accept(windows, waiver)
    assert preview.exit_code == 0, preview.output
    preview_value = json.loads(preview.output)
    assert preview_value["result"]["accepted"] is False and "acceptance_hash" not in preview_value["result"]
    assert preview_value["writes"] == 0 and not waiver.exists()
    redirected = tmp_path / "redirected-preview.json"
    redirected.write_text(json.dumps(preview_value["result"]))
    invalid = CliRunner().invoke(cli, [
        "domain", "design", "--window-run", str(windows), "--window-run-acceptance", str(redirected),
        "--input", str(source), "--intake", str(intake), "--out", str(tmp_path / "invalid-design.json"), "--live",
    ], obj={"_design_client": CoverageModel()})
    assert invalid.exit_code != 0 and not (tmp_path / "invalid-design.json").exists()
    accepted = _accept(windows, waiver, accept=True)
    assert accepted.exit_code == 0, accepted.output
    acceptance = load_window_run_acceptance(waiver)
    assert acceptance.coverage.processed_chunks == 99 and acceptance.coverage.total_chunks == 100
    assert len(acceptance.coverage.gaps) == 1 and acceptance.coverage.quarantined_candidates > 0
    assert acceptance.min_chunk_coverage == Decimal("0.99")
    model = CoverageModel()
    l1, domain, draft_path = _approve_integrated(
        tmp_path, source, intake, windows, model, acceptance=waiver,
    )
    draft = load_domain_design(draft_path)
    contract = load_domain_contract(domain)
    assert draft.window_run.state == "partial" and draft.window_run_acceptance == acceptance
    assert draft.artifact_version == "6.0.0"
    assert contract.window_run_acceptance == acceptance
    assert contract.window_run_binding.coverage_acceptance_hash == acceptance.acceptance_hash
    assert not contract.question_plans[-1].covered
    review = tmp_path / "mapping.json"
    reviewed = _review(windows, domain, review)
    assert reviewed["result"]["coverage_acceptance"]["acceptance_hash"] == acceptance.acceptance_hash
    monkeypatch.setattr(domain_design_cmd, "_build_client", lambda *_: pytest.fail("No model during waived replay"))
    l2 = tmp_path / "l2"
    replay = _invoke(_replay_args(source, windows, domain, l1, l2, review))
    gap = acceptance.coverage.gaps[0]
    assert replay["l2_model_calls"] == replay["targeted_model_calls"] == 0
    assert replay["total_chunks"] == 100 and replay["reused_chunks"] == 99
    assert replay["missing_chunks"] == [gap.chunk_id]
    assert replay["full_corpus_design_ready"] is False
    assert replay["window_run_status"] == "partial" and replay["original_quarantined_candidates"] > 0
    authority = json.loads((l2 / "window-run-reuse-authority.json").read_text())
    assert authority["window_run_acceptance"]["acceptance_hash"] == acceptance.acceptance_hash
    assert authority["coverage_gaps"]["gaps"][0]["chunk_id"] == gap.chunk_id
    assert any(row["disposition"] == "unprocessed_accepted_coverage" for row in authority["chunks"] if "disposition" in row)
    l3root, l4root = tmp_path / "l3", tmp_path / "l4"
    for args in [
        ["validate-evidence", "--state", str(l3root), "--l2-state", str(l2), "--l1-state", str(l1), "--domain", str(domain)],
        ["project-serving", "--state", str(l4root), "--l3-state", str(l3root), "--l2-state", str(l2),
         "--l1-state", str(l1), "--domain", str(domain)],
    ]:
        result = CliRunner().invoke(cli, args)
        assert result.exit_code == 0, result.output
    l3 = run_l3(state_root=l3root, l2_state_root=l2, l1_state_root=l1, domain_path=domain)
    l4 = run_l4(l3, state_root=l4root)
    assert l4.rows.semantic_asserted_relationships and l4.rows.semantic_asserted_properties
    assert all(json.loads(row["normalized_value_json"]) == 80.5 for row in l4.rows.semantic_asserted_properties)
    assert load_windowed_run(windows).state == "partial"
    assert all(path.read_bytes() == value for path, value in before.items())


def test_partial_coverage_floor_and_explicit_threshold_cannot_be_relaxed(partial_case, tmp_path):
    _, _, intake, prepared, windows = partial_case
    for minimum in ("0.98", "0.9900000000000000000000000001", "1.0"):
        result = _accept(windows, tmp_path / f"waiver-{minimum}.json", accept=True, minimum=minimum)
        assert result.exit_code != 0 and not (tmp_path / f"waiver-{minimum}.json").exists()
    lower = tmp_path / "lower-windows"
    result = _run(prepared, intake, lower, 98)
    assert result["status"] == "partial"
    result = _accept(lower, tmp_path / "below99.json", accept=True)
    assert result.exit_code != 0 and "98/100" in result.output
    assert not (tmp_path / "below99.json").exists()


def test_partial_waiver_dry_run_conflict_and_stale_binding_refused(partial_case, tmp_path):
    _, source, intake, _, windows = partial_case
    waiver = tmp_path / "waiver.json"
    conflict = _accept(windows, waiver, accept=True, dry_run=True)
    assert conflict.exit_code == 2 and not waiver.exists()
    result = _accept(windows, waiver, accept=True)
    assert result.exit_code == 0, result.output
    stale = json.loads(waiver.read_text())
    stale["context_hash"] = "f" * 64
    stale["acceptance_hash"] = canonical_sha256({key: value for key, value in stale.items() if key != "acceptance_hash"})
    waiver.write_text(json.dumps(stale))
    result = CliRunner().invoke(cli, [
        "domain", "design", "--window-run", str(windows), "--window-run-acceptance", str(waiver),
        "--input", str(source), "--intake", str(intake), "--out", str(tmp_path / "stale-design.json"), "--live",
    ], obj={"_design_client": CoverageModel()})
    assert result.exit_code != 0 and "WINDOW_RUN_ACCEPTANCE_BINDING_DRIFT" in result.output
    assert not (tmp_path / "stale-design.json").exists()


def test_source_preparation_failure_cannot_shrink_waiver_denominator(partial_case):
    run = load_windowed_run(partial_case[-1])
    preview = preview_window_run_partial(run, actor="reviewer", rationale="Keep denominator.")
    assert sum(row.total_chunks for row in preview.coverage.per_file_coverage) == 100
    assert preview.coverage.source_preparation_errors == []
    from fabric_kg_builder.domain.window_run_acceptance import WindowRunCoveragePreview

    payload = preview.model_dump(mode="python")
    payload["coverage"]["source_preparation_errors"] = [{
        "source_file_id": "unprepared-source", "prepared_status": "failed", "prepared_reason": "Missing parser result",
        "processed_chunks": 0, "total_chunks": 0, "missing_chunk_ids": [],
    }]
    payload["coverage"]["per_file_coverage"].extend(payload["coverage"]["source_preparation_errors"])
    with pytest.raises(ValueError, match="unknown chunk denominators"):
        WindowRunCoveragePreview.model_validate(payload)


def test_changed_source_invalidates_previously_accepted_partial_coverage(partial_case, tmp_path):
    _, source, _, _, windows = partial_case
    waiver = tmp_path / "waiver.json"
    accepted = _accept(windows, waiver, accept=True)
    assert accepted.exit_code == 0, accepted.output
    run, acceptance = load_windowed_run(windows), load_window_run_acceptance(waiver)
    source_file = next(source.glob("*.html"))
    original = source_file.read_bytes()
    try:
        source_file.write_bytes(original + b"<p>Changed source.</p>")
        with pytest.raises(ValueError):
            validate_window_run_acceptance(acceptance, run)
    finally:
        source_file.write_bytes(original)
