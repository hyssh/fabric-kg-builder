"""Source-valid evidence outside an explicitly approved prefix is not authorized."""

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
from fabric_kg_builder.domain.discovery import prepare_discovery_corpus
from fabric_kg_builder.domain.service import load_domain_contract
from fabric_kg_builder.domain.stage import preflight_l1_inputs
from fabric_kg_builder.domain.window_run import load_windowed_run
from fabric_kg_builder.enrichment import window_prefix
from fabric_kg_builder.enrichment.schema2_evidence import verify_and_mint_extraction_span
from fabric_kg_builder.enrichment.schema2_extraction import ProposedAnchor
from fabric_kg_builder.enrichment.schema2_stage import run_l2
from fabric_kg_builder.enrichment.schema2_validation_stage import _mint_evidence, run_l3
from fabric_kg_builder.enrichment.window_run_reuse import prepare_window_run_reuse
from fabric_kg_builder.serving.lifecycle_projection import run_l4
from fabric_kg_builder.sources.preparation import indexed_corpus_reader
from tests.unit.test_domain_discovery_cli import _invoke, _paths
from tests.unit.test_window_run_approved_cli import _approve_integrated
from tests.unit.test_window_run_coverage_acceptance_cli import CoverageModel


SUFFIX = "EXCLUDED_SUFFIX."


class PrefixModel(CoverageModel):
    def complete_json(self, **request):
        if "schema_proposals" not in request["json_schema"]["properties"]:
            return super().complete_json(**request)
        self.calls.append(request)
        payload = json.loads(request["user"])["input"]
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
                "concept": {"concept_id": "working:record", "kind": "entity", "name": "Service Record",
                            "definition": "A source reading record.", "identity_policy": {"mode": "unresolved"}},
            }],
            "pending": [], "working_context": {},
        }


class SuffixClient:
    _config = SimpleNamespace(chat_deployment="offline-window-model")

    def __init__(self):
        self.calls = []

    def complete_json(self, **request):
        self.calls.append(request)
        assert SUFFIX not in request["user"]
        return {"candidates": [{
            "candidate_kind": "entity", "local_id": "outside", "observed_type": "Service Record",
            "label": "Excluded record", "anchors": [{"span_start": 0, "span_end": 8, "quote": SUFFIX}],
        }]}


@pytest.fixture
def approved_prefix(tmp_path):
    source, intake, _, _, _ = _paths(tmp_path, count=1)
    next(source.glob("*.html")).write_text("<p>APPROVED_PREFIX." + "80.5;" * 700 + SUFFIX + "</p>")
    preflight = preflight_l1_inputs(
        source_path=source, intake_raw=json.loads(intake.read_text()),
        project_id="project:records", run_id="run:scope-guard",
        model_version="offline", model_hash=canonical_sha256({"offline": True}),
    )
    prepared = prepare_discovery_corpus(
        preflight, reader=indexed_corpus_reader(preflight.corpus, source, project_id="project:records"),
    )
    prepared_path, windows = tmp_path / "prepared.json", tmp_path / "windows"
    prepared_path.write_text(canonical_json(prepared))
    _invoke([
        "domain", "window-run", "--prepared", str(prepared_path), "--intake", str(intake),
        "--out-state", str(windows), "--window-size", "1", "--concurrency", "1",
        "--max-chunk-chars", "512", "--max-calls", "1", "--max-repair-calls", "0", "--live",
    ], model=PrefixModel())
    assert load_windowed_run(windows).cursor == 1
    acceptance = tmp_path / "scope.json"
    _invoke([
        "domain", "accept-window-run-prefix", "--window-run", str(windows),
        "--actor", "scope-reviewer", "--rationale", "Approve only the first committed source range.",
        "--out", str(acceptance), "--accept",
    ])
    l1, domain, _ = _approve_integrated(tmp_path, source, intake, windows, CoverageModel(), acceptance=acceptance)
    inputs, _, _, materialized, _ = prepare_window_run_reuse(windows, source, l1, domain)
    return source, l1, domain, inputs, materialized.source_units[0]


def _ordinary_enrich(case, state, client):
    source, l1, domain, _, _ = case
    return CliRunner().invoke(cli, [
        "enrich", "--input", str(source), "--domain-file", str(domain),
        "--l1-state", str(l1), "--l2-state", str(state),
    ], obj={"_foundry_client": client})


def _historical_l2(case, state, monkeypatch):
    # Reproduce pre-fix L2 behavior in a disposable fixture; real approval remains intact.
    with monkeypatch.context() as patch:
        patch.setattr(window_prefix, "validate_prefix_candidate_anchors", lambda *_args, **_kwargs: None)
        result = _ordinary_l2(case, state, SuffixClient())
    assert result.receipt.status == "succeeded"


def _ordinary_l2(case, state, client):
    source, l1, domain, inputs, _ = case

    class Service:
        def complete(self, *, prompt, work_unit):
            return client.complete_json(user=prompt)

    return run_l2(
        reader=indexed_corpus_reader(inputs.corpus_manifest, source, project_id=inputs.l1_receipt.identity.project_id),
        service=Service(), state_root=state, l1_state_root=l1, domain_path=domain,
        prompt_hash=canonical_sha256({"test": "scope-guard"}), model_version="offline-scope-guard",
        model_hash=canonical_sha256({"offline-scope-guard": True}),
    )


def test_scope_is_checked_after_relocation_without_mutating_source(approved_prefix):
    _, _, domain, inputs, unit = approved_prefix
    original = canonical_json(unit)
    contract = load_domain_contract(domain)
    anchor = ProposedAnchor(span_start=0, span_end=8, quote=SUFFIX)
    outcome = _mint_evidence(
        source_unit=unit, anchor=anchor, occurred_at_utc=inputs.l1_receipt.completed_at_utc,
        domain_contract=contract,
    )
    assert outcome.span is None
    assert set(outcome.reason_codes) >= {"EVIDENCE_ANCHOR_RELOCATED", "EVIDENCE_OUTSIDE_APPROVED_PREFIX"}
    control = verify_and_mint_extraction_span(
        source_unit=unit, anchor=anchor, verified_at_utc=inputs.l1_receipt.completed_at_utc,
    )
    assert control.span is not None and control.span.span_start == unit.text.index(SUFFIX)
    inside = _mint_evidence(
        source_unit=unit, anchor=ProposedAnchor(span_start=2, span_end=3, quote="APPROVED_PREFIX."),
        occurred_at_utc=inputs.l1_receipt.completed_at_utc, domain_contract=contract,
    )
    assert inside.span is not None and inside.span.span_start == 0
    assert canonical_json(unit) == original
    assert not window_prefix.prefix_span_allowed(
        contract, source_unit_id=unit.source_unit_id, span_start=511, span_end=513,
        source_text_hash=unit.text_content_hash,
    )


def test_ordinary_l2_rejects_relocated_excluded_quote_without_window_run_flag(approved_prefix, tmp_path):
    client = SuffixClient()
    state = tmp_path / "ordinary-l2"
    result = _ordinary_enrich(approved_prefix, state, client)
    assert result.exit_code != 0 and "supply --window-run and --mapping-review" in result.output
    assert client.calls == []
    with pytest.raises(ValueError, match="WINDOW_PREFIX_EVIDENCE_OUTSIDE_SCOPE"):
        _ordinary_l2(approved_prefix, state, client)
    assert len(client.calls) == 1
    assert not (state / "stage-receipt.json").exists()


def test_cached_l2_outside_scope_is_rejected_without_model_call(approved_prefix, tmp_path, monkeypatch):
    state = tmp_path / "historical-l2"
    _historical_l2(approved_prefix, state, monkeypatch)
    before = (state / "stage-receipt.json").read_bytes()
    client = SuffixClient()
    with pytest.raises(ValueError, match="WINDOW_PREFIX_EVIDENCE_OUTSIDE_SCOPE"):
        _ordinary_l2(approved_prefix, state, client)
    assert client.calls == [] and (state / "stage-receipt.json").read_bytes() == before


def test_l3_fresh_and_cached_results_and_l4_publication_enforce_scope(approved_prefix, tmp_path, monkeypatch):
    _, l1, domain, _, _ = approved_prefix
    l2 = tmp_path / "historical-l2"
    _historical_l2(approved_prefix, l2, monkeypatch)
    rejected = run_l3(state_root=tmp_path / "guarded-l3", l2_state_root=l2, l1_state_root=l1, domain_path=domain)
    assert rejected.candidate_results and not rejected.evidence_spans
    assert all(item.current_state == "rejected" for item in rejected.candidate_results)
    assert all("EVIDENCE_OUTSIDE_APPROVED_PREFIX" in item.reason_codes for item in rejected.candidate_results)
    projected = run_l4(rejected, state_root=tmp_path / "guarded-l4")
    assert not projected.rows.semantic_asserted_entities
    historical_l3 = tmp_path / "historical-l3"
    # Produce the exact formerly accepted, source-valid but scope-invalid cached result.
    with monkeypatch.context() as patch:
        patch.setattr(window_prefix, "prefix_span_allowed", lambda *_args, **_kwargs: True)
        old = run_l3(state_root=historical_l3, l2_state_root=l2, l1_state_root=l1, domain_path=domain)
    assert old.evidence_spans and any(item.current_state == "asserted" for item in old.candidate_results)
    with pytest.raises(ValueError, match="L3_EVIDENCE_OUTSIDE_APPROVED_PREFIX"):
        run_l3(state_root=historical_l3, l2_state_root=l2, l1_state_root=l1, domain_path=domain)
    with pytest.raises(ValueError, match="L4_EVIDENCE_OUTSIDE_APPROVED_PREFIX"):
        run_l4(old, state_root=tmp_path / "forbidden-l4")
