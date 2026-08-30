"""Unit tests for FoundryClient.

Uses the mock Foundry client from conftest.py — no live API calls.
"""

from __future__ import annotations

import json
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

    with pytest.raises(_FakeConnectionError):
        module._call_with_transport_retry(
            operation, max_attempts=3, sleep=lambda _seconds: None
        )
    assert calls["n"] == 3


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
