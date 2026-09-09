"""Public pipeline tests with injected offline models, not live corpus acceptance."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.domain.discovery import discovery_observation_cache_path, load_discovery
from tests.unit.test_l1_stage import _intake
from tests.unit.test_question_routing import _mixed, _routing
from tests.unit.test_schema2_validation_stage import _Service


class Model:
    _config = SimpleNamespace(chat_deployment="offline-discovery-model")

    def __init__(self, *, unknown_second=False, numeric_property=False):
        self.calls = []
        self.observed = []
        self.targeted = []
        self.unknown_second = unknown_second
        self.numeric_property = numeric_property

    def complete_json(self, **request):
        self.calls.append(request)
        fields = request["json_schema"]["properties"]
        if "candidates" in fields:
            payload = json.loads(request["user"])
            discovery = "input" in payload
            data = payload["input"] if discovery else payload
            text = data["text"] if discovery else data["source_text"]
            offset = data["offset_base"] if discovery else data["source_identity"]["slice_start"]
            response = _Service("records").complete(
                prompt=request["user"], work_unit=SimpleNamespace(text=text, slice_start=offset),
            )
            for candidate in response["candidates"]:
                anchors = candidate.get("anchors", []) if candidate["candidate_kind"] == "entity" else [candidate.get("anchor")]
                for anchor in anchors:
                    if anchor:
                        anchor["model_authored_evidence_id"] = None
            if self.numeric_property:
                response["candidates"].append({
                    "candidate_kind": "property", "owner_local_id": "subject-1",
                    "observed_property": "voltage", "value": 80.5, "normalized_value": 80.5,
                    "temporal_key": None, "anchor": {
                        "span_start": offset, "span_end": offset + len(text.rstrip()), "quote": text.rstrip(),
                        "model_authored_evidence_id": None,
                    },
                })
            if discovery:
                self.observed.append(data["chunk"]["chunk_id"])
                if self.unknown_second and len(self.observed) == 2:
                    response["candidates"][0]["observed_type"] = "UnknownRecordClass"
            else:
                self.targeted.append(data["source_identity"])
            return response
        if "summary" in fields:
            data = json.loads(request["user"])["input"]
            return {
                "summary": "Source records describe subjects; retain the original source context and SQL requirements.",
                "covered_input_ids": [item["id"] for item in data["inputs"]],
            }
        sketch = _mixed()
        if self.numeric_property:
            sketch["properties"].append({
                "owner_key": "subject", "key": "voltage", "display_name": "Source Voltage",
                "value_type": "number", "required": False,
            })
        return sketch


def _invoke(args, *, model=None):
    result = CliRunner().invoke(cli, args, obj={"_design_client": model} if model else {})
    assert result.exit_code == 0, (result.output[:1500], str(result.exception)[:1500])
    return json.loads(result.output)


def _paths(tmp_path, count=3):
    source = tmp_path / "source"
    source.mkdir()
    for index in range(count):
        (source / f"records-{index}.html").write_text(
            "<html><body><p>A governed record describes a governed subject.</p></body></html>"
        )
    intake = _intake()
    intake["competency_questions"][-1] = {
        "id": "cq:q5", "question": "How many approved records are in the total population?",
        "business_critical": True, "routing": _routing().model_dump(mode="json"),
    }
    intake_path = tmp_path / "intake.json"
    intake_path.write_text(json.dumps(intake))
    output, cache = tmp_path / "discovery.json", tmp_path / "cache"
    args = [
        "domain", "discover", "--input", str(source), "--intake", str(intake_path),
        "--project-id", "project:records", "--out", str(output), "--cache-dir", str(cache),
        "--concurrency", "1",
    ]
    return source, intake_path, output, cache, args


def _approved(tmp_path, source, intake_path, discovery, model, *, discovery_acceptance=None):
    draft, evaluation = tmp_path / "draft.json", tmp_path / "evaluation.json"
    design_mode = ["--sample-only"] if discovery is None else [
        "--discovery", str(discovery),
        "--discovery-node", next(iter(load_discovery(discovery).document_summaries.values())),
    ]
    if discovery_acceptance is not None:
        design_mode = ["--discovery", str(discovery), "--discovery-acceptance", str(discovery_acceptance)]
    _invoke([
        "domain", "design", "--input", str(source), "--intake", str(intake_path),
        *design_mode, "--out", str(draft), "--live",
    ], model=model)
    evaluated = _invoke(["domain", "evaluate-design", "--file", str(draft), "--out", str(evaluation)])
    l1, domain = tmp_path / ".fkg" / "l1", tmp_path / "domain.yaml"
    compiled = _invoke([
        "domain", "compile-design", "--input", str(source), "--file", str(draft),
        "--evaluation", str(evaluation), "--accept-evaluation-hash", evaluated["result"]["evaluation_hash"],
        "--out-state", str(l1), "--out-domain", str(domain),
    ])
    result = CliRunner().invoke(cli, [
        "domain", "approve", "--file", str(domain), "--state-dir", str(l1),
        "--approved-by", "offline-reviewer", "--project-id", compiled["result"]["project_id"],
        "--run-id", compiled["result"]["run_id"], "--proposal-hash", compiled["result"]["proposal_hash"],
    ])
    assert result.exit_code == 0, (result.output[:1500], str(result.exception)[:1500])
    return l1, domain


def test_discover_default_plan_does_not_parse_call_or_write(tmp_path, monkeypatch):
    from fabric_kg_builder.domain import discovery
    from fabric_kg_builder.cli import domain_design_cmd

    _, _, out, cache, args = _paths(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("planning crossed the parse/model boundary")
    monkeypatch.setattr(discovery, "prepare_discovery_corpus", forbidden)
    monkeypatch.setattr(domain_design_cmd, "_build_client", forbidden)
    planned = _invoke(args)
    assert planned["corpus_entries"] == 3
    assert planned["chunk_count"] is None
    assert planned["model_calls"] == planned["writes"] == 0
    assert not out.exists() and not cache.exists()


def test_discovery_without_intake_precedes_real_questions_and_approved_replay(tmp_path, monkeypatch):
    from fabric_kg_builder.cli import domain_design_cmd
    from fabric_kg_builder.enrichment.schema2_validation_stage import run_l3
    from fabric_kg_builder.serving.lifecycle_projection import run_l4

    source, intake, out, cache, args = _paths(tmp_path, count=1)
    actual_intake = intake.read_text()
    intake.unlink()
    intake_index = args.index("--intake")
    del args[intake_index:intake_index + 2]
    model = Model()
    def forbidden(*args, **kwargs):
        raise AssertionError("discovery must not construct a synthetic L1 intake")
    with monkeypatch.context() as patch:
        patch.setattr(domain_design_cmd, "preflight_l1_inputs", forbidden)
        plan = _invoke(args)
        assert plan["writes"] == plan["model_calls"] == 0
        assert not out.exists() and not cache.exists() and not intake.exists()
        discovered = _invoke([*args, "--live"], model=model)
        assert discovered["status"] == "complete" and not intake.exists()
        original = load_discovery(out)
        assert original.business_context is None
        resumed = list(args)
        resumed[resumed.index("--out") + 1] = str(tmp_path / "resumed.json")
        before = len(model.calls)
        replay = _invoke([*resumed, "--resume", str(out), "--live", "--max-calls", "0"], model=model)
        assert replay["model_calls"] == 0 and len(model.calls) == before
    intake.write_text(actual_intake)
    l1, domain = _approved(tmp_path, source, intake, out, model)
    l2 = tmp_path / "l2"
    before = len(model.calls)
    replay = _invoke([
        "enrich", "--input", str(source), "--domain-file", str(domain),
        "--l1-state", str(l1), "--l2-state", str(l2), "--discovery", str(out), "--replay-only",
    ], model=model)
    assert replay["l2_model_calls"] == replay["targeted_model_calls"] == 0
    assert len(model.calls) == before
    rebound = [json.loads(path.read_text()) for path in (l2 / "source-units").glob("*.json")]
    assert {item["source_unit_id"] for item in rebound} == {item.source_unit_id for item in original.prepared.source_units}
    l3 = run_l3(state_root=tmp_path / "l3", l2_state_root=l2, l1_state_root=l1, domain_path=domain)
    l4 = run_l4(l3, state_root=tmp_path / "l4")
    assert len(l4.rows.semantic_asserted_relationships) == 1


def test_design_requires_full_discovery_or_explicit_sample_only(tmp_path):
    source, intake, _, _, _ = _paths(tmp_path, count=1)
    result = CliRunner().invoke(cli, [
        "domain", "design", "--input", str(source), "--intake", str(intake),
        "--out", str(tmp_path / "draft.json"),
    ])
    assert result.exit_code == 2
    assert "--discovery" in result.output and "--sample-only" in result.output


def test_partial_discovery_resumes_without_repeating_observed_chunks(tmp_path):
    source, intake, out, cache, args = _paths(tmp_path)
    model = Model()
    partial = _invoke([*args, "--live", "--max-calls", "1"], model=model)
    assert partial["status"] == "partial"
    assert partial["chunk_count"] == 3 and partial["pending_chunks"] == 2
    assert len(model.observed) == 1
    refused = CliRunner().invoke(cli, [
        "domain", "design", "--input", str(source), "--intake", str(intake),
        "--discovery", str(out), "--out", str(tmp_path / "not-created.json"),
    ])
    assert refused.exit_code == 1
    resumed_path = tmp_path / "resumed.json"
    resumed_args = [*args]
    resumed_args[resumed_args.index("--out") + 1] = str(resumed_path)
    completed = _invoke([*resumed_args, "--resume", str(out), "--live"], model=model)
    assert completed["status"] == "complete"
    assert len(model.observed) == len(set(model.observed)) == 3
    before = len(model.calls)
    resumed_args[resumed_args.index("--out") + 1] = str(tmp_path / "replayed.json")
    intake_index = resumed_args.index("--intake")
    del resumed_args[intake_index:intake_index + 2]
    replayed = _invoke([*resumed_args, "--resume", str(resumed_path), "--live", "--max-calls", "0"], model=model)
    assert replayed["status"] == "complete" and replayed["model_calls"] == 0
    assert len(model.calls) == before
    assert load_discovery(tmp_path / "replayed.json").business_context == load_discovery(resumed_path).business_context


def test_resume_retries_only_failed_preparation_and_reuses_successful_units_and_observations(tmp_path, monkeypatch):
    from collections import Counter
    from fabric_kg_builder.enrichment.schema2_sources import IndexedSourceCorpusReader

    _, _, out, _, args = _paths(tmp_path, count=2)
    original_read = IndexedSourceCorpusReader.read
    attempts = Counter()
    recovered = False
    def sometimes_unavailable(reader, entry):
        attempts[entry.relative_source_ref] += 1
        if entry.relative_source_ref == "records-1.html" and not recovered:
            raise ValueError("offline transient source preparation failure")
        return original_read(reader, entry)
    monkeypatch.setattr(IndexedSourceCorpusReader, "read", sometimes_unavailable)
    model = Model()
    partial = _invoke([*args, "--live"], model=model)
    assert partial["status"] == "partial" and partial["pending_sources"] == 1
    assert len(model.observed) == 1
    prior = load_discovery(out)
    recovered = True
    resumed = list(args)
    completed_path = tmp_path / "completed.json"
    resumed[resumed.index("--out") + 1] = str(completed_path)
    completed = _invoke([*resumed, "--resume", str(out), "--live"], model=model)
    assert completed["status"] == "complete" and completed["pending_sources"] == 0
    assert attempts == {"records-0.html": 1, "records-1.html": 2}
    assert len(model.observed) == len(set(model.observed)) == 2
    current = load_discovery(completed_path)
    assert prior.prepared.source_units[0] in current.prepared.source_units
    assert prior.chunks[0] in current.chunks
    before = len(model.calls)
    resumed[resumed.index("--out") + 1] = str(tmp_path / "complete-replay.json")
    replay = _invoke([*resumed, "--resume", str(completed_path), "--live", "--max-calls", "0"], model=model)
    assert replay["status"] == "complete" and replay["model_calls"] == 0
    assert len(model.calls) == before and attempts == {"records-0.html": 1, "records-1.html": 2}


def test_discovery_to_approved_replayed_l2_and_l4_preserves_context(tmp_path):
    from fabric_kg_builder.enrichment.schema2_validation_stage import run_l3
    from fabric_kg_builder.serving.lifecycle_projection import run_l4

    source, intake, out, _, args = _paths(tmp_path)
    for path in source.glob("*.html"):
        path.write_text("<p>A governed record describes a governed subject at 80.5 volts.</p>")
    model = Model(numeric_property=True)
    discovered = _invoke([*args, "--live"], model=model)
    assert discovered["status"] == "complete" and len(set(model.observed)) == 3
    l1, domain = _approved(tmp_path, source, intake, out, model)
    assert "all_corpus_sources_and_chunks" in model.calls[-1]["user"]
    assert '"level":"document"' in model.calls[-1]["user"]
    assert '"level":"corpus"' in model.calls[-1]["user"]
    before = len(model.calls)
    l2 = tmp_path / "l2"
    enrich = [
        "enrich", "--input", str(source), "--domain-file", str(domain),
        "--l1-state", str(l1), "--l2-state", str(l2), "--discovery", str(out), "--replay-only",
    ]
    plan = _invoke([*enrich, "--dry-run"], model=model)
    assert plan["reused_chunks"] == 3 and plan["writes"] == 0 and not l2.exists()
    result = _invoke(enrich, model=model)
    assert result["reused_chunks"] == 3 and result["pending_chunks"] == 0
    assert result["targeted_model_calls"] == result["l2_model_calls"] == 0
    assert len(model.calls) == before
    metrics = json.loads((l2 / "resource-metrics.json").read_text())
    assert metrics["foundry_calls"] == 0 and metrics["cache_hits"] == 3
    proposals = [
        item for path in (l2 / "proposed-candidates").glob("*.json") for item in json.loads(path.read_text())
    ]
    numbers = [item for item in proposals if item["candidate_kind"] == "property"]
    assert len(numbers) == 3 and all(item["normalized_value_json"] == "80.5" for item in numbers)
    original_units = {item.source_unit_id for item in load_discovery(out).prepared.source_units}
    rebound = [json.loads(path.read_text()) for path in (l2 / "source-units").glob("*.json")]
    assert {item["source_unit_id"] for item in rebound} == original_units
    l3_root = tmp_path / "l3"
    l3 = run_l3(state_root=l3_root, l2_state_root=l2, l1_state_root=l1, domain_path=domain)
    l4 = run_l4(l3, state_root=tmp_path / "l4")
    assert len(l4.rows.semantic_asserted_properties) == 3
    assert all(item["normalized_value_json"] == "80.5" for item in l4.rows.semantic_asserted_properties)
    exported = _invoke(["domain", "question-context", "--l4-run", str(l4.run_root), "--l3-root", str(l3_root)])
    question = exported["question_routing_context"]["questions"][0]
    assert question["question_id"] == "cq:q5" and question["business_critical"]
    assert question["routing"]["physical_binding_state"] == "unresolved"
    repeated = _invoke(enrich, model=model)
    assert repeated["l2_receipt_hash"] == result["l2_receipt_hash"]
    assert len(model.calls) == before


def test_only_unknown_chunk_is_reextracted_when_explicitly_requested(tmp_path):
    source, intake, out, _, args = _paths(tmp_path)
    model = Model(unknown_second=True)
    _invoke([*args, "--live"], model=model)
    l1, domain = _approved(tmp_path, source, intake, out, model)
    base = [
        "enrich", "--input", str(source), "--domain-file", str(domain),
        "--l1-state", str(l1), "--discovery", str(out),
    ]
    before = len(model.calls)
    pending = _invoke([*base, "--l2-state", str(tmp_path / "pending"), "--replay-only"], model=model)
    assert pending["pending_chunks"] == 1 and len(model.calls) == before
    assert pending["mapping_complete_chunks"] == 2 and pending["mapping_review_chunks"] == 1
    corrected = _invoke([
        *base, "--l2-state", str(tmp_path / "corrected"), "--reextract-pending",
        "--max-reextract-calls", "1",
    ], model=model)
    assert corrected["pending_chunks"] == 0 and corrected["targeted_model_calls"] == 1
    assert corrected["mapping_complete_chunks"] == 3 and corrected["mapping_review_chunks"] == 0
    assert corrected["original_corrected_candidates"] == 1
    assert len(model.targeted) == 1 and len(model.calls) == before + 1
    assert model.targeted[0]["source_unit_id"] == load_discovery(out).chunks[1].chunk.source_unit_id
    authority = json.loads((tmp_path / "corrected" / "discovery-reuse-authority.json").read_text())
    changed = authority["chunks"][1]
    correction = next(
        row for row in changed["original_candidate_dispositions"]
        if row["disposition"] == "superseded_by_explicit_correction"
    )
    old = load_discovery(out).chunks[1]
    original = next(row for row in old.candidate_grounding if row.observation_id == correction["observation_id"])
    assert correction["original_candidate_hash"] == original.raw_candidate_hash
    assert correction["original_index"] == original.candidate_index
    target_ids = {row["observation_id"] for row in changed["targeted_candidate_grounding"]}
    assert set(correction["replacement_observation_ids"]) <= target_ids
    assert target_ids.isdisjoint(row.observation_id for row in old.candidate_grounding)
    assert changed["targeted_request_hash"] != old.request_hash
    assert changed["targeted_raw_response_hash"]


@pytest.mark.parametrize("retry_kind", ["empty", "addition", "property-alternative"])
def test_empty_or_partial_targeted_cache_preserves_original_candidates_and_pending_mapping(tmp_path, retry_kind):
    class OmittingRetry(Model):
        def complete_json(self, **request):
            payload = json.loads(request["user"]) if "candidates" in request["json_schema"]["properties"] else {}
            if "source_identity" not in payload:
                response = super().complete_json(**request)
                if "input" in payload and retry_kind == "property-alternative" and len(self.observed) == 2:
                    response["candidates"][-1]["observed_property"] = "UnknownNumericField"
                return response
            self.calls.append(request)
            self.targeted.append(payload["source_identity"])
            original = payload["discovery_retry"]["original_candidates"]
            assert len(original) == 4
            if retry_kind == "empty":
                return {"candidates": []}
            property_ = dict(next(item for item in original if item["candidate_kind"] == "property"))
            if retry_kind == "addition":
                property_.update(value=81.5, normalized_value=81.5)
            else:
                property_["observed_property"] = "voltage"
            return {"candidates": [property_]}

    source, intake, out, _, args = _paths(tmp_path, count=2)
    for path in source.glob("*.html"):
        path.write_text("<p>A governed record describes a governed subject at 80.5 volts and 81.5 volts.</p>")
    model = OmittingRetry(unknown_second=retry_kind != "property-alternative", numeric_property=True)
    _invoke([*args, "--live"], model=model)
    original_bytes = out.read_bytes()
    l1, domain = _approved(tmp_path, source, intake, out, model)
    l2 = tmp_path / "l2"
    base = [
        "enrich", "--input", str(source), "--domain-file", str(domain),
        "--l1-state", str(l1), "--l2-state", str(l2), "--discovery", str(out),
    ]
    result = _invoke([*base, "--reextract-pending", "--max-reextract-calls", "1"], model=model)
    assert result["targeted_model_calls"] == 1 and result["pending_chunks"] == 1
    assert result["original_raw_candidates"] == result["original_retained_candidates"] == 8
    assert result["original_mapped_candidates"] + result["original_pending_candidates"] == 8
    assert result["original_pending_candidates"] > 0 and result["original_corrected_candidates"] == 0
    assert result["targeted_added_candidates"] == int(retry_kind != "empty")
    proposals = [
        item for path in (l2 / "proposed-candidates").glob("*.json") for item in json.loads(path.read_text())
    ]
    assert len(proposals) == 8 + int(retry_kind != "empty")
    authority = json.loads((l2 / "discovery-reuse-authority.json").read_text())
    original_rows = [row for chunk in authority["chunks"] for row in chunk["original_candidate_dispositions"]]
    assert len(original_rows) == 8
    assert all(row["verified_original_candidate_hash"] == row["effective_candidate_hash"] for row in original_rows)
    if retry_kind == "property-alternative":
        assert authority["chunks"][1]["targeted_candidate_dispositions"][0]["correspondence"] == "not_assumed"
    before = len(model.calls)
    replay = _invoke([*base, "--replay-only"], model=model)
    assert replay["targeted_model_calls"] == 0 and replay["reused_targeted_responses"] == 1
    assert replay["pending_chunks"] == 1 and replay["original_retained_candidates"] == 8
    assert replay["l2_receipt_hash"] == result["l2_receipt_hash"]
    assert len(model.calls) == before and out.read_bytes() == original_bytes
    assert model.targeted[0]["source_unit_id"] == load_discovery(out).chunks[1].chunk.source_unit_id


@pytest.mark.parametrize("quarantine_kind", ["partial_quote", "all_quotes", "case_collision"])
def test_grounding_quarantine_stays_accounted_pending_and_explicitly_retryable(tmp_path, quarantine_kind):
    quarantine_all = quarantine_kind != "partial_quote"
    class QuarantineModel(Model):
        def complete_json(self, **request):
            payload = json.loads(request["user"]) if "candidates" in request["json_schema"]["properties"] else {}
            if "source_identity" in payload:
                self.calls.append(request)
                self.targeted.append(payload["source_identity"])
                originals = payload["discovery_retry"]["original_candidates"]
                assert len(originals) == 4
                if quarantine_kind == "case_collision":
                    return {"candidates": originals}
                return {"candidates": [originals[0]] if quarantine_all else []}
            response = super().complete_json(**request)
            if "input" in payload and len(self.observed) == 2:
                if quarantine_kind == "case_collision":
                    response["candidates"][0]["local_id"] = "E"
                    response["candidates"][1]["local_id"] = "e"
                    response["candidates"][2].update(source_local_id="E", target_local_id="e")
                    response["candidates"][3]["owner_local_id"] = "e"
                    return response
                candidates = response["candidates"] if quarantine_all else response["candidates"][:1]
                for candidate in candidates:
                    anchors = candidate.get("anchors", []) if candidate["candidate_kind"] == "entity" else [candidate["anchor"]]
                    for anchor in anchors:
                        anchor.update(span_start=0, span_end=12, quote="ABSENT QUOTE")
            return response

    source, intake, out, cache, args = _paths(tmp_path, count=2)
    for path in source.glob("*.html"):
        path.write_text("<p>A governed record describes a governed subject at 80.5 volts.</p>")
    model = QuarantineModel(numeric_property=True)
    discovered = _invoke([*args, "--live"], model=model)
    quarantined_count = 4 if quarantine_all else 2
    assert discovered["status"] == "complete" and discovered["grounding_quality"] == "gaps"
    assert discovered["raw_candidate_count"] == 8
    assert discovered["verified_candidate_count"] == 8 - quarantined_count
    assert discovered["quarantined_candidate_count"] == quarantined_count
    assert discovered["received_chunks"] == discovered["accounted_chunks"] == 2
    run = load_discovery(out)
    bad = run.chunks[1]
    assert bad.status == "processed" and len(bad.response.candidates) == 4 - quarantined_count
    assert len(bad.candidate_grounding) == 4
    if quarantine_kind == "case_collision":
        assert [row["local_id"] for row in bad.raw_response["candidates"][:2]] == ["E", "e"]
        assert all("LOCAL_ENTITY_ID_DUPLICATED" in row.issue_codes for row in bad.candidate_grounding[:2])
        assert "ENDPOINT_NOT_GROUNDED" in bad.candidate_grounding[2].issue_codes
        assert "OWNER_NOT_GROUNDED" in bad.candidate_grounding[3].issue_codes
    assert all(discovery_observation_cache_path(cache, item).exists() for item in run.chunks)
    original_bytes = out.read_bytes()
    l1, domain = _approved(tmp_path, source, intake, out, model)
    before = len(model.calls)
    base = [
        "enrich", "--input", str(source), "--domain-file", str(domain),
        "--l1-state", str(l1), "--discovery", str(out),
    ]
    replay = _invoke([*base, "--l2-state", str(tmp_path / "replay"), "--replay-only"], model=model)
    assert replay["original_raw_candidates"] == replay["original_retained_candidates"] == 8
    assert replay["original_quarantined_candidates"] == replay["original_pending_candidates"] == quarantined_count
    assert replay["original_verified_candidates"] == replay["original_mapped_candidates"] == 8 - quarantined_count
    assert replay["pending_chunks"] == replay["mapping_review_chunks"] == 1
    assert replay["mapping_complete_chunks"] == 1 and len(model.calls) == before
    expected_ids = {row.observation_id for row in bad.candidate_grounding if row.disposition == "quarantined"}
    assert set(replay["pending"][0]["quarantined_observation_ids"]) == expected_ids
    repaired_root = tmp_path / "targeted"
    targeted = _invoke([
        *base, "--l2-state", str(repaired_root), "--reextract-pending", "--max-reextract-calls", "1",
    ], model=model)
    assert targeted["targeted_model_calls"] == 1 and targeted["pending_chunks"] == 1
    assert targeted["original_quarantined_candidates"] == quarantined_count
    assert targeted["targeted_quarantined_candidates"] == (4 if quarantine_kind == "case_collision" else int(quarantine_all))
    assert targeted["targeted_verified_candidates"] == targeted["targeted_added_candidates"] == 0
    from fabric_kg_builder.contracts.base import canonical_sha256
    authority = json.loads((repaired_root / "discovery-reuse-authority.json").read_text())
    rows = authority["chunks"][1]["original_candidate_dispositions"]
    for grounding in bad.candidate_grounding:
        row = rows[grounding.candidate_index]
        assert row["observation_id"] == grounding.observation_id
        assert row["original_candidate_hash"] == grounding.raw_candidate_hash
        if grounding.disposition == "verified":
            assert row["verified_original_candidate_hash"] == canonical_sha256(
                bad.response.candidates[grounding.verified_candidate_index]
            )
    proposals = [
        item for path in (repaired_root / "proposed-candidates").glob("*.json")
        for item in json.loads(path.read_text())
    ]
    assert len(proposals) == 8 - quarantined_count
    before = len(model.calls)
    cached = _invoke([*base, "--l2-state", str(repaired_root), "--replay-only"], model=model)
    assert cached["pending_chunks"] == 1 and cached["targeted_model_calls"] == 0
    assert len(model.calls) == before and out.read_bytes() == original_bytes


def test_unique_casefolded_references_ground_and_replay_without_model_calls(tmp_path):
    class CasefoldModel(Model):
        def complete_json(self, **request):
            response = super().complete_json(**request)
            if "candidates" in response:
                response["candidates"][0]["local_id"] = "E"
                response["candidates"][1]["local_id"] = "F"
                response["candidates"][2].update(source_local_id="e", target_local_id="f")
                response["candidates"][3]["owner_local_id"] = "f"
            return response

    source, intake, out, _, args = _paths(tmp_path, count=1)
    next(source.glob("*.html")).write_text("<p>A governed record describes a governed subject at 80.5 volts.</p>")
    model = CasefoldModel(numeric_property=True)
    discovered = _invoke([*args, "--live"], model=model)
    assert discovered["verified_candidate_count"] == 4 and discovered["quarantined_candidate_count"] == 0
    original = out.read_bytes()
    l1, domain = _approved(tmp_path, source, intake, out, model)
    before = len(model.calls)
    replay = _invoke([
        "enrich", "--input", str(source), "--domain-file", str(domain),
        "--l1-state", str(l1), "--l2-state", str(tmp_path / "l2"),
        "--discovery", str(out), "--reextract-pending", "--max-reextract-calls", "1",
    ], model=model)
    assert replay["original_mapped_candidates"] == 4 and replay["pending_chunks"] == 0
    assert replay["targeted_model_calls"] == replay["l2_model_calls"] == 0
    assert len(model.calls) == before and out.read_bytes() == original


def test_source_drift_fails_before_replay_or_model_construction(tmp_path):
    source, intake, out, _, args = _paths(tmp_path, count=1)
    model = Model()
    _invoke([*args, "--live"], model=model)
    l1, domain = _approved(tmp_path, source, intake, out, model)
    before = len(model.calls)
    next(source.glob("*.html")).write_text("<p>Changed source.</p>")
    result = CliRunner().invoke(cli, [
        "enrich", "--input", str(source), "--domain-file", str(domain),
        "--l1-state", str(l1), "--l2-state", str(tmp_path / "l2"),
        "--discovery", str(out), "--reextract-pending",
    ], obj={"_design_client": model})
    assert result.exit_code == 1 and len(model.calls) == before
    assert not (tmp_path / "l2").exists()


def test_resume_rejects_changed_response_cache_without_repeating_calls(tmp_path):
    _, _, out, cache, args = _paths(tmp_path, count=1)
    model = Model()
    _invoke([*args, "--live"], model=model)
    run = load_discovery(out)
    path = discovery_observation_cache_path(cache, run.chunks[0])
    path.write_text("{}")
    resumed = list(args)
    resumed[resumed.index("--out") + 1] = str(tmp_path / "different.json")
    before = len(model.calls)
    result = CliRunner().invoke(cli, [*resumed, "--resume", str(out), "--live"], obj={"_design_client": model})
    assert result.exit_code == 1 and len(model.calls) == before


@pytest.mark.parametrize("legacy_case_collision", [False, True])
def test_public_resume_revalidates_received_legacy_failed_raw_without_calls_or_overwrites(tmp_path, monkeypatch, legacy_case_collision):
    from copy import deepcopy
    from fabric_kg_builder.domain import discovery as core
    from fabric_kg_builder.enrichment.schema2_sources import IndexedSourceCorpusReader

    _, _, out, cache, args = _paths(tmp_path, count=1)
    model = Model()
    _invoke([*args, "--live", "--max-calls", "0"], model=model)
    initial = load_discovery(out)
    chunk = initial.chunks[0].chunk
    unit = initial.prepared.source_units[0]
    prior_budget = initial.budget.model_copy(update={"max_calls": 1})
    _, key = core._chunk_request(
        initial.prepared, chunk, {unit.source_unit_id: unit}, prior_budget,
        initial.model_version, initial.model_hash, initial.business_context,
        core.LEGACY_DISCOVERY_PROMPT_VERSION,
    )
    raw = _Service("records").complete(
        prompt="", work_unit=SimpleNamespace(text=unit.text, slice_start=chunk.slice_start),
    )
    raw["candidates"][2]["anchor"]["model_authored_evidence_id"] = None
    if legacy_case_collision:
        raw["candidates"][0]["local_id"] = "E"
        raw["candidates"][1]["local_id"] = "e"
        raw["candidates"][2].update(source_local_id="E", target_local_id="e")
        grounded, ledger = core._ground_response(raw, unit, chunk, casefold_references=False)
        assert len(grounded.candidates) == 3
        record = core._seal(
            core.ChunkObservation, chunk=chunk, request_hash=key, status="processed",
            raw_response=raw, response=grounded, candidate_grounding=ledger,
            verifier_version=core.LEGACY_GROUNDING_VERSION,
            request_prompt_version=core.LEGACY_DISCOVERY_PROMPT_VERSION,
        )
    else:
        bad = deepcopy(raw["candidates"][0])
        bad["local_id"] = "bad-observation"
        bad["anchors"][0].update(span_start=0, span_end=12, quote="ABSENT QUOTE")
        raw["candidates"].append(bad)
        record = core._seal(
            core.ChunkObservation, chunk=chunk, request_hash=key, status="failed",
            raw_response=raw, reason="legacy whole-chunk quote rejection",
        )
    cached_path = discovery_observation_cache_path(cache, record)
    core._write(cached_path, record)
    prior = core._seal(
        core.DiscoveryRun, prompt_version=core.LEGACY_DISCOVERY_PROMPT_VERSION,
        verifier_version=core.LEGACY_GROUNDING_VERSION if legacy_case_collision else None,
        prepared=initial.prepared, model_version=initial.model_version, model_hash=initial.model_hash,
        business_context=initial.business_context, budget=prior_budget,
        chunks=[record], summaries=[], document_summaries={}, corpus_summary_id=None,
        status="partial", model_call_count=1, reserved_tokens=0, reused_response_count=0,
    )
    prior_path = tmp_path / "legacy.json"
    core.save_discovery(prior_path, prior)
    protected = {path: path.read_bytes() for path in (prior_path, cached_path)}
    def forbidden(*args, **kwargs):
        raise AssertionError("received legacy raw must not be reparsed or re-observed")
    monkeypatch.setattr(IndexedSourceCorpusReader, "read", forbidden)
    monkeypatch.setattr(model, "complete_json", forbidden)
    resumed = list(args)
    revalidated_path = tmp_path / "revalidated.json"
    resumed[resumed.index("--out") + 1] = str(revalidated_path)
    result = _invoke([*resumed, "--resume", str(prior_path), "--live", "--max-calls", "0"], model=model)
    assert result["model_calls"] == 0 and result["accounted_chunks"] == 1
    assert result["raw_candidate_count"] == (3 if legacy_case_collision else 4)
    assert result["quarantined_candidate_count"] == (3 if legacy_case_collision else 1)
    assert result["verified_candidate_count"] == (0 if legacy_case_collision else 3)
    assert result["status"] == "partial"
    current = load_discovery(revalidated_path)
    assert current.chunks[0].raw_response == raw
    assert current.chunks[0].request_hash == record.request_hash
    resumed[resumed.index("--out") + 1] = str(tmp_path / "revalidated-again.json")
    again = _invoke([*resumed, "--resume", str(revalidated_path), "--live", "--max-calls", "0"], model=model)
    assert again["model_calls"] == 0 and again["quarantined_candidate_count"] == (3 if legacy_case_collision else 1)
    assert all(path.read_bytes() == content for path, content in protected.items())


def test_approved_discovery_binding_rejects_another_run_of_same_corpus(tmp_path):
    source, intake, out, _, args = _paths(tmp_path, count=1)
    model = Model()
    _invoke([*args, "--live"], model=model)
    l1, domain = _approved(tmp_path, source, intake, out, model)
    replacement = tmp_path / "replacement.json"
    resumed = list(args)
    resumed[resumed.index("--out") + 1] = str(replacement)
    _invoke([*resumed, "--resume", str(out), "--live", "--max-calls", "0"], model=model)
    before = len(model.calls)
    result = CliRunner().invoke(cli, [
        "enrich", "--input", str(source), "--domain-file", str(domain),
        "--l1-state", str(l1), "--l2-state", str(tmp_path / "l2"),
        "--discovery", str(replacement), "--replay-only",
    ], obj={"_design_client": model})
    assert result.exit_code == 1 and "APPROVED_RUN_DRIFT" in result.output
    assert load_discovery(out).run_hash in result.output
    assert len(model.calls) == before


def test_discovery_bound_domain_cannot_silently_launch_a_second_model_pass(tmp_path):
    source, intake, out, _, args = _paths(tmp_path, count=1)
    model = Model()
    _invoke([*args, "--live"], model=model)
    l1, domain = _approved(tmp_path, source, intake, out, model)
    before = len(model.calls)
    result = CliRunner().invoke(cli, [
        "enrich", "--input", str(source), "--domain-file", str(domain),
        "--l1-state", str(l1), "--l2-state", str(tmp_path / "l2"),
    ], obj={"_design_client": model})
    assert result.exit_code == 2 and "second model pass" in result.output
    assert load_discovery(out).run_hash in result.output and "--discovery FILE" in result.output
    assert len(model.calls) == before and not (tmp_path / "l2").exists()


def test_legacy_approved_domain_without_discovery_preserves_existing_extraction_route(tmp_path):
    from fabric_kg_builder.domain.service import load_domain_contract

    source, intake, _, _, _ = _paths(tmp_path, count=1)
    model = Model()
    l1, domain = _approved(tmp_path, source, intake, None, model)
    assert load_domain_contract(domain).discovery_run_hash is None
    l2 = tmp_path / "l2"
    before = len(model.calls)
    result = CliRunner().invoke(cli, [
        "enrich", "--input", str(source), "--domain-file", str(domain),
        "--l1-state", str(l1), "--l2-state", str(l2),
    ], obj={"_foundry_client": model})
    assert result.exit_code == 0, result.output
    assert "schema-2 extraction succeeded" in result.output
    assert len(model.calls) == before + 1
    assert json.loads((l2 / "resource-metrics.json").read_text())["foundry_calls"] == 1
    assert not (l2 / "discovery-reuse-authority.json").exists()


def test_cached_source_units_are_not_reparsed_for_planning_resume_or_approved_replay(tmp_path, monkeypatch):
    from fabric_kg_builder.enrichment.schema2_sources import IndexedSourceCorpusReader

    source, intake, out, _, args = _paths(tmp_path, count=1)
    model = Model()
    _invoke([*args, "--live"], model=model)
    def forbidden(*args, **kwargs):
        raise AssertionError("cached SourceUnits must not be parsed again")
    monkeypatch.setattr(IndexedSourceCorpusReader, "read", forbidden)
    l1, domain = _approved(tmp_path, source, intake, out, model)
    _invoke([
        "domain", "design", "--input", str(source), "--intake", str(intake),
        "--discovery", str(out), "--out", str(tmp_path / "planned-only.json"),
    ])
    resumed = list(args)
    resumed[resumed.index("--out") + 1] = str(tmp_path / "resumed.json")
    _invoke([*resumed, "--resume", str(out), "--live", "--max-calls", "0"], model=model)
    before = len(model.calls)
    replay = _invoke([
        "enrich", "--input", str(source), "--domain-file", str(domain),
        "--l1-state", str(l1), "--l2-state", str(tmp_path / "l2"),
        "--discovery", str(out), "--replay-only",
    ], model=model)
    assert replay["l2_model_calls"] == replay["targeted_model_calls"] == 0
    assert len(model.calls) == before


def test_multiple_chunks_share_a_source_unit_without_reference_collisions_or_model_resplitting(tmp_path):
    import re
    from fabric_kg_builder.enrichment.schema2_validation_stage import run_l3
    from fabric_kg_builder.serving.lifecycle_projection import run_l4

    class RepeatedLocalIds(Model):
        def __init__(self):
            super().__init__()
            self.relationships = 0

        def complete_json(self, **request):
            payload = json.loads(request["user"]) if "candidates" in request["json_schema"]["properties"] else {}
            if "input" not in payload:
                return super().complete_json(**request)
            self.calls.append(request)
            data = payload["input"]
            self.observed.append(data["chunk"]["chunk_id"])
            candidates = []
            for index, match in enumerate(re.finditer(r"A governed record describes a governed subject\.", data["text"])):
                raw = _Service("records").complete(
                    prompt=request["user"],
                    work_unit=SimpleNamespace(text=match.group(), slice_start=data["offset_base"] + match.start()),
                )["candidates"]
                raw[0]["local_id"] = f"record-{index}"
                raw[1]["local_id"] = f"subject-{index}"
                raw[2]["source_local_id"] = raw[0]["local_id"]
                raw[2]["target_local_id"] = raw[1]["local_id"]
                raw[2]["anchor"]["model_authored_evidence_id"] = None
                candidates.extend(raw)
                self.relationships += 1
            return {"candidates": candidates}

    source, intake, out, _, args = _paths(tmp_path, count=1)
    next(source.glob("*.html")).write_text(
        "<p>" + " ".join(["A governed record describes a governed subject."] * 50) + "</p>"
    )
    model = RepeatedLocalIds()
    discovered = _invoke([*args, "--live", "--max-chunk-chars", "512"], model=model)
    assert discovered["chunk_count"] > 1
    l1, domain = _approved(tmp_path, source, intake, out, model)
    before = len(model.calls)
    l2 = tmp_path / "l2"
    replay = _invoke([
        "enrich", "--input", str(source), "--domain-file", str(domain),
        "--l1-state", str(l1), "--l2-state", str(l2), "--discovery", str(out), "--replay-only",
    ], model=model)
    assert replay["pending_chunks"] == 0 and len(model.calls) == before
    checkpoints = json.loads((l2 / "checkpoint.json").read_text())["work_units"]
    assert any(item["status"] == "split" for item in checkpoints.values())
    l3 = run_l3(state_root=tmp_path / "l3", l2_state_root=l2, l1_state_root=l1, domain_path=domain)
    l4 = run_l4(l3, state_root=tmp_path / "l4")
    assert len(l4.rows.semantic_asserted_relationships) == model.relationships
    assert len(l4.rows.semantic_asserted_entities) == 2 * model.relationships
