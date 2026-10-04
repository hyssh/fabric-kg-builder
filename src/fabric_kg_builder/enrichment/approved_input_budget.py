"""Offline, model-independent conservative accounting for approved requests."""

from __future__ import annotations

import json

from .foundry_client import generation_parameters, strict_response_schema

VERSION = "approved-input-budget/1.0.0"
DEFAULT_MAX_CONTEXT_TOKENS = 96_000
FRAMING_RESERVE_TOKENS = 1_024


def budget_policy(max_context_tokens, max_output_tokens):
    if (
        type(max_context_tokens) is not int or type(max_output_tokens) is not int
        or max_output_tokens < 256
        or max_context_tokens <= max_output_tokens + FRAMING_RESERVE_TOKENS
    ):
        raise ValueError(
            "APPROVED_REEXTRACTION_INVALID_CONTEXT_BUDGET: --max-context-tokens must exceed "
            "--max-output-tokens plus 1024 framing tokens"
        )
    return {
        "version": VERSION,
        "accounting": "conservative_utf8_bytes_as_tokens_not_model_tokenizer",
        "max_context_tokens": max_context_tokens,
        "output_reserve_tokens": max_output_tokens,
        "framing_reserve_tokens": FRAMING_RESERVE_TOKENS,
        "oversize_policy": "fail_closed_rechunk_new_approved_scope",
    }


def _serialized_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8"))


def request_envelope(config, *, system, user, schema, output):
    """Mirror the first Foundry request, including schema in both possible locations.

    Physical attempts are independently checked against their actual SDK kwargs;
    transport edits cannot silently invalidate this offline preflight.
    """
    strict_schema = None
    if schema:
        try:
            strict_schema = strict_response_schema(config, schema)
        except ValueError:
            pass
    if config.inference_api == "project_responses":
        instructions = system + "\nReturn only a complete JSON object."
        if schema:
            instructions += "\nRequired JSON schema:\n" + json.dumps(schema, sort_keys=True)
        format_value = (
            {"type": "json_schema", "name": "fabric_kg_structured_response",
             "schema": strict_schema, "strict": True}
            if strict_schema is not None else {"type": "json_object"}
        )
        return {
            "model": config.chat_deployment, "instructions": instructions,
            "input": "Return only a valid JSON object for this request.\n" + user,
            "text": {"format": format_value}, "max_output_tokens": output,
            **generation_parameters(config), "store": False,
        }
    instructions = system
    if schema:
        instructions += (
            "\nReturn an object that validates exactly against this JSON "
            "Schema. Do not add fields that the schema does not permit.\n"
            + json.dumps(schema, sort_keys=True)
        )
    format_value = (
        {"type": "json_schema", "json_schema": {
            "name": "fabric_kg_structured_response", "strict": True, "schema": strict_schema,
        }}
        if strict_schema is not None else {"type": "json_object"}
    )
    return {
        "model": config.chat_deployment,
        "messages": [{"role": "system", "content": instructions}, {"role": "user", "content": user}],
        "response_format": format_value, **generation_parameters(config),
        "max_completion_tokens": output,
    }


def account_request(request, policy):
    """Count every serialized byte, not chars/4; never expose request content."""
    input_estimate = _serialized_bytes(request) + policy["framing_reserve_tokens"]
    total = input_estimate + policy["output_reserve_tokens"]
    return {
        "estimated_input_tokens_upper_bound": input_estimate,
        "estimated_total_tokens_upper_bound": total,
        "remaining_context_tokens": policy["max_context_tokens"] - total,
    }


def enforce_request(request, policy):
    accounting = account_request(request, policy)
    if accounting["remaining_context_tokens"] < 0:
        raise ValueError(
            "APPROVED_REEXTRACTION_INPUT_BUDGET_EXCEEDED: "
            f"estimated_total={accounting['estimated_total_tokens_upper_bound']} "
            f"cap={policy['max_context_tokens']} "
            f"output_reserve={policy['output_reserve_tokens']}; "
            "conservative UTF-8 byte accounting, not model tokenization. "
            "Verify backend capacity before increasing --max-context-tokens, or re-chunk "
            "into a new approved scope; never truncate schema/source or edit a sealed cache."
        )
    return accounting


def preflight_prompt(config, authority, user):
    policy = authority["input_budget"]
    request = request_envelope(
        config, system=authority["system_prompt"], user=user,
        schema=authority["response_schema"], output=authority["max_output_tokens"],
    )
    # Removing source here measures fixed overhead only; this envelope is never sent.
    payload = json.loads(user)
    source = payload.pop("source_text")
    payload["source_text"] = ""
    fixed_request = request_envelope(
        config, system=authority["system_prompt"],
        user=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        schema=authority["response_schema"], output=authority["max_output_tokens"],
    )
    fixed = account_request(fixed_request, policy)
    try:
        accounting = enforce_request(request, policy)
    except ValueError as exc:
        reason = "fixed_overhead_exceeds_cap" if fixed["remaining_context_tokens"] < 0 else "source_slice_requires_rechunk"
        raise ValueError(
            f"{exc} reason={reason}; fixed_total={fixed['estimated_total_tokens_upper_bound']}; "
            f"available_serialized_source_bytes={max(0, fixed['remaining_context_tokens'])}"
        ) from exc
    return {
        **accounting,
        "fixed_total_tokens_upper_bound": fixed["estimated_total_tokens_upper_bound"],
        "source_utf8_bytes": len(source.encode("utf-8")),
        "rendered_system_utf8_bytes": len(authority["system_prompt"].encode("utf-8")),
        "user_payload_utf8_bytes": len(user.encode("utf-8")),
        "appended_schema_utf8_bytes": len(json.dumps(authority["response_schema"], sort_keys=True).encode("utf-8")),
    }


def summarize_preflight(config, authority, prompts):
    rows = [preflight_prompt(config, authority, prompt) for prompt in prompts]
    return {
        **authority["input_budget"], "checked_requests": len(rows),
        "maxima": {
            key: max(row[key] for row in rows)
            for key in rows[0] if key != "remaining_context_tokens"
        } if rows else {},
        "minimum_remaining_context_tokens": min(
            (row["remaining_context_tokens"] for row in rows), default=None,
        ),
        "contains_source_content": False,
    }
