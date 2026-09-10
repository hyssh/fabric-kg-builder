"""Large genuine integrated ledgers stay local; model input is representative."""

import json
import shutil
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256, deterministic_contract_id
from fabric_kg_builder.domain import design
from fabric_kg_builder.domain.discovery import prepare_discovery_corpus
from fabric_kg_builder.domain.stage import preflight_l1_inputs
from fabric_kg_builder.domain.window_design_context import WINDOW_DESIGN_CONTEXT_VERSION, window_design_context
from fabric_kg_builder.domain.window_run import RunBudget, RunConfig, load_window_inputs, load_windowed_run, run_windowed
from fabric_kg_builder.domain.window_run_acceptance import accept_window_run_partial
from fabric_kg_builder.sources.preparation import indexed_corpus_reader
from tests.unit.test_domain_discovery_cli import _paths
from tests.unit.test_window_run_approved_cli import IntegratedModel, integrated_case


@pytest.fixture(scope="module")
def large_runs(tmp_path_factory):
    root = tmp_path_factory.getbasetemp() / "representative-22-document-corpus"
    root.mkdir()
    source, intake, _, _, _ = _paths(root, count=22)
    for index, path in enumerate(sorted(source.glob("*.html"))):
        paragraphs = 5 if index < 12 else 4
        text = "A governed record describes a governed subject at 80.5 volts. " + (
            "Detailed source context is retained for evidence review, not repeated as all instance facts. " * 30
        )
        path.write_text("".join(f"<p>{text} Section {index}-{part}.</p>" for part in range(paragraphs)))
    preflight = preflight_l1_inputs(
        source_path=source, intake_raw=json.loads(intake.read_text()),
        project_id="project:records", run_id="run:large-window-context",
        model_version="offline", model_hash=canonical_sha256({"offline": "large-context"}),
    )
    prepared = prepare_discovery_corpus(
        preflight, reader=indexed_corpus_reader(preflight.corpus, source, project_id="project:records"),
    )
    assert len(prepared.source_units) == 100
    path = root / "prepared.json"
    path.write_text(canonical_json(prepared))
    inputs = load_window_inputs(prepared_path=path, intake_path=intake)
    partial_path = root / "partial"
    config = RunConfig(window_size=1, max_concurrency=1, max_request_chars=160_000)
    model = IntegratedModel()
    result = run_windowed(
        inputs=inputs, output_dir=partial_path, client=model, model_version="offline",
        config=config, budget=RunBudget(max_calls=99, max_repair_calls=0),
    )
    assert result.state == "partial" and result.cursor == 99
    partial = load_windowed_run(partial_path)
    acceptance = accept_window_run_partial(partial, actor="reviewer", rationale="Keep exact one-chunk coverage gap.")
    complete_path = root / "complete"
    shutil.copytree(partial_path, complete_path)
    finished = run_windowed(
        inputs=inputs, output_dir=complete_path, client=model, model_version="offline",
        config=config, budget=RunBudget(max_calls=1, max_repair_calls=0),
    )
    assert finished.state == "complete" and finished.cursor == 100
    return preflight, partial, acceptance, load_windowed_run(complete_path)


@pytest.mark.parametrize("partial", [False, True])
def test_representative_context_bounds_large_ledger_and_preserves_exact_authority(large_runs, partial):
    preflight, partial_run, waiver, complete = large_runs
    run, acceptance = (partial_run, waiver) if partial else (complete, None)
    original_hash = canonical_sha256(run)
    context = window_design_context(run, acceptance)
    assert len(canonical_json(run)) > 1_000_000
    assert len(canonical_json(context)) < 100_000
    assert context["format_version"] == WINDOW_DESIGN_CONTEXT_VERSION
    assert context["context"]["intake_raw"] == run.context.intake_raw
    assert context["final_snapshot"] == run.final_snapshot.model_dump(mode="json")
    assert context["binding"]["window_run_hash"] == run.artifact_hash
    assert context["binding"]["final_mapping_hash"] == run.final_mapping.artifact_hash
    coverage = context["coverage"]
    assert coverage["source_count"] == len(coverage["source_inventory"]) == 22
    assert coverage["planned_chunks"] == 100
    assert coverage["processed_chunks"] == (99 if partial else 100)
    assert coverage["candidate_count"] == len(run.final_mapping.records)
    assert coverage["mapping_records_hash"] == canonical_sha256(run.final_mapping.records)
    assert coverage["observation_ledger_hash"] == canonical_sha256(run.chunks)
    assert sum(coverage["status_counts"].values()) == len(run.final_mapping.records)
    patterns = context["patterns"]["rows"]
    assert context["patterns"]["omitted_rows"] == 0
    assert sum(row["count"] for row in patterns) == len(run.final_mapping.records)
    assert all(sum(row["per_source_counts"].values()) == row["count"] for row in patterns)
    units = {unit.source_unit_id: unit for unit in run.prepared.source_units}
    examples = [example for row in patterns for example in row["examples"]]
    assert examples and len(examples) < len(run.final_mapping.records)
    mapping = {row.observation_id: row for row in run.final_mapping.records}
    for example in examples:
        assert example["quote"] == units[example["source_unit_id"]].text[example["span_start"]:example["span_end"]]
        assert mapping[example["observation_id"]].status != "quarantined"
        assert len(example["quote"]) <= 320
        assert example["verified_candidate_hash"] == mapping[example["observation_id"]].verified_candidate_hash
    if partial:
        assert context["coverage_acceptance"]["gap_count"] == 1
        assert context["coverage_acceptance"]["acceptance_hash"] == acceptance.acceptance_hash
    client = IntegratedModel()
    draft = design.generate_domain_design(preflight, window_run=run, window_run_acceptance=acceptance, client=client)
    request = client.calls[-1]
    assert len(canonical_json(request)) < 192_000
    assert draft.window_run == run and draft.window_run_acceptance == acceptance
    assert draft.window_context_version == WINDOW_DESIGN_CONTEXT_VERSION and draft.artifact_version == "6.0.0"
    assert draft.request_hash == canonical_sha256(request)
    assert design.DomainDesignDraft.model_validate_json(draft.model_dump_json()) == draft
    assert original_hash == canonical_sha256(run)
    old = design._design_request(
        draft.inputs, draft.seed, draft.samples, window_run=run, window_run_acceptance=acceptance,
    )
    assert len(canonical_json(old)) > 192_000
    assert len(canonical_json(old)) > len(canonical_json(request)) * 5


@pytest.mark.parametrize("partial", [False, True])
def test_historical_integrated_request_hashes_remain_loadable(large_runs, partial):
    preflight, partial_run, waiver, complete = large_runs
    run, acceptance = (partial_run, waiver) if partial else (complete, None)
    draft = design.generate_domain_design(preflight, window_run=run, window_run_acceptance=acceptance, client=IntegratedModel())
    values = draft.model_dump(mode="json", exclude={"draft_id", "draft_hash", "window_context_version"})
    values.update(
        artifact_version="5.0.0" if partial else "4.0.0",
        prompt_version=design.WINDOW_PARTIAL_DESIGN_PROMPT_VERSION if partial else design.WINDOW_DESIGN_PROMPT_VERSION,
        prompt_hash=design.WINDOW_PARTIAL_DESIGN_PROMPT_HASH if partial else design.WINDOW_DESIGN_PROMPT_HASH,
        request_hash=canonical_sha256(design._design_request(
            draft.inputs, draft.seed, draft.samples, window_run=run, window_run_acceptance=acceptance,
        )),
    )
    digest = canonical_sha256(values)
    historical = design.DomainDesignDraft.model_validate_json(canonical_json({
        **values, "draft_hash": digest, "draft_id": deterministic_contract_id("domain-design-draft", {"draft_hash": digest}),
    }))
    assert historical.window_context_version is None and "window_context_version" not in historical.model_dump(mode="json")
    assert historical.request_hash != draft.request_hash


def test_context_drift_changes_binding_and_corrupt_request_is_rejected(integrated_case):
    _, _, _, _, _, _, _, path = integrated_case
    draft = design.load_domain_design(path)
    values = draft.model_dump(mode="json", exclude={"draft_id", "draft_hash"})
    values["request_hash"] = "f" * 64
    digest = canonical_sha256(values)
    with pytest.raises(ValueError, match="design request"):
        design.DomainDesignDraft.model_validate_json(canonical_json({
            **values, "draft_hash": digest, "draft_id": deterministic_contract_id("domain-design-draft", {"draft_hash": digest}),
        }))


def test_design_cli_explicit_operational_bound_never_truncates_mandatory_inputs(integrated_case, tmp_path):
    _, source, intake, windows, _, _, _, _ = integrated_case
    client = IntegratedModel()
    args = [
        "domain", "design", "--input", str(source), "--intake", str(intake), "--window-run", str(windows),
        "--out", str(tmp_path / "bounded.json"), "--live", "--max-prompt-chars", "256",
    ]
    failed = CliRunner().invoke(cli, args, obj={"_design_client": client})
    assert failed.exit_code != 0 and "nothing was truncated" in failed.output
    assert not client.calls and not (tmp_path / "bounded.json").exists()
    args[-1] = "250000"
    succeeded = CliRunner().invoke(cli, args, obj={"_design_client": client})
    assert succeeded.exit_code == 0, succeeded.output
    assert len(client.calls) == 1
    assert client.calls[0]["max_completion_tokens"] == 16_000


def test_large_conflict_registry_has_bounded_self_contained_representatives(integrated_case):
    from fabric_kg_builder.domain.window_design_context import _conflict_context
    from fabric_kg_builder.domain.window_schema import PendingProposal
    from fabric_kg_builder.domain.window_run import _pending_context

    draft = design.load_domain_design(integrated_case[-1])
    record = draft.window_run.final_mapping.records[0]
    pending = [
        PendingProposal(observation_ids=[record.observation_id], reason=f"Distinct review concern {index}: " + "x" * 90)
        for index in range(400)
    ]
    logs = [SimpleNamespace(pending=pending, diagnostics=[], records=[record], decisions=[])]
    raw = _pending_context(logs)
    assert len(canonical_json(raw)) > 24_000
    compact = _conflict_context(logs)
    assert len(canonical_json(compact)) < 24_000
    assert compact["full_registry_hash"] == canonical_sha256(raw)
    assert compact["ledger_entry_count"] == compact["unique_signature_count"] == 400
    selected = compact["representative_conflicts"]
    assert 0 < selected["represented_rows"] < 400
    assert selected["omitted_rows"] + selected["represented_rows"] == 400
    assert isinstance(selected["rows"][0]["reason"]["conflict_reason"], str)
    assert isinstance(selected["rows"][0]["scope"], dict)
