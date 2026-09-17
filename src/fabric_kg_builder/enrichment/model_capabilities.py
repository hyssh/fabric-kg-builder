"""Explicit deployment limits and complete-request estimates for document discovery."""

from __future__ import annotations

import json
import math
from typing import Any, Literal
from urllib.parse import urlsplit

import tiktoken
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .approved_input_budget import request_envelope
from ..config.schema import FoundryConfig

ACCOUNTING_VERSION = "document-model-budget/1.0.0"
FRAMING_RESERVE_TOKENS = 1024
SAFETY_MARGIN = 0.05
_SKU_CONTEXT_CEILINGS = {
    "GlobalStandard": 1047576,
    "DataZoneStandard": 1047576,
    "Standard": 300000,
    "ProvisionedManaged": 128000,
    "GlobalProvisionedManaged": 128000,
    "DataZoneProvisionedManaged": 128000,
}


class ModelCapabilityProfile(BaseModel):
    """Operator-declared deployment capabilities, not inferred from an alias or TPM."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    deployment: str = Field(min_length=1)
    model_name: Literal["gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "gpt-5.4"]
    model_version: Literal["2025-04-14", "2026-03-05"]
    deployment_sku: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    context_tokens: int = Field(default=128000, ge=1024, le=1050000)
    max_input_tokens: int = Field(default=128000, ge=1024, le=1047576)
    max_output_tokens: int = Field(default=32768, ge=256, le=128000)
    encoding: Literal["o200k_base"] = "o200k_base"
    source: str = Field(min_length=1)

    @field_validator("deployment", "deployment_sku", "source")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("Capability fields must be nonblank without surrounding whitespace")
        return value

    @field_validator("endpoint")
    @classmethod
    def _endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or value != value.strip()
        ):
            raise ValueError("Capability endpoint must be HTTPS without credentials, query or fragment")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _consistent_limits(self) -> ModelCapabilityProfile:
        is_gpt54 = self.model_name == "gpt-5.4"
        expected_version = "2026-03-05" if is_gpt54 else "2025-04-14"
        if self.model_version != expected_version:
            raise ValueError("Unsupported model/version combination")
        ceilings = (
            {"GlobalStandard": 1050000, "DataZoneStandard": 1050000,
             "GlobalProvisionedManaged": 128000, "DataZoneProvisionedManaged": 128000}
            if is_gpt54 else _SKU_CONTEXT_CEILINGS
        )
        ceiling = ceilings.get(self.deployment_sku)
        if ceiling is None:
            raise ValueError("Unsupported deployment SKU for synchronous model discovery")
        if self.context_tokens > ceiling:
            raise ValueError("Declared context exceeds the documented deployment SKU ceiling")
        if self.max_output_tokens > (128000 if is_gpt54 else 32768):
            raise ValueError("Declared output exceeds the documented model ceiling")
        if is_gpt54 and self.max_input_tokens > 922000:
            raise ValueError("Declared input exceeds the documented model ceiling")
        if self.max_input_tokens > self.context_tokens:
            raise ValueError("Maximum input cannot exceed the context window")
        if self.max_output_tokens > self.context_tokens:
            raise ValueError("Maximum output cannot exceed the context window")
        return self


def validate_model_binding(config: FoundryConfig, profile: ModelCapabilityProfile) -> None:
    endpoint = (
        config.endpoint if config.inference_api == "project_responses"
        else config.openai_endpoint
    )
    if (
        config.chat_deployment != profile.deployment
        or endpoint.rstrip("/") != profile.endpoint
        or (config.chat_model and config.chat_model != profile.model_name)
        or (
            profile.model_name == "gpt-5.4"
            and (config.chat_model or config.chat_deployment) != profile.model_name
        )
    ):
        raise ValueError(
            "DOCUMENT_MODEL_CAPABILITY_BINDING_MISMATCH: configured endpoint/deployment "
            "differs from the explicit capability profile"
        )


def validate_model_response(model: str | None, profile: ModelCapabilityProfile) -> None:
    """Detect a retargeted deployment before its proposals become schema history."""
    if model != f"{profile.model_name}-{profile.model_version}":
        raise ValueError(
            "DOCUMENT_MODEL_VERSION_MISMATCH: provider response does not match "
            "the model/version in the sealed capability profile"
        )


def enforce_model_request(
    request: dict[str, Any], profile: ModelCapabilityProfile,
) -> dict[str, Any]:
    """Estimate the whole envelope, then reserve framing, safety margin and output.

    SDK JSON is not the provider's internal token stream. This deliberately counts
    duplicated schema metadata and retains headroom; only the provider reports
    authoritative usage. Source text that resembles special tokens is plain data.
    """
    if request.get("model") != profile.deployment:
        raise ValueError("DOCUMENT_MODEL_CAPABILITY_BINDING_MISMATCH: request deployment changed")
    output_fields = [
        request[key] for key in ("max_completion_tokens", "max_output_tokens")
        if key in request
    ]
    if len(output_fields) != 1 or type(output_fields[0]) is not int or output_fields[0] < 256:
        raise ValueError("DOCUMENT_MODEL_OUTPUT_LIMIT_INVALID: supply one integer output reserve >=256")
    output = output_fields[0]
    if output > profile.max_output_tokens:
        raise ValueError(
            f"DOCUMENT_MODEL_OUTPUT_LIMIT_EXCEEDED: requested={output} "
            f"cap={profile.max_output_tokens}"
        )
    serialized = json.dumps(request, ensure_ascii=False, sort_keys=True, allow_nan=False)
    encoding = tiktoken.get_encoding(profile.encoding)
    tokens = len(encoding.encode(serialized, disallowed_special=()))
    safety = math.ceil(tokens * SAFETY_MARGIN)
    estimated_input = tokens + FRAMING_RESERVE_TOKENS + safety
    total = estimated_input + output
    accounting = {
        "version": ACCOUNTING_VERSION,
        "accounting": "serialized_sdk_envelope_tokenizer_estimate_with_headroom",
        "encoding": profile.encoding,
        "serialized_request_tokens": tokens,
        "serialized_request_utf8_bytes": len(serialized.encode("utf-8")),
        "framing_reserve_tokens": FRAMING_RESERVE_TOKENS,
        "safety_reserve_tokens": safety,
        "estimated_input_tokens": estimated_input,
        "output_reserve_tokens": output,
        "estimated_total_tokens": total,
        "context_tokens": profile.context_tokens,
        "max_input_tokens": profile.max_input_tokens,
        "remaining_context_tokens": profile.context_tokens - total,
        "remaining_input_tokens": profile.max_input_tokens - estimated_input,
        "capability_authority": "explicit_operator_profile_not_live_capacity_attestation",
        "contains_source_content": False,
    }
    if estimated_input > profile.max_input_tokens or total > profile.context_tokens:
        raise ValueError(
            "DOCUMENT_MODEL_CONTEXT_EXCEEDED: "
            f"estimated_input={estimated_input} input_cap={profile.max_input_tokens} "
            f"estimated_total={total} context_cap={profile.context_tokens}; "
            "whole-document discovery does not truncate or silently switch to chunks. "
            "Use a separately reviewed higher-capacity deployment/profile or explicit chunked discovery."
        )
    return accounting


def preflight_model_prompt(
    config: FoundryConfig,
    profile: ModelCapabilityProfile,
    *,
    system: str,
    user: str,
    schema: dict[str, Any],
    output: int,
) -> dict[str, Any]:
    validate_model_binding(config, profile)
    return enforce_model_request(
        request_envelope(config, system=system, user=user, schema=schema, output=output),
        profile,
    )
