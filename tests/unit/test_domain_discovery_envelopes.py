"""Envelope recovery and durable failed-summary accounting, entirely offline."""

from __future__ import annotations

import copy
import json

import pytest

from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.domain import discovery as core
from fabric_kg_builder.domain.design import evaluate_domain_design, generate_domain_design
from tests.unit.test_domain_design import Client, _sketch
from tests.unit.test_domain_discovery import DiscoveryClient, _entity, _prepared, _run


class NoCalls:
    def complete_json(self, **request):
        pytest.fail("No model calls are allowed for received raw/cache reuse")


class ExtraFieldsClient(DiscoveryClient):
    def __init__(self, *, empty=False):
        super().__init__()
        self.empty_array = empty

    def complete_json(self, **request):
        payload = json.loads(request["user"])["input"]
        if "inputs" in payload:
            return super().complete_json(**request)
        self.calls.append(copy.deepcopy(request))
        candidates = [] if self.empty_array else [
            _entity("good", payload["text"], payload["offset_base"]),
            {"local_id": "missing-kind", "observed_type": "Record", "label": "No invented kind",
             "anchors": [{"span_start": payload["offset_base"],
                          "span_end": payload["offset_base"] + len(payload["text"]), "quote": payload["text"]}]},
        ]
        return {
            "candidates": candidates, "candidate_kind": "entity", "local_id": "stray",
            "label": "DO_NOT_SUMMARIZE_UNTYPED_EXTRA", "value": {"uncertain": [1, 2]},
        }


@pytest.mark.parametrize("empty", [False, True])
def test_extra_fields_are_separate_quarantine_not_invented_candidates(tmp_path, empty):
    preflight, _, prepared = _prepared(tmp_path)
    client = ExtraFieldsClient(empty=empty)
    run = _run(tmp_path, prepared, client)
    assert run.status == "complete"
    for record in run.chunks:
        assert record.status == "processed"
        assert record.candidate_grounding_scope == "raw_response.candidates"
        assert len(record.candidate_grounding) == (0 if empty else 2)
        assert len(record.response.candidates) == (0 if empty else 1)
        if not empty:
            assert record.candidate_grounding[1].issue_codes == ["CANDIDATE_SCHEMA_INVALID"]
        anomaly, = record.envelope_anomalies
        assert anomaly.disposition == "quarantined"
        assert anomaly.issue_codes == ["EXTRA_TOP_LEVEL_FIELDS"]
        assert anomaly.field_names == ["candidate_kind", "label", "local_id", "value"]
        assert anomaly.raw_response_hash == canonical_sha256(record.raw_response)
        assert anomaly.extra_fields_hash == canonical_sha256({
            key: value for key, value in record.raw_response.items() if key != "candidates"
        })
        assert anomaly.observation_id not in {item.observation_id for item in record.candidate_grounding}
        assert record.raw_response["label"] == "DO_NOT_SUMMARIZE_UNTYPED_EXTRA"
    report = core.discovery_grounding_report(run)
    assert report["raw_candidate_count"] == len(run.chunks) * (0 if empty else 2)
    assert report["envelope_anomaly_count"] == len(run.chunks)
    assert report["envelope_quarantined_field_count"] == len(run.chunks) * 4
    assert report["grounding_quality"] == "gaps"
    assert "candidate_ledger_entry_count" not in report
    ledger_report = core.discovery_grounding_report(run, include_ledger_accounting=True)
    assert ledger_report["candidate_ledger_entry_count"] == report["raw_candidate_count"]
    assert ledger_report["received_array_ledger_complete"]
    assert ledger_report["unaccounted_raw_candidate_count"] == 0
    assert ledger_report["envelope_anomaly_count"] == len(run.chunks)
    for request in client.calls:
        if "inputs" in json.loads(request["user"])["input"]:
            assert "DO_NOT_SUMMARIZE_UNTYPED_EXTRA" not in request["user"]
            assert "envelope_anomalies" in request["user"]
    designer = Client(_sketch())
    draft = generate_domain_design(preflight, discovery=run, client=designer)
    assert "DO_NOT_SUMMARIZE_UNTYPED_EXTRA" not in designer.calls[0]["user"]
    assert "envelope_anomaly_count" in designer.calls[0]["user"]
    assert any(item.code == "discovery_envelope_anomalies" for item in evaluate_domain_design(draft).findings)
    tampered = run.model_dump(mode="json")
    tampered["chunks"][0].pop("envelope_anomalies")
    tampered["chunks"][0]["artifact_hash"] = canonical_sha256({
        key: value for key, value in tampered["chunks"][0].items()
        if key != "artifact_hash"
    })
    tampered["artifact_hash"] = canonical_sha256({
        key: value for key, value in tampered.items() if key != "artifact_hash"
    })
    with pytest.raises(ValueError, match="grounding/accounting"):
        core.DiscoveryRun.model_validate(tampered)


@pytest.mark.parametrize("raw", [
    {"candidate_kind": "entity", "label": "Not an array"},
    {"candidates": {"candidate_kind": "entity"}},
])
def test_no_candidate_array_is_not_fabricated_from_root_fields(tmp_path, raw):
    _, _, prepared = _prepared(tmp_path)
    chunk = core.plan_discovery_chunks(prepared, core.DiscoveryBudget())[0]
    unit = next(item for item in prepared.source_units if item.source_unit_id == chunk.source_unit_id)
    with pytest.raises(ValueError, match="contain a candidates array"):
        core.ground_discovery_response(raw_response=raw, source_unit=unit, chunk=chunk)


def _legacy_record(prepared, chunk, budget, *, extras=False, missing=False):
    units = {unit.source_unit_id: unit for unit in prepared.source_units}
    _, key = core._chunk_request(
        prepared, chunk, units, budget, "fixture/1.0.0",
        canonical_sha256({"fixture": "records"}), None, core.DISCOVERY_PROMPT_VERSION,
    )
    raw = {"candidates": [_entity("good", units[chunk.source_unit_id].text)]}
    if extras:
        raw.update(label="stray original", normalized_value=9, owner_local_id="good")
    values = dict(
        chunk=chunk, request_hash=key, raw_response=None if missing else raw,
        verifier_version=core.STRICT_ENVELOPE_GROUNDING_VERSION,
        request_prompt_version=core.DISCOVERY_PROMPT_VERSION,
    )
    if extras or missing:
        values.update(status="failed", reason="missing response" if missing else "discovery response must contain a candidates array")
    else:
        response, ledger = core._ground_response(raw, units[chunk.source_unit_id], chunk, allow_envelope_extras=False)
        values.update(status="processed", response=response, candidate_grounding=ledger)
    return core._seal(core.ChunkObservation, **values)


def test_21_failed_extra_envelope_recovers_without_calls_or_rewriting_good_leaves(tmp_path, monkeypatch):
    _, _, prepared = _prepared(tmp_path, files=2)
    budget = core.DiscoveryBudget(max_calls=200, max_tokens=20_000_000)
    records, originals = [], {}
    for index, chunk in enumerate(core.plan_discovery_chunks(prepared, budget)):
        record = _legacy_record(prepared, chunk, budget, extras=index == 0, missing=index == 1)
        records.append(record)
        path = core.discovery_observation_cache_path(tmp_path / "cache", record)
        core._write(path, record)
        originals[path] = path.read_bytes()
    prior = core._seal(
        core.DiscoveryRun, verifier_version=core.STRICT_ENVELOPE_GROUNDING_VERSION,
        prepared=prepared, model_version="fixture/1.0.0", model_hash=canonical_sha256({"fixture": "records"}),
        budget=budget, chunks=records, summaries=[], document_summaries={}, corpus_summary_id=None,
        status="partial", model_call_count=len(records), reserved_tokens=0, reused_response_count=0,
    )
    path = tmp_path / "legacy21.json"
    core.save_discovery(path, prior)
    originals[path] = path.read_bytes()
    prior_accounting = core.discovery_grounding_report(prior, include_ledger_accounting=True)
    assert prior_accounting["unaccounted_raw_array_count"] == 1
    assert prior_accounting["unaccounted_raw_candidate_count"] == 1
    assert prior_accounting["missing_raw_response_count"] == 1
    assert not prior_accounting["received_array_ledger_complete"]
    def no_parse(*args, **kwargs):
        pytest.fail("Received cache revalidation must reuse prepared units")
    monkeypatch.setattr("fabric_kg_builder.enrichment.schema2_sources.IndexedSourceCorpusReader.read", no_parse)
    current = core.run_discovery(
        prepared, prior=core.load_discovery(path), client=NoCalls(),
        model_version=prior.model_version, model_hash=prior.model_hash, cache_dir=tmp_path / "cache",
        budget=budget.model_copy(update={"max_calls": 0}),
    )
    assert current.model_call_count == 0
    assert current.chunks[0].raw_response == records[0].raw_response
    assert current.chunks[0].request_hash == records[0].request_hash
    assert current.chunks[0].envelope_anomalies
    assert current.chunks[0].response.candidates[0].local_id == "good"
    assert current.chunks[0].verifier_version == core.GROUNDING_VERSION
    assert core.discovery_observation_cache_path(tmp_path / "cache", current.chunks[0]).parent.name == "v2.2"
    assert current.chunks[1].raw_response is None
    assert current.chunks[1].status == "deferred"
    assert current.chunks[2:] == records[2:]
    accounting = core.discovery_grounding_report(current, include_ledger_accounting=True)
    assert accounting["candidate_ledger_entry_count"] == len(records) - 1
    assert accounting["received_array_ledger_complete"]
    assert accounting["unaccounted_raw_array_count"] == accounting["unaccounted_raw_candidate_count"] == 0
    assert accounting["missing_raw_response_count"] == 1
    assert accounting["invalid_raw_envelope_count"] == 0
    repeated = core.run_discovery(
        prepared, prior=current, client=NoCalls(), model_version=prior.model_version,
        model_hash=prior.model_hash, cache_dir=tmp_path / "cache", budget=budget.model_copy(update={"max_calls": 0}),
    )
    assert repeated.chunks == current.chunks
    assert all(path.read_bytes() == data for path, data in originals.items())


def test_compatible_21_leaves_preserve_successful_summary_cache_keys(tmp_path):
    _, _, prepared = _prepared(tmp_path, files=2)
    budget = core.DiscoveryBudget(max_calls=200, max_tokens=20_000_000)
    for chunk in core.plan_discovery_chunks(prepared, budget):
        record = _legacy_record(prepared, chunk, budget)
        core._write(tmp_path / "cache" / "chunks" / f"{record.request_hash}.json", record)
        core._write(core.discovery_observation_cache_path(tmp_path / "cache", record), record)
    complete = _run(tmp_path, prepared)
    values = {
        name: getattr(complete, name) for name in core.DiscoveryRun.model_fields if name != "artifact_hash"
    }
    values["verifier_version"] = core.STRICT_ENVELOPE_GROUNDING_VERSION
    prior = core._seal(core.DiscoveryRun, **values)
    assert all(item.verifier_version == core.STRICT_ENVELOPE_GROUNDING_VERSION for item in prior.chunks)
    originals = {path: path.read_bytes() for path in (tmp_path / "cache").rglob("*.json")}
    current = core.run_discovery(
        prepared, prior=prior, client=NoCalls(), model_version=prior.model_version,
        model_hash=prior.model_hash, cache_dir=tmp_path / "cache", budget=budget.model_copy(update={"max_calls": 0}),
    )
    assert current.status == "complete"
    assert current.model_call_count == 0
    assert current.chunks == prior.chunks
    assert current.summaries == prior.summaries
    assert current.corpus_summary_id == prior.corpus_summary_id
    assert all(path.read_bytes() == data for path, data in originals.items())


@pytest.mark.parametrize("failure_kind", ["ids", "length", "schema", "array"])
def test_failed_summary_raw_is_durable_and_only_missing_summaries_retry(tmp_path, failure_kind):
    _, _, prepared = _prepared(tmp_path, files=2)
    class FailingSummaryClient(DiscoveryClient):
        failed_raw = None
        failed_request = None
        def complete_json(self, **request):
            payload = json.loads(request["user"])["input"]
            if "inputs" in payload and self.failed_raw is None:
                raw = {
                    "summary": "A bounded summary.", "covered_input_ids": [item["id"] for item in payload["inputs"]],
                }
                if failure_kind == "ids":
                    raw["covered_input_ids"] = ["model-invented-child"]
                elif failure_kind == "length":
                    raw["summary"] = "X" * (request["json_schema"]["properties"]["summary"]["maxLength"] + 1)
                elif failure_kind == "schema":
                    raw["extra"] = {"original": "Preserve this schema-invalid response"}
                else:
                    raw = [raw]
                self.failed_raw = copy.deepcopy(raw)
                self.failed_request = copy.deepcopy(request)
                self.calls.append(copy.deepcopy(request))
                return raw
            return super().complete_json(**request)
    client = FailingSummaryClient()
    prior = _run(tmp_path, prepared, client)
    assert prior.status == "partial"
    failure, = prior.failed_summaries
    assert failure.raw_response == client.failed_raw
    assert failure.child_ids == [
        item["id"] for item in json.loads(client.failed_request["user"])["input"]["inputs"]
    ]
    assert failure.reason
    if failure_kind == "ids":
        assert "SUMMARY_INPUT_IDS_MISMATCH" in failure.reason
        assert failure.raw_response["covered_input_ids"] != failure.child_ids
    if failure_kind == "length":
        assert "SUMMARY_LENGTH_EXCEEDED" in failure.reason
    failed_path = core.discovery_summary_failure_cache_path(tmp_path / "cache", failure)
    assert core.FailedDiscoverySummary.model_validate_json(failed_path.read_text()) == failure
    failed_bytes = failed_path.read_bytes()
    path = tmp_path / "summary-failure-run.json"
    core.save_discovery(path, prior)
    prior_bytes = path.read_bytes()
    report = core.discovery_grounding_report(prior)
    assert report["failed_summary_attempt_count"] == report["failed_summary_responses_received"] == 1
    assert report["summary_semantic_coverage"] == "not_verified"
    bad = prior.model_dump(mode="json")
    bad["failed_summaries"][0]["child_ids"] = ["unknown-child"]
    bad["failed_summaries"][0]["artifact_hash"] = canonical_sha256({
        key: value for key, value in bad["failed_summaries"][0].items() if key != "artifact_hash"
    })
    bad["artifact_hash"] = canonical_sha256({key: value for key, value in bad.items() if key != "artifact_hash"})
    with pytest.raises(ValueError, match="failed summary input provenance"):
        core.DiscoveryRun.model_validate(bad)
    zero_call = core.run_discovery(
        prepared, prior=prior, client=NoCalls(), model_version=prior.model_version,
        model_hash=prior.model_hash, cache_dir=tmp_path / "cache",
        budget=prior.budget.model_copy(update={"max_calls": 0}),
    )
    assert zero_call.status == "partial"
    assert zero_call.model_call_count == 0
    assert failed_path.read_bytes() == failed_bytes
    class OnlySummaryCalls(DiscoveryClient):
        def complete_json(self, **request):
            assert "inputs" in json.loads(request["user"])["input"]
            return super().complete_json(**request)
    retry = OnlySummaryCalls()
    current = core.run_discovery(
        prepared, prior=zero_call, client=retry, model_version=prior.model_version,
        model_hash=prior.model_hash, cache_dir=tmp_path / "cache",
        budget=prior.budget.model_copy(update={"max_calls": 2}),
    )
    assert current.status == "complete"
    assert current.model_call_count == len(retry.calls) == 2
    assert retry.calls[0] == client.failed_request
    assert current.failed_summaries == []
    assert path.read_bytes() == prior_bytes
    assert failed_path.read_bytes() == failed_bytes
