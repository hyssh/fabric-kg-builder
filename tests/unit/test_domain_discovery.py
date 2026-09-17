from __future__ import annotations

import copy
import json
import threading
from pathlib import Path

import pytest

from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
from fabric_kg_builder.domain.design import (
    DomainDesignError, compile_domain_design, evaluate_domain_design,
    generate_domain_design, load_domain_design, save_design_artifact,
)
from fabric_kg_builder.domain.discovery import (
    DiscoveryBudget, DiscoveryRun, PreparedCorpus, discovery_design_context,
    discovery_raw_responses, load_discovery, plan_discovery_chunks,
    prepare_discovery_corpus, preflight_discovery_inputs, resume_discovery_preparation,
    run_discovery, save_discovery, validate_discovery, discovery_grounding_report,
)
from fabric_kg_builder.domain.service import load_domain_contract
from fabric_kg_builder.domain.stage import (
    approve_persisted_l1_draft, finalize_l1_stage, preflight_l1_inputs,
)
from fabric_kg_builder.enrichment.schema2_sources import materialize_corpus_entry
from fabric_kg_builder.sources.preparation import indexed_corpus_reader
from tests.unit.test_domain_design import Client, _preflight, _sketch
from tests.unit.test_l1_stage import _intake
from tests.unit.test_schema2_cached_sources import cached_pdf


class DiscoveryClient:
    def __init__(self, *, empty=False, false_quote=False):
        self.calls = []
        self.empty = empty
        self.false_quote = false_quote
        self.lock = threading.Lock()

    def complete_json(self, **request):
        with self.lock:
            self.calls.append(copy.deepcopy(request))
        payload = json.loads(request["user"])["input"]
        if "inputs" in payload:
            return {
                "summary": "Records describe subjects. Preserve applicability, exceptions, cross-references and pending SQL population review.",
                "covered_input_ids": [item["id"] for item in payload["inputs"]],
            }
        if self.empty:
            return {"candidates": []}
        quote = "Fabricated source statement." if self.false_quote else payload["text"]
        start = payload["offset_base"]
        return {"candidates": [{
            "candidate_kind": "entity", "local_id": "observation",
            "observed_type": "Open record", "label": "Observed record",
            "anchors": [{"span_start": start, "span_end": start + len(quote), "quote": quote}],
        }]}


def _prepared(tmp_path, *, files=1, paragraphs=1):
    source = tmp_path / "source"
    source.mkdir()
    for index in range(files):
        body = "".join(
            f"<p>Document {index} record {number} describes a subject, except when cancelled. See adjacent requirements.</p>"
            for number in range(paragraphs)
        )
        (source / f"records-{index:02d}.html").write_text(f"<html><body><h1>Governed records {index}</h1>{body}</body></html>")
    preflight = preflight_l1_inputs(
        source_path=source, intake_raw=_intake(), project_id="project:records",
        run_id="run:records", model_version="fixture/1.0.0",
        model_hash=canonical_sha256({"fixture": "records"}),
    )
    reader = indexed_corpus_reader(preflight.corpus, source, project_id=preflight.base_identity.project_id)
    prepared = prepare_discovery_corpus(preflight, reader=reader, cache_dir=tmp_path / "cache")
    return preflight, reader, prepared


def _run(tmp_path, prepared, client=None, **changes):
    return run_discovery(
        prepared, client=client or DiscoveryClient(), model_version="fixture/1.0.0",
        model_hash=canonical_sha256({"fixture": "records"}), cache_dir=tmp_path / "cache",
        budget=DiscoveryBudget(max_calls=200, max_tokens=20_000_000, **changes),
    )


def test_every_document_chunk_is_discovered_and_consolidated_with_provenance(tmp_path):
    preflight, reader, prepared = _prepared(tmp_path, files=3, paragraphs=7)
    client = DiscoveryClient()
    run = _run(tmp_path, prepared, client, max_chunk_chars=128, fan_in=2)
    assert run.status == "complete"
    assert len(run.document_summaries) == 3
    assert {item.chunk.source_file_id for item in run.chunks} == {item.source_file_id for item in preflight.corpus.entries}
    assert all(item.status == "processed" for item in run.chunks)
    assert all(len(node.child_ids) <= 2 for node in run.summaries)
    assert all(item.response for item in run.chunks)
    assert len(client.calls) == run.model_call_count
    assert "grounding both its owner and value" in client.calls[0]["system"]
    assert "Never invent, concatenate" in client.calls[0]["system"]
    assert "downstream validation may leave them unverified" in client.calls[0]["system"]
    assert run.semantic_recall == "not_claimed"
    validate_discovery(run, source_path=preflight.source_path, reader=reader)
    context = discovery_design_context(run)
    assert context["source_count"] == 3
    assert context["chunk_count"] == len(run.chunks)
    child = context["retrieved_nodes"][0]["child_ids"][0]
    assert discovery_design_context(run, node_ids=[child])["retrieved_nodes"][0]["node_id"] == child
    leaf = discovery_design_context(run, node_ids=[run.chunks[0].chunk.chunk_id])
    assert leaf["retrieved_nodes"][0]["kind"] == "verified_chunk_observations"
    assert leaf["retrieved_nodes"][0]["response"]["candidates"]
    with pytest.raises(ValueError, match="bound"):
        discovery_design_context(run, max_chars=10)


def test_source_units_match_the_exact_shared_materializer_and_keep_context(tmp_path):
    preflight, reader, prepared = _prepared(tmp_path, files=2)
    actual = []
    for entry in preflight.corpus.entries:
        units, _, _ = materialize_corpus_entry(
            entry, reader, base_identity=preflight.base_identity,
            corpus_manifest_id=preflight.corpus.source_corpus_manifest_id,
        )
        actual.extend(units)
    assert prepared.source_units == actual
    chunks = plan_discovery_chunks(prepared, DiscoveryBudget())
    assert any(chunk.context_unit_ids for chunk in chunks)
    assert all(unit.locator.blob_uri.startswith("https://fabric-kg.invalid/assets/") for unit in actual)


def test_empty_results_are_counted_and_resumed_without_new_model_calls(tmp_path):
    _, _, prepared = _prepared(tmp_path, files=2)
    run = _run(tmp_path, prepared, DiscoveryClient(empty=True))
    assert run.status == "complete"
    assert all(item.status == "no_candidates" for item in run.chunks)
    path = tmp_path / "discovery.json"
    save_discovery(path, run)
    loaded = load_discovery(path)
    assert loaded == run
    resumed_client = DiscoveryClient(false_quote=True)
    resumed = _run(tmp_path, loaded.prepared, resumed_client)
    assert resumed.model_call_count == len(resumed_client.calls) == 0
    assert resumed.reused_response_count == len(run.chunks) + len(run.summaries)
    assert all(discovery_raw_responses(resumed, unit.source_unit_id) for unit in prepared.source_units)


def test_budget_exhaustion_is_partial_and_resume_only_calls_missing_work(tmp_path):
    _, _, prepared = _prepared(tmp_path, files=2)
    initial = run_discovery(
        prepared, client=DiscoveryClient(), model_version="fixture/1.0.0",
        model_hash=canonical_sha256({"fixture": "records"}), cache_dir=tmp_path / "cache",
        budget=DiscoveryBudget(max_calls=1, max_tokens=20_000_000, max_concurrency=1),
    )
    assert initial.status == "partial"
    assert initial.model_call_count == 1
    assert any(item.status == "deferred" for item in initial.chunks)
    with pytest.raises(ValueError, match="complete discovery"):
        discovery_design_context(initial)
    resumed = _run(tmp_path, prepared, max_concurrency=1)
    assert resumed.status == "complete"
    assert resumed.reused_response_count >= 1


def test_false_quotes_are_quarantined_not_fake_empty_success_even_after_rehash(tmp_path):
    _, _, prepared = _prepared(tmp_path)
    failed = _run(tmp_path, prepared, DiscoveryClient(false_quote=True))
    assert failed.status == "complete"
    assert all(item.status == "processed" for item in failed.chunks)
    assert all(not item.response.candidates for item in failed.chunks)
    assert all(item.candidate_grounding[0].disposition == "quarantined" for item in failed.chunks)
    assert discovery_grounding_report(failed)["grounding_quality"] == "gaps"
    assert discovery_raw_responses(failed, prepared.source_units[0].source_unit_id)
    valid = _run(tmp_path / "valid", prepared)
    raw = valid.model_dump(mode="json")
    raw["chunks"][0]["raw_response"]["candidates"][0]["anchors"][0]["quote"] = "Forged quote"
    raw["chunks"][0]["artifact_hash"] = canonical_sha256({
        key: value for key, value in raw["chunks"][0].items() if key != "artifact_hash"
    })
    raw["artifact_hash"] = canonical_sha256({key: value for key, value in raw.items() if key != "artifact_hash"})
    with pytest.raises(ValueError, match="quote|grounding"):
        DiscoveryRun.model_validate(raw)


def test_current_source_bytes_are_required_for_discovery_reuse_and_design(tmp_path):
    preflight, _, prepared = _prepared(tmp_path)
    run = _run(tmp_path, prepared)
    next(preflight.source_path.glob("*.html")).write_text("<p>Changed bytes</p>")
    with pytest.raises(ValueError):
        validate_discovery(run, source_path=preflight.source_path)
    with pytest.raises(ValueError):
        _run(tmp_path, prepared)
    with pytest.raises(ValueError):
        generate_domain_design(preflight, discovery=run, client=Client(_sketch()))


def test_sealed_reuse_checks_bytes_and_hash_without_reparsing(tmp_path, monkeypatch):
    preflight, _, prepared = _prepared(tmp_path)
    run = _run(tmp_path, prepared)
    def forbidden(*args, **kwargs):
        raise AssertionError("Binding-only reuse must not parse prepared units again")
    monkeypatch.setattr("fabric_kg_builder.domain.discovery.materialize_corpus_entry", forbidden)
    validate_discovery(
        run, source_path=preflight.source_path, reparse=False, expected_run_hash=run.run_hash,
    )
    with pytest.raises(ValueError, match="sealed run hash"):
        validate_discovery(run, source_path=preflight.source_path, reparse=False, expected_run_hash="0" * 64)
    with pytest.raises(AssertionError, match="must not parse"):
        validate_discovery(run, source_path=preflight.source_path, reparse=True)
    next(preflight.source_path.glob("*.html")).write_text("<p>Changed source bytes</p>")
    with pytest.raises(ValueError, match="source corpus"):
        validate_discovery(run, source_path=preflight.source_path, reparse=False)


def test_discovery_design_compile_approval_binds_full_run_without_sampling(tmp_path, monkeypatch):
    preflight, _, prepared = _prepared(tmp_path, files=2)
    run = _run(tmp_path, prepared)
    def forbidden(*args, **kwargs):
        raise AssertionError("full discovery path must not call the legacy sampler")
    monkeypatch.setattr("fabric_kg_builder.domain.design.build_l1_design_artifacts", forbidden)
    monkeypatch.setattr("fabric_kg_builder.domain.stage.build_l1_design_artifacts", forbidden)
    monkeypatch.setattr("fabric_kg_builder.enrichment.schema2_sources.IndexedSourceCorpusReader.read", forbidden)
    client = Client(_sketch())
    original = preflight.intake.model_dump_json()
    draft = generate_domain_design(preflight, discovery=run, client=client, description="Operator context.")
    assert draft.artifact_version == "2.0.0"
    assert draft.discovery.run_hash == run.run_hash
    assert run.run_hash in client.calls[0]["user"]
    path = tmp_path / "design.json"
    save_design_artifact(path, draft)
    draft = load_domain_design(path)
    compiled = compile_domain_design(draft, evaluate_domain_design(draft), preflight=preflight)
    assert compiled.model_call_count == 0
    assert {unit.source_unit_id for unit in compiled.source_units} == {unit.source_unit_id for unit in prepared.source_units}
    state, domain = tmp_path / "l1", tmp_path / "domain.yaml"
    finalize_l1_stage(compiled, decision=None, actor=None, state_root=state, domain_path=domain)
    approve_persisted_l1_draft(
        actor="discovery-reviewer", state_root=state, domain_path=domain,
        expected_project_id=preflight.base_identity.project_id,
        expected_run_id=preflight.run_id, expected_proposal_hash=compiled.proposal.proposal_hash,
    )
    approved = load_domain_contract(domain)
    assert approved.discovery_run_hash == run.run_hash
    assert approved.approval.status == "approved"
    assert "Operator context." in approved.business.organization_context
    assert preflight.intake.model_dump_json() == original


def test_default_requires_discovery_and_explicit_legacy_mode_still_works(tmp_path):
    preflight = _preflight(tmp_path)
    with pytest.raises(DomainDesignError, match="Complete discovery is required"):
        generate_domain_design(preflight, client=Client(_sketch()))
    legacy = generate_domain_design(preflight, client=Client(_sketch()), sample_only=True)
    assert legacy.artifact_version == "1.0.0"
    assert "discovery" not in legacy.model_dump(mode="json")
    assert "discovery_node_ids" not in legacy.model_dump(mode="json")


def test_discovery_precedes_intake_and_source_ids_survive_later_design_run(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "records.html").write_text("<p>A record describes a subject.</p>")
    neutral = preflight_discovery_inputs(source_path=source, project_id="project:records", run_id="run:discovery")
    assert not hasattr(neutral, "intake")
    reader = indexed_corpus_reader(neutral.corpus, source, project_id="project:records")
    prepared = prepare_discovery_corpus(neutral, reader=reader, cache_dir=tmp_path / "cache")
    run = _run(tmp_path, prepared)
    later = preflight_l1_inputs(
        source_path=source, intake_raw=_intake(), project_id="project:records",
        run_id="run:later-design", model_version="fixture/1.0.0",
        model_hash=canonical_sha256({"fixture": "records"}),
    )
    client = Client(_sketch())
    draft = generate_domain_design(
        later, discovery=run, client=client,
        discovery_node_ids=[run.chunks[0].chunk.chunk_id],
    )
    assert run.corpus_summary_id in client.calls[0]["user"]
    assert run.chunks[0].chunk.chunk_id in client.calls[0]["user"]
    assert {unit.identity.run_id for unit in draft.samples.source_units} == {"run:later-design"}
    assert [unit.source_unit_id for unit in draft.samples.source_units] == [unit.source_unit_id for unit in prepared.source_units]
    assert [unit.text for unit in draft.samples.source_units] == [unit.text for unit in prepared.source_units]
    compiled = compile_domain_design(draft, evaluate_domain_design(draft), preflight=later)
    assert compiled.proposal.draft_contract.discovery_run_hash == run.run_hash


def test_prepared_source_cache_avoids_reparsing_successful_entries(tmp_path, monkeypatch):
    preflight, reader, prepared = _prepared(tmp_path, files=3)
    monkeypatch.setattr(reader, "read", lambda entry: pytest.fail("Prepared bytes should be reused, not parsed again"))
    cached = prepare_discovery_corpus(preflight, reader=reader, cache_dir=tmp_path / "cache")
    assert cached == prepared


def test_partial_preparation_resume_retries_only_failed_sources(tmp_path, monkeypatch):
    import shutil
    preflight, reader, _ = _prepared(tmp_path, files=2)
    shutil.rmtree(tmp_path / "cache")
    original_read = reader.read
    failed_id = preflight.corpus.entries[0].source_file_id
    def failing(entry):
        if entry.source_file_id == failed_id:
            raise ValueError("Transient offline parser failure")
        return original_read(entry)
    monkeypatch.setattr(reader, "read", failing)
    partial = prepare_discovery_corpus(preflight, reader=reader, cache_dir=tmp_path / "cache")
    assert sorted(source.status for source in partial.sources) == ["failed", "processed"]
    calls = []
    def counted(entry):
        calls.append(entry.source_file_id)
        return original_read(entry)
    monkeypatch.setattr(reader, "read", counted)
    complete = prepare_discovery_corpus(preflight, reader=reader, cache_dir=tmp_path / "cache")
    assert calls == [failed_id]
    assert all(source.status == "processed" for source in complete.sources)


def test_failed_preparation_run_resumes_without_reparsing_or_reobserving_success(tmp_path, monkeypatch):
    import shutil
    preflight, reader, _ = _prepared(tmp_path, files=2)
    shutil.rmtree(tmp_path / "cache")
    original_read = reader.read
    failed_id = preflight.corpus.entries[0].source_file_id
    def fail_one(entry):
        if entry.source_file_id == failed_id:
            raise ValueError("Transient preparation failure")
        return original_read(entry)
    monkeypatch.setattr(reader, "read", fail_one)
    partial_prepared = prepare_discovery_corpus(preflight, reader=reader, cache_dir=tmp_path / "cache")
    prior = _run(tmp_path, partial_prepared)
    assert prior.status == "partial"
    prior_path = tmp_path / "partial-run.json"
    save_discovery(prior_path, prior)
    original_bytes = prior_path.read_bytes()
    attempts = {
        path: path.read_bytes() for path in (tmp_path / "cache" / "prepared").glob("partial-*.json")
    }
    reads = []
    def recover(entry):
        reads.append(entry.source_file_id)
        if entry.source_file_id != failed_id:
            pytest.fail("Successful prepared source must not be parsed again")
        return original_read(entry)
    monkeypatch.setattr(reader, "read", recover)
    recovered = resume_discovery_preparation(
        load_discovery(prior_path), source_path=preflight.source_path,
        reader=reader, cache_dir=tmp_path / "cache",
    )
    assert reads == [failed_id]
    assert recovered.prepared_hash != prior.prepared.prepared_hash
    client = DiscoveryClient()
    resumed = _run(tmp_path, recovered, client)
    assert resumed.status == "complete"
    by_id = {item.chunk.chunk_id: item for item in resumed.chunks}
    for old in prior.chunks:
        assert by_id[old.chunk.chunk_id] == old
    for request in client.calls:
        payload = json.loads(request["user"])["input"]
        if "chunk" in payload:
            assert payload["chunk"]["source_file_id"] == failed_id
        elif payload["level"] == "document":
            assert payload["source_file_id"] == failed_id
    assert resumed.reused_response_count >= len(prior.chunks) + len(prior.summaries)
    assert prior_path.read_bytes() == original_bytes
    assert all(path.read_bytes() == contents for path, contents in attempts.items())


def test_discovery_concurrency_is_bounded(tmp_path):
    import time
    _, _, prepared = _prepared(tmp_path, paragraphs=8)
    class ConcurrentClient(DiscoveryClient):
        active = peak = 0
        def complete_json(self, **request):
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                time.sleep(0.01)
                return super().complete_json(**request)
            finally:
                with self.lock:
                    self.active -= 1
    client = ConcurrentClient()
    run = _run(tmp_path, prepared, client, max_concurrency=2)
    assert run.status == "complete"
    assert client.peak == 2


def test_summary_cannot_silently_drop_provenance_inputs(tmp_path):
    _, _, prepared = _prepared(tmp_path, paragraphs=3)
    class DroppingClient(DiscoveryClient):
        def complete_json(self, **request):
            result = super().complete_json(**request)
            if "covered_input_ids" in result:
                result["covered_input_ids"] = []
            return result
    run = _run(tmp_path, prepared, DroppingClient())
    assert run.status == "partial"
    assert all(item.status == "processed" for item in run.chunks)
    assert any("SUMMARY_INPUT_IDS_MISMATCH" in issue for issue in run.issues)


def test_token_budget_defers_every_chunk_without_calls(tmp_path):
    _, _, prepared = _prepared(tmp_path)
    client = DiscoveryClient()
    run = run_discovery(
        prepared, client=client, model_version="fixture/1.0.0",
        model_hash=canonical_sha256({"fixture": "records"}), cache_dir=tmp_path / "cache",
        budget=DiscoveryBudget(max_tokens=0),
    )
    assert run.status == "partial"
    assert run.model_call_count == run.reserved_tokens == len(client.calls) == 0
    assert all(record.status == "deferred" for record in run.chunks)


@pytest.mark.parametrize("change", ["model", "business_context"])
def test_cache_binding_includes_model_and_influential_business_context(tmp_path, change):
    _, _, prepared = _prepared(tmp_path)
    initial = _run(tmp_path, prepared)
    client = DiscoveryClient()
    changed = run_discovery(
        prepared, client=client, model_version="fixture/1.0.0",
        model_hash=canonical_sha256({"fixture": "changed" if change == "model" else "records"}),
        cache_dir=tmp_path / "cache", budget=DiscoveryBudget(max_tokens=20_000_000),
        business_context={"purpose": "Retain cancelled-record exceptions"} if change == "business_context" else None,
    )
    assert changed.model_call_count == len(client.calls) > 0
    assert changed.reused_response_count == 0
    assert changed.chunks[0].request_hash != initial.chunks[0].request_hash


@pytest.mark.parametrize("partial", [False, True])
def test_discovery_cached_ocr_requires_full_page_coverage(cached_pdf, tmp_path, partial):
    from fabric_kg_builder.domain.discovery import DiscoveryPreflight
    from fabric_kg_builder.sources.docintel_cache import make_cached_layout, store_cached_layout
    case = cached_pdf
    raw = copy.deepcopy(case.raw)
    if partial:
        raw["pages"] = raw["pages"][:1]
        raw["content"] = case.page_text
    store_cached_layout(case.cache, make_cached_layout(case.path.read_bytes(), raw, case.identity))
    preflight = DiscoveryPreflight(source_path=case.path, base_identity=case.corpus.identity, corpus=case.corpus)
    prepared = prepare_discovery_corpus(preflight, reader=case.reader())
    run = _run(tmp_path, prepared)
    if partial:
        assert prepared.sources[0].status == "failed"
        assert "L2_LAYOUT_COVERAGE_INCOMPLETE" in prepared.sources[0].reason
        assert run.status == "partial"
    else:
        assert {unit.locator.page for unit in prepared.source_units} == {1, 2}
        assert run.status == "complete"
        validate_discovery(run, source_path=case.path, reader=case.reader())


def test_cached_ocr_binding_only_validation_rejects_extractor_drift(cached_pdf, tmp_path, monkeypatch):
    from fabric_kg_builder.domain.discovery import DiscoveryPreflight
    from fabric_kg_builder.sources.docintel_cache import make_cached_layout, store_cached_layout
    case = cached_pdf
    cache_path = store_cached_layout(case.cache, make_cached_layout(case.path.read_bytes(), case.raw, case.identity))
    preflight = DiscoveryPreflight(source_path=case.path, base_identity=case.corpus.identity, corpus=case.corpus)
    prepared = prepare_discovery_corpus(preflight, reader=case.reader())
    run = _run(tmp_path, prepared)
    monkeypatch.setattr(
        "fabric_kg_builder.domain.discovery.materialize_corpus_entry",
        lambda *args, **kwargs: pytest.fail("Binding validation must not parse cached pages"),
    )
    validate_discovery(run, source_path=case.path, reader=case.reader(), reparse=False, expected_run_hash=run.run_hash)
    changed = copy.deepcopy(case.identity)
    changed["options"]["features"] = []
    with pytest.raises(ValueError, match="extractor identity"):
        validate_discovery(run, source_path=case.path, reader=case.reader(changed), reparse=False)
    changed_raw = copy.deepcopy(case.raw)
    changed_raw["content"] = "X" + changed_raw["content"][1:]
    changed_record = make_cached_layout(case.path.read_bytes(), changed_raw, case.identity)
    cache_path.write_text(canonical_json(changed_record) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="extractor identity"):
        validate_discovery(run, source_path=case.path, reader=case.reader(), reparse=False)
    with pytest.raises(ValueError, match="successful source OCR"):
        resume_discovery_preparation(run, source_path=case.path, reader=case.reader(), cache_dir=tmp_path / "cache")


def test_preparation_resume_accepts_new_ocr_result_only_for_failed_source(cached_pdf, tmp_path):
    from fabric_kg_builder.domain.discovery import DiscoveryPreflight
    from fabric_kg_builder.sources.docintel_cache import make_cached_layout, store_cached_layout
    case = cached_pdf
    preflight = DiscoveryPreflight(source_path=case.path, base_identity=case.corpus.identity, corpus=case.corpus)
    prepared = prepare_discovery_corpus(preflight, reader=case.reader(), cache_dir=tmp_path / "discovery-cache")
    assert prepared.sources[0].status == "failed"
    prior = _run(tmp_path, prepared)
    assert prior.status == "partial"
    store_cached_layout(case.cache, make_cached_layout(case.path.read_bytes(), case.raw, case.identity))
    recovered = resume_discovery_preparation(
        prior, source_path=case.path, reader=case.reader(), cache_dir=tmp_path / "discovery-cache",
    )
    assert {unit.locator.page for unit in recovered.source_units} == {1, 2}
    assert _run(tmp_path, recovered).status == "complete"


def _entity(local_id, quote, start=0):
    return {
        "candidate_kind": "entity", "local_id": local_id,
        "observed_type": "Observed record", "label": local_id,
        "anchors": [{"span_start": start, "span_end": start + len(quote), "quote": quote}],
    }


def test_candidate_grounding_retains_good_and_quarantines_bad_without_losing_raw(tmp_path):
    from fabric_kg_builder.domain import discovery as core
    _, _, prepared = _prepared(tmp_path)
    chunk = plan_discovery_chunks(prepared, DiscoveryBudget())[0]
    unit = next(unit for unit in prepared.source_units if unit.source_unit_id == chunk.source_unit_id)
    raw = {"candidates": [
        _entity("good-original-id", unit.text),
        _entity("bad-original-id", "Text present only in a neighboring document."),
    ]}
    original = copy.deepcopy(raw)
    response, ledger = core._ground_response(raw, unit, chunk)
    assert raw == original
    assert [item.local_id for item in response.candidates] == ["good-original-id"]
    assert [item.local_id for item in ledger] == ["good-original-id", "bad-original-id"]
    assert [item.disposition for item in ledger] == ["verified", "quarantined"]
    assert ledger[0].verified_candidate_index == 0
    assert ledger[1].verified_candidate_index is None
    assert ledger[1].issue_codes == ["ANCHOR_OUTSIDE_PRIMARY_OR_MISMATCH"]
    assert len({item.observation_id for item in ledger}) == 2


def test_design_retains_grounding_gaps_without_using_quarantined_text(tmp_path):
    preflight, _, prepared = _prepared(tmp_path)
    client = DiscoveryClient(false_quote=True)
    run = _run(tmp_path, prepared, client)
    for request in client.calls:
        if "inputs" in json.loads(request["user"])["input"]:
            assert "Fabricated source statement." not in request["user"]
    designer = Client(_sketch())
    draft = generate_domain_design(preflight, discovery=run, client=designer)
    assert "quarantined_candidate_count" in designer.calls[0]["user"]
    assert "Fabricated source statement." not in designer.calls[0]["user"]
    evaluation = evaluate_domain_design(draft)
    assert any(item.code == "discovery_grounding_gaps" for item in evaluation.findings)
    assert all(item.status == "review_needed" for item in evaluation.questions)


@pytest.mark.parametrize("text,quote,start,expected_start", [
    ("Repeated Repeated", "Repeated", 9, 9),
    (" Repeated  Repeated ", " Repeated ", 0, 1),
])
def test_exact_offsets_accept_repeated_quote_without_unique_fallback(tmp_path, text, quote, start, expected_start):
    from fabric_kg_builder.contracts.evidence import SourceUnit
    from fabric_kg_builder.domain import discovery as core
    _, _, prepared = _prepared(tmp_path)
    template = prepared.source_units[0]
    unit = SourceUnit.mint(
        identity=template.identity, unit_kind="paragraph", text=text,
        ordinal=template.ordinal, locator=template.locator,
    )
    chunk = core.DiscoveryChunk(
        chunk_id="test:repeated", source_unit_id=unit.source_unit_id,
        source_file_id=unit.source_file_id, source_text_hash=unit.text_content_hash,
        slice_start=0, slice_end=len(text),
    )
    response, ledger = core._ground_response({"candidates": [_entity("repeated", quote, start)]}, unit, chunk)
    assert ledger[0].disposition == "verified"
    assert response.candidates[0].anchors[0].span_start == expected_start


def test_neighbor_body_is_not_in_new_requests_and_context_quote_is_quarantined(tmp_path):
    from fabric_kg_builder.domain import discovery as core
    _, _, prepared = _prepared(tmp_path, paragraphs=2)
    chunks = plan_discovery_chunks(prepared, DiscoveryBudget())
    chunk = next(item for item in chunks if item.context_unit_ids)
    units = {unit.source_unit_id: unit for unit in prepared.source_units}
    unit = units[chunk.source_unit_id]
    context_id = next(uid for uid in chunk.context_unit_ids if units[uid].text not in unit.text)
    payload = core._chunk_payload(prepared, chunk)
    assert all("text" not in item for item in payload["adjacency_context"])
    assert "governing_anchor" not in payload["chunk"]
    assert payload["primary_text_boundary"]["only_evidence_field"] == "input.text"
    response, ledger = core._ground_response(
        {"candidates": [_entity("context-only", units[context_id].text)]}, unit, chunk,
    )
    assert not response.candidates
    assert ledger[0].disposition == "quarantined"
    assert "ANCHOR_OUTSIDE_PRIMARY_OR_MISMATCH" in ledger[0].issue_codes


def test_overlapping_quote_occurrences_remain_ambiguous_without_exact_offsets(tmp_path):
    from fabric_kg_builder.contracts.evidence import SourceUnit
    from fabric_kg_builder.domain import discovery as core
    _, _, prepared = _prepared(tmp_path)
    template = prepared.source_units[0]
    unit = SourceUnit.mint(
        identity=template.identity, unit_kind="paragraph", text="aaaa",
        ordinal=template.ordinal, locator=template.locator,
    )
    chunk = core.DiscoveryChunk(
        chunk_id="test:overlap", source_unit_id=unit.source_unit_id,
        source_file_id=unit.source_file_id, source_text_hash=unit.text_content_hash,
        slice_start=0, slice_end=4,
    )
    response, ledger = core._ground_response({"candidates": [_entity("overlap", "aaa", 2)]}, unit, chunk)
    assert not response.candidates
    assert ledger[0].issue_codes == ["ANCHOR_AMBIGUOUS"]


def test_public_grounder_preserves_raw_correspondence_and_rejects_source_drift(tmp_path):
    from fabric_kg_builder.domain import discovery as core
    _, _, prepared = _prepared(tmp_path)
    chunk = plan_discovery_chunks(prepared, DiscoveryBudget())[0]
    unit = next(item for item in prepared.source_units if item.source_unit_id == chunk.source_unit_id)
    raw = {"candidates": [
        _entity("bad", "Not a source quotation."),
        _entity("good", unit.text),
    ]}
    original = copy.deepcopy(raw)
    response, ledger = core.ground_discovery_response(raw_response=raw, source_unit=unit, chunk=chunk)
    assert raw == original
    assert [entry.candidate_index for entry in ledger] == [0, 1]
    assert [entry.verified_candidate_index for entry in ledger] == [None, 0]
    assert response.candidates[0].local_id == "good"
    assert (response, ledger) == core.ground_discovery_response(
        raw_response=raw, source_unit=unit, chunk=chunk,
    )
    for change in (
        {"source_unit_id": "source-unit:other"}, {"source_file_id": "source-file:other"},
        {"source_text_hash": "f" * 64}, {"slice_end": unit.codepoint_count + 1},
        {"slice_start": chunk.slice_end},
    ):
        with pytest.raises(ValueError, match="source/chunk binding"):
            core.ground_discovery_response(
                raw_response=raw, source_unit=unit, chunk=chunk.model_copy(update=change),
            )


@pytest.mark.parametrize("first,second", [("E", "e"), ("Straße", "STRASSE")])
def test_casefold_local_id_collisions_and_dependents_are_quarantined(tmp_path, first, second):
    from fabric_kg_builder.domain import discovery as core
    from fabric_kg_builder.enrichment.discovery_reuse import map_discovery_candidates
    from fabric_kg_builder.enrichment.schema2_extraction import compile_closed_vocabulary
    from tests.unit.test_schema2_extraction import _domain
    _, _, prepared = _prepared(tmp_path)
    chunk = plan_discovery_chunks(prepared, DiscoveryBudget())[0]
    unit = next(item for item in prepared.source_units if item.source_unit_id == chunk.source_unit_id)
    anchor = {"span_start": 0, "span_end": len(unit.text), "quote": unit.text}
    raw = {"candidates": [
        _entity(first, unit.text), _entity(second, unit.text), _entity("good", unit.text),
        {"candidate_kind": "property", "owner_local_id": first, "observed_property": "Value",
         "value": "A", "normalized_value": "A", "anchor": anchor},
        {"candidate_kind": "relationship", "source_local_id": second, "target_local_id": "good",
         "observed_predicate": "refers to", "direction": "source_to_target", "anchor": anchor},
        {"candidate_kind": "property", "owner_local_id": "GOOD", "observed_property": "Value",
         "value": "B", "normalized_value": "B", "anchor": anchor},
    ]}
    original = copy.deepcopy(raw)
    response, ledger = core.ground_discovery_response(raw_response=raw, source_unit=unit, chunk=chunk)
    assert raw == original
    assert [entry.disposition for entry in ledger] == [
        "quarantined", "quarantined", "verified", "quarantined", "quarantined", "verified",
    ]
    assert ledger[0].issue_codes == ledger[1].issue_codes == ["LOCAL_ENTITY_ID_DUPLICATED"]
    assert ledger[3].issue_codes == ["OWNER_NOT_GROUNDED"]
    assert ledger[4].issue_codes == ["ENDPOINT_NOT_GROUNDED"]
    assert [entry.verified_candidate_index for entry in ledger] == [None, None, 0, None, None, 1]
    domain = _domain()
    mapped = map_discovery_candidates(
        response.model_dump(mode="json"), chunk_id=chunk.chunk_id,
        vocabulary=compile_closed_vocabulary(domain), contract=domain,
    )
    assert len(mapped.response["candidates"]) == 2


def test_grounding_20_cache_upgrades_without_calls_or_original_identity_loss(tmp_path):
    from fabric_kg_builder.domain import discovery as core
    _, _, prepared = _prepared(tmp_path)
    budget = DiscoveryBudget(max_calls=200, max_tokens=20_000_000)
    model_hash = canonical_sha256({"fixture": "records"})
    units = {unit.source_unit_id: unit for unit in prepared.source_units}
    records, originals = [], {}
    for chunk in plan_discovery_chunks(prepared, budget):
        unit = units[chunk.source_unit_id]
        raw = {"candidates": [_entity("E", unit.text), _entity("e", unit.text), _entity("good", unit.text)]}
        response, ledger = core._ground_response(raw, unit, chunk, casefold_references=False)
        _, key = core._chunk_request(
            prepared, chunk, units, budget, "fixture/1.0.0", model_hash, None,
            core.DISCOVERY_PROMPT_VERSION,
        )
        record = core._seal(
            core.ChunkObservation, chunk=chunk, request_hash=key, status="processed",
            raw_response=raw, response=response, candidate_grounding=ledger,
            verifier_version=core.LEGACY_GROUNDING_VERSION,
            request_prompt_version=core.DISCOVERY_PROMPT_VERSION,
        )
        records.append(record)
        path = core.discovery_observation_cache_path(tmp_path / "cache", record)
        assert path.parent.name == "v2"
        core._write(path, record)
        originals[path] = path.read_bytes()
    prior = core._seal(
        DiscoveryRun, verifier_version=core.LEGACY_GROUNDING_VERSION,
        prepared=prepared, model_version="fixture/1.0.0", model_hash=model_hash,
        budget=budget, chunks=records, summaries=[], document_summaries={},
        corpus_summary_id=None, status="partial", model_call_count=len(records),
        reserved_tokens=0, reused_response_count=0,
    )
    path = tmp_path / "grounding20.json"
    save_discovery(path, prior)
    originals[path] = path.read_bytes()
    class NoCalls:
        def complete_json(self, **request):
            pytest.fail("A verifier upgrade must not repeat received model calls")
    current = run_discovery(
        prepared, prior=load_discovery(path), client=NoCalls(), model_version=prior.model_version,
        model_hash=prior.model_hash, cache_dir=tmp_path / "cache",
        budget=budget.model_copy(update={"max_calls": 0}),
    )
    assert current.model_call_count == 0
    assert current.verifier_version == core.GROUNDING_VERSION
    for old, new in zip(records, current.chunks, strict=True):
        assert new.raw_response == old.raw_response
        assert [entry.observation_id for entry in new.candidate_grounding] == [
            entry.observation_id for entry in old.candidate_grounding
        ]
        assert [entry.disposition for entry in new.candidate_grounding] == ["quarantined", "quarantined", "verified"]
        assert new.response.candidates[0].local_id == "good"
        assert core.discovery_observation_cache_path(tmp_path / "cache", new).parent.name == "v2.2"
    assert all(path.read_bytes() == data for path, data in originals.items())


def test_cached_legacy_failed_raw_revalidates_without_new_chunk_model_calls(tmp_path):
    from fabric_kg_builder.domain import discovery as core
    _, _, prepared = _prepared(tmp_path)
    budget = DiscoveryBudget(max_calls=200, max_tokens=20_000_000)
    model_hash = canonical_sha256({"fixture": "records"})
    units = {unit.source_unit_id: unit for unit in prepared.source_units}
    records, original_files = [], {}
    for chunk in plan_discovery_chunks(prepared, budget):
        _, key = core._chunk_request(
            prepared, chunk, units, budget, "fixture/1.0.0", model_hash, None,
            core.LEGACY_DISCOVERY_PROMPT_VERSION,
        )
        raw = {"candidates": [
            _entity("good", units[chunk.source_unit_id].text),
            _entity("bad", "A quotation that is not in the primary page."),
        ]}
        record = core._seal(
            core.ChunkObservation, chunk=chunk, request_hash=key, status="failed",
            raw_response=raw, reason="discovery quote does not uniquely match its source slice",
        )
        records.append(record)
        path = core.discovery_observation_cache_path(tmp_path / "cache", record)
        core._write(path, record)
        original_files[path] = path.read_bytes()
    prior = core._seal(
        DiscoveryRun, prompt_version=core.LEGACY_DISCOVERY_PROMPT_VERSION,
        prepared=prepared, model_version="fixture/1.0.0", model_hash=model_hash,
        budget=budget, chunks=records, summaries=[], document_summaries={},
        corpus_summary_id=None, status="partial", model_call_count=len(records),
        reserved_tokens=0, reused_response_count=0,
    )
    path = tmp_path / "legacy-discovery.json"
    save_discovery(path, prior)
    original_files[path] = path.read_bytes()
    class NoCalls:
        def complete_json(self, **request):
            pytest.fail("Revalidation must not call a model for received raw responses")
    current = run_discovery(
        prepared, prior=load_discovery(path), client=NoCalls(), model_version=prior.model_version,
        model_hash=prior.model_hash, cache_dir=tmp_path / "cache",
        budget=budget.model_copy(update={"max_calls": 0}),
    )
    assert current.model_call_count == 0
    assert all(item.status == "processed" for item in current.chunks)
    assert all(len(item.response.candidates) == 1 for item in current.chunks)
    assert [item.raw_response for item in current.chunks] == [item.raw_response for item in records]
    assert [item.request_hash for item in current.chunks] == [item.request_hash for item in records]
    assert all(item.request_prompt_version == core.LEGACY_DISCOVERY_PROMPT_VERSION for item in current.chunks)
    assert all(item.verifier_version == core.GROUNDING_VERSION for item in current.chunks)
    assert all(core.discovery_observation_cache_path(tmp_path / "cache", item).exists() for item in current.chunks)
    report = discovery_grounding_report(current)
    assert report["all_chunks_accounted"]
    assert report["verified_candidate_count"] == report["quarantined_candidate_count"] == len(records)
    assert current.status == "partial"  # No budget for new consolidation requests.
    assert all(path.read_bytes() == contents for path, contents in original_files.items())
    resumed = run_discovery(
        prepared, prior=current, client=NoCalls(), model_version=prior.model_version,
        model_hash=prior.model_hash, cache_dir=tmp_path / "cache",
        budget=budget.model_copy(update={"max_calls": 0}),
    )
    assert resumed.chunks == current.chunks
    assert resumed.model_call_count == 0
