"""An explicit genuine 3/100 prefix is scoped, never a coverage waiver or empty remainder."""

import json

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.cli.domain_window_run_prefix_cmd import domain_accept_window_run_prefix_cmd
from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
from fabric_kg_builder.domain import window_run as core
from fabric_kg_builder.domain.discovery import prepare_discovery_corpus
from fabric_kg_builder.domain.service import load_domain_contract
from fabric_kg_builder.domain.stage import preflight_l1_inputs
from fabric_kg_builder.domain.window_run_acceptance import (
    WindowRunPrefixAcceptance, accept_window_run_prefix, load_window_run_acceptance,
    preview_window_run_partial, validate_window_run_acceptance,
)
from fabric_kg_builder.sources.preparation import indexed_corpus_reader
from tests.unit.test_domain_discovery_cli import _invoke, _paths
from tests.unit.test_window_run_approved_cli import IntegratedModel, _approve_integrated, _replay_args, _review
from tests.unit.test_window_run_coverage_acceptance_cli import CoverageModel, _run


@pytest.fixture(autouse=True)
def prefix_command(monkeypatch):
    monkeypatch.setitem(cli.commands["domain"].commands, "accept-window-run-prefix", domain_accept_window_run_prefix_cmd)


@pytest.fixture(scope="module")
def prefix_case(tmp_path_factory):
    root = tmp_path_factory.mktemp("committed-prefix")
    source, intake, _, _, _ = _paths(root, count=1)
    next(source.glob("*.html")).write_text("".join(
        f"<p>A governed record describes a governed subject at 80.5 volts. Section {index:03d}."
        f"{'UNPROCESSED_PREFIX_SENTINEL' if index >= 3 else ''}</p>"
        for index in range(100)
    ))
    preflight = preflight_l1_inputs(
        source_path=source, intake_raw=json.loads(intake.read_text()),
        project_id="project:records", run_id="run:prefix-100",
        model_version="offline", model_hash=canonical_sha256({"offline": True}),
    )
    prepared = prepare_discovery_corpus(
        preflight, reader=indexed_corpus_reader(preflight.corpus, source, project_id="project:records"),
    )
    path = root / "prepared.json"
    path.write_text(canonical_json(prepared))
    windows = root / "windows"
    assert _run(path, intake, windows, 3)["status"] == "partial"
    before = core.load_windowed_run(windows)
    assert before.cursor == 3 and len(before.chunk_plan) == 100
    # A genuine received fourth response is persisted, then interruption prevents its barrier commit.
    write = core._write

    def interrupt_commit(path, value):
        if path.parent == windows / "windows":
            raise RuntimeError("offline interruption after response, before commit")
        return write(path, value)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(core, "_write", interrupt_commit)
        model = IntegratedModel()
        result = CliRunner().invoke(cli, [
            "domain", "window-run", "--prepared", str(path), "--intake", str(intake),
            "--out-state", str(windows), "--window-size", "1", "--concurrency", "1",
            "--max-calls", "4", "--max-repair-calls", "0", "--max-tokens", "10000000", "--live", "--resume",
        ], obj={"_design_client": model})
        assert result.exit_code != 0 and model.calls, result.output
    assert core.load_windowed_run(windows).artifact_hash == before.artifact_hash
    assert len(list((windows / "responses").glob("*.json"))) > len(before.chunks)
    return source, intake, windows


def _accept(windows, out, *, accept=False, dry_run=False):
    return CliRunner().invoke(cli, [
        *(["--dry-run"] if dry_run else []), "domain", "accept-window-run-prefix",
        "--window-run", str(windows), "--out", str(out), "--actor", "prefix-reviewer",
        "--rationale", "User explicitly paused full extraction and authorized only the committed prefix prototype.",
        *(["--accept"] if accept else []),
    ])


def test_prefix_public_cli_approval_replay_excludes_unprocessed_work(prefix_case, tmp_path, monkeypatch):
    from fabric_kg_builder.cli import domain_design_cmd
    from fabric_kg_builder.domain.design import load_domain_design
    from fabric_kg_builder.enrichment.schema2_validation_stage import run_l3
    from fabric_kg_builder.serving.lifecycle_projection import run_l4

    source, intake, windows = prefix_case
    before = {path: path.read_bytes() for path in windows.rglob("*") if path.is_file()}
    run = core.load_windowed_run(windows)
    rejected = CliRunner().invoke(cli, [
        "domain", "design", "--window-run", str(windows), "--input", str(source),
        "--intake", str(intake), "--out", str(tmp_path / "unapproved.json"), "--live",
    ], obj={"_design_client": CoverageModel()})
    assert rejected.exit_code != 0 and "WINDOW_RUN_INCOMPLETE" in rejected.output
    with pytest.raises(ValueError, match="COVERAGE_BELOW_THRESHOLD"):
        preview_window_run_partial(run, actor="reviewer", rationale="not a >=99% run")
    accepted_path = tmp_path / "scope.json"
    preview = _accept(windows, accepted_path)
    assert preview.exit_code == 0, preview.output
    value = json.loads(preview.output)
    assert value["writes"] == 0 and not accepted_path.exists()
    assert value["result"]["accepted"] is False and "acceptance_hash" not in value["result"]
    redirected = tmp_path / "preview.json"
    redirected.write_text(canonical_json(value["result"]))
    with pytest.raises(ValueError):
        load_window_run_acceptance(redirected)
    rejected = CliRunner().invoke(cli, [
        "domain", "design", "--window-run", str(windows), "--window-run-acceptance", str(redirected),
        "--input", str(source), "--intake", str(intake), "--out", str(tmp_path / "bad-preview-design.json"), "--live",
    ], obj={"_design_client": CoverageModel()})
    assert rejected.exit_code != 0 and not (tmp_path / "bad-preview-design.json").exists()
    result = _accept(windows, accepted_path, accept=True)
    assert result.exit_code == 0, result.output
    acceptance = load_window_run_acceptance(accepted_path)
    assert isinstance(acceptance, WindowRunPrefixAcceptance)
    assert acceptance.cursor == 3 and acceptance.coverage.total_chunks == 100
    assert len(acceptance.omitted_chunk_ids) == 97
    assert acceptance.selected_committed_chunk_ids == [chunk.chunk_id for chunk in run.chunk_plan[:3]]
    assert "min_chunk_coverage" not in acceptance.model_dump()
    model = CoverageModel()
    l1, domain, design = _approve_integrated(tmp_path, source, intake, windows, model, acceptance=accepted_path)
    draft = load_domain_design(design)
    assert draft.artifact_version == "7.0.0" and draft.window_run.state == "partial"
    assert len(model.calls) == 1
    assert "UNPROCESSED_PREFIX_SENTINEL" not in model.calls[0]["user"]
    assert len(draft.window_run.prepared.source_units) == 100 and len(draft.samples.source_units) == 3
    assert len(draft.samples.evidence_spans) == 3
    assert all("UNPROCESSED_PREFIX_SENTINEL" not in span.quote for span in draft.samples.evidence_spans)
    assert "UNPROCESSED_PREFIX_SENTINEL" not in canonical_json(draft.samples.source_profile)
    assert draft.window_run.context.intake_raw == json.loads(intake.read_text())
    from fabric_kg_builder.sources.evidence_verifier import mint_verified_span

    omitted_unit = run.prepared.source_units[-1]
    omitted_span = mint_verified_span(
        source_unit=omitted_unit, span_start=0, span_end=omitted_unit.codepoint_count,
        purpose="domain_design", verified_at_utc=draft.samples.evidence_spans[0].verified_at_utc,
    )
    with pytest.raises(ValueError, match="WINDOW_PREFIX_DESIGN_SUPPORT_OUTSIDE_SCOPE"):
        draft.model_copy(update={"samples": draft.samples.model_copy(update={
            "source_units": (*draft.samples.source_units, omitted_unit),
            "evidence_spans": (*draft.samples.evidence_spans, omitted_span),
        })})
    contract = load_domain_contract(domain)
    assert contract.window_run_acceptance == acceptance
    assert contract.window_run_binding.scope_acceptance_hash == acceptance.acceptance_hash
    assert "coverage_acceptance_hash" not in contract.window_run_binding.model_dump()
    assert acceptance.scope_notice in contract.business.organization_context
    assert not contract.question_plans[-1].covered
    review = tmp_path / "mapping.json"
    mapping = _review(windows, domain, review)["result"]
    assert "coverage_acceptance" not in mapping
    assert mapping["scope_acceptance"]["acceptance_hash"] == acceptance.acceptance_hash
    monkeypatch.setattr(domain_design_cmd, "_build_client", lambda *_: pytest.fail("No prefix extraction/replay model calls"))
    l2 = tmp_path / "l2"
    replay = _invoke(_replay_args(source, windows, domain, l1, l2, review))
    assert replay["l2_model_calls"] == replay["targeted_model_calls"] == 0
    assert replay["reused_chunks"] == 3 and replay["total_chunks"] == 100
    assert replay["excluded_chunk_count"] == 97 and replay["window_run_status"] == "partial"
    assert replay["missing_chunks"] == acceptance.omitted_chunk_ids
    assert replay["coverage_authority"] == "limited_committed_prefix_only"
    assert replay["agent_scope_notice"] == acceptance.scope_notice
    assert replay["original_quarantined_candidates"] > 0 and not replay["full_corpus_design_ready"]
    assert replay["original_raw_candidates"] == sum(len(chunk.raw_response["candidates"]) for chunk in run.chunks)
    checkpoint = json.loads((l2 / "checkpoint.json").read_text())
    assert len(checkpoint["work_units"]) == 3  # Not 100 successful, possibly-empty extraction leaves.
    authority = json.loads((l2 / "window-run-reuse-authority.json").read_text())
    excluded = [item for item in authority["chunks"] if item.get("disposition") == "excluded_outside_approved_prefix"]
    assert len(excluded) == 97 and all(item["original_observation_hash"] is None for item in excluded)
    assert len(authority["source_unit_rebindings"]) == 100  # Full immutable source inventory is retained.
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
    exported = json.loads(l4.rows.semantic_publication_authority[0]["domain_contract_json"])
    assert exported["window_run_acceptance"]["scope_notice"] == acceptance.scope_notice
    assert exported["window_run_binding"]["scope_acceptance_hash"] == acceptance.acceptance_hash
    assert core.load_windowed_run(windows).state == "partial"
    assert all(path.read_bytes() == data for path, data in before.items())


def test_prefix_preview_conflict_and_exact_scope_tampering(prefix_case, tmp_path):
    source, _, windows = prefix_case
    out = tmp_path / "scope.json"
    result = _accept(windows, out, accept=True, dry_run=True)
    assert result.exit_code == 2 and "--accept conflicts with --dry-run" in result.output
    assert not out.exists()
    run = core.load_windowed_run(windows)
    acceptance = accept_window_run_prefix(run, actor="reviewer", rationale="Explicit prefix scope")
    for field in ("window_run_hash", "prepared_corpus_hash", "context_hash", "snapshot_hash", "final_mapping_hash"):
        values = acceptance.model_dump(mode="json")
        values[field] = "0" * 64
        values["acceptance_hash"] = canonical_sha256({key: value for key, value in values.items() if key != "acceptance_hash"})
        changed = WindowRunPrefixAcceptance.model_validate_json(canonical_json(values))
        with pytest.raises(ValueError, match="BINDING_DRIFT"):
            validate_window_run_acceptance(changed, run)
    for update in (
        {"cursor": 4}, {"selected_committed_chunk_ids": acceptance.selected_committed_chunk_ids[::-1]},
        {"omitted_chunk_ids": acceptance.omitted_chunk_ids[1:]}, {"scope_notice": "Full corpus is approved."},
    ):
        with pytest.raises(ValueError):
            validate_window_run_acceptance(acceptance.model_copy(update=update), run)
    path = next(source.glob("*.html"))
    original = path.read_bytes()
    try:
        path.write_bytes(original + b"<p>changed source</p>")
        result = _accept(windows, out, accept=True)
        assert result.exit_code != 0 and not out.exists()
    finally:
        path.write_bytes(original)


def test_prefix_schedules_only_selected_ranges_inside_a_long_source_unit(tmp_path):
    from fabric_kg_builder.enrichment.schema2_work_units import WorkUnitCheckpoint
    from fabric_kg_builder.enrichment.window_prefix import plan_approved_work_units
    from fabric_kg_builder.enrichment.window_run_reuse import prepare_window_run_reuse

    class LongUnitModel(CoverageModel):
        def complete_json(self, **request):
            if "schema_proposals" not in request["json_schema"]["properties"]:
                return super().complete_json(**request)
            self.calls.append(request)
            payload = json.loads(request["user"])["input"]
            if payload["schema"]["concepts"]:
                return {"candidates": [], "schema_proposals": [], "pending": [], "working_context": {}}
            return {
                "candidates": [{
                    "candidate_kind": "entity", "local_id": "reading", "observed_type": "Service Record",
                    "label": "Reading record", "anchors": [{
                        "span_start": payload["offset_base"],
                        "span_end": payload["offset_base"] + len(payload["text"]), "quote": payload["text"],
                    }],
                }],
                "schema_proposals": [{
                    "action": "add_concept", "candidate_indices": [0], "reason": "A source reading record.",
                    "abstraction": {
                        "level": "reusable_type", "rationale": "A reusable reading record role.",
                        "reuse_assessment": "The schema is empty; no existing reading record class.",
                        "representation": "entity_type",
                        "independent_identity_rationale": "A reading record can be identified independently of its value.",
                    },
                    "concept": {"concept_id": "working:record", "kind": "entity", "name": "Service Record",
                                "definition": "A source reading record.", "identity_policy": {"mode": "unresolved"}},
                }],
                "pending": [], "working_context": {},
            }

    source, intake, _, _, _ = _paths(tmp_path, count=1)
    next(source.glob("*.html")).write_text(
        "<p>" + "80.5;" * 700 + "UNPROCESSED_SUFFIX_MARKER</p>"
    )
    preflight = preflight_l1_inputs(
        source_path=source, intake_raw=json.loads(intake.read_text()),
        project_id="project:records", run_id="run:long-source-prefix",
        model_version="offline", model_hash=canonical_sha256({"offline": True}),
    )
    prepared = prepare_discovery_corpus(
        preflight, reader=indexed_corpus_reader(preflight.corpus, source, project_id="project:records"),
    )
    assert len(prepared.source_units) == 1
    prepared_path, windows = tmp_path / "prepared.json", tmp_path / "windows"
    prepared_path.write_text(canonical_json(prepared))
    _invoke([
        "domain", "window-run", "--prepared", str(prepared_path), "--intake", str(intake),
        "--schema-policy", "concepts",
        "--out-state", str(windows), "--window-size", "1", "--concurrency", "1",
        "--max-chunk-chars", "512", "--max-calls", "2", "--max-repair-calls", "0", "--live",
    ], model=LongUnitModel())
    run = core.load_windowed_run(windows)
    assert run.cursor == 2 and run.state == "partial" and len(run.chunk_plan) > 2
    acceptance_path = tmp_path / "scope.json"
    accepted = _accept(windows, acceptance_path, accept=True)
    assert accepted.exit_code == 0, accepted.output
    model = CoverageModel()
    l1, domain, design = _approve_integrated(tmp_path, source, intake, windows, model, acceptance=acceptance_path)
    from fabric_kg_builder.domain.design import load_domain_design

    draft = load_domain_design(design)
    assert "UNPROCESSED_SUFFIX_MARKER" not in model.calls[0]["user"]
    assert "UNPROCESSED_SUFFIX_MARKER" in draft.samples.source_units[0].text
    assert all("UNPROCESSED_SUFFIX_MARKER" not in span.quote for span in draft.samples.evidence_spans)
    assert {(span.span_start, span.span_end) for span in draft.samples.evidence_spans} == {
        (chunk.slice_start, chunk.slice_end) for chunk in run.chunk_plan[:2]
    }
    assert len(draft.samples.sample_manifest.entries[0].evidence_span_ids) == 2
    review = tmp_path / "mapping.json"
    _review(windows, domain, review)
    l2 = tmp_path / "l2"
    replay = _invoke(_replay_args(source, windows, domain, l1, l2, review))
    assert replay["reused_chunks"] == 2 and replay["l2_model_calls"] == 0
    checkpoint_data = json.loads((l2 / "checkpoint.json").read_text())
    assert len(checkpoint_data["work_units"]) == 2
    fingerprint = next(iter(checkpoint_data["work_units"].values()))["authority_fingerprint"]
    inputs, _, _, materialized, _ = prepare_window_run_reuse(windows, source, l1, domain)
    roots = plan_approved_work_units(
        materialized.source_units, contract=inputs.domain_contract,
        pass_name="schema-constrained-extraction", authority_fingerprint=fingerprint,
    )
    assert [(root.slice_start, root.slice_end) for root in roots] == [
        (chunk.slice_start, chunk.slice_end) for chunk in run.chunk_plan[:2]
    ]
    assert max(root.slice_end for root in roots) < prepared.source_units[0].codepoint_count
    checkpoint = WorkUnitCheckpoint(l2 / "checkpoint.json", l2 / "checkpoint-leaves")
    assert all(checkpoint.reuse(root) is not None for root in roots)
    unit = materialized.source_units[0]
    with pytest.raises(ValueError, match="hash exact|SOURCE_RANGE_DRIFT"):
        plan_approved_work_units(
            [unit.model_copy(update={"text_content_hash": "0" * 64})], contract=inputs.domain_contract,
            pass_name="schema-constrained-extraction", authority_fingerprint=fingerprint,
        )
