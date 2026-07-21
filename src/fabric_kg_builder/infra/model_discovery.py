"""Model availability and quota discovery before deployment.

Before deploying any model, this module queries:
1. ``az cognitiveservices model list`` — catalog of available models in the region.
2. ``az cognitiveservices usage list``  — current quota usage per SKU/model.
3. Azure REST modelCapacities API        — available capacity per model/SKU/location.

Capacity/quota failures block model deployment. No hardcoded deployability.

GPT-4.1 target: 200,000 TPM (GlobalStandard, capacity unit 200).
text-embedding-3-large: verify availability in region.

Quota is subscription + region scoped.  The scope returned by the Foundry
account is used; we do not assume it is always region-scoped.

SPEC-006 §6.4 / INF-007.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .schema import (
    ModelCapacityInfo,
    ModelDiscoveryResult,
    ModelSku,
)
from .runner import CommandRunner, CommandError

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Model catalog helpers
# ---------------------------------------------------------------------------


def list_available_models(
    runner: CommandRunner,
    subscription_id: str,
    location: str,
    resource_group: str,
    account_name: str,
) -> list[dict]:
    """Return the list of deployable models from the Foundry account catalog.

    Calls ``az cognitiveservices model list``.

    Returns an empty list if the command fails; callers must treat an empty
    list as a failure (cannot assume deployability).
    """
    try:
        result = runner.run([
            "az", "cognitiveservices", "model", "list",
            "--location", location,
            "--subscription", subscription_id,
            "--output", "json",
        ])
    except CommandError:
        return []
    if not result.succeeded:
        return []
    try:
        models = json.loads(result.stdout)
        if isinstance(models, list):
            return models
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def list_usage(
    runner: CommandRunner,
    subscription_id: str,
    location: str,
) -> list[dict]:
    """Return current quota usage from ``az cognitiveservices usage list``.

    Returns an empty list if the command fails.
    """
    try:
        result = runner.run([
            "az", "cognitiveservices", "usage", "list",
            "--location", location,
            "--subscription", subscription_id,
            "--output", "json",
        ])
    except CommandError:
        return []
    if not result.succeeded:
        return []
    try:
        usages = json.loads(result.stdout)
        if isinstance(usages, list):
            return usages
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def query_model_capacities(
    runner: CommandRunner,
    subscription_id: str,
    location: str,
    model_name: str,
    model_version: str | None = None,
    sku: str = "GlobalStandard",
) -> list[dict]:
    """Query the modelCapacities REST API via ``az rest``.

    Endpoint: GET /subscriptions/{sub}/providers/Microsoft.CognitiveServices/
              locations/{location}/modelCapacities?...

    Returns an empty list on failure; callers treat empty as unknown/blocked.
    """
    api_version = "2024-10-01"
    url = (
        f"/subscriptions/{subscription_id}/providers/Microsoft.CognitiveServices"
        f"/locations/{location}/modelCapacities"
        f"?api-version={api_version}"
        f"&modelFormat=OpenAI"
        f"&modelName={model_name}"
        f"&modelVersion={model_version or '*'}"
        f"&skuName={sku}"
    )
    try:
        result = runner.run([
            "az", "rest",
            "--method", "GET",
            "--uri", url,
            "--subscription", subscription_id,
        ])
    except CommandError:
        return []
    if not result.succeeded:
        return []
    try:
        data = json.loads(result.stdout)
        if isinstance(data, dict):
            return data.get("value", [])
    except (json.JSONDecodeError, TypeError):
        pass
    return []


# ---------------------------------------------------------------------------
# Capacity resolution
# ---------------------------------------------------------------------------


def _find_model_in_catalog(models: list[dict], model_name: str) -> dict | None:
    """Return the first catalog entry matching *model_name*."""
    for m in models:
        name = (
            m.get("name")
            or m.get("modelName")
            or (m.get("model", {}) or {}).get("name")
            or ""
        )
        if isinstance(name, str) and name.lower() == model_name.lower():
            return m
    return None


def _find_usage_for_model(usages: list[dict], model_name: str, sku: str) -> dict | None:
    """Return the first usage entry matching *model_name* and *sku*."""
    for u in usages:
        name_val = (
            u.get("name", {}) or {}
        )
        usage_name = (
            name_val.get("value", "") if isinstance(name_val, dict)
            else str(name_val)
        )
        if model_name.lower() in usage_name.lower() and sku.lower() in usage_name.lower():
            return u
    return None


def _compute_available_tpm(
    capacities: list[dict],
    usages: list[dict],
    model_name: str,
    sku: str,
) -> tuple[int | None, int | None]:
    """Return (available_capacity, used_capacity) in 1000-TPM units."""
    # From modelCapacities API
    for cap in capacities:
        avail = cap.get("availableCapacity")
        if avail is not None:
            # Convert to TPM: capacity * 1000 = TPM
            usage_entry = _find_usage_for_model(usages, model_name, sku)
            used = None
            if usage_entry:
                current_value = usage_entry.get("currentValue", 0)
                used = int(current_value)
            return int(avail), used
    return None, None


# ---------------------------------------------------------------------------
# Main discovery entry point
# ---------------------------------------------------------------------------


def discover_model_capacity(
    runner: CommandRunner,
    subscription_id: str,
    location: str,
    resource_group: str,
    account_name: str,
    chat_model: str,
    chat_sku: str,
    chat_target_tpm: int,
    embedding_model: str,
) -> ModelDiscoveryResult:
    """Query availability and quota for chat and embedding models.

    Returns a ``ModelDiscoveryResult`` with ``all_deployable=True`` only when
    both models are verified available with sufficient capacity.  Any query
    failure sets ``all_deployable=False`` and populates ``errors``.

    SPEC-006 §6.4 / INF-007.
    """
    errors: list[str] = []

    # --- Catalog ---
    catalog = list_available_models(
        runner, subscription_id, location, resource_group, account_name
    )
    if not catalog:
        errors.append(
            f"Model catalog query failed for location '{location}'. "
            "Cannot verify model deployability. "
            "Ensure Microsoft.CognitiveServices provider is registered and "
            "the Foundry account exists."
        )
        # Cannot verify anything further without a catalog
        return ModelDiscoveryResult(
            subscription_id=subscription_id,
            location=location,
            chat_model=None,
            embedding_model=None,
            all_deployable=False,
            errors=errors,
        )

    # --- Usage ---
    usages = list_usage(runner, subscription_id, location)
    if not usages and not errors:
        errors.append(
            f"Quota usage query failed for subscription '{subscription_id}' "
            f"in '{location}'. Cannot verify quota availability."
        )

    # --- Chat model capacity ---
    chat_info: ModelCapacityInfo | None = None
    if not errors:
        chat_capacities = query_model_capacities(
            runner, subscription_id, location,
            model_name=chat_model,
            sku=chat_sku,
        )
        catalog_entry = _find_model_in_catalog(catalog, chat_model)
        avail_cap, used_cap = _compute_available_tpm(
            chat_capacities, usages, chat_model, chat_sku
        )

        if catalog_entry is None:
            chat_info = ModelCapacityInfo(
                model=chat_model,
                sku=chat_sku,
                subscription_id=subscription_id,
                location=location,
                deployable=False,
                reason=f"Model '{chat_model}' not found in catalog for location '{location}'.",
            )
            errors.append(chat_info.reason)
        elif avail_cap is None:
            chat_info = ModelCapacityInfo(
                model=chat_model,
                sku=chat_sku,
                subscription_id=subscription_id,
                location=location,
                deployable=False,
                reason=(
                    f"Capacity query returned no results for '{chat_model}' ({chat_sku}). "
                    "Cannot confirm availability."
                ),
            )
            errors.append(chat_info.reason)
        else:
            # target_tpm in units of 1000 = capacity units
            target_capacity_units = chat_target_tpm // 1000
            deployable = avail_cap >= target_capacity_units
            chat_info = ModelCapacityInfo(
                model=chat_model,
                sku=chat_sku,
                subscription_id=subscription_id,
                location=location,
                available_capacity=avail_cap,
                used_capacity=used_cap,
                unit="1000_tokens_per_minute",
                deployable=deployable,
                reason=(
                    None if deployable else
                    f"Insufficient capacity: need {target_capacity_units} units "
                    f"({chat_target_tpm:,} TPM), available {avail_cap}."
                ),
            )
            if not deployable:
                errors.append(chat_info.reason)  # type: ignore[arg-type]

    # --- Embedding model capacity ---
    embedding_info: ModelCapacityInfo | None = None
    if not errors:
        embedding_capacities = query_model_capacities(
            runner, subscription_id, location,
            model_name=embedding_model,
            sku="GlobalStandard",
        )
        emb_catalog = _find_model_in_catalog(catalog, embedding_model)
        emb_avail, emb_used = _compute_available_tpm(
            embedding_capacities, usages, embedding_model, "GlobalStandard"
        )

        if emb_catalog is None:
            embedding_info = ModelCapacityInfo(
                model=embedding_model,
                sku="GlobalStandard",
                subscription_id=subscription_id,
                location=location,
                deployable=False,
                reason=(
                    f"Embedding model '{embedding_model}' not found in catalog "
                    f"for location '{location}'."
                ),
            )
            errors.append(embedding_info.reason)
        elif emb_avail is None:
            # Unknown or unavailable capacity data MUST block — no partial deployment.
            embedding_info = ModelCapacityInfo(
                model=embedding_model,
                sku="GlobalStandard",
                subscription_id=subscription_id,
                location=location,
                deployable=False,
                reason=(
                    f"Capacity data unavailable for '{embedding_model}' ({location}). "
                    "Unknown quota blocks deployment — use explicit operator override if "
                    "capacity is confirmed out-of-band."
                ),
            )
            errors.append(embedding_info.reason)
        else:
            deployable = emb_avail > 0
            embedding_info = ModelCapacityInfo(
                model=embedding_model,
                sku="GlobalStandard",
                subscription_id=subscription_id,
                location=location,
                available_capacity=emb_avail,
                used_capacity=emb_used,
                unit="1000_tokens_per_minute",
                deployable=deployable,
                reason=(
                    None if deployable else
                    f"No embedding capacity available for '{embedding_model}'."
                ),
            )
            if not deployable:
                errors.append(embedding_info.reason)  # type: ignore[arg-type]

    return ModelDiscoveryResult(
        subscription_id=subscription_id,
        location=location,
        chat_model=chat_info,
        embedding_model=embedding_info,
        all_deployable=len(errors) == 0,
        errors=errors,
    )
