"""Public CLI/replay consumption of candidate-array scoped envelope accounting."""

import json
from copy import deepcopy

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.domain import discovery as core
from tests.unit.test_domain_discovery_cli import Model, _approved, _invoke, _paths


@pytest.mark.parametrize("empty_array", [False, True])
@pytest.mark.parametrize("target_has_candidate", [False, True])
def test_envelope_extras_remain_pending_without_becoming_candidates(tmp_path, empty_array, target_has_candidate):
    marker = "TOP_LEVEL_UNGROUNDED_SENTINEL"

    class EnvelopeModel(Model):
        def complete_json(self, **request):
            response = super().complete_json(**request)
            if "candidates" in request["json_schema"]["properties"]:
                payload = json.loads(request["user"])
                if "input" in payload and len(self.observed) == 2:
                    if empty_array:
                        response["candidates"] = []
                    response.update(entities=[{"unapproved": marker}], notes=marker)
                elif "source_identity" in payload:
                    return {
                        "candidates": response["candidates"][:1] if target_has_candidate else [],
                        "notes": marker + "_TARGETED",
                    }
            return response

    source, intake, out, _, args = _paths(tmp_path, count=2)
    model = EnvelopeModel()
    discovered = _invoke([*args, "--live"], model=model)
    run = core.load_discovery(out)
    bad = run.chunks[1]
    raw_count = 3 if empty_array else 6
    assert bad.status == "processed"
    assert bad.candidate_grounding_scope == "raw_response.candidates"
    assert len(bad.candidate_grounding) == (0 if empty_array else 3)
    assert bad.raw_response["notes"] == marker
    assert discovered["raw_candidate_count"] == discovered["verified_candidate_count"] == raw_count
    assert discovered["candidate_ledger_entry_count"] == raw_count
    assert discovered["unaccounted_candidate_count"] == 0 and discovered["candidate_ledger_complete"] is True
    assert discovered["candidate_ledger_scope"] == "received_raw_response.candidates_only"
    assert discovered["unaccounted_raw_candidate_count"] == discovered["unaccounted_raw_array_count"] == 0
    assert discovered["received_array_ledger_complete"] is True
    assert "candidate_ledger_entry_count" not in core.discovery_grounding_report(run)
    assert discovered["envelope_anomaly_count"] == 1
    assert discovered["envelope_quarantined_field_count"] == 2
    assert discovered["grounding_quality"] == "gaps"
    protected = out.read_bytes()
    l1, domain = _approved(tmp_path, source, intake, out, model)
    base = [
        "enrich", "--input", str(source), "--domain-file", str(domain),
        "--l1-state", str(l1), "--discovery", str(out),
    ]
    before = len(model.calls)
    replay = _invoke([*base, "--l2-state", str(tmp_path / "replay"), "--replay-only"], model=model)
    assert len(model.calls) == before
    assert replay["original_raw_candidates"] == replay["original_candidate_ledger_entries"] == raw_count
    assert replay["original_candidate_ledger_complete"] is True
    assert replay["original_unaccounted_candidates"] == replay["original_quarantined_candidates"] == 0
    assert replay["original_envelope_anomaly_count"] == replay["pending_chunks"] == 1
    assert replay["pending"][0]["envelope_anomaly_ids"] == [bad.envelope_anomalies[0].observation_id]
    l2 = tmp_path / "targeted"
    targeted = _invoke([
        *base, "--l2-state", str(l2), "--reextract-pending", "--max-reextract-calls", "1",
    ], model=model)
    assert targeted["targeted_model_calls"] == 1
    assert targeted["targeted_received_candidates"] == targeted["targeted_candidate_ledger_entries"] == int(target_has_candidate)
    assert targeted["targeted_candidate_ledger_complete"] is True
    assert targeted["targeted_review_candidates"] == targeted["targeted_response_failures"] == 0
    assert targeted["targeted_envelope_anomaly_count"] == targeted["pending_chunks"] == 1
    assert targeted["original_envelope_anomaly_count"] == 1
    authority = json.loads((l2 / "discovery-reuse-authority.json").read_text())
    chunk = next(item for item in authority["chunks"] if item["chunk_id"] == bad.chunk.chunk_id)
    original = chunk["original_envelope_anomalies"][0]
    target = chunk["targeted_envelope_anomalies"][0]
    assert original["observation_id"] == bad.envelope_anomalies[0].observation_id
    assert original["extra_fields_hash"] == canonical_sha256({
        "entities": [{"unapproved": marker}], "notes": marker,
    })
    assert target["observation_id"] != original["observation_id"]
    assert target["raw_response_hash"] == chunk["targeted_raw_response_hash"]
    assert marker not in json.dumps(authority)
    assert all(marker not in request["user"] for request in model.calls)
    target_raw = json.loads(next((l2 / "discovery-targeted-cache").glob("*.json")).read_text())
    assert target_raw["response"]["notes"] == marker + "_TARGETED"
    assert len(target_raw["response"]["candidates"]) == int(target_has_candidate)
    if target_has_candidate:
        assert chunk["targeted_candidate_grounding"][0]["raw_candidate_hash"] == canonical_sha256(
            target_raw["response"]["candidates"][0]
        )
    before = len(model.calls)
    cached = _invoke([*base, "--l2-state", str(l2), "--replay-only"], model=model)
    assert cached["targeted_model_calls"] == 0 and cached["reused_targeted_responses"] == 1
    assert cached["targeted_envelope_anomaly_count"] == cached["pending_chunks"] == 1
    assert len(model.calls) == before and out.read_bytes() == protected


def test_invalid_target_envelope_is_a_response_issue_not_a_candidate(tmp_path):
    class InvalidTarget(Model):
        def complete_json(self, **request):
            if "candidates" in request["json_schema"]["properties"]:
                payload = json.loads(request["user"])
                if "source_identity" in payload:
                    self.calls.append(request)
                    return {"unexpected": ["NOT_A_CANDIDATE_ARRAY"]}
            return super().complete_json(**request)

    source, intake, out, _, args = _paths(tmp_path, count=2)
    model = InvalidTarget(unknown_second=True)
    _invoke([*args, "--live"], model=model)
    l1, domain = _approved(tmp_path, source, intake, out, model)
    l2 = tmp_path / "l2"
    result = _invoke([
        "enrich", "--input", str(source), "--domain-file", str(domain),
        "--l1-state", str(l1), "--l2-state", str(l2), "--discovery", str(out),
        "--reextract-pending", "--max-reextract-calls", "1",
    ], model=model)
    assert result["targeted_response_failures"] == result["targeted_missing_candidate_arrays"] == 1
    assert result["targeted_received_candidates"] == result["targeted_review_candidates"] == 0
    assert result["targeted_envelope_anomaly_count"] == 0
    assert result["pending_chunks"] == 1
    raw = json.loads(next((l2 / "discovery-targeted-cache").glob("*.json")).read_text())["response"]
    assert raw == {"unexpected": ["NOT_A_CANDIDATE_ARRAY"]}


@pytest.mark.parametrize("corruption", ["missing", "malformed", "different"])
def test_failed_summary_cache_drift_blocks_public_resume_before_calls(tmp_path, corruption):
    class BadSummary(Model):
        def complete_json(self, **request):
            if "summary" in request["json_schema"]["properties"]:
                self.calls.append(request)
                return {"summary": "Retain this whole failed response.", "covered_input_ids": ["invented-id"],
                        "stray": {"preserve": True}}
            return super().complete_json(**request)

    _, _, out, cache, args = _paths(tmp_path, count=1)
    model = BadSummary()
    result = _invoke([*args, "--live"], model=model)
    run = core.load_discovery(out)
    assert result["status"] == "partial" and result["retained_failed_summary_attempts"] > 0
    assert run.failed_summaries and not run.document_summaries
    failure = run.failed_summaries[0]
    path = core.discovery_summary_failure_cache_path(cache, failure)
    assert json.loads(path.read_text())["raw_response"]["stray"] == {"preserve": True}
    if corruption == "missing":
        path.unlink()
    elif corruption == "malformed":
        path.write_text("{")
    else:
        changed = failure.model_dump(mode="json")
        changed.pop("artifact_hash")
        changed["reason"] = "different-but-sealed-failure"
        replacement = core._seal(core.FailedDiscoverySummary, **changed)
        path.write_text(replacement.model_dump_json())
    before = len(model.calls)
    resumed = list(args)
    resumed[resumed.index("--out") + 1] = str(tmp_path / "resumed.json")
    response = CliRunner().invoke(
        cli, [*resumed, "--resume", str(out), "--live"], obj={"_design_client": model},
    )
    assert response.exit_code != 0 and "DISCOVERY_RESUME_CACHE_DRIFT" in response.output
    assert len(model.calls) == before
    assert not (tmp_path / "resumed.json").exists()


def test_failed_summary_valid_cache_survives_zero_call_resume_and_bounded_summary_retry(tmp_path, monkeypatch):
    class FailingSummary(Model):
        fail = True

        def complete_json(self, **request):
            if self.fail and "summary" in request["json_schema"]["properties"]:
                self.calls.append(request)
                return {"summary": "Received but invalid accounting", "covered_input_ids": ["wrong-id"]}
            return super().complete_json(**request)

    _, _, out, cache, args = _paths(tmp_path, count=1)
    model = FailingSummary()
    _invoke([*args, "--live"], model=model)
    prior = core.load_discovery(out)
    protected = {out: out.read_bytes()}
    protected.update({
        core.discovery_summary_failure_cache_path(cache, failure):
        core.discovery_summary_failure_cache_path(cache, failure).read_bytes()
        for failure in prior.failed_summaries
    })
    assert len(protected) > 1
    zero_path = tmp_path / "zero-budget.json"
    resumed = list(args)
    resumed[resumed.index("--out") + 1] = str(zero_path)
    before = len(model.calls)
    with monkeypatch.context() as patch:
        patch.setattr(model, "complete_json", lambda **_kwargs: pytest.fail("Zero budget cannot infer"))
        result = _invoke([*resumed, "--resume", str(out), "--live", "--max-calls", "0"], model=model)
    assert result["model_calls"] == 0 and result["status"] == "partial"
    assert not core.load_discovery(zero_path).document_summaries
    assert len(model.calls) == before
    observed = list(model.observed)
    model.fail = False
    completed = tmp_path / "summary-retry.json"
    resumed[resumed.index("--out") + 1] = str(completed)
    result = _invoke([*resumed, "--resume", str(zero_path), "--live", "--max-calls", "2"], model=model)
    assert result["status"] == "complete" and 0 < result["model_calls"] <= 2
    assert model.observed == observed
    assert core.load_discovery(completed).chunks == prior.chunks
    assert all(path.read_bytes() == original for path, original in protected.items())


def test_compatible_21_leaf_and_summary_caches_survive_public_22_resume(tmp_path, monkeypatch):
    _, _, out, cache, args = _paths(tmp_path, count=1)
    model = Model()
    _invoke([*args, "--live"], model=model)
    run = core.load_discovery(out)
    values = {
        key: deepcopy(getattr(run.chunks[0], key))
        for key in core.ChunkObservation.model_fields if key != "artifact_hash"
    }
    values.pop("candidate_grounding_scope", None)
    values.pop("envelope_anomalies", None)
    values["verifier_version"] = "discovery-grounding/2.1.0"
    old_leaf = core._seal(core.ChunkObservation, **values)
    old_path = core.discovery_observation_cache_path(cache, old_leaf)
    core._write(old_path, old_leaf)
    prior_values = {
        key: deepcopy(getattr(run, key))
        for key in core.DiscoveryRun.model_fields if key != "artifact_hash"
    }
    prior_values.update(
        verifier_version="discovery-grounding/2.1.0", chunks=[old_leaf],
        summaries=[], document_summaries={}, corpus_summary_id=None, status="partial",
    )
    prior = core._seal(core.DiscoveryRun, **prior_values)
    old_run = tmp_path / "old-21.json"
    core.save_discovery(old_run, prior)
    # Build summary caches for the actual old leaf hash, then retain them under
    # a 2.1 run header. This makes the test about leaf/cache identity, not hashes
    # synthesized against a different 2.2 leaf.
    rebuilt_path = tmp_path / "rebuilt.json"
    resumed = list(args)
    resumed[resumed.index("--out") + 1] = str(rebuilt_path)
    _invoke([*resumed, "--resume", str(old_run), "--live"], model=model)
    rebuilt = core.load_discovery(rebuilt_path)
    assert rebuilt.chunks[0] == old_leaf
    assert rebuilt.status == "complete"
    protected = {old_path: old_path.read_bytes()}
    protected.update({
        cache / "summaries" / f"{item.request_hash}.json":
        (cache / "summaries" / f"{item.request_hash}.json").read_bytes()
        for item in rebuilt.summaries
    })
    monkeypatch.setattr(model, "complete_json", lambda **_kwargs: pytest.fail("No chunk/summary calls expected"))
    resumed[resumed.index("--out") + 1] = str(tmp_path / "again.json")
    result = _invoke([*resumed, "--resume", str(rebuilt_path), "--live", "--max-calls", "0"], model=model)
    again = core.load_discovery(tmp_path / "again.json")
    assert result["model_calls"] == 0 and result["status"] == "complete"
    assert again.chunks[0] == old_leaf and again.summaries == rebuilt.summaries
    assert all(path.read_bytes() == original for path, original in protected.items())


def test_public_zero_call_resume_salvages_legacy_extra_root_array_without_overwriting_raw(tmp_path, monkeypatch):
    _, _, out, cache, args = _paths(tmp_path, count=1)
    model = Model()
    _invoke([*args, "--live"], model=model)
    initial = core.load_discovery(out)
    original = initial.chunks[0]
    raw = {**deepcopy(original.raw_response), "entities": [{"not_array_authority": True}]}
    failed = core._seal(
        core.ChunkObservation, chunk=original.chunk, request_hash=original.request_hash,
        status="failed", raw_response=raw, reason="legacy strict-envelope extra fields",
        verifier_version="discovery-grounding/2.1.0",
        request_prompt_version=original.request_prompt_version,
    )
    cached = core.discovery_observation_cache_path(cache, failed)
    core._write(cached, failed)
    values = {
        key: deepcopy(getattr(initial, key))
        for key in core.DiscoveryRun.model_fields if key != "artifact_hash"
    }
    values.update(
        verifier_version="discovery-grounding/2.1.0", chunks=[failed],
        summaries=[], document_summaries={}, corpus_summary_id=None, status="partial",
    )
    prior = core._seal(core.DiscoveryRun, **values)
    from fabric_kg_builder.cli.domain_design_cmd import _discovery_report

    before = _discovery_report(prior)
    assert before["raw_candidate_count"] == before["unaccounted_candidate_count"] == 3
    assert before["candidate_ledger_entry_count"] == 0 and before["candidate_ledger_complete"] is False
    assert before["unaccounted_raw_array_count"] == 1 and before["unaccounted_raw_candidate_count"] == 3
    legacy = tmp_path / "legacy-envelope.json"
    core.save_discovery(legacy, prior)
    protected = {path: path.read_bytes() for path in (legacy, cached)}
    monkeypatch.setattr(model, "complete_json", lambda **_kwargs: pytest.fail("Recovery must not call a model"))
    resumed = list(args)
    recovered = tmp_path / "recovered.json"
    resumed[resumed.index("--out") + 1] = str(recovered)
    result = _invoke([*resumed, "--resume", str(legacy), "--live", "--max-calls", "0"], model=model)
    run = core.load_discovery(recovered)
    assert result["model_calls"] == 0 and result["status"] == "partial"
    assert result["raw_candidate_count"] == result["verified_candidate_count"] == 3
    assert result["candidate_ledger_entry_count"] == 3
    assert result["unaccounted_candidate_count"] == 0 and result["candidate_ledger_complete"] is True
    assert result["quarantined_candidate_count"] == 0 and result["envelope_anomaly_count"] == 1
    assert run.chunks[0].raw_response == raw
    assert run.chunks[0].candidate_grounding_scope == "raw_response.candidates"
    assert run.chunks[0].origin_artifact_hash == failed.artifact_hash
    assert len(run.chunks[0].candidate_grounding) == 3
    resumed[resumed.index("--out") + 1] = str(tmp_path / "recovered-again.json")
    again = _invoke([*resumed, "--resume", str(recovered), "--live", "--max-calls", "0"], model=model)
    assert again["model_calls"] == 0 and again["envelope_anomaly_count"] == 1
    assert core.load_discovery(tmp_path / "recovered-again.json").chunks[0] == run.chunks[0]
    assert all(path.read_bytes() == original for path, original in protected.items())
