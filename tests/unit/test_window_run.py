"""Integrated windows use genuine prepared offline sources, never fake discovery."""

import copy
import json
import threading
from pathlib import Path

import pytest

from fabric_kg_builder.contracts.base import canonical_json
from fabric_kg_builder.domain.discovery import save_discovery
from fabric_kg_builder.domain.window_run import (
    RunBudget, RunConfig, load_window_inputs, load_windowed_run, plan_window_run,
    run_windowed, windowed_history, windowed_model_binding, windowed_schema, windowed_status,
)
from tests.unit.test_domain_discovery import _prepared, _run
from tests.unit.test_l1_stage import _intake


class Client:
    def __init__(self, callback=None):
        self.requests = []
        self.lock = threading.Lock()
        self.callback = callback

    def complete_json(self, **request):
        with self.lock:
            self.requests.append(copy.deepcopy(request))
        payload = json.loads(request["user"])["input"]
        if self.callback:
            return self.callback(payload)
        return response(payload)


def response(payload, *, term="Record", concept_id="type:record", invalid=False):
    text, start = payload["text"], payload["offset_base"]
    anchor = {"span_start": start, "span_end": start + len(text), "quote": text}
    candidates = [{"candidate_kind": "entity", "local_id": "r", "observed_type": term,
                   "label": "Record", "anchors": [anchor]}]
    proposals = []
    if not any(c["concept_id"] == concept_id for c in payload["schema"]["concepts"]):
        proposals.append({
            "action": "add_concept", "candidate_indices": [0], "reason": "Source terminology",
            "concept": {"concept_id": concept_id, "kind": "entity", "name": term,
                        "definition": "A governed source record", "identity_policy": {"mode": "unresolved"}},
        })
    if invalid:
        proposals.append({
            "action": "add_concept", "candidate_indices": [0], "reason": "Broken original bootstrap edge",
            "concept": {"concept_id": "rel:sku", "kind": "relationship", "name": "SKU",
                        "definition": "Literal SKU not yet typed", "source_type_ids": [concept_id],
                        "target_type_ids": [], "identity_policy": {"context_policy": "source"}},
        })
    return {"candidates": candidates, "schema_proposals": proposals, "pending": [],
            "working_context": {"layer": "domain", "optional_details": "retained"}}


def inputs(tmp_path, *, files=2, paragraphs=4, discovery=False):
    _, _, prepared = _prepared(tmp_path, files=files, paragraphs=paragraphs)
    intake = tmp_path / "intake.json"
    intake.write_text(json.dumps(_intake(), indent=2))
    if discovery:
        cache = _run(tmp_path, prepared, max_chunk_chars=128)
        path = tmp_path / "discovery.json"
        save_discovery(path, cache)
        return load_window_inputs(discovery_path=path, intake_path=intake)
    path = tmp_path / "prepared.json"
    path.write_text(canonical_json(prepared))
    return load_window_inputs(prepared_path=path, intake_path=intake)


def budget(**overrides):
    return RunBudget(**{"max_calls": 200, "max_tokens": 10_000_000, **overrides})


def pending_rows(registry):
    return [dict(zip(registry["columns"], row)) for row in registry["rows"]]


def pending_scope(registry, row):
    return dict(zip(registry["scope_columns"], registry["catalogs"]["scopes"][row["scope"]]))


@pytest.mark.parametrize("formats", [("list",), ("v1.0",), ("v1.1",), ("list", "v1.0", "v1.1")])
def test_pending_validation_reconstructs_once_per_format_per_window_and_checks_every_request(
        tmp_path, monkeypatch, formats):
    import fabric_kg_builder.domain.window_run as engine
    from collections import Counter

    data = inputs(tmp_path, files=1, paragraphs=11)
    config = RunConfig(window_size=6)
    result = run_windowed(inputs=data, output_dir=tmp_path / "windows", config=config,
                          budget=budget(), client=Client(lambda p: response(p, invalid=True)))
    history, log = result.logs[:1], result.logs[1]
    snapshot = engine._effective_window(
        engine.seed_snapshot(), result.chunks[:6], history[0].exchanges, config).snapshot
    assert len(log.exchanges) == 6
    rendered = {"list": engine._pending_ledger(history), "v1.0": engine._pending_context_v1(history),
                "v1.1": engine._pending_context(history)}
    values = [rendered[formats[i % len(formats)]] for i in range(6)]
    units = {u.source_unit_id: u for u in result.prepared.source_units}

    def rewritten(pending_values):
        exchanges, items = [], []
        for original, item, pending in zip(log.exchanges, result.chunks[6:12], pending_values):
            payload = original.request.model_dump(mode="json")["payload"]
            user = json.loads(payload["request"]["user"])
            user["input"]["pending"] = pending
            payload["request"]["user"] = canonical_json(user)
            request = engine._seal(engine._Ledger, kind="request", payload=payload)
            received = engine._seal(engine._Ledger, kind="response", payload={
                **original.response.payload, "request_hash": request.artifact_hash})
            exchanges.append(engine.WindowedExchange(request=request, response=received))
            items.append(engine._grounded_observation(
                item.chunk, request.artifact_hash,
                {"candidates": received.payload["response"]["candidates"]},
                units[item.chunk.source_unit_id], request_prompt_version=None))
        updated = engine._seal(engine.WindowedLog, **{
            **{name: getattr(log, name) for name in type(log).model_fields if name != "artifact_hash"},
            "request_hashes": [e.request.artifact_hash for e in exchanges],
            "response_hashes": [e.response.artifact_hash for e in exchanges], "exchanges": exchanges})
        return updated, items

    calls = Counter()
    expected_pending = engine._expected_pending

    def counted(stored, actual_history):
        assert actual_history is history
        calls["list" if isinstance(stored, list) else stored["format_version"]] += 1
        return expected_pending(stored, actual_history)

    monkeypatch.setattr(engine, "_expected_pending", counted)

    def validate(pending_values):
        updated, items = rewritten(pending_values)
        engine._validate_exchanges(
            updated, items, snapshot, prepared=result.prepared, context=result.context,
            manifest_hash=result.manifest_hash, model_hash=result.model_hash, config=config, history=history)

    keys = {"list": "list", "v1.0": engine.PENDING_CONTEXT_V1, "v1.1": engine.PENDING_CONTEXT_VERSION}
    expected_counts = Counter({keys[fmt]: 1 for fmt in formats})
    validate(values)
    assert calls == expected_counts
    validate(values)
    assert calls == Counter({key: 2 for key in expected_counts})

    tampered = copy.deepcopy(values)
    if isinstance(tampered[-1], list):
        tampered[-1][-1]["reason"] = "tampered later request"
    else:
        tampered[-1]["ledger_entry_count"] += 1
    with pytest.raises(ValueError, match="source/context/schema drift"):
        validate(tampered)
    assert calls == Counter({key: 3 for key in expected_counts})
    for unsupported in ({"format_version": "legacy-list"}, {"format_version": ["unknown"]}, None):
        with pytest.raises(ValueError, match="unsupported pending context"):
            validate([*values[:-1], unsupported])


@pytest.mark.parametrize("repair_mode", ["none", "valid", "invalid", "empty"])
def test_effective_window_reuses_initial_barrier_only_without_repair_exchanges(tmp_path, monkeypatch, repair_mode):
    import fabric_kg_builder.domain.window_run as engine

    data = inputs(tmp_path, files=1, paragraphs=1)
    config = RunConfig(window_size=8)

    def draft(payload):
        if repair_mode == "none":
            return response(payload, invalid=True)
        if repair_mode == "invalid" and "repair" in payload:
            return {"schema_proposals": []}
        if repair_mode == "empty" and "repair" in payload:
            return {"candidates": [], "schema_proposals": []}
        return _identity_property_repair(payload)

    result = run_windowed(inputs=data, output_dir=tmp_path / "windows", config=config,
                          budget=budget(max_repair_calls=0 if repair_mode == "none" else 1), client=Client(draft))
    log, seed = result.logs[0], engine.seed_snapshot()
    barrier, calls = engine._barrier, []

    def counted(*args):
        evaluated = barrier(*args)
        calls.append(((args[0], *(list(value) for value in args[1:])), evaluated))
        return evaluated

    monkeypatch.setattr(engine, "_barrier", counted)
    evaluated = engine._effective_window(seed, result.chunks, log.exchanges, config)
    assert len(calls) == (1 if repair_mode == "none" else 2)
    engine._validate_evaluation(log, evaluated, seed)
    assert evaluated.original_diagnostics == calls[0][1][2]
    # Independently repeat the final pure evaluation used before the optimization.
    repeated = barrier(*calls[-1][0])
    assert canonical_json(repeated) == canonical_json((
        evaluated.snapshot, evaluated.decisions, evaluated.diagnostics, evaluated.records))
    assert evaluated.snapshot.artifact_hash == result.final_snapshot.artifact_hash
    assert bool(evaluated.supersessions) == (repair_mode == "valid")
    if repair_mode == "none":
        assert evaluated.snapshot is calls[0][1][0]
        assert evaluated.records is calls[0][1][3]
    pending_calls, expected_pending = [], engine._expected_pending

    def counted_pending(stored, history):
        pending_calls.append(stored)
        return expected_pending(stored, history)

    monkeypatch.setattr(engine, "_expected_pending", counted_pending)
    engine._validate_exchanges(
        log, result.chunks, seed, prepared=result.prepared, context=result.context,
        manifest_hash=result.manifest_hash, model_hash=result.model_hash, config=config, history=[])
    assert len(pending_calls) == 1


def test_raw_windows_full_context_empty_seed_frozen_barrier_and_completed_resume(tmp_path):
    data = inputs(tmp_path)
    config = RunConfig(window_size=2, max_chunk_chars=128)
    root = tmp_path / "windows"
    client = Client()
    result = run_windowed(inputs=data, output_dir=root, config=config, budget=budget(), client=client)
    assert result.state == "complete"
    assert result.cursor == plan_window_run(data, config)["chunk_count"]
    assert result.model_call_count == result.cursor
    assert result.final_snapshot.provisional_concept_ids == ["type:record"]
    assert all(r.status == "mapped" for r in result.final_mapping.records)
    assert result.prepared == data.prepared
    assert result.context.intake_raw == _intake()
    for request in client.requests:
        payload = json.loads(request["user"])["input"]
        assert payload["context"]["intake_raw"] == _intake()
        assert payload["context_hash"] == data.context.artifact_hash
        assert "summaries" not in payload
        log = next(log for log in result.logs if payload["chunk"]["chunk_id"] in log.chunk_ids)
        assert payload["schema_hash"] == log.before_hash
        assert payload["schema_version"] == log.input_schema_version
        assert all(c["source_file_id"] == payload["chunk"]["source_file_id"]
                   for c in payload["adjacency_context"])
    assert result.logs[0].input_schema_version == 0
    assert all(c.request_prompt_version is None for c in result.chunks)
    before = (root / "run.json").read_bytes()
    resumed = run_windowed(inputs=data, output_dir=root, config=config)
    assert resumed.artifact_hash == result.artifact_hash
    assert resumed.model_call_count == 0
    assert (root / "run.json").read_bytes() == before
    assert load_windowed_run(root).artifact_hash == result.artifact_hash
    assert windowed_status(root)["cursor"] == result.cursor
    assert windowed_history(root) == result.logs
    assert windowed_schema(root) == result.final_snapshot


def test_discovery_is_only_authentic_source_cache_and_documents_do_not_reset(tmp_path):
    data = inputs(tmp_path, discovery=True)
    config = RunConfig(window_size=2)
    root = tmp_path / "windows"
    first = run_windowed(inputs=data, output_dir=root, config=config,
                         budget=budget(stop_after_document=1), client=Client())
    first_doc = data.prepared.sources[0].source_file_id
    assert first.reason == "document_stop"
    assert first.cursor == sum(c.source_file_id == first_doc for c in data.chunk_plan)
    assert len(first.prepared.sources) == 2
    assert all(len({c.source_file_id for c in data.chunk_plan if c.chunk_id in log.chunk_ids}) == 1
               for log in first.logs)
    client = Client()
    last = run_windowed(inputs=data, output_dir=root, config=config, budget=budget(), client=client)
    assert last.state == "complete"
    assert last.cursor == len(data.chunk_plan)
    payload = json.loads(client.requests[0]["user"])["input"]
    assert payload["schema_hash"] == first.final_snapshot.artifact_hash
    assert "Open record" not in client.requests[0]["user"]
    assert [c.chunk for c in last.chunks] == data.chunk_plan


def test_invalid_empty_target_keeps_valid_sibling_and_bad_pending_quarantined(tmp_path):
    data = inputs(tmp_path, files=1, paragraphs=1)
    def draft(payload):
        raw = response(payload, invalid=True)
        raw["pending"] = [{"observation_ids": ["fabricated"], "reason": "Unknown source"}]
        return raw
    result = run_windowed(inputs=data, output_dir=tmp_path / "windows", budget=budget(), client=Client(draft))
    assert result.state == "complete"
    assert [c.concept_id for c in result.final_snapshot.concepts] == ["type:record"]
    assert any("endpoints" in d.reason for log in result.logs for d in log.diagnostics)
    assert any("pending references" in d.reason for log in result.logs for d in log.diagnostics)
    assert all(r.status == "mapped" for r in result.final_mapping.records)


def test_cached_empty_pending_object_preserves_candidates_and_reports_shape(tmp_path, monkeypatch):
    import fabric_kg_builder.domain.window_run as engine

    data = inputs(tmp_path, files=1, paragraphs=1)
    root = tmp_path / "windows"
    def empty_pending(payload):
        raw = response(payload)
        raw["pending"] = {}
        return raw
    client = Client(empty_pending)
    decode = engine._decode
    def previous_decoder(received, config):
        decoded = decode(received, config)
        if decoded.get("pending") == {}:
            raise ValueError("invalid pending/working_context envelope")
        return decoded
    monkeypatch.setattr(engine, "_decode", previous_decoder)
    prior = run_windowed(inputs=data, output_dir=root, budget=budget(), client=client)
    assert prior.state == "partial" and prior.cursor == 0
    calls = len(client.requests)
    assert calls > 0
    before = {path: path.read_bytes() for path in (root / "responses").glob("*.json")}
    monkeypatch.setattr(engine, "_decode", decode)
    resumed = run_windowed(inputs=data, output_dir=root, client=None)
    assert resumed.state == "complete" and resumed.model_call_count == 0
    assert len(client.requests) == calls
    assert resumed.chunks[0].response.candidates
    assert any(d.channel == "pending" and "empty_pending_object" in d.reason
               for log in resumed.logs for d in log.diagnostics)
    assert before == {path: path.read_bytes() for path in (root / "responses").glob("*.json")}
    assert load_windowed_run(root).artifact_hash == resumed.artifact_hash


def test_nonempty_pending_object_is_not_silently_discarded(tmp_path):
    data = inputs(tmp_path, files=1, paragraphs=1)
    def malformed_pending(payload):
        raw = response(payload)
        raw["pending"] = {"reason": "This content must not be erased"}
        return raw
    result = run_windowed(
        inputs=data, output_dir=tmp_path / "windows", budget=budget(),
        client=Client(malformed_pending),
    )
    assert result.state == "complete" and result.cursor > 0
    assert all(not log.pending for log in result.logs)
    assert any(d.channel == "pending" and "invalid_pending_metadata" in d.reason
               and d.proposal == {"reason": "This content must not be erased"}
               for log in result.logs for d in log.diagnostics)
    assert result.logs[0].exchanges[0].response.payload["response"]["pending"] == {
        "reason": "This content must not be erased",
    }


def test_candidate_grounding_blocks_fabrication_and_dependency_closure(tmp_path):
    data = inputs(tmp_path, files=1, paragraphs=1)
    def draft(payload):
        raw = response(payload)
        raw["candidates"][0]["anchors"][0]["quote"] = "Invented"
        return raw
    result = run_windowed(inputs=data, output_dir=tmp_path / "windows", budget=budget(), client=Client(draft))
    assert result.state == "partial" and result.reason == "bootstrap_blocked"
    assert not result.final_snapshot.concepts
    assert result.final_mapping.records[0].status == "quarantined"
    assert any("quarantined" in d.reason for log in result.logs for d in log.diagnostics)


def test_same_window_entity_property_relationship_resolve_at_one_barrier(tmp_path):
    data = inputs(tmp_path, files=1, paragraphs=1)
    def draft(payload):
        raw = response(payload)
        anchor = raw["candidates"][0]["anchors"][0]
        raw["candidates"] += [
            {"candidate_kind": "property", "owner_local_id": "r", "observed_property": "description",
             "value": payload["text"], "normalized_value": payload["text"], "anchor": anchor},
            {"candidate_kind": "relationship", "source_local_id": "r", "target_local_id": "r",
             "observed_predicate": "describes", "direction": "source_to_target", "anchor": anchor},
        ]
        raw["schema_proposals"] = [
            {"action": "add_concept", "candidate_indices": [1], "reason": "Owned scalar",
             "concept": {"concept_id": "prop:description", "kind": "property", "name": "description",
                         "definition": "Source description", "owner_type_ids": ["type:record"], "value_type": "string"}},
            {"action": "add_concept", "candidate_indices": [2], "reason": "Scoped relation",
             "concept": {"concept_id": "rel:describes", "kind": "relationship", "name": "describes",
                         "definition": "Source describes a record", "source_type_ids": ["type:record"],
                         "target_type_ids": ["type:record"], "identity_policy": {"context_policy": "source record"}}},
            *raw["schema_proposals"],
        ]
        return raw
    result = run_windowed(inputs=data, output_dir=tmp_path / "windows", budget=budget(), client=Client(draft))
    assert result.state == "complete"
    assert len(result.final_snapshot.concepts) == 3
    assert all(r.status == "mapped" for r in result.final_mapping.records)
    assert result.final_snapshot.version == 1


def test_call_budget_mid_window_preserves_received_responses_and_resume(tmp_path):
    data = inputs(tmp_path, files=1, paragraphs=5)
    config = RunConfig(window_size=3, max_chunk_chars=128)
    root = tmp_path / "windows"
    first = run_windowed(inputs=data, output_dir=root, config=config,
                         budget=budget(max_calls=1), client=Client())
    assert first.cursor == 0 and first.reason == "model_budget_exhausted"
    assert len(list((root / "responses").glob("*.json"))) == 1
    next_client = Client()
    last = run_windowed(inputs=data, output_dir=root, config=config, budget=budget(), client=next_client)
    assert last.state == "complete"
    assert last.reused_response_count == 1
    assert last.model_call_count == last.cursor - 1


def test_transport_uncertain_requires_explicit_retry_and_persists_exact_request(tmp_path):
    data = inputs(tmp_path, files=1, paragraphs=1)
    root = tmp_path / "windows"
    def fail(payload):
        raise TimeoutError("unknown provider outcome")
    first = run_windowed(inputs=data, output_dir=root, budget=budget(), client=Client(fail))
    assert first.cursor == 0 and first.reason == "uncertain_dispatch_requires_explicit_retry"
    client = Client()
    blocked = run_windowed(inputs=data, output_dir=root, budget=budget(), client=client)
    assert blocked.model_call_count == 0 and not client.requests
    assert list((root / "requests").glob("*.json"))
    retried = run_windowed(inputs=data, output_dir=root, budget=budget(retry_uncertain=True), client=client)
    assert retried.state == "complete" and retried.run.model_call_count == 2 * retried.cursor


def test_request_bound_never_truncates_and_config_intake_source_drift_reject(tmp_path):
    data = inputs(tmp_path, files=1, paragraphs=1)
    root = tmp_path / "windows"
    client = Client()
    result = run_windowed(inputs=data, output_dir=root, config=RunConfig(max_request_chars=1024),
                          budget=budget(), client=client)
    assert result.cursor == 0 and result.reason == "context_limit"
    assert not client.requests
    with pytest.raises(ValueError, match="cache conflict"):
        run_windowed(inputs=data, output_dir=root, budget=budget(), client=client)
    source_file = next((tmp_path / "source").glob("*.html"))
    source_file.write_text("Changed source")
    with pytest.raises(ValueError):
        run_windowed(inputs=data, output_dir=root, config=RunConfig(max_request_chars=1024))


def test_targeted_repair_retains_original_and_same_full_context(tmp_path):
    data = inputs(tmp_path, files=1, paragraphs=1)
    root = tmp_path / "windows"
    def draft(payload):
        if "repair" in payload:
            assert payload["repair"]["failed_proposals"]
            assert payload["context"]["intake_raw"] == _intake()
            return {"candidates": [], "schema_proposals": [], "pending": []}
        return response(payload, invalid=True)
    client = Client(draft)
    result = run_windowed(inputs=data, output_dir=root, budget=budget(max_repair_calls=1), client=client)
    assert result.state == "complete" and result.repair_call_count == 1
    assert len(client.requests) == result.cursor + 1
    assert len(list((root / "responses").glob("*.json"))) == result.cursor + 1
    assert result.logs[0].repair_request_hashes
    assert result.logs[0].diagnostics


def test_whole_invalid_envelope_persists_raw_before_validation_no_duplicate(tmp_path):
    data = inputs(tmp_path, files=1, paragraphs=1)
    root = tmp_path / "windows"
    invalid = "{ definitely not JSON"
    first = run_windowed(inputs=data, output_dir=root, budget=budget(), client=Client(lambda _: invalid))
    assert first.cursor == 0 and "response_validation_failed" in first.reason
    cache = json.loads(next((root / "responses").glob("*.json")).read_text())
    assert cache["payload"]["response"] == invalid
    client = Client()
    second = run_windowed(inputs=data, output_dir=root, budget=budget(), client=client)
    assert not client.requests and second.reused_response_count == first.model_call_count


def test_mapping_reconciles_earlier_synonym_after_explicit_alias(tmp_path):
    data = inputs(tmp_path, files=1, paragraphs=5)
    config = RunConfig(window_size=1, max_chunk_chars=128)
    def draft(payload):
        version = payload["schema_version"]
        raw = response(payload, term="Old record" if version else "Record")
        if version == 2:
            raw["schema_proposals"] = [{
                "action": "add_alias", "concept_id": "type:record", "alias": "Old record",
                "candidate_indices": [0], "reason": "Explicit reviewed vocabulary mapping",
            }]
        return raw
    result = run_windowed(inputs=data, output_dir=tmp_path / "windows", config=config,
                          budget=budget(), client=Client(draft))
    assert result.state == "complete"
    assert result.logs[1].records[0].status == "pending"
    assert result.logs[2].newly_mapped_previous_count == 1
    assert all(r.status == "mapped" for r in result.final_mapping.records)


def test_tampered_commit_and_scope_filter_rejected(tmp_path):
    data = inputs(tmp_path, files=1, paragraphs=1)
    with pytest.raises(ValueError, match="scope filtering"):
        load_window_inputs(prepared_path=tmp_path / "prepared.json", intake_path=tmp_path / "intake.json",
                           source_file_ids=[data.prepared.sources[0].source_file_id])
    root = tmp_path / "windows"
    run_windowed(inputs=data, output_dir=root, budget=budget(), client=Client())
    path = root / "windows" / "000000.json"
    value = json.loads(path.read_text())
    value["log"]["chunk_ids"] = []
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        load_windowed_run(root)


def test_mixed_owner_evidence_does_not_poison_valid_scoped_property(tmp_path):
    data = inputs(tmp_path, files=1, paragraphs=1)
    def draft(payload):
        raw = response(payload)
        anchor = raw["candidates"][0]["anchors"][0]
        raw["candidates"].append({"candidate_kind": "entity", "local_id": "s", "observed_type": "Subject",
                                  "label": "Subject", "anchors": [anchor]})
        raw["schema_proposals"].append({
            "action": "add_concept", "candidate_indices": [1], "reason": "Different owner type",
            "concept": {"concept_id": "type:subject", "kind": "entity", "name": "Subject",
                        "definition": "A subject", "identity_policy": {"mode": "unresolved"}},
        })
        for owner in ["r", "s"]:
            raw["candidates"].append({"candidate_kind": "property", "owner_local_id": owner,
                                      "observed_property": "description", "value": payload["text"],
                                      "normalized_value": payload["text"], "anchor": anchor})
        raw["schema_proposals"].append({
            "action": "add_concept", "candidate_indices": [2, 3], "reason": "Mixed source owners",
            "concept": {"concept_id": "prop:description", "kind": "property", "name": "description",
                        "definition": "Record description", "owner_type_ids": ["type:record"], "value_type": "string"},
        })
        return raw
    result = run_windowed(inputs=data, output_dir=tmp_path / "windows", budget=budget(), client=Client(draft))
    assert result.state == "complete"
    assert "prop:description" in {c.concept_id for c in result.final_snapshot.concepts}
    properties = [r for r in result.final_mapping.records if r.kind == "property"]
    assert {r.status for r in properties} == {"mapped", "pending"}
    assert any("incompatible_owner" in d.reason for l in result.logs for d in l.diagnostics)


def test_targeted_repair_corrects_invalid_relationship_to_supported_property(tmp_path):
    data = inputs(tmp_path, files=1, paragraphs=1)
    def draft(payload):
        if "repair" in payload:
            return {"candidates": [], "schema_proposals": [{
                "action": "add_concept", "candidate_indices": [1], "replaces_proposal_index": 1,
                "reason": "Literal belongs to owner",
                "concept": {"concept_id": "prop:description", "kind": "property", "name": "description",
                            "definition": "Description", "owner_type_ids": ["type:record"], "value_type": "string"},
            }]}
        raw = response(payload, invalid=True)
        raw["candidates"].append({
            "candidate_kind": "property", "owner_local_id": "r", "observed_property": "description",
            "value": payload["text"], "normalized_value": payload["text"], "anchor": raw["candidates"][0]["anchors"][0],
        })
        return raw
    result = run_windowed(inputs=data, output_dir=tmp_path / "windows",
                          budget=budget(max_repair_calls=1), client=Client(draft))
    assert result.state == "complete"
    assert "prop:description" in {c.concept_id for c in result.final_snapshot.concepts}
    assert all(r.status == "mapped" for r in result.final_mapping.records)
    assert result.repair_call_count == 1


def test_interrupted_after_raw_persistence_reuses_without_duplicate_call(tmp_path, monkeypatch):
    import fabric_kg_builder.domain.window_run as engine
    data = inputs(tmp_path, files=1, paragraphs=1)
    root = tmp_path / "windows"
    real_write = engine._write
    def interrupted(path, artifact):
        if path.parent.name == "windows" and artifact.__class__.__name__ == "_Commit":
            raise RuntimeError("simulated crash before barrier")
        real_write(path, artifact)
    monkeypatch.setattr(engine, "_write", interrupted)
    client = Client()
    with pytest.raises(RuntimeError, match="simulated crash"):
        run_windowed(inputs=data, output_dir=root, budget=budget(), client=client)
    expected_calls = len(client.requests)
    monkeypatch.setattr(engine, "_write", real_write)
    last = run_windowed(inputs=data, output_dir=root, budget=budget(), client=Client())
    assert last.state == "complete" and last.model_call_count == 0
    assert last.reused_response_count == expected_calls


def test_uncertain_repair_cannot_be_silently_skipped_when_budget_exhausted(tmp_path):
    data = inputs(tmp_path, files=1, paragraphs=1)
    root = tmp_path / "windows"
    def draft(payload):
        if "repair" in payload:
            raise TimeoutError("repair response uncertain")
        return response(payload, invalid=True)
    first = run_windowed(inputs=data, output_dir=root, budget=budget(max_repair_calls=1), client=Client(draft))
    assert first.cursor == 0 and first.reason == "uncertain_dispatch_requires_explicit_retry"
    resumed = run_windowed(inputs=data, output_dir=root, budget=budget(max_repair_calls=1), client=Client())
    assert resumed.cursor == 0 and resumed.reason == "uncertain_dispatch_requires_explicit_retry"
    assert resumed.model_call_count == 0


def test_model_drift_and_full_unrouted_question_snapshot_rejected_on_resume(tmp_path):
    data = inputs(tmp_path, files=1, paragraphs=1)
    root = tmp_path / "windows"
    run_windowed(inputs=data, output_dir=root)
    with pytest.raises(ValueError, match="cache conflict"):
        run_windowed(inputs=data, output_dir=root, model_version="different")
    path = tmp_path / "intake.json"
    raw = json.loads(path.read_text())
    raw["additional_unrouted_question"] = {"id": "q:extra", "question": "Keep this verbatim"}
    path.write_text(json.dumps(raw))
    changed = load_window_inputs(prepared_path=tmp_path / "prepared.json", intake_path=path)
    assert changed.context.intake_raw["additional_unrouted_question"] == raw["additional_unrouted_question"]
    with pytest.raises(ValueError, match="cache conflict"):
        run_windowed(inputs=changed, output_dir=root)


def test_neighbor_text_is_not_primary_evidence_and_output_is_bounded(tmp_path):
    data = inputs(tmp_path, files=1, paragraphs=1)
    def borrowed(payload):
        raw = response(payload)
        neighbor = payload["adjacency_context"][0]["text"]
        raw["candidates"][0]["anchors"] = [{"span_start": 0, "span_end": len(neighbor), "quote": neighbor}]
        return raw
    result = run_windowed(inputs=data, output_dir=tmp_path / "borrowed",
                          budget=budget(), client=Client(borrowed))
    assert result.state == "partial"
    assert all(r.status == "quarantined" for r in result.final_mapping.records)
    big = "x" * (128 * 16 + 1)
    bounded = run_windowed(inputs=data, output_dir=tmp_path / "bounded",
                           config=RunConfig(max_completion_tokens=128),
                           budget=budget(), client=Client(lambda _: big))
    assert bounded.cursor == 0
    assert "output character bound" in bounded.reason
    assert json.loads(next((tmp_path / "bounded" / "responses").glob("*.json")).read_text())["payload"]["response"] == big


def test_inverse_predicate_does_not_become_accepted_forward_edge(tmp_path):
    data = inputs(tmp_path, files=1, paragraphs=1)
    def inverse(payload):
        raw = response(payload)
        raw["candidates"].append({
            "candidate_kind": "relationship", "source_local_id": "r", "target_local_id": "r",
            "observed_predicate": "describes", "direction": "reverse",
            "anchor": raw["candidates"][0]["anchors"][0],
        })
        raw["schema_proposals"].append({
            "action": "add_concept", "candidate_indices": [1], "reason": "Do not silently reverse",
            "concept": {"concept_id": "rel:describes", "kind": "relationship", "name": "describes",
                        "definition": "A relation", "source_type_ids": ["type:record"],
                        "target_type_ids": ["type:record"], "identity_policy": {"context_policy": "source"}},
        })
        return raw
    result = run_windowed(inputs=data, output_dir=tmp_path / "windows", budget=budget(), client=Client(inverse))
    assert result.state == "complete"
    assert not any(c.kind == "relationship" for c in result.final_snapshot.concepts)
    assert all(r.status == "pending" for r in result.final_mapping.records if r.kind == "relationship")


def test_token_and_window_budgets_leave_exact_durable_cursor(tmp_path):
    data = inputs(tmp_path, files=1, paragraphs=4)
    config = RunConfig(window_size=1, max_chunk_chars=128)
    root = tmp_path / "windows"
    client = Client()
    first = run_windowed(inputs=data, output_dir=root, config=config,
                         budget=budget(max_tokens=1), client=client)
    assert first.cursor == 0 and first.reason == "model_budget_exhausted"
    assert not client.requests
    second = run_windowed(inputs=data, output_dir=root, config=config,
                          budget=budget(max_windows=1), client=client)
    assert second.cursor == 1 and second.reason == "window_budget_exhausted"
    assert second.final_snapshot.version == 1
    assert load_windowed_run(root).artifact_hash == second.artifact_hash
    status = windowed_status(root)
    assert status["completed_primary_codepoints"] == second.chunks[0].chunk.slice_end
    assert status["completed_document_ids"] == []


def test_model_binding_available_before_first_report_without_client(tmp_path, monkeypatch):
    import fabric_kg_builder.domain.window_run as engine
    data = inputs(tmp_path, files=1, paragraphs=1)
    root = tmp_path / "windows"
    original = engine._write
    def interrupted(path, artifact):
        original(path, artifact)
        if path.name == "manifest.json":
            raise RuntimeError("crash before dispatch")
    monkeypatch.setattr(engine, "_write", interrupted)
    with pytest.raises(RuntimeError, match="crash before dispatch"):
        run_windowed(inputs=data, output_dir=root, model_version="fixture/resume")
    assert not (root / "latest.json").exists()
    binding = windowed_model_binding(root)
    assert binding["model_version"] == "fixture/resume"
    monkeypatch.setattr(engine, "_write", original)
    result = run_windowed(inputs=data, output_dir=root, model_version=binding["model_version"],
                          model_hash=binding["model_hash"], budget=RunBudget())
    assert result.model_call_count == 0 and result.reason == "model_budget_exhausted"


def test_omitted_token_budget_allows_explicitly_call_bounded_cli_run(tmp_path):
    data = inputs(tmp_path, files=1, paragraphs=1)
    result = run_windowed(inputs=data, output_dir=tmp_path / "windows",
                          budget=RunBudget(max_calls=10), client=Client())
    assert result.state == "complete"
    assert 0 < result.model_call_count <= 10
    assert result.reserved_tokens > 0


def _identity_property_repair(payload, *, missing_link=False, duplicate_link=False):
    if "repair" in payload:
        replacements = [{
            "action": "add_concept", "candidate_indices": [0], "replaces_proposal_index": 0,
            "reason": "Keep working entity identity unresolved",
            "concept": {"concept_id": "type:record", "kind": "entity", "name": "Record",
                        "definition": "A record", "identity_policy": {"mode": "unresolved"}},
        }, {
            "action": "add_concept", "candidate_indices": [1], "replaces_proposal_index": 1,
            "reason": "The description is textual",
            "concept": {"concept_id": "prop:description", "kind": "property", "name": "description",
                        "definition": "Description", "owner_type_ids": ["type:record"], "value_type": "string"},
        }]
        if missing_link:
            for item in replacements:
                item.pop("replaces_proposal_index")
        if duplicate_link:
            replacements.append(copy.deepcopy(replacements[1]))
        return {"candidates": [], "schema_proposals": replacements, "pending": []}
    raw = response(payload)
    raw["schema_proposals"][0]["concept"]["identity_policy"] = {"mode": "invented_key"}
    raw["candidates"].append({
        "candidate_kind": "property", "owner_local_id": "r", "observed_property": "description",
        "value": payload["text"], "normalized_value": payload["text"], "anchor": raw["candidates"][0]["anchors"][0],
    })
    raw["schema_proposals"].append({
        "action": "add_concept", "candidate_indices": [1], "reason": "Incorrect initial scalar type",
        "concept": {"concept_id": "prop:description", "kind": "property", "name": "description",
                    "definition": "Description", "owner_type_ids": ["type:record"], "value_type": "integer"},
    })
    return raw


@pytest.mark.parametrize("repair_cap", [1, 2])
def test_repair_replaces_failed_originals_before_entity_dependency_resolution(tmp_path, repair_cap):
    data = inputs(tmp_path, files=1, paragraphs=1)
    root = tmp_path / "windows"
    result = run_windowed(inputs=data, output_dir=root, budget=budget(max_repair_calls=repair_cap),
                          client=Client(_identity_property_repair))
    assert result.state == "complete"
    prop = next(c for c in result.final_snapshot.concepts if c.concept_id == "prop:description")
    assert prop.value_type == "string"
    log = result.logs[0]
    assert len(log.supersessions) == 2 * repair_cap
    assert {s.original_proposal_index for s in log.supersessions} == {0, 1}
    assert log.original_diagnostics
    assert bool(log.diagnostics) == (repair_cap < len(log.chunk_ids))
    assert all(d.change.concept.value_type != "integer" for d in log.decisions if d.change.concept)
    originals = [e for e in log.exchanges if not e.request.payload["repair"]]
    assert all(e.response.payload["response"]["schema_proposals"][1]["concept"]["value_type"] == "integer"
               for e in originals)
    assert load_windowed_run(root).artifact_hash == result.artifact_hash
    resumed = run_windowed(inputs=data, output_dir=root)
    assert resumed.artifact_hash == result.artifact_hash and resumed.model_call_count == 0


@pytest.mark.parametrize("mode", ["missing", "duplicate"])
def test_repair_linkage_must_identify_one_unique_failed_original(tmp_path, mode):
    data = inputs(tmp_path, files=1, paragraphs=1)
    result = run_windowed(
        inputs=data, output_dir=tmp_path / "windows", budget=budget(max_repair_calls=2),
        client=Client(lambda payload: _identity_property_repair(
            payload, missing_link=mode == "missing", duplicate_link=mode == "duplicate")),
    )
    assert any("unique failed original" in d.reason for log in result.logs for d in log.diagnostics)
    if mode == "missing":
        assert result.reason == "bootstrap_blocked"
        assert not result.logs[0].supersessions
    else:
        assert all(s.original_proposal_index == 0 for s in result.logs[0].supersessions)
        assert not any(c.kind == "property" for c in result.final_snapshot.concepts)


def _rehash(payload):
    from fabric_kg_builder.contracts.base import canonical_sha256
    payload["artifact_hash"] = canonical_sha256({key: value for key, value in payload.items() if key != "artifact_hash"})
    return payload


def test_rehashed_removed_raw_proposals_cannot_keep_unsupported_committed_schema(tmp_path):
    import fabric_kg_builder.domain.window_run as engine
    data = inputs(tmp_path, files=1, paragraphs=1)
    root = tmp_path / "windows"
    result = run_windowed(inputs=data, output_dir=root, budget=budget(), client=Client())
    run_raw = json.loads((root / "run.json").read_text())
    commit_path = root / "windows" / "000000.json"
    commit = json.loads(commit_path.read_text())
    log = commit["log"]
    for index, exchange in enumerate(log["exchanges"]):
        response = exchange["response"]
        response["payload"]["response"]["schema_proposals"] = []
        _rehash(response)
        log["response_hashes"][index] = response["artifact_hash"]
        log["working_context"][index]["schema_annotations"] = []
        log["working_context"][index]["response_hash"] = response["artifact_hash"]
        key = exchange["request"]["artifact_hash"]
        (root / "responses" / f"{key}.json").write_text(canonical_json(response))
    _rehash(log)
    _rehash(commit)
    commit_path.write_text(canonical_json(commit))
    run_raw["logs"] = [log]
    run_raw["working_context"] = log["working_context"]
    _rehash(run_raw)
    (root / "run.json").write_text(canonical_json(run_raw))
    assert run_raw["final_snapshot"] == result.final_snapshot.model_dump(mode="json")
    with pytest.raises(ValueError, match="raw proposal envelopes"):
        engine._load_commits(root, engine._read(root / "manifest.json", engine._Manifest))
    with pytest.raises(ValueError, match="raw proposal envelopes"):
        load_windowed_run(root)
    with pytest.raises(ValueError, match="raw proposal envelopes"):
        load_windowed_run(root / "run.json")
    with pytest.raises(ValueError, match="raw proposal envelopes"):
        engine.WindowedRun.model_validate(run_raw)


def test_rehashed_repair_linkage_tampering_cannot_authorize_different_replacement(tmp_path):
    import fabric_kg_builder.domain.window_run as engine
    data = inputs(tmp_path, files=1, paragraphs=1)
    root = tmp_path / "windows"
    run_windowed(inputs=data, output_dir=root, budget=budget(max_repair_calls=2),
                  client=Client(_identity_property_repair))
    raw = json.loads((root / "run.json").read_text())
    log = raw["logs"][0]
    exchange = log["exchanges"][len(log["chunk_ids"])]
    response = exchange["response"]
    response["payload"]["response"]["schema_proposals"][1]["replaces_proposal_index"] = 0
    _rehash(response)
    log["repair_response_hashes"][0] = response["artifact_hash"]
    _rehash(log)
    _rehash(raw)
    with pytest.raises(ValueError, match="raw proposal envelopes"):
        engine.WindowedRun.model_validate(raw)


def test_old_prompt_state_is_rejected_without_dispatch_or_artifact_migration(tmp_path):
    import fabric_kg_builder.domain.window_run as engine
    data = inputs(tmp_path, files=1, paragraphs=1)
    root = tmp_path / "windows"
    run_windowed(inputs=data, output_dir=root)
    path = root / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["config"]["prompt_version"] = "raw-working-window/1.0.0"
    _rehash(manifest)
    path.write_text(canonical_json(manifest))
    original = path.read_bytes()
    with pytest.raises(ValueError):
        engine.windowed_model_binding(root)
    with pytest.raises(ValueError):
        run_windowed(inputs=data, output_dir=root, budget=budget(), client=Client())
    assert path.read_bytes() == original


def test_inspection_before_first_report_uses_verified_committed_prefix_read_only(tmp_path, monkeypatch):
    import fabric_kg_builder.domain.window_run as engine
    from click.testing import CliRunner
    from fabric_kg_builder.cli import cli
    data = inputs(tmp_path, files=1, paragraphs=4)
    root = tmp_path / "windows"
    config = RunConfig(window_size=1, max_chunk_chars=128)
    def no_report(*args, **kwargs):
        raise RuntimeError("pause before report publication")
    monkeypatch.setattr(engine, "_publish", no_report)
    with pytest.raises(RuntimeError, match="before report"):
        run_windowed(inputs=data, output_dir=root, config=config,
                      budget=budget(max_windows=1), client=Client())
    assert not (root / "latest.json").exists()
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    status = windowed_status(root)
    assert status["state"] == status["publication_state"] == "unpublished_progress"
    assert status["cursor"] == status["completed_windows"] == status["schema_version"] == 1
    assert status["run_hash"] is None and status["published_run_hash"] is None
    assert status["execution_state"] == "not_inferred_from_artifacts"
    assert len(windowed_history(root)) == 1
    assert windowed_schema(root).artifact_hash == status["schema_hash"]
    for name in ["window-run-status", "window-run-history"]:
        result = CliRunner().invoke(cli, ["domain", name, "--state", str(root)])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)
    after = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert before == after


def test_manifest_only_inspection_reports_pending_not_running(tmp_path, monkeypatch):
    import fabric_kg_builder.domain.window_run as engine
    data = inputs(tmp_path, files=1, paragraphs=1)
    root = tmp_path / "windows"
    original = engine._write
    def manifest_only(path, artifact):
        original(path, artifact)
        if path.name == "manifest.json":
            raise RuntimeError("pause after manifest")
    monkeypatch.setattr(engine, "_write", manifest_only)
    with pytest.raises(RuntimeError, match="after manifest"):
        run_windowed(inputs=data, output_dir=root)
    status = windowed_status(root)
    assert status["state"] == "report_pending" and status["cursor"] == 0
    assert status["execution_state"] == "not_inferred_from_artifacts"
    assert status["model_call_count"] == 0 and status["run_hash"] is None
    assert windowed_history(root) == [] and windowed_schema(root).version == 0
    assert not (root / "latest.json").exists()


def test_inspection_advances_past_stale_report_without_republishing_it(tmp_path, monkeypatch):
    import fabric_kg_builder.domain.window_run as engine
    data = inputs(tmp_path, files=1, paragraphs=5)
    root = tmp_path / "windows"
    config = RunConfig(window_size=1, max_chunk_chars=128)
    first = run_windowed(inputs=data, output_dir=root, config=config, budget=budget(max_windows=1), client=Client())
    latest = (root / "latest.json").read_bytes()
    report = (root / "reports" / f"{first.artifact_hash}.json").read_bytes()
    def no_report(*args, **kwargs):
        raise RuntimeError("pause second invocation")
    monkeypatch.setattr(engine, "_publish", no_report)
    with pytest.raises(RuntimeError, match="second invocation"):
        run_windowed(inputs=data, output_dir=root, config=config, budget=budget(max_windows=1), client=Client())
    status = windowed_status(root)
    assert status["state"] == "unpublished_progress" and status["cursor"] == 2
    assert status["published_cursor"] == 1 and status["published_run_hash"] == first.artifact_hash
    assert status["run_hash"] is None
    assert len(windowed_history(root)) == 2 and windowed_schema(root).version == 2
    assert (root / "latest.json").read_bytes() == latest
    assert (root / "reports" / f"{first.artifact_hash}.json").read_bytes() == report
    with pytest.raises(ValueError, match="differs from immutable commit"):
        load_windowed_run(root)


def test_received_invalid_foundry_output_is_private_durable_and_not_uncertain(tmp_path):
    from fabric_kg_builder.enrichment.foundry_client import FoundryJSONResponseError
    data = inputs(tmp_path, files=1, paragraphs=1)
    root = tmp_path / "windows"
    config = RunConfig(window_size=1)
    raw_output = '  {"candidates": [{"candidate_kind": "entity",\n'
    diagnostics = {
        "transport": "responses", "raw_output": raw_output, "response_id": "fixture-response",
        "status": "incomplete", "finish_reason": "length", "incomplete_reason": "max_output_tokens",
        "max_completion_tokens": 8192, "attempt": 1, "usage": {"output_tokens": 8192},
        "parse_error": "Unterminated object", "response_format": "json_schema",
        "authorization": "must-not-persist-nonallowlisted-fields",
    }
    def truncated(payload):
        raise FoundryJSONResponseError("received no complete JSON", diagnostics)
    first = run_windowed(inputs=data, output_dir=root, config=config, budget=budget(), client=Client(truncated))
    assert first.cursor == 0 and first.reason == "received_invalid_response_requires_explicit_retry"
    assert first.model_call_count == 1
    assert not list((root / "errors").glob("*.json"))
    diagnostic_path = next((root / "diagnostics").glob("*.json"))
    saved = json.loads(diagnostic_path.read_text())
    assert saved["diagnostics"]["raw_output"] == raw_output
    assert saved["diagnostics"]["finish_reason"] == "length"
    assert saved["diagnostics"]["usage"] == {"output_tokens": 8192}
    assert "authorization" not in saved["diagnostics"]
    assert diagnostic_path.stat().st_mode & 0o777 == 0o600
    before = {str(p): p.read_bytes() for folder in ["received-invalid", "diagnostics"]
              for p in (root / folder).glob("*.json")}
    client = Client()
    blocked = run_windowed(inputs=data, output_dir=root, config=config,
                           budget=budget(retry_uncertain=True), client=client)
    assert blocked.reason == first.reason and blocked.model_call_count == 0 and not client.requests
    retried = run_windowed(inputs=data, output_dir=root, config=config,
                           budget=budget(retry_invalid_response=True), client=client)
    assert retried.state == "complete"
    assert retried.run.model_call_count == retried.cursor + 1
    assert all(p.read_bytes() == before[str(p)] for folder in ["received-invalid", "diagnostics"]
               for p in (root / folder).glob("*.json"))
    completed = run_windowed(inputs=data, output_dir=root, config=config)
    assert completed.artifact_hash == retried.artifact_hash and completed.model_call_count == 0


def test_received_invalid_retry_opt_in_cannot_retry_unknown_transport(tmp_path):
    data = inputs(tmp_path, files=1, paragraphs=1)
    root = tmp_path / "windows"
    def timeout(payload):
        raise TimeoutError("unknown remote outcome")
    run_windowed(inputs=data, output_dir=root, budget=budget(), client=Client(timeout))
    client = Client()
    result = run_windowed(inputs=data, output_dir=root, budget=budget(retry_invalid_response=True), client=client)
    assert result.reason == "uncertain_dispatch_requires_explicit_retry"
    assert result.model_call_count == 0 and not client.requests


def test_received_invalid_repair_cannot_be_automatically_retried_or_skipped(tmp_path):
    from fabric_kg_builder.enrichment.foundry_client import FoundryJSONResponseError
    data = inputs(tmp_path, files=1, paragraphs=1)
    root = tmp_path / "windows"
    def truncated_repair(payload):
        if "repair" in payload:
            raise FoundryJSONResponseError("JSON truncated", {"raw_output": '{"schema_proposals": [', "finish_reason": "length"})
        return response(payload, invalid=True)
    first = run_windowed(inputs=data, output_dir=root, budget=budget(max_repair_calls=1),
                         client=Client(truncated_repair))
    assert first.cursor == 0 and first.reason == "received_invalid_response_requires_explicit_retry"
    blocked = run_windowed(inputs=data, output_dir=root, budget=budget(max_repair_calls=1), client=Client())
    assert blocked.cursor == 0 and blocked.reason == first.reason
    no_budget = run_windowed(inputs=data, output_dir=root,
                            budget=budget(max_repair_calls=1, retry_invalid_response=True), client=Client())
    assert no_budget.cursor == 0 and no_budget.reason == "repair_budget_exhausted"
    client = Client(lambda _: {"candidates": [], "schema_proposals": [], "pending": []})
    retried = run_windowed(inputs=data, output_dir=root,
                           budget=budget(max_repair_calls=2, retry_invalid_response=True), client=client)
    assert retried.state == "complete" and retried.repair_call_count == 1


def test_nested_concept_layer_is_retained_in_raw_diagnostics_not_silently_dropped(tmp_path):
    data = inputs(tmp_path, files=1, paragraphs=1)
    def nested(payload):
        raw = response(payload)
        raw["schema_proposals"][0]["concept"]["layer"] = "common"
        return raw
    result = run_windowed(inputs=data, output_dir=tmp_path / "windows", budget=budget(), client=Client(nested))
    assert result.reason == "bootstrap_blocked"
    assert not result.final_snapshot.concepts
    assert all("layer" in d.reason for log in result.logs for d in log.diagnostics)
    assert result.logs[0].exchanges[0].response.payload["response"]["schema_proposals"][0]["concept"]["layer"] == "common"


def test_known_invalid_stop_is_not_hidden_by_previously_empty_working_schema(tmp_path):
    from fabric_kg_builder.enrichment.foundry_client import FoundryJSONResponseError
    data = inputs(tmp_path, files=1, paragraphs=2)
    root = tmp_path / "windows"
    config = RunConfig(window_size=1)
    def no_schema(payload):
        raw = response(payload)
        raw["schema_proposals"] = []
        return raw
    first = run_windowed(inputs=data, output_dir=root, config=config, budget=budget(), client=Client(no_schema))
    assert first.cursor == 1 and first.reason == "bootstrap_blocked"
    def invalid(payload):
        raise FoundryJSONResponseError("No complete JSON", {"raw_output": "{", "finish_reason": "length"})
    second = run_windowed(inputs=data, output_dir=root, config=config, budget=budget(), client=Client(invalid))
    assert second.cursor == 1 and second.reason == "received_invalid_response_requires_explicit_retry"


def test_supported_layer_placement_is_explained_and_preserved(tmp_path):
    data = inputs(tmp_path, files=1, paragraphs=1)
    def common(payload):
        raw = response(payload)
        for proposal in raw["schema_proposals"]:
            proposal["layer"] = "common"
        raw["working_context"] = {"concept_layers": {"type:record": "common"}}
        return raw
    client = Client(common)
    result = run_windowed(inputs=data, output_dir=tmp_path / "windows", budget=budget(), client=client)
    assert result.state == "complete"
    assert all("NOT inside concept" in request["system"] for request in client.requests)
    for log in result.logs:
        for context in log.working_context:
            assert context["annotations"]["concept_layers"]["type:record"] == "common"
            assert all(proposal["layer"] == "common" for proposal in context["schema_annotations"])
        assert not log.diagnostics


def test_pending_registry_deduplicates_large_repeated_conflicts_without_losing_history(tmp_path):
    import fabric_kg_builder.domain.window_run as engine
    from fabric_kg_builder.contracts.base import canonical_sha256
    data = inputs(tmp_path, files=1, paragraphs=12)
    config = RunConfig(window_size=1, max_chunk_chars=128, max_request_chars=36_000)
    detail = "Retain this full unsupported proposal locally. " * 160
    def repeated(payload):
        raw = response(payload, invalid=True)
        raw["schema_proposals"][-1]["concept"]["definition"] = detail
        return raw
    client = Client(repeated)
    result = run_windowed(inputs=data, output_dir=tmp_path / "windows", config=config,
                          budget=budget(), client=client)
    assert result.state == "complete"
    request = client.requests[-1]
    payload = json.loads(request["user"])["input"]
    registry = payload["pending"]
    ledger = engine._pending_ledger(result.logs[:-1])
    assert len(canonical_json(ledger)) > config.max_request_chars
    assert len(canonical_json(request)) < config.max_request_chars
    assert registry["format_version"] == engine.PENDING_CONTEXT_VERSION
    assert registry["full_ledger_hash"] == canonical_sha256(ledger)
    assert registry["ledger_entry_count"] == len(ledger)
    assert sum(entry["count"] for entry in pending_rows(registry)) == len(ledger)
    assert registry["unique_signature_count"] == 1
    assert all(len(entry["example_ids"]) <= 3 for entry in pending_rows(registry))
    assert detail not in request["user"]
    assert all(log.diagnostics[0].proposal["concept"]["definition"] == detail for log in result.logs)
    assert all(log.exchanges[0].response.payload["response"]["schema_proposals"][-1]["concept"]["definition"] == detail
               for log in result.logs)
    assert payload["context"]["intake_text"] == data.context.intake_text
    assert payload["schema"]["artifact_hash"] == result.logs[-1].before_hash
    unit = next(u for u in data.prepared.source_units if u.source_unit_id == payload["chunk"]["source_unit_id"])
    assert payload["text"] == unit.text[payload["chunk"]["slice_start"]:payload["chunk"]["slice_end"]]
    assert registry["coverage"] == "aggregated_pending_vocabulary_not_occurrence_adjudication"


def test_pending_registry_keeps_owner_scopes_distinct(tmp_path):
    import fabric_kg_builder.domain.window_run as engine
    data = inputs(tmp_path, files=1, paragraphs=2)
    def scoped(payload):
        raw = response(payload)
        raw["candidates"].append({
            "candidate_kind": "property", "owner_local_id": "r", "observed_property": "description",
            "value": payload["text"], "normalized_value": payload["text"], "anchor": raw["candidates"][0]["anchors"][0],
        })
        for owner in ["missing:a", "missing:b"]:
            raw["schema_proposals"].append({
                "action": "add_concept", "candidate_indices": [1], "reason": "Unresolved ownership",
                "concept": {"concept_id": "prop:description", "kind": "property", "name": "description",
                            "definition": "Source description", "owner_type_ids": [owner], "value_type": "string"},
            })
        return raw
    result = run_windowed(inputs=data, output_dir=tmp_path / "windows", config=RunConfig(window_size=1),
                          budget=budget(), client=Client(scoped))
    registry = engine._pending_context(result.logs)
    assert registry["unique_signature_count"] == 2
    assert {tuple(registry["catalogs"]["identifiers"][i] for i in pending_scope(registry, e)["owners"])
            for e in pending_rows(registry)} == {
        ("missing:a",), ("missing:b",),
    }
    assert sum(e["count"] for e in pending_rows(registry)) == 2 * result.cursor


@pytest.mark.parametrize("old_format", ["list", "v1.0"])
def test_legacy_pending_prefix_and_received_response_reuse_without_repeating_calls(tmp_path, monkeypatch, old_format):
    import fabric_kg_builder.domain.window_run as engine
    data = inputs(tmp_path, files=1, paragraphs=5)
    config = RunConfig(window_size=2, max_chunk_chars=128)
    root = tmp_path / "windows"
    compact_context = engine._pending_context
    monkeypatch.setattr(engine, "_pending_context", engine._pending_ledger if old_format == "list" else engine._pending_context_v1)
    first = run_windowed(inputs=data, output_dir=root, config=config,
                         budget=budget(max_windows=1), client=Client(lambda p: response(p, invalid=True)))
    second = run_windowed(inputs=data, output_dir=root, config=config,
                          budget=budget(max_calls=1), client=Client(lambda p: response(p, invalid=True)))
    assert first.cursor == second.cursor == 2
    assert second.model_call_count == 1
    immutable = {str(p): p.read_bytes() for folder in ["requests", "responses", "windows"]
                 for p in (root / folder).glob("*.json")}
    manifest = (root / "manifest.json").read_bytes()
    monkeypatch.setattr(engine, "_pending_context", compact_context)
    assert load_windowed_run(root).artifact_hash == second.artifact_hash
    client = Client(lambda p: response(p, invalid=True))
    result = run_windowed(inputs=data, output_dir=root, config=config, budget=budget(), client=client)
    assert result.state == "complete"
    assert result.model_call_count == result.cursor - 3
    assert result.reused_response_count == 1
    assert all(json.loads(request["user"])["input"]["pending"]["format_version"] == engine.PENDING_CONTEXT_VERSION
               for request in client.requests)
    old_pending = json.loads(result.logs[1].exchanges[0].request.payload["request"]["user"])["input"]["pending"]
    assert isinstance(old_pending, list) if old_format == "list" else old_pending["format_version"] == engine.PENDING_CONTEXT_V1
    assert (root / "manifest.json").read_bytes() == manifest
    assert all(Path(path).read_bytes() == contents for path, contents in immutable.items())
    assert load_windowed_run(root).artifact_hash == result.artifact_hash
    completed = run_windowed(inputs=data, output_dir=root, config=config)
    assert completed.artifact_hash == result.artifact_hash and completed.model_call_count == 0


@pytest.mark.parametrize("old_format", ["list", "v1.0"])
def test_pending_representation_change_does_not_bypass_uncertain_legacy_dispatch(tmp_path, monkeypatch, old_format):
    import fabric_kg_builder.domain.window_run as engine
    data = inputs(tmp_path, files=1, paragraphs=2)
    config = RunConfig(window_size=1)
    root = tmp_path / "windows"
    compact_context = engine._pending_context
    monkeypatch.setattr(engine, "_pending_context", engine._pending_ledger if old_format == "list" else engine._pending_context_v1)
    run_windowed(inputs=data, output_dir=root, config=config, budget=budget(max_windows=1),
                  client=Client(lambda p: response(p, invalid=True)))
    def timeout(payload):
        raise TimeoutError("unknown remote outcome")
    stopped = run_windowed(inputs=data, output_dir=root, config=config, budget=budget(), client=Client(timeout))
    assert stopped.reason == "uncertain_dispatch_requires_explicit_retry"
    monkeypatch.setattr(engine, "_pending_context", compact_context)
    client = Client()
    result = run_windowed(inputs=data, output_dir=root, config=config, budget=budget(), client=client)
    assert result.reason == stopped.reason and result.cursor == stopped.cursor
    assert not client.requests and result.model_call_count == 0


def test_compact_pending_counts_are_reconstructed_not_trusted_from_ledger_hash(tmp_path):
    import fabric_kg_builder.domain.window_run as engine
    from fabric_kg_builder.domain.discovery import _seal
    data = inputs(tmp_path, files=1, paragraphs=1)
    config = RunConfig(window_size=1)
    result = run_windowed(inputs=data, output_dir=tmp_path / "windows", config=config,
                          budget=budget(), client=Client(lambda p: response(p, invalid=True)))
    log = result.logs[-1]
    exchange = log.exchanges[0]
    request_payload = exchange.request.model_dump(mode="json")["payload"]
    user = json.loads(request_payload["request"]["user"])
    registry = user["input"]["pending"]
    unchanged_hash = registry["full_ledger_hash"]
    registry["rows"][0][registry["columns"].index("count")] += 1
    assert registry["full_ledger_hash"] == unchanged_hash
    request_payload["request"]["user"] = canonical_json(user)
    request = _seal(engine._Ledger, kind="request", payload=request_payload)
    response_payload = exchange.response.model_dump(mode="json")["payload"]
    response_payload["request_hash"] = request.artifact_hash
    received = _seal(engine._Ledger, kind="response", payload=response_payload)
    altered = _seal(engine.WindowedLog, **{
        **{name: getattr(log, name) for name in type(log).model_fields if name != "artifact_hash"},
        "request_hashes": [request.artifact_hash], "response_hashes": [received.artifact_hash],
        "exchanges": [engine.WindowedExchange(request=request, response=received)],
    })
    before = engine._effective_window(engine.seed_snapshot(), result.chunks[:1], result.logs[0].exchanges, config).snapshot
    with pytest.raises(ValueError, match="source/context/schema drift"):
        engine._validate_exchanges(
            altered, result.chunks[-1:], before, prepared=result.prepared, context=result.context,
            manifest_hash=result.manifest_hash, model_hash=result.model_hash, config=config, history=result.logs[:-1])


@pytest.mark.parametrize("old_format", ["list", "v1.0"])
def test_completed_legacy_pending_run_keeps_authoritative_bytes_and_hash(tmp_path, monkeypatch, old_format):
    import fabric_kg_builder.domain.window_run as engine
    data = inputs(tmp_path, files=1, paragraphs=1)
    config = RunConfig(window_size=1)
    root = tmp_path / "windows"
    compact_context = engine._pending_context
    monkeypatch.setattr(engine, "_pending_context", engine._pending_ledger if old_format == "list" else engine._pending_context_v1)
    original = run_windowed(inputs=data, output_dir=root, config=config, budget=budget(),
                            client=Client(lambda p: response(p, invalid=True)))
    assert original.state == "complete"
    before = (root / "run.json").read_bytes()
    monkeypatch.setattr(engine, "_pending_context", compact_context)
    resumed = run_windowed(inputs=data, output_dir=root, config=config, client=Client())
    assert resumed.artifact_hash == original.artifact_hash
    assert resumed.model_call_count == 0 and (root / "run.json").read_bytes() == before


def test_hundreds_of_instance_like_proposed_ids_group_as_type_conflicts_not_instances(tmp_path):
    import fabric_kg_builder.domain.window_run as engine
    from fabric_kg_builder.contracts.base import canonical_sha256
    data = inputs(tmp_path, files=1, paragraphs=1)
    config = RunConfig(window_size=1, max_completion_tokens=32768, max_request_chars=60_000)
    names = ["Record", "Subject", "Requirement"]
    def instance_ids(payload):
        anchor = {"span_start": payload["offset_base"],
                  "span_end": payload["offset_base"] + len(payload["text"]), "quote": payload["text"]}
        candidates = [{
            "candidate_kind": "entity", "local_id": f"r{index}",
            "observed_type": name, "label": name, "anchors": [anchor],
        } for index, name in enumerate(names)]
        proposals = [{
            "action": "add_concept", "candidate_indices": [index % 3], "reason": "Observed type vocabulary",
            "concept": {
                "kind": "entity", "name": names[index % 3], "definition": f"A source {names[index % 3]}",
                "concept_id": f"{names[index % 3]}.keyboard_assembly.platinum_swiss_luxembourg_15_ep2_"
                              f"{payload['chunk']['chunk_id']}_{index}",
                "identity_policy": {"mode": "unresolved"},
            },
        } for index in range(300)]
        return {"candidates": candidates, "schema_proposals": proposals, "pending": []}
    client = Client(instance_ids)
    result = run_windowed(inputs=data, output_dir=tmp_path / "windows", config=config,
                          budget=budget(), client=client)
    assert result.state == "complete" and len(result.final_snapshot.concepts) == 3
    registry = engine._pending_context(result.logs)
    rows = pending_rows(registry)
    assert registry["unique_signature_count"] == 3
    assert registry["ledger_entry_count"] == 597
    assert sum(row["count"] for row in rows) == 597
    assert sum(row["proposed_id_count"] for row in rows) == 597
    assert sum(row["source_member_count"] for row in rows) == 597
    assert all(len(row["example_ids"]) == 3 for row in rows)
    assert registry["identity_effect"] == "no_instance_or_concept_identity_merge"
    assert len(registry["catalogs"]["identity_policy_hashes"]) == 1
    assert "signature_hash" not in registry["columns"]
    assert registry["catalog_hash"] == canonical_sha256(registry["catalogs"])
    assert registry["full_ledger_hash"] == canonical_sha256(engine._pending_ledger(result.logs))
    assert len(registry["membership_index_hash"]) == 64
    assert len(canonical_json(registry)) < 10_000
    assert "platinum_swiss_luxembourg" not in canonical_json(registry)
    assert len(canonical_json(engine._pending_context_v1(result.logs))) > config.max_request_chars
    assert all(len(canonical_json(request)) < config.max_request_chars for request in client.requests)
    assert all("platinum_swiss_luxembourg" in d.proposal["concept"]["concept_id"] for log in result.logs for d in log.diagnostics)
    assert len(result.final_mapping.records) == 6
    assert load_windowed_run(tmp_path / "windows").artifact_hash == result.artifact_hash


def test_columnar_registry_preserves_alias_target_id_scope(tmp_path):
    import fabric_kg_builder.domain.window_run as engine
    data = inputs(tmp_path, files=1, paragraphs=1)
    def aliases(payload):
        raw = response(payload)
        for target in ["missing:alias-target-a", "missing:alias-target-b"]:
            raw["schema_proposals"].append({
                "action": "add_alias", "candidate_indices": [0], "concept_id": target,
                "alias": "Shared", "reason": "Target unresolved",
            })
        return raw
    result = run_windowed(inputs=data, output_dir=tmp_path / "windows", config=RunConfig(window_size=1),
                          budget=budget(), client=Client(aliases))
    registry = engine._pending_context(result.logs)
    assert registry["unique_signature_count"] == 2
    targets = {registry["catalogs"]["identifiers"][pending_scope(registry, row)["target_concept"]]
               for row in pending_rows(registry)}
    assert targets == {"missing:alias-target-a", "missing:alias-target-b"}
    assert all(registry["catalogs"]["actions"][row["action"]] == "add_alias" for row in pending_rows(registry))


def test_columnar_registry_preserves_relationship_endpoint_direction_scopes(tmp_path):
    import fabric_kg_builder.domain.window_run as engine
    data = inputs(tmp_path, files=1, paragraphs=1)
    def endpoints(payload):
        raw = response(payload)
        raw["candidates"].append({
            "candidate_kind": "relationship", "source_local_id": "r", "target_local_id": "r",
            "observed_predicate": "relates", "direction": "source_to_target",
            "anchor": raw["candidates"][0]["anchors"][0],
        })
        for source, target in [("missing:type", "type:record"), ("type:record", "missing:type")]:
            raw["schema_proposals"].append({
                "action": "add_concept", "candidate_indices": [1], "reason": "Endpoint types unresolved",
                "concept": {
                    "concept_id": f"rel:{source}:{target}", "kind": "relationship", "name": "relates",
                    "definition": "Source relation", "source_type_ids": [source], "target_type_ids": [target],
                    "identity_policy": {"context_policy": "source"},
                },
            })
        return raw
    result = run_windowed(inputs=data, output_dir=tmp_path / "windows", config=RunConfig(window_size=1),
                          budget=budget(), client=Client(endpoints))
    registry = engine._pending_context(result.logs)
    assert registry["unique_signature_count"] == 2
    pairs = set()
    for row in pending_rows(registry):
        scope = pending_scope(registry, row)
        pairs.add((tuple(registry["catalogs"]["identifiers"][i] for i in scope["sources"]),
                   tuple(registry["catalogs"]["identifiers"][i] for i in scope["targets"])))
    assert pairs == {(("missing:type",), ("type:record",)), (("type:record",), ("missing:type",))}


def test_explicit_invocation_request_ceiling_admits_same_request_without_config_migration(tmp_path):
    data = inputs(tmp_path, files=1, paragraphs=1)
    config = RunConfig(max_request_chars=1024)
    root = tmp_path / "windows"
    client = Client()
    stopped = run_windowed(inputs=data, output_dir=root, config=config, budget=budget(), client=client)
    assert stopped.reason == "context_limit" and stopped.model_call_count == 0 and not client.requests
    manifest = (root / "manifest.json").read_bytes()
    saved_requests = {p: p.read_bytes() for p in (root / "requests").glob("*.json")}
    admitted = run_windowed(inputs=data, output_dir=root, config=config,
                            budget=budget(request_char_budget=512_000), client=client)
    assert admitted.state == "complete" and admitted.model_call_count == admitted.cursor
    assert admitted.config.max_request_chars == 1024
    assert (root / "manifest.json").read_bytes() == manifest
    assert all(p.read_bytes() == original for p, original in saved_requests.items())
    for path in (root / "dispatches").glob("*.json"):
        record = json.loads(path.read_text())["payload"]
        request = json.loads((root / "requests" / f"{record['request_hash']}.json").read_text())["payload"]["request"]
        assert record["request_chars"] == len(canonical_json(request))
        assert 1024 < record["request_chars"] <= record["effective_request_char_budget"] == 512_000
    responses = {p: p.read_bytes() for p in (root / "responses").glob("*.json")}
    completed = run_windowed(inputs=data, output_dir=root, config=config)
    assert completed.artifact_hash == admitted.artifact_hash and completed.model_call_count == 0
    assert all(p.read_bytes() == original for p, original in responses.items())


def test_old_dispatch_and_received_responses_reuse_under_lower_invocation_ceiling(tmp_path, monkeypatch):
    import fabric_kg_builder.domain.window_run as engine
    data = inputs(tmp_path, files=1, paragraphs=1)
    root = tmp_path / "windows"
    seal, write = engine._seal, engine._write
    def old_dispatch(model, **values):
        if model is engine._Ledger and values.get("kind") == "dispatch":
            values["payload"] = {k: v for k, v in values["payload"].items()
                                 if k not in {"request_chars", "effective_request_char_budget"}}
        return seal(model, **values)
    def before_commit(path, artifact):
        if isinstance(artifact, engine._Commit):
            raise RuntimeError("stop after durable responses")
        write(path, artifact)
    monkeypatch.setattr(engine, "_seal", old_dispatch)
    monkeypatch.setattr(engine, "_write", before_commit)
    client = Client()
    with pytest.raises(RuntimeError, match="durable responses"):
        run_windowed(inputs=data, output_dir=root, budget=budget(), client=client)
    old = {p: p.read_bytes() for folder in ["dispatches", "responses", "requests"]
           for p in (root / folder).glob("*.json")}
    assert all("effective_request_char_budget" not in json.loads(p.read_text())["payload"]
               for p in (root / "dispatches").glob("*.json"))
    monkeypatch.setattr(engine, "_seal", seal)
    monkeypatch.setattr(engine, "_write", write)
    result = run_windowed(inputs=data, output_dir=root, budget=RunBudget(request_char_budget=1024))
    assert result.state == "complete" and result.model_call_count == 0
    assert result.reused_response_count == len(client.requests)
    assert all(p.read_bytes() == original for p, original in old.items())
    assert load_windowed_run(root).artifact_hash == result.artifact_hash


def test_request_admission_override_is_opt_in_and_never_bypasses_call_budget(tmp_path):
    with pytest.raises(ValueError):
        RunBudget(request_char_budget=0)
    with pytest.raises(ValueError):
        RunBudget(request_char_budget=1023)
    assert RunBudget().request_char_budget is None
    data = inputs(tmp_path, files=1, paragraphs=1)
    client = Client()
    result = run_windowed(inputs=data, output_dir=tmp_path / "windows",
                          config=RunConfig(max_request_chars=1024),
                          budget=RunBudget(request_char_budget=512_000), client=client)
    assert result.reason == "model_budget_exhausted" and result.model_call_count == 0
    assert not client.requests and not list((tmp_path / "windows" / "dispatches").glob("*.json"))


@pytest.mark.parametrize("field,value", [
    ("pending", None), ("pending", "unstructured pending text"), ("pending", {"reason": "retain all content"}),
    ("working_context", None), ("working_context", ["unstructured annotation"]), ("working_context", "raw context"),
])
def test_cached_malformed_optional_metadata_is_quarantined_without_losing_valid_channels(tmp_path, monkeypatch, field, value):
    import fabric_kg_builder.domain.window_run as engine
    data = inputs(tmp_path, files=1, paragraphs=1)
    root = tmp_path / "windows"
    def metadata(payload):
        raw = response(payload)
        raw[field] = value
        return raw
    decoder = engine._decode
    def old_decoder(received, config):
        raw = decoder(received, config)
        if (not isinstance(raw.get("pending", []), list)
                or not isinstance(raw.get("working_context", {}), dict)):
            raise ValueError("invalid pending/working_context envelope")
        return raw
    monkeypatch.setattr(engine, "_decode", old_decoder)
    client = Client(metadata)
    before = run_windowed(inputs=data, output_dir=root, budget=budget(), client=client)
    assert before.cursor == 0 and before.state == "partial"
    response_bytes = {p: p.read_bytes() for p in (root / "responses").glob("*.json")}
    manifest = (root / "manifest.json").read_bytes()
    monkeypatch.setattr(engine, "_decode", decoder)
    recovered = run_windowed(inputs=data, output_dir=root, budget=RunBudget(), client=None)
    assert recovered.state == "complete" and recovered.model_call_count == 0
    assert recovered.reused_response_count == len(client.requests)
    assert all(r.status == "mapped" for r in recovered.final_mapping.records)
    for log in recovered.logs:
        assert not log.pending
        assert any(d.channel == field and d.proposal == value for d in log.diagnostics)
        assert all(e.response.payload["response"][field] == value for e in log.exchanges)
        if field == "working_context":
            assert all(context["annotations"] == {} for context in log.working_context)
    assert (root / "manifest.json").read_bytes() == manifest
    assert all(p.read_bytes() == original for p, original in response_bytes.items())
    completed = run_windowed(inputs=data, output_dir=root)
    assert completed.artifact_hash == recovered.artifact_hash and completed.model_call_count == 0


@pytest.mark.parametrize("channel,value", [
    ("candidates", None), ("candidates", {}), ("schema_proposals", None), ("schema_proposals", {}),
])
def test_optional_metadata_quarantine_never_waives_required_response_channels(tmp_path, channel, value):
    data = inputs(tmp_path, files=1, paragraphs=1)
    def invalid(payload):
        raw = response(payload)
        raw[channel] = value
        raw["pending"] = None
        raw["working_context"] = []
        return raw
    result = run_windowed(inputs=data, output_dir=tmp_path / "windows", budget=budget(), client=Client(invalid))
    assert result.state == "partial" and result.cursor == 0
    assert "response_validation_failed" in result.reason
    assert not result.chunks and not result.final_snapshot.concepts


def test_optional_metadata_quarantine_does_not_waive_candidate_grounding(tmp_path):
    data = inputs(tmp_path, files=1, paragraphs=1)
    def unsupported(payload):
        raw = response(payload)
        raw["candidates"][0]["anchors"][0]["quote"] = "Fabricated source statement"
        raw["pending"] = {"unstructured": "keep"}
        raw["working_context"] = ["not an annotation object"]
        return raw
    result = run_windowed(inputs=data, output_dir=tmp_path / "windows", budget=budget(), client=Client(unsupported))
    assert result.state == "partial" and result.reason == "bootstrap_blocked"
    assert not result.final_snapshot.concepts
    assert all(r.status == "quarantined" for r in result.final_mapping.records)
    assert {d.channel for log in result.logs for d in log.diagnostics} >= {"pending", "working_context"}
