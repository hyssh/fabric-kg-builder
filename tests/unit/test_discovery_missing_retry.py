"""Missing-only output-budget recovery and private provider diagnostics."""

from __future__ import annotations

import json
import stat
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fabric_kg_builder.contracts.base import canonical_json
from fabric_kg_builder.domain import discovery as core
from fabric_kg_builder.enrichment.foundry_client import FoundryClient, FoundryJSONResponseError
from tests.unit.test_domain_discovery import DiscoveryClient, _entity, _prepared, _run
from tests.unit.test_domain_discovery_envelopes import NoCalls
from tests.unit.test_foundry_client import _FOUNDRY_CONFIG


@pytest.mark.parametrize("raw,status,reason", [
    ('{"candidates":[{"unfinished"', "incomplete", "max_output_tokens"),
    ('{"candidates":[]}', "incomplete", "max_output_tokens"),
    ('not JSON', "completed", None),
    ('[]', "completed", None),
    ("", "completed", None),
])
def test_project_json_failure_keeps_private_diagnostics_without_accepting_incomplete_output(raw, status, reason):
    response = SimpleNamespace(
        output_text=raw, id="response:test", status=status,
        incomplete_details=SimpleNamespace(reason=reason),
        usage=SimpleNamespace(input_tokens=200, output_tokens=4096, total_tokens=4296),
        headers={"Authorization": "must-not-be-copied"},
    )
    create = Mock(return_value=response)
    config = _FOUNDRY_CONFIG.model_copy(update={"inference_api": "project_responses"})
    client = FoundryClient(config, _sdk_client=SimpleNamespace(responses=SimpleNamespace(create=create)))
    with pytest.raises(FoundryJSONResponseError) as error:
        client.complete_json("Only JSON.", "Private input.", {"type": "object"}, max_attempts=1)
    assert create.call_count == 1
    assert create.call_args.kwargs["max_output_tokens"] == 4096
    diagnostic = error.value.diagnostics
    assert diagnostic["raw_output"] == raw
    assert diagnostic["status"] == status
    assert diagnostic["usage"]["output_tokens"] == 4096
    assert "headers" not in diagnostic
    assert "Private input." not in str(error.value)
    assert "unfinished" not in str(error.value)
    if reason:
        assert diagnostic["incomplete_reason"] == reason


def test_chat_json_length_finish_does_not_turn_valid_empty_json_into_success():
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"candidates":[]}'), finish_reason="length")],
        usage=SimpleNamespace(completion_tokens=4096),
    )
    create = Mock(return_value=response)
    client = FoundryClient(
        _FOUNDRY_CONFIG, _sdk_client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    with pytest.raises(FoundryJSONResponseError) as error:
        client.complete_json("Only JSON.", "Source", {}, max_attempts=1)
    assert error.value.diagnostics["finish_reason"] == "length"
    assert error.value.diagnostics["raw_output"] == '{"candidates":[]}'
    assert create.call_count == 1


@pytest.mark.parametrize("ceiling", [16_384, 32_768])
def test_project_transport_uses_the_explicit_per_request_output_ceiling(ceiling):
    create = Mock(return_value=SimpleNamespace(output_text='{"candidates":[]}', status="completed"))
    config = _FOUNDRY_CONFIG.model_copy(update={"inference_api": "project_responses"})
    client = FoundryClient(config, _sdk_client=SimpleNamespace(responses=SimpleNamespace(create=create)))
    before = client.execution_identity()
    result = client.complete_json("Only JSON.", "Source", {}, max_completion_tokens=ceiling, max_attempts=1)
    assert result == {"candidates": []}
    assert create.call_args.kwargs["max_output_tokens"] == ceiling
    assert client.execution_identity() == before


class DenseClient(DiscoveryClient):
    def __init__(self, target, minimum_tokens=16_384):
        super().__init__()
        self.target = target
        self.minimum_tokens = minimum_tokens

    def complete_json(self, **request):
        payload = json.loads(request["user"])["input"]
        if "inputs" not in payload and payload["chunk"]["chunk_id"] == self.target:
            if request["max_completion_tokens"] < self.minimum_tokens:
                self.calls.append(request)
                raise FoundryJSONResponseError("No complete JSON object", {
                    "transport": "project_responses", "status": "incomplete",
                    "incomplete_reason": "max_output_tokens", "raw_output": '{"candidates":[{"unfinished"',
                    "max_completion_tokens": request["max_completion_tokens"], "attempt": 1,
                    "headers": {"Authorization": "not retained"},
                })
        return super().complete_json(**request)


def _partial(tmp_path):
    _, _, prepared = _prepared(tmp_path, files=2)
    target = core.plan_discovery_chunks(prepared, core.DiscoveryBudget())[0].chunk_id
    prior = _run(tmp_path, prepared, DenseClient(target))
    assert prior.status == "partial"
    assert len(prior.summaries) == 1
    failed, = [item for item in prior.chunks if item.raw_response is None]
    path = core.discovery_provider_diagnostic_cache_path(tmp_path / "cache", failed)
    diagnostic = core.DiscoveryProviderDiagnostic.model_validate_json(path.read_text())
    assert diagnostic.artifact_hash == failed.provider_diagnostic_hash
    assert diagnostic.request_hash == failed.request_hash
    assert diagnostic.diagnostics["raw_output"] == '{"candidates":[{"unfinished"'
    assert "headers" not in diagnostic.diagnostics
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    return prepared, prior, failed


def test_missing_only_retry_raises_one_leaf_cap_and_preserves_successes_and_summaries(tmp_path, monkeypatch):
    prepared, prior, failed = _partial(tmp_path)
    path = tmp_path / "original.json"
    core.save_discovery(path, prior)
    originals = {item: item.read_bytes() for item in tmp_path.rglob("*.json")}
    policy = core.DiscoveryMissingRetry(max_completion_tokens=16_384)
    plan = core.plan_missing_discovery_retry(prior, policy)
    assert plan["pending_chunk_ids"] == [failed.chunk.chunk_id]
    assert plan["pending_chunk_count"] == 1
    def no_parse(*args, **kwargs):
        pytest.fail("Per-request retry cannot reparse successful sources")
    monkeypatch.setattr("fabric_kg_builder.enrichment.schema2_sources.IndexedSourceCorpusReader.read", no_parse)
    client = DenseClient(failed.chunk.chunk_id)
    result = core.run_discovery(
        prepared, prior=core.load_discovery(path), client=client,
        model_version=prior.model_version, model_hash=prior.model_hash, cache_dir=tmp_path / "cache",
        budget=prior.budget.model_copy(update={"max_calls": 3}), retry_missing=policy,
    )
    assert result.status == "complete"
    assert result.model_call_count == len(client.calls) == 3
    assert result.budget.max_completion_tokens == prior.budget.max_completion_tokens == 4096
    assert result.model_hash == prior.model_hash
    assert result.prepared == prior.prepared
    recovered = next(item for item in result.chunks if item.chunk.chunk_id == failed.chunk.chunk_id)
    assert recovered.chunk == failed.chunk
    assert recovered.request_hash != failed.request_hash
    assert recovered.request_max_completion_tokens == 16_384
    assert recovered.retry_operation_version == policy.operation_version
    assert recovered.retry_of_artifact_hash == failed.artifact_hash
    assert recovered.provider_diagnostic_hash is None
    for old, new in zip(prior.chunks, result.chunks, strict=True):
        if old.raw_response is not None:
            assert new == old
    assert all(item in result.summaries for item in prior.summaries)
    chunk_calls = [request for request in client.calls if "inputs" not in json.loads(request["user"])["input"]]
    assert len(chunk_calls) == 1
    assert chunk_calls[0]["max_completion_tokens"] == 16_384
    assert all(request["max_completion_tokens"] == 4096 for request in client.calls if request not in chunk_calls)
    assert result.reserved_tokens == sum(
        len(canonical_json(request).encode("utf-8")) + request["max_completion_tokens"] + 1024
        for request in client.calls
    )
    repeated = core.run_discovery(
        prepared, prior=result, client=NoCalls(), model_version=prior.model_version,
        model_hash=prior.model_hash, cache_dir=tmp_path / "cache",
        budget=prior.budget.model_copy(update={"max_calls": 0}),
    )
    assert repeated.status == "complete"
    assert repeated.model_call_count == 0
    assert repeated.chunks == result.chunks
    assert repeated.summaries == result.summaries
    assert all(item.read_bytes() == content for item, content in originals.items())
    tampered = result.model_dump(mode="json")
    row = next(item for item in tampered["chunks"] if item["chunk"]["chunk_id"] == failed.chunk.chunk_id)
    row["request_max_completion_tokens"] = 32_768
    from fabric_kg_builder.contracts.base import canonical_sha256
    row["artifact_hash"] = canonical_sha256({key: value for key, value in row.items() if key != "artifact_hash"})
    tampered["artifact_hash"] = canonical_sha256({
        key: value for key, value in tampered.items() if key != "artifact_hash"
    })
    with pytest.raises(ValueError, match="request/source binding"):
        core.DiscoveryRun.model_validate(tampered)


def test_retry_reservation_uses_actual_leaf_cap_and_never_silently_escalates_global_budget(tmp_path):
    prepared, prior, failed = _partial(tmp_path)
    policy = core.DiscoveryMissingRetry()
    units = {unit.source_unit_id: unit for unit in prepared.source_units}
    request, _ = core._chunk_request(
        prepared, failed.chunk, units, prior.budget, prior.model_version, prior.model_hash,
        prior.business_context, core.DISCOVERY_PROMPT_VERSION,
        request_max_completion_tokens=policy.max_completion_tokens,
    )
    required = len(canonical_json(request).encode("utf-8")) + policy.max_completion_tokens + 1024
    result = core.run_discovery(
        prepared, prior=prior, client=NoCalls(), model_version=prior.model_version,
        model_hash=prior.model_hash, cache_dir=tmp_path / "cache",
        budget=prior.budget.model_copy(update={"max_tokens": required - 1}), retry_missing=policy,
    )
    assert result.model_call_count == 0
    row = next(item for item in result.chunks if item.chunk.chunk_id == failed.chunk.chunk_id)
    assert row.status == "deferred"
    assert row.request_max_completion_tokens == policy.max_completion_tokens
    with pytest.raises(ValueError, match="increase the output ceiling"):
        core.plan_missing_discovery_retry(prior, core.DiscoveryMissingRetry(max_completion_tokens=4096))
    with pytest.raises(ValueError, match="cannot reduce"):
        core.plan_missing_discovery_retry(result, core.DiscoveryMissingRetry(max_completion_tokens=8192))
    with pytest.raises(ValueError, match="requires a prior"):
        core.run_discovery(
            prepared, client=NoCalls(), model_version=prior.model_version, model_hash=prior.model_hash,
            cache_dir=tmp_path / "cache", retry_missing=policy,
        )
    with pytest.raises(ValueError, match="exact source/model/context/chunk/request"):
        core.run_discovery(
            prepared, prior=prior, client=NoCalls(), model_version=prior.model_version, model_hash=prior.model_hash,
            cache_dir=tmp_path / "cache",
            budget=prior.budget.model_copy(update={"max_completion_tokens": 8192}), retry_missing=policy,
        )


def test_failed_target_can_explicitly_escalate_again_without_restarting_successful_work(tmp_path):
    prepared, prior, failed = _partial(tmp_path)
    partial = core.run_discovery(
        prepared, prior=prior, client=DenseClient(failed.chunk.chunk_id, minimum_tokens=32_768),
        model_version=prior.model_version, model_hash=prior.model_hash, cache_dir=tmp_path / "cache",
        budget=prior.budget.model_copy(update={"max_calls": 1}),
        retry_missing=core.DiscoveryMissingRetry(max_completion_tokens=16_384),
    )
    retried = next(item for item in partial.chunks if item.raw_response is None)
    assert partial.model_call_count == 1
    assert retried.request_max_completion_tokens == 16_384
    diagnostic = core.DiscoveryProviderDiagnostic.model_validate_json(
        core.discovery_provider_diagnostic_cache_path(tmp_path / "cache", retried).read_text()
    )
    assert diagnostic.diagnostics["max_completion_tokens"] == 16_384
    result = core.run_discovery(
        prepared, prior=partial, client=DenseClient(failed.chunk.chunk_id, minimum_tokens=32_768),
        model_version=prior.model_version, model_hash=prior.model_hash, cache_dir=tmp_path / "cache",
        budget=prior.budget.model_copy(update={"max_calls": 3}),
        retry_missing=core.DiscoveryMissingRetry(max_completion_tokens=32_768),
    )
    assert result.status == "complete"
    recovered = next(item for item in result.chunks if item.chunk.chunk_id == failed.chunk.chunk_id)
    assert recovered.request_max_completion_tokens == 32_768
    assert recovered.retry_of_artifact_hash == retried.artifact_hash
    assert recovered.request_hash not in {failed.request_hash, retried.request_hash}
    assert result.budget.max_completion_tokens == 4096
    assert all(item in result.chunks for item in prior.chunks if item.raw_response is not None)
    assert all(item in result.summaries for item in prior.summaries)


def test_explicit_missing_scope_does_not_launch_calls_for_newly_prepared_unlisted_chunks(tmp_path):
    _, _, prepared = _prepared(tmp_path, files=2)
    first, second = prepared.sources
    values = {
        name: getattr(prepared, name) for name in core.PreparedCorpus.model_fields if name != "artifact_hash"
    }
    values["source_units"] = [unit for unit in prepared.source_units if unit.source_file_id == first.source_file_id]
    values["sources"] = [first, core.PreparedSource(source_file_id=second.source_file_id, status="failed", reason="fixture")]
    partial_prepared = core._seal(core.PreparedCorpus, **values)
    prior = _run(tmp_path, partial_prepared)
    assert core.plan_missing_discovery_retry(prior, core.DiscoveryMissingRetry())["pending_chunk_ids"] == []
    current = core.run_discovery(
        prepared, prior=prior, client=NoCalls(), model_version=prior.model_version,
        model_hash=prior.model_hash, cache_dir=tmp_path / "cache",
        budget=prior.budget, retry_missing=core.DiscoveryMissingRetry(),
    )
    assert current.model_call_count == 0
    assert all(item in current.chunks for item in prior.chunks)
    deferred = [item for item in current.chunks if item.chunk.source_file_id == second.source_file_id]
    assert deferred
    assert all(item.status == "deferred" and "outside explicit" in item.reason for item in deferred)


def _cached_retry_response(prepared, prior, failed, raw):
    units = {unit.source_unit_id: unit for unit in prepared.source_units}
    policy = core.DiscoveryMissingRetry()
    _, key = core._chunk_request(
        prepared, failed.chunk, units, prior.budget, prior.model_version, prior.model_hash,
        prior.business_context, core.DISCOVERY_PROMPT_VERSION,
        request_max_completion_tokens=policy.max_completion_tokens,
    )
    return core._seal(
        core.ChunkObservation, chunk=failed.chunk, request_hash=key, status="failed",
        raw_response=raw, reason="Interrupted after receipt of a schema-invalid response",
        verifier_version=core.GROUNDING_VERSION, request_prompt_version=core.DISCOVERY_PROMPT_VERSION,
        request_max_completion_tokens=policy.max_completion_tokens,
        retry_operation_version=policy.operation_version, retry_of_artifact_hash=failed.artifact_hash,
    )


@pytest.mark.parametrize("namespace", ["chunks", "grounded"])
def test_older_artifact_reuses_later_original_cap_success_before_retry_dispatch(tmp_path, namespace):
    prepared, prior, failed = _partial(tmp_path)
    unit = next(item for item in prepared.source_units if item.source_unit_id == failed.chunk.source_unit_id)
    received = core._grounded_observation(
        failed.chunk, failed.request_hash, {"candidates": [_entity("cached-later", unit.text)]},
        unit, request_prompt_version=core.DISCOVERY_PROMPT_VERSION,
    )
    path = (
        tmp_path / "cache" / "chunks" / f"{received.request_hash}.json"
        if namespace == "chunks" else core.discovery_observation_cache_path(tmp_path / "cache", received)
    )
    core._write(path, received)
    later = _run(tmp_path, prepared)
    assert later.status == "complete"
    originals = {item: item.read_bytes() for item in (tmp_path / "cache").rglob("*.json")}
    replay = core.run_discovery(
        prepared, prior=prior, client=NoCalls(), model_version=prior.model_version,
        model_hash=prior.model_hash, cache_dir=tmp_path / "cache", budget=prior.budget,
        retry_missing=core.DiscoveryMissingRetry(),
    )
    assert replay.status == "complete"
    assert replay.model_call_count == 0
    assert replay.chunks == later.chunks
    assert replay.summaries == later.summaries
    selected = next(item for item in replay.chunks if item.chunk.chunk_id == failed.chunk.chunk_id)
    assert selected.request_hash == failed.request_hash
    assert selected.request_max_completion_tokens is None
    assert all(item.read_bytes() == data for item, data in originals.items())


def test_interrupted_retry_received_invalid_raw_is_reused_without_another_call(tmp_path):
    prepared, prior, failed = _partial(tmp_path)
    raw = {"unexpected_response_shape": {"retained": "not an empty candidate list"}}
    received = _cached_retry_response(prepared, prior, failed, raw)
    path = core.discovery_observation_cache_path(tmp_path / "cache", received)
    core._write(path, received)
    before = path.read_bytes()
    for _ in range(2):
        replay = core.run_discovery(
            prepared, prior=prior, client=NoCalls(), model_version=prior.model_version,
            model_hash=prior.model_hash, cache_dir=tmp_path / "cache", budget=prior.budget,
            retry_missing=core.DiscoveryMissingRetry(),
        )
        assert replay.model_call_count == 0
        selected = next(item for item in replay.chunks if item.chunk.chunk_id == failed.chunk.chunk_id)
        assert selected.raw_response == raw
        assert selected.response is None
        assert selected.status == "failed"
        assert selected.request_hash == received.request_hash
        assert selected.request_max_completion_tokens == 16_384
        assert selected.retry_of_artifact_hash == failed.artifact_hash
        assert core.plan_missing_discovery_retry(replay, core.DiscoveryMissingRetry())["pending_chunk_ids"] == []
    assert path.read_bytes() == before


def test_ambiguous_cached_responses_abort_before_any_missing_chunk_is_called(tmp_path):
    _, _, prepared = _prepared(tmp_path, files=2)
    chunks = core.plan_discovery_chunks(prepared, core.DiscoveryBudget())
    missing = {chunks[0].chunk_id, chunks[-1].chunk_id}
    class MissingClient(DiscoveryClient):
        def complete_json(self, **request):
            payload = json.loads(request["user"])["input"]
            if "inputs" not in payload and payload["chunk"]["chunk_id"] in missing:
                raise ValueError("No parsed response")
            return super().complete_json(**request)
    prior = _run(tmp_path, prepared, MissingClient())
    failed = next(item for item in prior.chunks if item.chunk.chunk_id == chunks[-1].chunk_id)
    unit = next(item for item in prepared.source_units if item.source_unit_id == failed.chunk.source_unit_id)
    original_cap = core._grounded_observation(
        failed.chunk, failed.request_hash, {"candidates": [_entity("original-cap", unit.text)]},
        unit, request_prompt_version=core.DISCOVERY_PROMPT_VERSION,
    )
    core._write(tmp_path / "cache" / "chunks" / f"{original_cap.request_hash}.json", original_cap)
    retry_cap = _cached_retry_response(prepared, prior, failed, {"different_received_response": True})
    core._write(core.discovery_observation_cache_path(tmp_path / "cache", retry_cap), retry_cap)
    client = Mock()
    with pytest.raises(ValueError, match="multiple distinct received responses"):
        core.run_discovery(
            prepared, prior=prior, client=client, model_version=prior.model_version,
            model_hash=prior.model_hash, cache_dir=tmp_path / "cache", budget=prior.budget,
            retry_missing=core.DiscoveryMissingRetry(),
        )
    client.complete_json.assert_not_called()


def test_new_retry_cache_diagnostic_corruption_is_private_and_blocks_dispatch(tmp_path):
    prepared, prior, failed = _partial(tmp_path)
    retry = _cached_retry_response(prepared, prior, failed, None)
    diagnostic = core._seal(
        core.DiscoveryProviderDiagnostic, request_hash=retry.request_hash,
        diagnostics={"raw_output": "PRIVATE_DIAGNOSTIC_SENTINEL"},
    )
    values = {name: getattr(retry, name) for name in core.ChunkObservation.model_fields if name != "artifact_hash"}
    values["provider_diagnostic_hash"] = diagnostic.artifact_hash
    retry = core._seal(core.ChunkObservation, **values)
    core._write(core.discovery_observation_cache_path(tmp_path / "cache", retry), retry)
    path = core.discovery_provider_diagnostic_cache_path(tmp_path / "cache", retry)
    core._write(path, diagnostic)
    path.write_text('{"raw_output":"PRIVATE_DIAGNOSTIC_SENTINEL"')
    client = Mock()
    with pytest.raises(ValueError, match="DISCOVERY_RESUME_DIAGNOSTIC_DRIFT") as error:
        core.run_discovery(
            prepared, prior=prior, client=client, model_version=prior.model_version,
            model_hash=prior.model_hash, cache_dir=tmp_path / "cache", budget=prior.budget,
            retry_missing=core.DiscoveryMissingRetry(),
        )
    assert "PRIVATE_DIAGNOSTIC_SENTINEL" not in str(error.value)
    client.complete_json.assert_not_called()
