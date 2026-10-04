import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from fabric_kg_builder.config.loader import load_config
from fabric_kg_builder.config.schema import FoundryConfig
from fabric_kg_builder.enrichment.approved_input_budget import request_envelope
from fabric_kg_builder.enrichment.foundry_client import FoundryClient, _azure_strict_schema
from fabric_kg_builder.enrichment.model_capabilities import (
    ModelCapabilityProfile, validate_model_binding, validate_model_response,
)


def profile(**overrides):
    return ModelCapabilityProfile(**{
        "deployment": "ontology-model",
        "model_name": "gpt-5.4",
        "model_version": "2026-03-05",
        "deployment_sku": "GlobalStandard",
        "endpoint": "https://example.openai.azure.com",
        "source": "Operator verified model version and deployment",
        **overrides,
    })


@pytest.mark.parametrize("transport", ["chat_completions", "project_responses"])
@pytest.mark.parametrize("alias", [False, True])
def test_gpt54_preflight_matches_dispatch_and_retry(transport, alias):
    cfg = FoundryConfig(
        endpoint="https://example.openai.azure.com",
        openai_endpoint="https://example.openai.azure.com",
        chat_deployment="ontology-model" if alias else "gpt-5.4",
        chat_model="gpt-5.4" if alias else "",
        inference_api=transport,
    )
    sdk = MagicMock()
    if transport == "chat_completions":
        create = sdk.chat.completions.create
        create.side_effect = [
            SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=text), finish_reason="stop",
            )], usage=None, model="gpt-5.4-2026-03-05")
            for text in ("bad JSON", '{"concepts":[]}')
        ]
    else:
        create = sdk.responses.create
        create.side_effect = [
            SimpleNamespace(output_text=text, status="completed", incomplete_details=None,
                            usage=None, model="gpt-5.4-2026-03-05")
            for text in ("bad JSON", '{"concepts":[]}')
        ]
    client = FoundryClient(cfg, _sdk_client=sdk)
    schema = {
        "type": "object", "properties": {"concepts": {"type": "array", "items": {"type": "string"}}},
        "required": ["concepts"], "additionalProperties": False,
    }
    assert client.complete_json("Fixed JSON instructions", "Document", schema) == {"concepts": []}
    assert create.call_args_list[0].kwargs == request_envelope(
        cfg, system="Fixed JSON instructions", user="Document", schema=schema, output=32768,
    )
    for call in create.call_args_list:
        assert "temperature" not in call.kwargs
        assert "seed" not in call.kwargs
        if transport == "chat_completions":
            assert call.kwargs["reasoning_effort"] == "medium"
        else:
            assert call.kwargs["reasoning"] == {"effort": "medium"}
    identity = client.execution_identity()
    assert "temperature" not in identity and "seed" not in identity
    assert "medium" in json.dumps(identity)


def test_config_loads_explicit_model_for_alias(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AZURE_AI_FOUNDRY_ENDPOINT", "https://example.services.ai.azure.com")
    monkeypatch.setenv("AZURE_AI_CHAT_DEPLOYMENT", "ontology-model")
    monkeypatch.setenv("AZURE_AI_CHAT_MODEL", "gpt-5.4")
    cfg = load_config().foundry
    assert cfg.chat_deployment == "ontology-model"
    assert cfg.chat_model == "gpt-5.4"


def test_gpt54_profile_requires_matching_model_and_version():
    cfg = FoundryConfig(
        endpoint=profile().endpoint, openai_endpoint=profile().endpoint,
        chat_deployment="ontology-model", chat_model="gpt-5.4",
    )
    validate_model_binding(cfg, profile())
    validate_model_response("gpt-5.4-2026-03-05", profile())
    with pytest.raises(ValueError, match="BINDING_MISMATCH"):
        validate_model_binding(cfg.model_copy(update={"chat_model": ""}), profile())
    with pytest.raises(ValueError, match="VERSION_MISMATCH"):
        validate_model_response("gpt-4.1-2025-04-14", profile())


@pytest.mark.parametrize("changes", [
    {"model_version": "2025-04-14"},
    {"max_input_tokens": 922001, "context_tokens": 1050000},
    {"deployment_sku": "GlobalBatch"},
    {"deployment_sku": "Standard"},
])
def test_gpt54_rejects_invalid_capability_claims(changes):
    with pytest.raises(ValidationError):
        profile(**changes)


def test_gpt54_allows_documented_input_output_ceiling():
    limits = profile(context_tokens=1050000, max_input_tokens=922000, max_output_tokens=128000)
    assert limits.context_tokens == limits.max_input_tokens + limits.max_output_tokens


@pytest.mark.parametrize("transport", ["chat_completions", "project_responses"])
def test_gpt54_explicit_maximum_output_is_not_reduced(transport):
    cfg = FoundryConfig(endpoint=profile().endpoint, chat_deployment="ontology-model",
                        chat_model="gpt-5.4", inference_api=transport)
    sdk = MagicMock()
    response = SimpleNamespace(output_text="{}", status="completed", usage=None,
                               choices=[SimpleNamespace(message=SimpleNamespace(content="{}"), finish_reason="stop")])
    sdk.responses.create.return_value = sdk.chat.completions.create.return_value = response
    client = FoundryClient(cfg, _sdk_client=sdk)
    assert client.complete_json("Return JSON", "Document", {}, max_completion_tokens=128000) == {}
    create = sdk.responses.create if transport == "project_responses" else sdk.chat.completions.create
    output_key = "max_output_tokens" if transport == "project_responses" else "max_completion_tokens"
    assert create.call_args.kwargs[output_key] == 128000
    with pytest.raises(ValueError, match="128000"):
        client.complete_json("Return JSON", "Document", {}, max_completion_tokens=128001)
    assert create.call_count == 1


def test_incomplete_response_records_reasoning_usage_not_hidden_text():
    from fabric_kg_builder.enrichment.foundry_client import _json_response_diagnostics

    response = SimpleNamespace(model="gpt-5.4-2026-03-05", usage=SimpleNamespace(
        prompt_tokens=14519, completion_tokens=32768, total_tokens=47287,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=32768, hidden_text="not retained"),
        prompt_tokens_details=SimpleNamespace(cached_tokens=14000),
    ))
    diagnostic = _json_response_diagnostics(
        response, "", transport="chat_completions", output_limit=32768, attempt=1, finish_reason="length")
    assert diagnostic["provider_model"] == response.model
    assert diagnostic["usage_details"] == {
        "completion_tokens_details": {"reasoning_tokens": 32768},
        "prompt_tokens_details": {"cached_tokens": 14000},
    }
    assert "not retained" not in json.dumps(diagnostic)


@pytest.mark.parametrize("mapping", [
    {"type": "object", "additionalProperties": True},
    {"type": "object", "additionalProperties": {"type": "string"}},
    {"type": "object"},
])
def test_open_mappings_are_not_sent_as_invalid_strict_schemas(mapping):
    schema = {"type": "object", "properties": {"policy": mapping}}
    with pytest.raises(ValueError, match="open-ended"):
        _azure_strict_schema(schema, reject_open_mappings=True)
    cfg = FoundryConfig(endpoint="https://example.openai.azure.com", chat_deployment="gpt-5.4")
    request = request_envelope(cfg, system="JSON schema", user="Document", schema=schema, output=8192)
    assert request["response_format"] == {"type": "json_object"}
    legacy = request_envelope(
        cfg.model_copy(update={"chat_deployment": "gpt-4.1"}),
        system="JSON schema", user="Document", schema=schema, output=8192,
    )
    assert legacy["response_format"]["type"] == "json_schema"
