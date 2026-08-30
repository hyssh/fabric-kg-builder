"""Unit tests for FoundryClient.

Uses the mock Foundry client from conftest.py — no live API calls.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel, ValidationError

from tests.conftest import make_foundry_client
from fabric_kg_builder.config.schema import FoundryConfig
from fabric_kg_builder.enrichment.foundry_client import FoundryClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FOUNDRY_CONFIG = FoundryConfig(
    endpoint="https://fake.ai.azure.com",
    project="test-project",
    chat_deployment="gpt-5-4-mini",
    embedding_deployment="embedding",
    embedding_dimensions=1536,
)

_FIXTURE_PAYLOAD = {
    "source_file_id": "test-source",
    "pass": "p2",
    "entities": [
        {
            "id_hint": "test:device:laptop",
            "type": "Device",
            "label": "Test Laptop",
            "confidence": 0.95,
        }
    ],
    "relationships": [],
    "chunks": [],
    "visual_assets": [],
    "visual_regions": [],
    "evidence": [],
    "placeholder_suggestions": [],
}

_EMBEDDING_DIM = 1536


def _make_embed_mock(sdk_mock: MagicMock, n_texts: int = 1) -> None:
    """Wire the embedding call on *sdk_mock* to return deterministic vectors."""
    vectors = [[float(i) / _EMBEDDING_DIM] * _EMBEDDING_DIM for i in range(n_texts)]
    embed_data = [MagicMock(embedding=v) for v in vectors]
    sdk_mock.embeddings.create.return_value = MagicMock(data=embed_data)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_constructs_from_config_with_injected_client() -> None:
    """FoundryClient should accept _sdk_client and expose it as _client."""
    sdk_mock = make_foundry_client(_FIXTURE_PAYLOAD)
    client = FoundryClient(_FOUNDRY_CONFIG, _sdk_client=sdk_mock)
    assert client._client is sdk_mock
    assert client._config.chat_deployment == "gpt-5-4-mini"
    assert client._config.embedding_dimensions == 1536


# ---------------------------------------------------------------------------
# complete_json
# ---------------------------------------------------------------------------


def test_complete_json_returns_parsed_dict() -> None:
    """complete_json should parse the mock's JSON content and return a dict."""
    sdk_mock = make_foundry_client(_FIXTURE_PAYLOAD)
    client = FoundryClient(_FOUNDRY_CONFIG, _sdk_client=sdk_mock)

    schema = {"type": "object", "properties": {"source_file_id": {"type": "string"}}}
    result = client.complete_json(
        system="Extract entities.",
        user="Source: laptop docs",
        json_schema=schema,
    )

    assert isinstance(result, dict)
    assert result["source_file_id"] == "test-source"
    assert result["pass"] == "p2"


def test_complete_json_passes_correct_deployment() -> None:
    """complete_json should forward the configured chat_deployment to create()."""
    sdk_mock = make_foundry_client(_FIXTURE_PAYLOAD)
    client = FoundryClient(_FOUNDRY_CONFIG, _sdk_client=sdk_mock)

    client.complete_json("sys", "usr", {})

    call_kwargs = sdk_mock.chat.completions.create.call_args
    assert call_kwargs.kwargs["model"] == "gpt-5-4-mini"


def test_complete_json_uses_strict_structured_output_schema() -> None:
    sdk_mock = make_foundry_client(_FIXTURE_PAYLOAD)
    client = FoundryClient(_FOUNDRY_CONFIG, _sdk_client=sdk_mock)
    schema = {
        "type": "object",
        "properties": {"source_file_id": {"type": "string"}},
        "required": ["source_file_id"],
        "additionalProperties": False,
    }
    client.complete_json("sys", "usr", schema)
    response_format = (
        sdk_mock.chat.completions.create.call_args.kwargs[
            "response_format"
        ]
    )
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    strict_schema = response_format["json_schema"]["schema"]
    assert strict_schema["required"] == ["source_file_id"]
    assert strict_schema["additionalProperties"] is False


def test_oversized_schema_falls_back_to_json_object() -> None:
    from fabric_kg_builder.domain.proposal import (
        domain_proposal_candidates_schema,
    )

    sdk_mock = make_foundry_client(_FIXTURE_PAYLOAD)
    client = FoundryClient(_FOUNDRY_CONFIG, _sdk_client=sdk_mock)
    client.complete_json(
        "sys",
        "usr",
        domain_proposal_candidates_schema(),
    )
    response_format = (
        sdk_mock.chat.completions.create.call_args.kwargs[
            "response_format"
        ]
    )
    assert response_format == {"type": "json_object"}


def test_untyped_schema_branch_falls_back_to_json_object() -> None:
    from fabric_kg_builder.domain.models import DomainReviewPayload

    sdk_mock = make_foundry_client(_FIXTURE_PAYLOAD)
    client = FoundryClient(_FOUNDRY_CONFIG, _sdk_client=sdk_mock)
    client.complete_json(
        "sys",
        "usr",
        DomainReviewPayload.model_json_schema(),
    )
    response_format = (
        sdk_mock.chat.completions.create.call_args.kwargs[
            "response_format"
        ]
    )
    assert response_format == {"type": "json_object"}


def test_strict_capability_rejection_retries_json_object_once() -> None:
    class UnsupportedStrictSchema(Exception):
        status_code = 400

    sdk_mock = make_foundry_client(_FIXTURE_PAYLOAD)
    success = sdk_mock.chat.completions.create.return_value
    sdk_mock.chat.completions.create.side_effect = [
        UnsupportedStrictSchema(),
        success,
    ]
    client = FoundryClient(_FOUNDRY_CONFIG, _sdk_client=sdk_mock)
    result = client.complete_json(
        "sys",
        "usr",
        {
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
    )
    assert result == _FIXTURE_PAYLOAD
    assert sdk_mock.chat.completions.create.call_count == 2
    assert (
        sdk_mock.chat.completions.create.call_args.kwargs[
            "response_format"
        ]
        == {"type": "json_object"}
    )


def test_local_sdk_strict_validation_retries_json_object_once() -> None:
    class RequiredValue(BaseModel):
        value: str

    try:
        RequiredValue.model_validate({})
    except ValidationError as validation_error:
        local_rejection = validation_error

    sdk_mock = make_foundry_client(_FIXTURE_PAYLOAD)
    success = sdk_mock.chat.completions.create.return_value
    sdk_mock.chat.completions.create.side_effect = [
        local_rejection,
        success,
    ]
    client = FoundryClient(_FOUNDRY_CONFIG, _sdk_client=sdk_mock)
    result = client.complete_json(
        "sys",
        "usr",
        {
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
    )
    assert result == _FIXTURE_PAYLOAD
    assert sdk_mock.chat.completions.create.call_count == 2
    assert (
        sdk_mock.chat.completions.create.call_args.kwargs[
            "response_format"
        ]
        == {"type": "json_object"}
    )


def test_complete_json_puts_system_in_system_role() -> None:
    """System prompt must be sent with role='system', user content with role='user'."""
    sdk_mock = make_foundry_client(_FIXTURE_PAYLOAD)
    client = FoundryClient(_FOUNDRY_CONFIG, _sdk_client=sdk_mock)

    client.complete_json(
        system="Developer instruction",
        user="User domain context",
        json_schema={},
    )

    messages = sdk_mock.chat.completions.create.call_args.kwargs["messages"]
    roles = [m["role"] for m in messages]
    assert roles[0] == "system"
    assert roles[1] == "user"
    assert messages[0]["content"] == "Developer instruction"
    assert messages[1]["content"] == "User domain context"


def test_complete_json_raises_on_invalid_json() -> None:
    """complete_json should raise ValueError when the model returns unparseable content."""
    sdk_mock = MagicMock()
    sdk_mock.chat.completions.create.return_value = (
        MagicMock(choices=[MagicMock(message=MagicMock(content="not json {{"))])
    )
    client = FoundryClient(_FOUNDRY_CONFIG, _sdk_client=sdk_mock)

    with pytest.raises(ValueError, match="could not be parsed as JSON"):
        client.complete_json("sys", "usr", {})


def test_complete_json_uses_temperature_zero_and_seed() -> None:
    """Determinism settings: temperature=0.0, seed=42 must always be forwarded."""
    sdk_mock = make_foundry_client(_FIXTURE_PAYLOAD)
    client = FoundryClient(_FOUNDRY_CONFIG, _sdk_client=sdk_mock)
    client.complete_json("sys", "usr", {})

    kwargs = sdk_mock.chat.completions.create.call_args.kwargs
    assert kwargs["temperature"] == 0.0
    assert kwargs["seed"] == 42


# ---------------------------------------------------------------------------
# embed
# ---------------------------------------------------------------------------


def test_embed_returns_list_of_vectors() -> None:
    """embed() should return one vector per input text."""
    sdk_mock = make_foundry_client(_FIXTURE_PAYLOAD)
    _make_embed_mock(sdk_mock, n_texts=2)
    client = FoundryClient(_FOUNDRY_CONFIG, _sdk_client=sdk_mock)

    result = client.embed(["hello world", "laptop battery"])

    assert isinstance(result, list)
    assert len(result) == 2
    for vec in result:
        assert isinstance(vec, list)
        assert len(vec) == _EMBEDDING_DIM


def test_embed_requests_correct_dimensions() -> None:
    """embed() must forward dimensions=1536 to the SDK."""
    sdk_mock = make_foundry_client(_FIXTURE_PAYLOAD)
    _make_embed_mock(sdk_mock, n_texts=1)
    client = FoundryClient(_FOUNDRY_CONFIG, _sdk_client=sdk_mock)

    client.embed(["test text"])

    call_kwargs = sdk_mock.embeddings.create.call_args.kwargs
    assert call_kwargs["dimensions"] == 1536


def test_embed_requests_correct_deployment() -> None:
    """embed() must forward the configured embedding_deployment to the SDK."""
    sdk_mock = make_foundry_client(_FIXTURE_PAYLOAD)
    _make_embed_mock(sdk_mock, n_texts=1)
    client = FoundryClient(_FOUNDRY_CONFIG, _sdk_client=sdk_mock)

    client.embed(["test"])

    call_kwargs = sdk_mock.embeddings.create.call_args.kwargs
    assert call_kwargs["model"] == "embedding"


# ---------------------------------------------------------------------------
# Transient transport retry (issue: L2 aborted on a single "Connection error.")
# ---------------------------------------------------------------------------


class _FakeConnectionError(Exception):
    """Stands in for ``openai.APIConnectionError`` without importing the SDK."""


_FakeConnectionError.__name__ = "APIConnectionError"


class _FakeStatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code


def test_transport_retry_recovers_from_connection_error() -> None:
    """A transient connection failure must be retried, not surfaced."""
    from fabric_kg_builder.enrichment import foundry_client as module

    calls = {"n": 0}

    def operation() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise _FakeConnectionError("Connection error.")
        return "ok"

    slept: list[float] = []
    result = module._call_with_transport_retry(
        operation, sleep=slept.append
    )

    assert result == "ok"
    assert calls["n"] == 3
    assert slept == [1.0, 2.0]


def test_transport_retry_does_not_retry_client_errors() -> None:
    """Deterministic request/authority failures must surface immediately."""
    from fabric_kg_builder.enrichment import foundry_client as module

    for status in (400, 401, 403, 404, 422):
        calls = {"n": 0}

        def operation(status: int = status) -> None:
            calls["n"] += 1
            raise _FakeStatusError(status)

        with pytest.raises(_FakeStatusError):
            module._call_with_transport_retry(
                operation, sleep=lambda _seconds: None
            )
        assert calls["n"] == 1


def test_transport_retry_retries_server_and_throttle_errors() -> None:
    """Server faults and throttling are retryable transport failures."""
    from fabric_kg_builder.enrichment import foundry_client as module

    for status in (408, 429, 500, 502, 503, 504):
        calls = {"n": 0}

        def operation(status: int = status) -> str:
            calls["n"] += 1
            if calls["n"] < 2:
                raise _FakeStatusError(status)
            return "ok"

        assert (
            module._call_with_transport_retry(
                operation, sleep=lambda _seconds: None
            )
            == "ok"
        )
        assert calls["n"] == 2


def test_transport_retry_gives_up_after_bounded_attempts() -> None:
    """Retries are bounded so a hard outage still terminates."""
    from fabric_kg_builder.enrichment import foundry_client as module

    calls = {"n": 0}

    def operation() -> None:
        calls["n"] += 1
        raise _FakeConnectionError("Connection error.")

    breaker = module._TransportOutageBreaker(
        budget_seconds=0.0, sleep=lambda _seconds: None
    )
    with pytest.raises(module.TransportOutageError) as excinfo:
        module._call_with_transport_retry(
            operation,
            max_attempts=3,
            sleep=lambda _seconds: None,
            breaker=breaker,
        )
    assert calls["n"] == 3
    assert isinstance(excinfo.value.__cause__, _FakeConnectionError)


def test_transport_retry_retries_unenumerated_server_faults() -> None:
    """Gateway/proxy 5xx codes are still transient server faults."""
    from fabric_kg_builder.enrichment import foundry_client as module

    for status in (507, 520, 522, 524, 529):
        calls = {"n": 0}

        def operation(status: int = status) -> str:
            calls["n"] += 1
            if calls["n"] < 2:
                raise _FakeStatusError(status)
            return "ok"

        assert (
            module._call_with_transport_retry(
                operation, sleep=lambda _seconds: None
            )
            == "ok"
        )
        assert calls["n"] == 2


def test_complete_json_retries_transient_transport_failure(monkeypatch) -> None:
    """complete_json must survive one transient connection error."""
    from fabric_kg_builder.enrichment import foundry_client as module

    slept: list[float] = []
    monkeypatch.setattr(
        module, "_transport_retry_sleep", slept.append
    )
    sdk_mock = make_foundry_client(_FIXTURE_PAYLOAD)
    good = sdk_mock.chat.completions.create.return_value
    sdk_mock.chat.completions.create.side_effect = [
        _FakeConnectionError("Connection error."),
        good,
    ]
    client = FoundryClient(_FOUNDRY_CONFIG, _sdk_client=sdk_mock)

    result = client.complete_json(
        system="system", user="user", json_schema={}
    )

    assert result == json.loads(good.choices[0].message.content)
    assert sdk_mock.chat.completions.create.call_count == 2
    assert slept == [1.0]


def test_empty_completion_is_retried_then_reported_distinctly() -> None:
    """An empty completion must retry and then fail with an exact reason."""
    sdk_mock = make_foundry_client(_FIXTURE_PAYLOAD)
    empty = MagicMock()
    empty.choices = [MagicMock(message=MagicMock(content=""))]
    sdk_mock.chat.completions.create.return_value = empty
    client = FoundryClient(_FOUNDRY_CONFIG, _sdk_client=sdk_mock)

    with pytest.raises(ValueError, match="empty completion"):
        client.complete_json(
            system="system", user="user", json_schema={}, max_attempts=2
        )

    assert sdk_mock.chat.completions.create.call_count == 2


def test_empty_completion_recovers_on_retry() -> None:
    """A single empty completion must not abort a resumable run."""
    sdk_mock = make_foundry_client(_FIXTURE_PAYLOAD)
    good = sdk_mock.chat.completions.create.return_value
    empty = MagicMock()
    empty.choices = [MagicMock(message=MagicMock(content="   "))]
    sdk_mock.chat.completions.create.side_effect = [empty, good]
    client = FoundryClient(_FOUNDRY_CONFIG, _sdk_client=sdk_mock)

    result = client.complete_json(
        system="system", user="user", json_schema={}, max_attempts=3
    )

    assert result == json.loads(good.choices[0].message.content)
    assert sdk_mock.chat.completions.create.call_count == 2


class _FakeClock:
    """Deterministic monotonic clock whose sleeps advance time."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _breaker(clock: _FakeClock, **kwargs):
    from fabric_kg_builder.enrichment import foundry_client as module

    return module._TransportOutageBreaker(
        monotonic=clock.monotonic, sleep=clock.sleep, **kwargs
    )


def test_outage_tier_recovers_after_sustained_provider_outage() -> None:
    """A multi-minute outage must not terminate a resumable stage."""
    from fabric_kg_builder.enrichment import foundry_client as module

    clock = _FakeClock()
    breaker = _breaker(clock, budget_seconds=900.0)
    calls = {"n": 0}

    def operation() -> str:
        calls["n"] += 1
        # Outlast the request-local budget by several minutes.
        if clock.now - 1000.0 < 240.0:
            raise _FakeConnectionError("Connection error.")
        return "ok"

    result = module._call_with_transport_retry(
        operation, max_attempts=3, sleep=clock.sleep, breaker=breaker
    )

    assert result == "ok"
    metrics = breaker.metrics()
    assert metrics["transport_outages"] == 1
    assert metrics["transport_outages_recovered"] == 1
    assert metrics["transport_delayed_retries"] > 0
    assert metrics["transport_outage_budget_exhausted"] is False


def test_outage_tier_is_bounded_by_its_budget() -> None:
    """A permanent outage still terminates once the budget is spent."""
    from fabric_kg_builder.enrichment import foundry_client as module

    clock = _FakeClock()
    breaker = _breaker(clock, budget_seconds=120.0)

    def operation() -> None:
        raise _FakeConnectionError("Connection error.")

    with pytest.raises(module.TransportOutageError) as excinfo:
        module._call_with_transport_retry(
            operation, max_attempts=2, sleep=clock.sleep, breaker=breaker
        )

    assert excinfo.value.budget_seconds == 120.0
    # No new request is started once the open window has spent its budget:
    # the outage itself never exceeds the budget.
    assert breaker.metrics()["transport_longest_outage_seconds"] <= 120.0
    assert excinfo.value.elapsed_seconds <= 120.0
    assert breaker.metrics()["transport_outage_budget_exhausted"] is True


def test_outage_tier_never_retries_deterministic_failures() -> None:
    """Authority and request errors bypass the outage tier entirely."""
    from fabric_kg_builder.enrichment import foundry_client as module

    clock = _FakeClock()
    breaker = _breaker(clock, budget_seconds=900.0)

    for status in (400, 401, 403, 404, 409, 413, 422):
        calls = {"n": 0}

        def operation(status: int = status) -> None:
            calls["n"] += 1
            raise _FakeStatusError(status)

        with pytest.raises(_FakeStatusError):
            module._call_with_transport_retry(
                operation, sleep=clock.sleep, breaker=breaker
            )
        assert calls["n"] == 1

    assert breaker.metrics()["transport_outages"] == 0


def test_exhausted_breaker_fails_queued_workers_fast() -> None:
    """Once the budget is spent, queued work must not keep calling out."""
    from fabric_kg_builder.enrichment import foundry_client as module

    clock = _FakeClock()
    breaker = _breaker(clock, budget_seconds=0.0)

    def failing() -> None:
        raise _FakeConnectionError("Connection error.")

    with pytest.raises(module.TransportOutageError):
        module._call_with_transport_retry(
            failing, max_attempts=1, sleep=clock.sleep, breaker=breaker
        )

    calls = {"n": 0}

    def queued() -> str:
        calls["n"] += 1
        return "ok"

    with pytest.raises(module.TransportOutageError):
        module._call_with_transport_retry(
            queued, sleep=clock.sleep, breaker=breaker
        )
    assert calls["n"] == 0


def test_concurrent_workers_share_one_outage_budget() -> None:
    """Parallel workers must not each burn an independent outage budget."""
    from fabric_kg_builder.enrichment import foundry_client as module

    clock = _FakeClock()
    breaker = _breaker(clock, budget_seconds=60.0)

    def operation() -> None:
        raise _FakeConnectionError("Connection error.")

    for _ in range(4):
        with pytest.raises(module.TransportOutageError):
            module._call_with_transport_retry(
                operation, max_attempts=1, sleep=clock.sleep, breaker=breaker
            )

    # One shared window, not one per worker.
    assert breaker.metrics()["transport_outages"] == 1


def test_real_threads_share_one_window_and_resume_together() -> None:
    """Eight real workers must share one window and resume after recovery."""
    import threading

    from fabric_kg_builder.enrichment import foundry_client as module

    workers = 8
    breaker = module._TransportOutageBreaker(
        budget_seconds=30.0,
        base_seconds=0.01,
        max_seconds=0.05,
        sleep=lambda seconds: time.sleep(min(seconds, 0.02)),
    )
    down = threading.Event()
    down.set()
    started = threading.Barrier(workers)
    calls = {"n": 0}
    counter_lock = threading.Lock()
    results: list[object] = []

    def operation() -> str:
        with counter_lock:
            calls["n"] += 1
        if down.is_set():
            raise _FakeConnectionError("Connection error.")
        return "ok"

    def worker() -> None:
        started.wait(timeout=10.0)
        try:
            results.append(
                module._call_with_transport_retry(
                    operation, max_attempts=2, breaker=breaker
                )
            )
        except BaseException as exc:  # pragma: no cover - failure diagnostic
            results.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    # Let every worker discover the outage, then restore the provider.
    time.sleep(0.5)
    outages_during = breaker.metrics()["transport_outages"]
    down.clear()
    for thread in threads:
        thread.join(timeout=20.0)

    assert not any(thread.is_alive() for thread in threads), "worker deadlock"
    assert results == ["ok"] * workers
    # One shared window for all eight workers, not one each.
    assert outages_during == 1
    assert breaker.metrics()["transport_outages"] == 1
    assert breaker.metrics()["transport_outage_budget_exhausted"] is False


def test_recovery_releases_workers_without_waiting_out_the_backoff() -> None:
    """A closed window must not leave a worker sleeping out a stale delay."""
    from fabric_kg_builder.enrichment import foundry_client as module

    clock = _FakeClock()
    breaker = _breaker(clock, budget_seconds=600.0)
    breaker.record_outage()
    # A long wait is abandoned as soon as another worker closes the window.
    breaker.record_success()

    breaker.wait_before_retry(300.0)

    assert clock.now - 1000.0 <= module._TRANSPORT_OUTAGE_POLL_SECONDS


def test_outage_budget_override_rejects_non_finite_values(monkeypatch) -> None:
    """An infinite or NaN budget would retry forever and is rejected."""
    from fabric_kg_builder.enrichment import foundry_client as module

    for raw in ("nan", "inf", "-inf", "Infinity"):
        monkeypatch.setenv(module._TRANSPORT_OUTAGE_BUDGET_ENV, raw)
        with pytest.raises(ValueError, match="finite"):
            module._configured_outage_budget_seconds()


def test_breaker_metrics_are_sanitized() -> None:
    """Metrics carry timing and counts only — never request content."""
    clock = _FakeClock()
    breaker = _breaker(clock, budget_seconds=30.0)
    breaker.record_outage()

    metrics = breaker.metrics()

    assert metrics["contains_source_content"] is False
    assert set(metrics) == {
        "transport_outages",
        "transport_outages_recovered",
        "transport_delayed_retries",
        "transport_delay_seconds_total",
        "transport_longest_outage_seconds",
        "transport_outage_budget_exhausted",
        "contains_source_content",
    }
    assert all(
        isinstance(value, (int, float, bool)) for value in metrics.values()
    )


def test_outage_budget_override_rejects_invalid_values(monkeypatch) -> None:
    """A malformed budget override is a configuration error, not a default."""
    from fabric_kg_builder.enrichment import foundry_client as module

    monkeypatch.setenv(module._TRANSPORT_OUTAGE_BUDGET_ENV, "not-a-number")
    with pytest.raises(ValueError, match="must be a number"):
        module._configured_outage_budget_seconds()

    monkeypatch.setenv(module._TRANSPORT_OUTAGE_BUDGET_ENV, "-1")
    with pytest.raises(ValueError, match="must not be negative"):
        module._configured_outage_budget_seconds()

    monkeypatch.setenv(module._TRANSPORT_OUTAGE_BUDGET_ENV, " 45 ")
    assert module._configured_outage_budget_seconds() == 45.0

    monkeypatch.delenv(module._TRANSPORT_OUTAGE_BUDGET_ENV)
    assert (
        module._configured_outage_budget_seconds()
        == module._TRANSPORT_OUTAGE_BUDGET_SECONDS
    )
