"""Coordinator-owned retries preserve budgets, request identity and evidence barriers."""

import json
from email.utils import formatdate
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError

from fabric_kg_builder.domain import window_run
from fabric_kg_builder.enrichment import foundry_client
from tests.unit.test_document_schema import DocumentModel, config, document_sdk, profile
from tests.unit.test_window_run import budget, inputs


def failure(kind, headers=None):
    request = httpx.Request("POST", "https://offline.invalid")
    if kind == "timeout":
        return APITimeoutError(request=request)
    if kind == "connection":
        return APIConnectionError(request=request)
    return APIStatusError(
        "provider error", response=httpx.Response(kind, request=request, headers=headers), body=None)


@pytest.fixture
def waits(monkeypatch):
    values = []
    monkeypatch.setattr(window_run.time, "sleep", values.append)
    monkeypatch.setattr(window_run.random, "uniform", lambda *_: 0.25)
    monkeypatch.setattr(foundry_client, "_call_with_transport_retry",
                        lambda *_: pytest.fail("Coordinator must own every physical retry"))
    return values


def client_with_failures(errors, transport="chat_completions"):
    model = DocumentModel()
    model._config = model._config.model_copy(update={
        "chat_model": "gpt-5.4", "inference_api": transport,
    })
    model._client = document_sdk(model.complete_json, transport=transport,
                                response_model="gpt-5.4-2026-03-05")
    original = (model._client.responses.create if transport == "project_responses"
                else model._client.chat.completions.create)
    attempts = []

    def create(**kwargs):
        attempts.append(kwargs)
        if len(attempts) <= len(errors):
            raise errors[len(attempts) - 1]
        return original(**kwargs)

    model._client.responses.create = model._client.chat.completions.create = create
    return model, attempts


def run(tmp_path, model, **overrides):
    data = (window_run.load_window_inputs(
        prepared_path=tmp_path / "prepared.json", intake_path=tmp_path / "intake.json")
        if (tmp_path / "prepared.json").exists() else inputs(tmp_path, files=1))
    return window_run.run_windowed(
        inputs=data, output_dir=tmp_path / "state",
        config=config(
            model_transport=model._config.inference_api,
            capability_profile=profile(model_name="gpt-5.4", model_version="2026-03-05")),
        budget=budget(**{"max_transport_retries": 3, **overrides}), client=model)


@pytest.mark.parametrize("transport", ["chat_completions", "project_responses"])
@pytest.mark.parametrize("kind", [429, 408, 500, 502, 503, 504, "timeout", "connection"])
def test_transient_retries_are_budgeted_and_replayable(tmp_path, waits, kind, transport):
    model, attempts = client_with_failures([failure(kind)], transport)
    result = run(tmp_path, model)
    assert result.state == "complete"
    assert result.model_call_count == result.run.model_call_count == len(attempts) == 2
    assert attempts[0] == attempts[1]
    assert waits == [60.25 if kind == 429 else 1.25]
    root = tmp_path / "state"
    assert len(list((root / "physical-requests").glob("*.json"))) == 2
    assert len(list((root / "physical-errors").glob("*.json"))) == 1
    dispatches = [json.loads(p.read_text()) for p in sorted((root / "dispatches").glob("*.json"))]
    assert dispatches[1]["payload"]["automatic_retry_of"] == dispatches[0]["artifact_hash"]
    assert dispatches[1]["payload"]["explicit_retry"] is False
    assert result.reserved_tokens == sum(d["payload"]["reserved_tokens"] for d in dispatches)
    event = json.loads(next((root / "retries").glob("*.json")).read_text())["payload"]
    assert event["failed_dispatch_hash"] == dispatches[0]["artifact_hash"]
    assert window_run.load_windowed_run(root) == result.run
    cached = run(tmp_path, model)
    assert cached.model_call_count == 0 and len(attempts) == 2


@pytest.mark.parametrize("headers,expected", [
    ({"retry-after-ms": "2500"}, 2.5),
    ({"x-ms-retry-after-ms": "1250"}, 1.25),
    ({"retry-after": "75"}, 75),
    ({"retry-after": formatdate(1075, usegmt=True)}, 75),
    ({"retry-after": formatdate(900, usegmt=True)}, 0),
    ({"retry-after-ms": "500", "retry-after": "3"}, 3),
    ({"retry-after-ms": "garbage", "retry-after": "12"}, 12),
    ({"retry-after": "NaN"}, None),
    ({"retry-after": "inf"}, None),
    ({"retry-after-ms": "-100"}, None),
    ({"retry-after": "not a date"}, None),
])
def test_retry_after_headers(monkeypatch, headers, expected):
    monkeypatch.setattr(foundry_client.time, "time", lambda: 1000)
    assert foundry_client.transport_retry_after_seconds(failure(429, headers)) == expected


def test_provider_delay_is_not_capped_to_short_backoff(tmp_path, waits):
    model, attempts = client_with_failures([failure(429, {"retry-after-ms": "125000"})])
    assert run(tmp_path, model).state == "complete"
    assert waits == [125.25] and len(attempts) == 2


def test_exponential_fallback_stops_at_attempt_limit(tmp_path, waits):
    model, attempts = client_with_failures([failure(429)] * 8)
    result = run(tmp_path, model)
    assert result.reason == "uncertain_dispatch_requires_explicit_retry"
    assert result.cursor == 0 and result.model_call_count == len(attempts) == 4
    assert waits == [60.25, 120.25, 240.25]
    paused = run(tmp_path, model)
    assert paused.model_call_count == 0 and len(attempts) == 4
    assert not list((tmp_path / "state" / "responses").glob("*.json"))


def test_retry_disable_and_explicit_resume_preserve_history(tmp_path, waits):
    model, attempts = client_with_failures([failure("connection")])
    first = run(tmp_path, model, max_transport_retries=0)
    assert first.model_call_count == 1 and not waits
    paused = run(tmp_path, model)
    assert paused.model_call_count == 0 and len(attempts) == 1
    resumed = run(tmp_path, model, retry_uncertain=True)
    assert resumed.state == "complete" and resumed.model_call_count == 1
    assert resumed.run.model_call_count == 2 and len(attempts) == 2
    assert attempts[0] == attempts[1]


def test_interrupted_retry_wait_cannot_silently_redispatch(tmp_path, waits, monkeypatch):
    model, attempts = client_with_failures([failure(429)])

    def interrupt(_):
        raise KeyboardInterrupt()

    monkeypatch.setattr(window_run.time, "sleep", interrupt)
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, model)
    assert len(attempts) == 1
    root = tmp_path / "state"
    assert len(list((root / "dispatches").glob("*.json"))) == 2
    paused = run(tmp_path, model)
    assert paused.reason == "uncertain_dispatch_requires_explicit_retry"
    assert paused.model_call_count == 0 and len(attempts) == 1
    resumed = run(tmp_path, model, retry_uncertain=True)
    assert resumed.state == "complete" and len(attempts) == 2
    assert resumed.run.model_call_count == 3


@pytest.mark.parametrize("limits,expected_waits,count,reason", [
    ({"max_calls": 1}, [], 1, "model_budget_exhausted"),
    ({"max_transport_retry_wait_seconds": 0}, [], 1, "transport_retry_wait_budget_exhausted"),
    ({"max_transport_retry_wait_seconds": 100}, [60.25], 2, "transport_retry_wait_budget_exhausted"),
])
def test_retry_limits_preserve_failed_attempts(tmp_path, waits, limits, expected_waits, count, reason):
    model, attempts = client_with_failures([failure(429)] * 8)
    result = run(tmp_path, model, **limits)
    assert result.reason == reason
    assert result.model_call_count == len(attempts) == count
    assert waits == expected_waits
    assert result.cursor == 0 and not result.final_snapshot.concepts


def test_token_budget_blocks_second_physical_call(tmp_path, waits):
    model, attempts = client_with_failures([failure(429)])
    data = inputs(tmp_path, files=1)
    cfg = config(capability_profile=profile(model_name="gpt-5.4", model_version="2026-03-05"))
    reserve = window_run.plan_window_run(data, cfg)["input_preflight"]["documents"][0]["reserved_tokens"]
    result = run(tmp_path, model, max_tokens=reserve)
    assert result.reason == "model_budget_exhausted"
    assert len(attempts) == 1 and not waits


@pytest.mark.parametrize("kind", [400, 401, 403, 404, 409, 413, 422])
def test_permanent_provider_errors_do_not_retry(tmp_path, waits, kind):
    model, attempts = client_with_failures([failure(kind)])
    result = run(tmp_path, model)
    assert result.state == "partial" and len(attempts) == 1 and not waits
    diagnostic = json.loads(next((tmp_path / "state" / "errors").glob("*.json")).read_text())
    assert diagnostic["payload"]["error_type"] == "APIStatusError"


@pytest.mark.parametrize("response_model", [None, "gpt-4.1-2025-04-14"])
def test_invalid_model_is_not_a_transport_retry(tmp_path, waits, response_model):
    model, _ = client_with_failures([])
    model._client = document_sdk(model.complete_json, response_model=response_model)
    result = run(tmp_path, model)
    assert result.reason == "received_invalid_response_requires_explicit_retry"
    assert result.model_call_count == 1 and not waits


@pytest.mark.parametrize("transport", ["chat_completions", "project_responses"])
def test_invalid_json_is_not_a_transport_retry(tmp_path, waits, transport):
    model, _ = client_with_failures([], transport)
    invalid = SimpleNamespace(
        model="gpt-5.4-2026-03-05", output_text="{", status="completed",
        choices=[SimpleNamespace(message=SimpleNamespace(content="{"), finish_reason="stop")])
    model._client.responses.create = model._client.chat.completions.create = lambda **_: invalid
    result = run(tmp_path, model)
    assert result.reason == "received_invalid_response_requires_explicit_retry"
    assert result.model_call_count == 1 and not waits
