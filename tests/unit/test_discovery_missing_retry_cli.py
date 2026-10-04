"""Per-request missing-only retry controls and private diagnostic cache gating."""

import json
from copy import deepcopy

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.domain import discovery as core
from tests.unit.test_domain_discovery_cli import Model, _invoke, _paths


class MissingModel(Model):
    def __init__(self, *, fail_summaries=False):
        super().__init__()
        self.chunk_ids = []
        self.discovery_requests = []
        self.fail_summaries = fail_summaries

    def complete_json(self, **request):
        fields = request["json_schema"]["properties"]
        if "candidates" in fields:
            payload = json.loads(request["user"])
            chunk_id = payload["input"]["chunk"]["chunk_id"]
            if chunk_id not in self.chunk_ids:
                self.chunk_ids.append(chunk_id)
            ordinal = self.chunk_ids.index(chunk_id)
            self.discovery_requests.append((chunk_id, request["max_completion_tokens"]))
            if ordinal == 1 and request["max_completion_tokens"] <= 4096:
                self.calls.append(request)
                raise ValueError("Fixture received no parseable JSON")
            if ordinal == 2:
                self.calls.append(request)
                return {"unexpected_root": "Received JSON is never retargeted"}
        elif "summary" in fields and self.fail_summaries:
            self.calls.append(request)
            raise ValueError("Fixture summary transport failed")
        return super().complete_json(**request)


def _resume_args(args, prior, out):
    result = list(args)
    result[result.index("--out") + 1] = str(out)
    return [*result, "--resume", str(prior)]


def _values(record):
    return {
        key: deepcopy(getattr(record, key))
        for key in type(record).model_fields if key != "artifact_hash"
    }


def test_missing_retry_plan_and_live_change_only_missing_leaf_output_cap(tmp_path):
    _, _, out, cache, args = _paths(tmp_path, count=3)
    model = MissingModel()
    model_config = deepcopy(vars(model._config))
    _invoke([*args, "--live"], model=model)
    prior = core.load_discovery(out)
    missing = next(item for item in prior.chunks if item.raw_response is None)
    successful = [item for item in prior.chunks if item.response is not None]
    received_invalid = next(item for item in prior.chunks if item.raw_response is not None and item.response is None)
    protected = {out: out.read_bytes()}
    for item in prior.chunks:
        path = core.discovery_observation_cache_path(cache, item)
        if path.exists():
            protected[path] = path.read_bytes()
    for item in prior.summaries:
        path = cache / "summaries" / f"{item.request_hash}.json"
        protected[path] = path.read_bytes()
    resumed_path = tmp_path / "retried.json"
    resumed = _resume_args(args, out, resumed_path)
    before = len(model.calls)
    planned = _invoke([*resumed, "--retry-missing"], model=model)
    retry = planned["missing_retry_plan"]
    assert retry["operation_version"] == "discovery-missing-retry/1.0.0"
    assert retry["max_completion_tokens"] == 16_384
    assert retry["pending_chunk_ids"] == [missing.chunk.chunk_id]
    assert retry["pending_chunk_count"] == 1 and retry["prior_run_hash"] == prior.run_hash
    assert planned["budget"]["max_completion_tokens"] == prior.budget.max_completion_tokens == 4096
    assert len(model.calls) == before and not resumed_path.exists()
    requests_before = len(model.discovery_requests)
    result = _invoke([*resumed, "--retry-missing", "--live", "--max-calls", "1"], model=model)
    current = core.load_discovery(resumed_path)
    assert result["model_calls"] == 1
    assert model.discovery_requests[requests_before:] == [(missing.chunk.chunk_id, 16_384)]
    assert current.model_hash == prior.model_hash and current.model_version == prior.model_version
    assert vars(model._config) == model_config
    assert current.budget.max_completion_tokens == 4096
    assert current.retry_missing_policy.max_completion_tokens == 16_384
    by_id = {item.chunk.chunk_id: item for item in current.chunks}
    for item in successful:
        assert by_id[item.chunk.chunk_id] == item
    assert by_id[received_invalid.chunk.chunk_id].request_hash == received_invalid.request_hash
    assert by_id[received_invalid.chunk.chunk_id].raw_response == received_invalid.raw_response
    new = by_id[missing.chunk.chunk_id]
    assert new.request_max_completion_tokens == 16_384
    assert new.chunk == missing.chunk
    assert new.retry_operation_version == retry["operation_version"]
    assert new.retry_of_artifact_hash == missing.artifact_hash
    assert new.request_hash != missing.request_hash
    assert current.reserved_tokens >= 16_384
    assert all(path.read_bytes() == original for path, original in protected.items())
    assert {item.artifact_hash for item in prior.summaries}.issubset(
        {item.artifact_hash for item in current.summaries}
    )
    assert all(
        request["max_completion_tokens"] == 4096
        for request in model.calls if "summary" in request["json_schema"]["properties"]
    )


@pytest.mark.parametrize("options", [
    ["--retry-missing"],
    ["--retry-max-completion-tokens", "16384"],
    ["--retry-missing", "--retry-max-completion-tokens", "255"],
    ["--retry-missing", "--retry-max-completion-tokens", "32769"],
])
def test_missing_retry_option_misuse_is_rejected_without_calls_or_output(tmp_path, options):
    _, _, out, _, args = _paths(tmp_path, count=1)
    model = MissingModel()
    result = CliRunner().invoke(cli, [*args, *options], obj={"_design_client": model})
    assert result.exit_code != 0
    assert model.calls == [] and not out.exists()


def test_actual_retry_ceiling_is_reserved_and_cannot_be_decreased(tmp_path):
    _, _, out, _, args = _paths(tmp_path, count=2)
    model = MissingModel()
    _invoke([*args, "--live"], model=model)
    limited_path = tmp_path / "token-limited.json"
    before = len(model.calls)
    result = _invoke([
        *_resume_args(args, out, limited_path), "--retry-missing",
        "--retry-max-completion-tokens", "16384", "--live", "--max-tokens", "8000",
    ], model=model)
    assert result["model_calls"] == 0 and len(model.calls) == before
    limited = core.load_discovery(limited_path)
    missing = next(item for item in limited.chunks if item.raw_response is None)
    assert missing.request_max_completion_tokens == 16_384
    result = CliRunner().invoke(cli, [
        *_resume_args(args, limited_path, tmp_path / "decreased.json"),
        "--retry-missing", "--retry-max-completion-tokens", "8192", "--live",
    ], obj={"_design_client": model})
    assert result.exit_code != 0 and len(model.calls) == before
    assert not (tmp_path / "decreased.json").exists()


@pytest.mark.parametrize("ceiling", [256, 4096, 32_768])
def test_explicit_retry_ceiling_must_exceed_global_and_respects_upper_boundary(tmp_path, ceiling):
    _, _, out, _, args = _paths(tmp_path, count=2)
    model = MissingModel()
    _invoke([*args, "--live"], model=model)
    before = len(model.calls)
    planned = tmp_path / "planned.json"
    result = CliRunner().invoke(cli, [
        *_resume_args(args, out, planned), "--retry-missing",
        "--retry-max-completion-tokens", str(ceiling),
    ], obj={"_design_client": model})
    assert (result.exit_code == 0) == (ceiling > 4096)
    if result.exit_code == 0:
        report = json.loads(result.output)
        assert report["missing_retry_plan"]["max_completion_tokens"] == ceiling
        assert report["budget"]["max_completion_tokens"] == 4096
    assert len(model.calls) == before and not planned.exists()


@pytest.mark.parametrize("kind", ["chunk", "summary"])
@pytest.mark.parametrize("corruption", ["missing", "malformed", "changed", "wrong-request"])
def test_referenced_private_diagnostic_drift_blocks_resume_before_calls(tmp_path, kind, corruption):
    _, _, out, cache, args = _paths(tmp_path, count=2)
    model = MissingModel(fail_summaries=True)
    _invoke([*args, "--live"], model=model)
    prior = core.load_discovery(out)
    original = next(item for item in prior.chunks if item.raw_response is None) if kind == "chunk" else prior.failed_summaries[0]
    marker = "PRIVATE_PROVIDER_OUTPUT_SENTINEL"
    diagnostic = core._seal(
        core.DiscoveryProviderDiagnostic,
        request_hash="f" * 64 if corruption == "wrong-request" else original.request_hash,
        diagnostics={"status": "incomplete", "incomplete_reason": "max_output_tokens", "raw_output": marker},
    )
    values = _values(original)
    values["provider_diagnostic_hash"] = diagnostic.artifact_hash
    linked = core._seal(type(original), **values)
    values = _values(prior)
    key = "chunks" if kind == "chunk" else "failed_summaries"
    values[key] = [linked if item == original else item for item in getattr(prior, key)]
    linked_run = core._seal(core.DiscoveryRun, **values)
    path = core.discovery_provider_diagnostic_cache_path(cache, linked)
    assert path is not None
    core._write(path, diagnostic)
    record_path = (
        core.discovery_observation_cache_path(cache, linked) if kind == "chunk"
        else core.discovery_summary_failure_cache_path(cache, linked)
    )
    core._write(record_path, linked)
    linked_path = tmp_path / "linked.json"
    core.save_discovery(linked_path, linked_run)
    before = len(model.calls)
    if corruption == "missing":
        path.unlink()
    elif corruption == "malformed":
        path.write_text("{")
    elif corruption == "changed":
        changed = json.loads(path.read_text())
        changed["diagnostics"]["raw_output"] += "_changed"
        path.write_text(json.dumps(changed))
    result = CliRunner().invoke(cli, [
        *_resume_args(args, linked_path, tmp_path / "blocked.json"),
        "--retry-missing", "--live",
    ], obj={"_design_client": model})
    assert result.exit_code != 0 and "DISCOVERY_RESUME_DIAGNOSTIC_DRIFT" in result.output
    assert marker not in result.output
    assert len(model.calls) == before and not (tmp_path / "blocked.json").exists()


def test_valid_private_diagnostic_is_read_not_echoed_by_planning(tmp_path):
    from fabric_kg_builder.cli.domain_design_cmd import _validate_discovery_resume_cache

    _, _, out, cache, args = _paths(tmp_path, count=2)
    model = MissingModel()
    _invoke([*args, "--live"], model=model)
    prior = core.load_discovery(out)
    original = next(item for item in prior.chunks if item.raw_response is None)
    diagnostic = core._seal(
        core.DiscoveryProviderDiagnostic, request_hash=original.request_hash,
        diagnostics={"raw_output": "PRIVATE_PROVIDER_OUTPUT_SENTINEL", "status": "incomplete"},
    )
    linked = core._seal(type(original), **{
        **_values(original), "provider_diagnostic_hash": diagnostic.artifact_hash,
    })
    linked_run = core._seal(core.DiscoveryRun, **{
        **_values(prior), "chunks": [linked if item == original else item for item in prior.chunks],
    })
    path = core.discovery_provider_diagnostic_cache_path(cache, linked)
    core._write(path, diagnostic)
    core._write(core.discovery_observation_cache_path(cache, linked), linked)
    linked_path = tmp_path / "linked.json"
    core.save_discovery(linked_path, linked_run)
    _validate_discovery_resume_cache(linked_run, cache)
    before = len(model.calls)
    result = _invoke([
        *_resume_args(args, linked_path, tmp_path / "planned.json"), "--retry-missing",
    ], model=model)
    assert "PRIVATE_PROVIDER_OUTPUT_SENTINEL" not in json.dumps(result)
    assert result["writes"] == result["model_calls"] == 0 and len(model.calls) == before


@pytest.mark.parametrize("cached_outcome", ["original-cap-success", "retry-cap-malformed"])
def test_older_raw_none_run_reuses_later_received_cache_without_model_calls(tmp_path, monkeypatch, cached_outcome):
    class LaterCacheModel(MissingModel):
        later = False

        def complete_json(self, **request):
            if self.later and "candidates" in request["json_schema"]["properties"]:
                chunk_id = json.loads(request["user"])["input"]["chunk"]["chunk_id"]
                if chunk_id == self.chunk_ids[1]:
                    self.discovery_requests.append((chunk_id, request["max_completion_tokens"]))
                    if cached_outcome == "retry-cap-malformed":
                        self.calls.append(request)
                        return {"unexpected_root": {"original_received_value": ["retain", 7]}}
                    return Model.complete_json(self, **request)
            return super().complete_json(**request)

    _, _, older_path, cache, args = _paths(tmp_path, count=2)
    model = LaterCacheModel()
    _invoke([*args, "--live"], model=model)
    older = core.load_discovery(older_path)
    missing = next(item for item in older.chunks if item.raw_response is None)
    protected_older = older_path.read_bytes()
    model.later = True
    later_path = tmp_path / "later.json"
    extra = [] if cached_outcome == "original-cap-success" else [
        "--retry-missing", "--retry-max-completion-tokens", "16384",
    ]
    _invoke([
        *_resume_args(args, older_path, later_path), *extra, "--live", "--max-calls", "10",
    ], model=model)
    later = core.load_discovery(later_path)
    received = next(item for item in later.chunks if item.chunk.chunk_id == missing.chunk.chunk_id)
    assert received.raw_response is not None
    cached_path = core.discovery_observation_cache_path(cache, received)
    protected_cache = cached_path.read_bytes()
    # Reproduce interruption after durable response caching but before the caller
    # retains the new run artifact. Only the older rawNone run remains available.
    later_path.unlink()
    before = len(model.calls)
    monkeypatch.setattr(
        model, "complete_json",
        lambda **_kwargs: pytest.fail("Received cache must be selected before dispatching any model call"),
    )
    recovered_path = tmp_path / "recovered-from-cache.json"
    result = _invoke([
        *_resume_args(args, older_path, recovered_path),
        "--retry-missing", "--retry-max-completion-tokens", "16384", "--live", "--max-calls", "10",
    ], model=model)
    recovered = core.load_discovery(recovered_path)
    actual = next(item for item in recovered.chunks if item.chunk.chunk_id == missing.chunk.chunk_id)
    assert result["model_calls"] == 0 and len(model.calls) == before
    assert actual.request_hash == received.request_hash
    assert actual.raw_response == received.raw_response
    if cached_outcome == "original-cap-success":
        assert actual == received and actual.response is not None
        assert actual.request_max_completion_tokens is None
        assert actual.request_hash == missing.request_hash
    else:
        assert actual.status == "failed" and actual.response is None
        assert actual.request_max_completion_tokens == 16_384
        assert actual.raw_response == {"unexpected_root": {"original_received_value": ["retain", 7]}}
        assert actual.request_hash != missing.request_hash
        assert result["invalid_raw_envelope_count"] == 1
    assert older_path.read_bytes() == protected_older
    assert cached_path.read_bytes() == protected_cache
