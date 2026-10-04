import json

import pytest
import tiktoken
from pydantic import ValidationError

from fabric_kg_builder.config.schema import FoundryConfig
from fabric_kg_builder.enrichment.approved_input_budget import request_envelope
from fabric_kg_builder.enrichment.model_capabilities import (
    ModelCapabilityProfile,
    enforce_model_request,
    preflight_model_prompt,
    validate_model_binding,
    validate_model_response,
)


def profile(**overrides):
    return ModelCapabilityProfile(**{
        "deployment": "ontology-model",
        "model_name": "gpt-4.1",
        "model_version": "2025-04-14",
        "deployment_sku": "GlobalStandard",
        "endpoint": "https://example.openai.azure.com/",
        "source": "Operator checked deployment model/version and published limits",
        **overrides,
    })


def config(**overrides):
    return FoundryConfig(**{
        "endpoint": "https://example.services.ai.azure.com/api/projects/demo",
        "openai_endpoint": "https://example.openai.azure.com/",
        "chat_deployment": "ontology-model",
        **overrides,
    })


@pytest.mark.parametrize("transport", ["chat_completions", "project_responses"])
def test_counts_complete_envelope_and_separate_reserves(transport):
    cfg = config(inference_api=transport)
    limits = profile(endpoint=cfg.endpoint) if transport == "project_responses" else profile()
    schema = {
        "type": "object",
        "properties": {"concepts": {"type": "array", "items": {"type": "string"}}},
        "required": ["concepts"],
        "additionalProperties": False,
    }
    request = request_envelope(cfg, system="Fixed JSON instruction", user="Source", schema=schema, output=4096)
    result = preflight_model_prompt(
        cfg, limits, system="Fixed JSON instruction", user="Source", schema=schema, output=4096,
    )
    encoded = json.dumps(request, ensure_ascii=False, sort_keys=True, allow_nan=False)
    assert result["serialized_request_tokens"] == len(
        tiktoken.get_encoding("o200k_base").encode(encoded)
    )
    assert result["estimated_total_tokens"] == (
        result["serialized_request_tokens"] + 1024 + result["safety_reserve_tokens"] + 4096
    )
    assert result["remaining_input_tokens"] == limits.max_input_tokens - result["estimated_input_tokens"]
    assert "Source" not in json.dumps(result)
    assert result["contains_source_content"] is False


def test_output_schema_is_counted_in_both_locations():
    cfg = config()
    small = preflight_model_prompt(cfg, profile(), system="JSON", user="Text", schema={}, output=256)
    large = preflight_model_prompt(
        cfg, profile(), system="JSON", user="Text",
        schema={"type": "object", "properties": {"v": {"type": "string", "description": "word " * 5000}},
                "required": ["v"], "additionalProperties": False},
        output=256,
    )
    assert large["serialized_request_tokens"] - small["serialized_request_tokens"] > 10000


def test_special_token_text_remains_untrusted_plain_text():
    result = preflight_model_prompt(
        config(), profile(), system="JSON", user="<|endoftext|> multilingual \u4e2d\u6587",
        schema={}, output=256,
    )
    assert result["serialized_request_tokens"] > 0


@pytest.mark.parametrize("overrides", [
    {"model_name": "unknown"},
    {"model_version": "future"},
    {"max_output_tokens": 32769},
    {"context_tokens": 1047577},
    {"context_tokens": True},
    {"max_input_tokens": 128001},
    {"deployment": " "},
    {"deployment_sku": " "},
    {"deployment_sku": "GlobalBatch"},
    {"deployment_sku": "Standard", "context_tokens": 400000},
    {"deployment_sku": "ProvisionedManaged", "context_tokens": 300000},
    {"source": ""},
    {"endpoint": "http://example.com"},
    {"endpoint": "https://user:password@example.com"},
    {"endpoint": "https://example.com?key=secret"},
])
def test_invalid_profiles_rejected(overrides):
    with pytest.raises(ValidationError):
        profile(**overrides)


@pytest.mark.parametrize("overrides", [
    {"chat_deployment": "another-alias"},
    {"openai_endpoint": "https://another.openai.azure.com"},
    {"inference_api": "project_responses"},
])
def test_profile_is_bound_to_configured_endpoint_and_deployment(overrides):
    with pytest.raises(ValueError, match="BINDING_MISMATCH"):
        validate_model_binding(config(**overrides), profile())


@pytest.mark.parametrize("model", [None, "gpt-4.1", "gpt-4.1-2026-01-01", "gpt-5"])
def test_actual_response_detects_model_retargeting(model):
    with pytest.raises(ValueError, match="VERSION_MISMATCH"):
        validate_model_response(model, profile())


def test_actual_response_matches_sealed_model_version():
    validate_model_response("gpt-4.1-2025-04-14", profile())


@pytest.mark.parametrize("changes", [
    {"max_completion_tokens": 32769},
    {"max_completion_tokens": True},
    {"max_completion_tokens": 255},
    {"max_completion_tokens": 4096, "max_output_tokens": 4096},
    {"model": "another-alias"},
])
def test_actual_request_changes_are_rejected(changes):
    with pytest.raises(ValueError, match="DOCUMENT_MODEL_"):
        enforce_model_request(
            {"model": "ontology-model", "max_completion_tokens": 4096, **changes}, profile(),
        )


@pytest.mark.parametrize("limits", [
    {"context_tokens": 5000, "max_input_tokens": 5000, "max_output_tokens": 4096},
    {"max_input_tokens": 1024},
])
def test_separate_context_and_input_limits(limits):
    with pytest.raises(ValueError, match="CONTEXT_EXCEEDED"):
        preflight_model_prompt(
            config(), profile(**limits), system="JSON", user="word " * 2000, schema={}, output=4096,
        )


def test_whole_document_can_fit_without_byte_count_rejection():
    result = preflight_model_prompt(
        config(), profile(), system="JSON", user="Read the service manual. " * 10000,
        schema={}, output=8192,
    )
    assert result["serialized_request_utf8_bytes"] > 128000
    assert result["estimated_total_tokens"] < 128000


def test_no_implicit_truncation_at_context_boundary():
    with pytest.raises(ValueError, match="does not truncate"):
        preflight_model_prompt(
            config(), profile(), system="JSON", user="word " * 125000, schema={}, output=8192,
        )
