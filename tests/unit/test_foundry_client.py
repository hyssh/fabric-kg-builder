"""Unit tests for FoundryClient.

Uses the mock Foundry client from conftest.py — no live API calls.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

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
