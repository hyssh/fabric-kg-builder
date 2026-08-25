"""Infrastructure apply and status.

Orchestrates ``az deployment group create`` (Bicep), persists operation and
output state without secrets, supports dry-run, and is fully idempotent.

Key correctness invariants:
  - Managed resource IDs are recorded ONLY from successful deployment outputs.
    A failed apply writes ``last_operation_status: failed`` but does NOT add
    resources to ``managed_resource_ids`` — so a retry re-attempts them.
  - Parameters for Bicep are generated from the validated YAML manifest and
    written to build/infra/<env>/parameters.json before every apply.  No
    bicepparam file needs to exist on disk.
  - Live Azure operations only run from ``infra apply`` (or opted-in smoke tests).
    Unit tests inject FakeCommandRunner.

SPEC-006 §4.1 / INF-013.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from fabric_kg_builder.release.redact import canonicalize_https_authority
from fabric_kg_builder.infra.authority import (
    sanitize_infrastructure_outputs,
    sanitize_infrastructure_state,
)

from .runner import CommandRunner, CommandError
from .schema import (
    InfraManifest,
    InfraPlan,
    InfraState,
    PlanAction,
    PlanItem,
    ResourceMode,
)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_STATE_FILENAME = "state.json"
_OUTPUTS_FILENAME = "outputs.json"
_PARAMS_FILENAME = "parameters.json"


def _state_dir(build_root: Path, environment: str) -> Path:
    return build_root / "infra" / environment


def _state_path(build_root: Path, environment: str) -> Path:
    return _state_dir(build_root, environment) / _STATE_FILENAME


def _outputs_path(build_root: Path, environment: str) -> Path:
    return _state_dir(build_root, environment) / _OUTPUTS_FILENAME


def _params_path(build_root: Path, environment: str) -> Path:
    return _state_dir(build_root, environment) / _PARAMS_FILENAME


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def load_state(build_root: Path, environment: str) -> InfraState:
    """Load persisted state or return a fresh empty state."""
    path = _state_path(build_root, environment)
    if not path.exists():
        return InfraState(environment=environment)
    raw = json.loads(path.read_text(encoding="utf-8"))
    return InfraState.model_validate(
        sanitize_infrastructure_state(raw, environment=environment)
    )


def save_state(state: InfraState, build_root: Path) -> Path:
    """Persist state to build/infra/<env>/state.json."""
    path = _state_path(build_root, state.environment)
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_state = sanitize_infrastructure_state(
        state.model_dump(mode="json"),
        environment=state.environment,
    )
    path.write_text(
        json.dumps(safe_state, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


def save_outputs(outputs: dict, build_root: Path, environment: str) -> Path:
    """Persist non-secret outputs to build/infra/<env>/outputs.json."""
    safe_outputs = sanitize_infrastructure_outputs(outputs)
    path = _outputs_path(build_root, environment)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(safe_outputs, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


def load_outputs(build_root: Path, environment: str) -> dict:
    """Load persisted non-secret outputs, or return empty dict."""
    path = _outputs_path(build_root, environment)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    safe = sanitize_infrastructure_outputs(raw)
    if safe != raw:
        save_outputs(safe, build_root, environment)
    return safe


# ---------------------------------------------------------------------------
# Parameter file generation
# ---------------------------------------------------------------------------

# Maps Bicep output key → ARM resource type key used in managed_resource_ids.
# Only keys listed here are persisted; other outputs (endpoints, names) go to outputs.json.
_BICEP_OUTPUT_TO_RESOURCE_TYPE: dict[str, str] = {
    "storageAccountId": "Microsoft.Storage/storageAccounts",
    "documentIntelligenceId": "Microsoft.CognitiveServices/accounts/document-intelligence",
    "foundryAccountId": "Microsoft.CognitiveServices/accounts/foundry",
    "foundryProjectId": "Microsoft.CognitiveServices/accounts/projects",
    "chatDeploymentId": "Microsoft.CognitiveServices/accounts/deployments/chat",
    "embeddingDeploymentId": "Microsoft.CognitiveServices/accounts/deployments/embedding",
    "searchServiceId": "Microsoft.Search/searchServices",
    "containerRegistryId": "Microsoft.ContainerRegistry/registries",
    "identityId": "Microsoft.ManagedIdentity/userAssignedIdentities",
}


def generate_bicep_parameters(manifest: InfraManifest) -> dict:
    """Build an ARM deployment parameters dict from a validated manifest.

    The returned dict has the shape::

        {
            "$schema": "https://schema.management.azure.com/schemas/.../deploymentParameters.json#",
            "contentVersion": "1.0.0.0",
            "parameters": {"paramName": {"value": ...}, ...}
        }

    Resource names are resolved deterministically using the ``names`` module
    so Bicep always deploys to the same names as the Python plan.
    """
    from .names import (
        make_storage_name,
        make_document_intelligence_name,
        make_foundry_name,
        make_search_name,
        make_container_registry_name,
        make_identity_name,
    )

    env = manifest.environment
    tags: dict = dict(manifest.azure.tags) if manifest.azure.tags else {}
    tags.setdefault("application", "fabric-kg-builder")
    tags.setdefault("environment", env)
    tags.setdefault("managed-by", "fabric-kg-builder")

    def _name(
        user_name: Optional[str],
        resource_id: Optional[str],
        generator_fn,
    ) -> str:
        if user_name:
            return user_name
        if resource_id:
            return resource_id.rstrip("/").rsplit("/", 1)[-1]
        return generator_fn(env)

    resources = manifest.resources
    storage_name = _name(
        resources.storage.name if resources.storage else None,
        resources.storage.resource_id if resources.storage else None,
        make_storage_name,
    )
    di_name = _name(
        resources.document_intelligence.name if resources.document_intelligence else None,
        resources.document_intelligence.resource_id
        if resources.document_intelligence
        else None,
        make_document_intelligence_name,
    )
    foundry_name = _name(
        resources.foundry.name if resources.foundry else None,
        resources.foundry.resource_id if resources.foundry else None,
        make_foundry_name,
    )
    search_name = _name(
        resources.search.name if resources.search else None,
        resources.search.resource_id if resources.search else None,
        make_search_name,
    )
    container_registry_name = _name(
        resources.container_registry.name
        if resources.container_registry
        else None,
        resources.container_registry.resource_id
        if resources.container_registry
        else None,
        make_container_registry_name,
    )
    identity_name = (
        manifest.identity.name
        or make_identity_name(env)
    )

    params: dict[str, dict] = {
        "environment": {"value": env},
        "location": {"value": manifest.azure.default_location},
        "storageAccountName": {"value": storage_name},
        "createStorage": {
            "value": resources.storage.mode == ResourceMode.CREATE
        },
        "documentIntelligenceName": {"value": di_name},
        "createDocumentIntelligence": {
            "value": resources.document_intelligence.mode == ResourceMode.CREATE
        },
        "foundryAccountName": {"value": foundry_name},
        "createFoundryAccount": {
            "value": resources.foundry.mode == ResourceMode.CREATE
        },
        "searchServiceName": {"value": search_name},
        "createSearch": {
            "value": resources.search.mode == ResourceMode.CREATE
        },
        "containerRegistryName": {"value": container_registry_name},
        "deployContainerRegistry": {"value": manifest.features.reference_app},
        "createContainerRegistry": {
            "value": (
                manifest.features.reference_app
                and resources.container_registry.mode == ResourceMode.CREATE
            )
        },
        "containerRegistrySku": {
            "value": resources.container_registry.sku
        },
        "identityName": {"value": identity_name},
        "deployIdentity": {
            "value": (
                manifest.features.reference_app
                and manifest.identity.mode.value == "user-assigned"
            )
        },
        "foundryProjectName": {"value": resources.foundry.project_name},
        "chatModelName": {"value": resources.foundry.models.chat.model},
        "chatDeploymentName": {
            "value": (
                resources.foundry.models.chat.deployment_name
                or resources.foundry.models.chat.model
            )
        },
        "chatModelSku": {"value": resources.foundry.models.chat.sku.value},
        "chatCapacityUnits": {
            "value": (resources.foundry.models.chat.target_tpm or 1000) // 1000
        },
        "embeddingModelName": {
            "value": resources.foundry.models.embedding.model
        },
        "embeddingDeploymentName": {
            "value": (
                resources.foundry.models.embedding.deployment_name
                or resources.foundry.models.embedding.model
            )
        },
        "embeddingDimensions": {
            "value": resources.foundry.models.embedding.dimensions or 1536
        },
        "containerName": {"value": resources.storage.container},
        "retentionDays": {"value": resources.storage.retention_days},
        "tags": {"value": tags},
    }

    # Optional overrides from manifest
    if resources.storage:
        sku = getattr(resources.storage, "sku", None)
        if sku:
            params["storageSku"] = {"value": str(sku) if hasattr(sku, "value") else sku}
    if resources.document_intelligence:
        di_sku = resources.document_intelligence.sku
        params["documentIntelligenceSku"] = {
            "value": di_sku.value
        }
    if resources.search:
        s_sku = getattr(resources.search, "sku", None)
        if s_sku:
            params["searchSku"] = {"value": str(s_sku) if hasattr(s_sku, "value") else s_sku}
        sem = getattr(resources.search, "semantic_ranker", None)
        if sem:
            params["semanticRanker"] = {"value": str(sem) if hasattr(sem, "value") else sem}

    return {
        "$schema": (
            "https://schema.management.azure.com/schemas/"
            "2019-04-01/deploymentParameters.json#"
        ),
        "contentVersion": "1.0.0.0",
        "parameters": params,
    }


def save_bicep_parameters(manifest: InfraManifest, build_root: Path) -> Path:
    """Generate and persist the ARM parameters file, returning its path."""
    params = generate_bicep_parameters(manifest)
    path = _params_path(build_root, manifest.environment)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(params, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _extract_arm_ids_from_outputs(raw_outputs: dict) -> dict[str, str]:
    """Extract ARM resource IDs from Bicep deployment outputs.

    ``az deployment group create --output json`` returns outputs as::

        {"storageAccountId": {"type": "String", "value": "/subscriptions/..."}, ...}

    Returns a ``{resource_type: arm_id}`` dict for keys in
    ``_BICEP_OUTPUT_TO_RESOURCE_TYPE``.
    """
    result: dict[str, str] = {}
    for output_key, resource_type in _BICEP_OUTPUT_TO_RESOURCE_TYPE.items():
        if output_key not in raw_outputs:
            continue
        val = raw_outputs[output_key]
        arm_id = val.get("value") if isinstance(val, dict) else val
        if arm_id and isinstance(arm_id, str) and arm_id.startswith("/"):
            result[resource_type] = arm_id
    return result


# ---------------------------------------------------------------------------
# Apply status
# ---------------------------------------------------------------------------


class ApplyStatus:
    """Structured apply operation status."""

    def __init__(
        self,
        *,
        operation_id: str,
        environment: str,
        dry_run: bool,
        items_attempted: int,
        items_succeeded: int,
        items_skipped: int,
        items_failed: int,
        errors: list[str],
        state_path: Optional[Path] = None,
        outputs_path: Optional[Path] = None,
    ) -> None:
        self.operation_id = operation_id
        self.environment = environment
        self.dry_run = dry_run
        self.items_attempted = items_attempted
        self.items_succeeded = items_succeeded
        self.items_skipped = items_skipped
        self.items_failed = items_failed
        self.errors = errors
        self.state_path = state_path
        self.outputs_path = outputs_path

    @property
    def succeeded(self) -> bool:
        return self.items_failed == 0

    def as_dict(self) -> dict:
        return {
            "operation_id": self.operation_id,
            "environment": self.environment,
            "dry_run": self.dry_run,
            "items_attempted": self.items_attempted,
            "items_succeeded": self.items_succeeded,
            "items_skipped": self.items_skipped,
            "items_failed": self.items_failed,
            "errors": self.errors,
            "state_path": str(self.state_path) if self.state_path else None,
            "outputs_path": str(self.outputs_path) if self.outputs_path else None,
        }


# ---------------------------------------------------------------------------
# Individual operation helpers
# ---------------------------------------------------------------------------


def _run_bicep_what_if(
    runner: CommandRunner,
    subscription_id: str,
    resource_group: str,
    bicep_file: str,
    parameters_file: str,
) -> str:
    """Run az deployment group what-if for dry-run mode."""
    result = runner.run([
        "az", "deployment", "group", "what-if",
        "--subscription", subscription_id,
        "--resource-group", resource_group,
        "--template-file", bicep_file,
        "--parameters", f"@{parameters_file}",
        "--output", "json",
        "--no-pretty-print",
    ])
    if not result.succeeded:
        raise CommandError(
            f"what-if failed: {result.stderr}",
            result=result,
        )
    return result.stdout


def _run_bicep_deploy(
    runner: CommandRunner,
    subscription_id: str,
    resource_group: str,
    bicep_file: str,
    parameters_file: str,
    deployment_name: str,
) -> dict:
    """Run az deployment group create and return the raw outputs dict.

    The raw outputs dict has the structure::

        {"storageAccountId": {"type": "String", "value": "/subscriptions/..."}, ...}

    Raises ``CommandError`` on non-zero exit or unparseable JSON.
    """
    result = runner.run([
        "az", "deployment", "group", "create",
        "--subscription", subscription_id,
        "--resource-group", resource_group,
        "--name", deployment_name,
        "--template-file", bicep_file,
        "--parameters", f"@{parameters_file}",
        "--output", "json",
    ])
    if not result.succeeded:
        raise CommandError(
            f"Bicep deployment '{deployment_name}' failed: {result.stderr}",
            result=result,
        )
    try:
        data = json.loads(result.stdout)
        outputs = data.get("properties", {}).get("outputs")
        if not isinstance(outputs, dict):
            raise ValueError("deployment response has no properties.outputs")
        return outputs
    except (json.JSONDecodeError, TypeError, AttributeError, ValueError) as exc:
        raise CommandError(
            f"Bicep deployment '{deployment_name}' returned an invalid "
            f"response: {exc}",
            result=result,
        ) from exc


def _resource_group_id(manifest: InfraManifest) -> str:
    name = manifest.azure.resource_group.name or f"rg-{manifest.environment}"
    return (
        f"/subscriptions/{manifest.azure.subscription_id}"
        f"/resourceGroups/{name}"
    )


def _resource_name(resource_id: str | None) -> str:
    """Return the final segment of an ARM resource ID."""
    return str(resource_id or "").rstrip("/").rsplit("/", 1)[-1]


def _azure_resource_id(
    manifest: InfraManifest,
    provider_type: str,
    name: str,
) -> str:
    return f"{_resource_group_id(manifest)}/providers/{provider_type}/{name}"


def _azure_adoptions(
    manifest: InfraManifest,
) -> dict[tuple[str, str], tuple[str, str]]:
    """Map adopted plan items to stable state keys and ARM resource IDs."""
    from .names import (
        make_container_registry_name,
        make_document_intelligence_name,
        make_foundry_name,
        make_search_name,
        make_storage_name,
    )

    resources = manifest.resources
    result: dict[tuple[str, str], tuple[str, str]] = {}
    rg = manifest.azure.resource_group
    rg_name = rg.name or f"rg-{manifest.environment}"
    if rg.mode == ResourceMode.CONNECT:
        result[("Microsoft.Resources/resourceGroups", rg_name)] = (
            "Microsoft.Resources/resourceGroups",
            _resource_group_id(manifest),
        )

    configs = [
        (
            resources.storage,
            resources.storage.name
            or _resource_name(resources.storage.resource_id)
            or make_storage_name(manifest.environment),
            "Microsoft.Storage/storageAccounts",
            "Microsoft.Storage/storageAccounts",
        ),
        (
            resources.document_intelligence,
            resources.document_intelligence.name
            or _resource_name(resources.document_intelligence.resource_id)
            or make_document_intelligence_name(manifest.environment),
            "Microsoft.CognitiveServices/accounts",
            "Microsoft.CognitiveServices/accounts/document-intelligence",
        ),
        (
            resources.foundry,
            resources.foundry.name
            or _resource_name(resources.foundry.resource_id)
            or make_foundry_name(manifest.environment),
            "Microsoft.CognitiveServices/accounts",
            "Microsoft.CognitiveServices/accounts/foundry",
        ),
        (
            resources.search,
            resources.search.name
            or _resource_name(resources.search.resource_id)
            or make_search_name(manifest.environment),
            "Microsoft.Search/searchServices",
            "Microsoft.Search/searchServices",
        ),
        (
            resources.container_registry,
            resources.container_registry.name
            or _resource_name(resources.container_registry.resource_id)
            or make_container_registry_name(manifest.environment),
            "Microsoft.ContainerRegistry/registries",
            "Microsoft.ContainerRegistry/registries",
        ),
    ]
    for config, name, provider_type, state_key in configs:
        if config.mode != ResourceMode.CONNECT:
            continue
        resource_id = config.resource_id or _azure_resource_id(
            manifest, provider_type, name
        )
        result[(provider_type, name)] = (state_key, resource_id)

    foundry = resources.foundry
    if foundry.mode == ResourceMode.CONNECT:
        foundry_name = (
            foundry.name
            or _resource_name(foundry.resource_id)
            or make_foundry_name(manifest.environment)
        )
        account_id = foundry.resource_id or _azure_resource_id(
            manifest,
            "Microsoft.CognitiveServices/accounts",
            foundry_name,
        )
        nested_resources = [
            (
                "Microsoft.CognitiveServices/accounts/projects",
                f"{foundry_name}/{foundry.project_name}",
                "Microsoft.CognitiveServices/accounts/projects",
                f"{account_id}/projects/{foundry.project_name}",
            ),
            (
                "Microsoft.CognitiveServices/accounts/deployments",
                f"{foundry_name}/{foundry.models.chat.deployment_name}",
                "Microsoft.CognitiveServices/accounts/deployments/chat",
                f"{account_id}/deployments/{foundry.models.chat.deployment_name}",
            ),
            (
                "Microsoft.CognitiveServices/accounts/deployments",
                f"{foundry_name}/{foundry.models.embedding.deployment_name}",
                "Microsoft.CognitiveServices/accounts/deployments/embedding",
                f"{account_id}/deployments/{foundry.models.embedding.deployment_name}",
            ),
        ]
        for provider_type, name, state_key, resource_id in nested_resources:
            result[(provider_type, name)] = (state_key, resource_id)
    return result


def _connected_runtime_outputs(manifest: InfraManifest) -> dict[str, str]:
    """Return non-endpoint runtime metadata.

    Service endpoints are deliberately absent.  CREATE endpoints come from
    Bicep outputs, while CONNECT endpoints come from the corresponding ARM
    resource representation.
    """
    from .names import (
        make_foundry_name,
        make_search_name,
        make_storage_name,
    )

    resources = manifest.resources
    storage_name = (
        resources.storage.name
        or _resource_name(resources.storage.resource_id)
        or make_storage_name(manifest.environment)
    )
    foundry_name = (
        resources.foundry.name
        or _resource_name(resources.foundry.resource_id)
        or make_foundry_name(manifest.environment)
    )
    search_name = (
        resources.search.name
        or _resource_name(resources.search.resource_id)
        or make_search_name(manifest.environment)
    )
    return {
        "storageAccountName": storage_name,
        "containerName": resources.storage.container,
        "searchServiceName": search_name,
        "foundryAccountName": foundry_name,
        "foundryProjectName": resources.foundry.project_name,
        "chatDeploymentName": resources.foundry.models.chat.deployment_name
        or "",
        "embeddingDeploymentName": resources.foundry.models.embedding.deployment_name
        or "",
    }


def _property_path(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = payload
    for segment in path:
        if not isinstance(value, dict) or segment not in value:
            return None
        value = value[segment]
    return value


def _format_property_path(path: tuple[str, ...]) -> str:
    result = path[0]
    for segment in path[1:]:
        if segment.replace("_", "").isalnum() and " " not in segment:
            result += f".{segment}"
        else:
            result += f"[{json.dumps(segment)}]"
    return result


def _required_https_endpoint(
    payload: dict[str, Any],
    *,
    resource_id: str,
    path: tuple[str, ...],
) -> str:
    """Read and validate an authoritative HTTPS endpoint from an ARM payload."""
    value = _property_path(payload, path)
    property_path = _format_property_path(path)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"Connected resource '{resource_id}' is missing the required HTTPS "
            f"endpoint at '{property_path}'."
        )
    try:
        return canonicalize_https_endpoint(
            value,
            authority=f"{resource_id}:{property_path}",
        )
    except ValueError as exc:
        raise ValueError(
            f"Connected resource '{resource_id}' has a malformed HTTPS endpoint "
            f"at '{property_path}': {value!r}."
        ) from exc


def canonicalize_https_endpoint(value: Any, *, authority: str) -> str:
    """Return one canonical query-free HTTPS endpoint or fail closed."""
    endpoint = canonicalize_https_authority(value)
    if endpoint is None:
        raise ValueError(
            f"{authority} must be absolute HTTPS without userinfo, query, "
            "fragment, credential material, whitespace, or control characters."
        )
    return endpoint


def _required_https_endpoint_from_paths(
    payload: dict[str, Any],
    *,
    resource_id: str,
    paths: tuple[tuple[str, ...], ...],
) -> str:
    for path in paths:
        if _property_path(payload, path) not in (None, ""):
            return _required_https_endpoint(
                payload,
                resource_id=resource_id,
                path=path,
            )
    expected = " or ".join(
        f"'{_format_property_path(path)}'" for path in paths
    )
    raise ValueError(
        f"Connected resource '{resource_id}' is missing the required HTTPS "
        f"endpoint at {expected}."
    )


def _required_registry_login_server(
    payload: dict[str, Any],
    *,
    resource_id: str,
) -> str:
    value = _property_path(payload, ("properties", "loginServer"))
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"Connected resource '{resource_id}' is missing the required "
            "registry host at 'properties.loginServer'."
        )
    host = value.strip()
    parsed = urlsplit(f"//{host}")
    if (
        parsed.hostname != host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or any(character.isspace() for character in host)
    ):
        raise ValueError(
            f"Connected resource '{resource_id}' has a malformed registry host "
            "at 'properties.loginServer'."
        )
    return host


def _arm_runtime_outputs(
    *,
    state_key: str,
    resource_id: str,
    payload: dict[str, Any],
) -> dict[str, str]:
    """Extract connected-resource runtime outputs from ARM-reported properties."""
    if state_key == "Microsoft.Storage/storageAccounts":
        return {
            "blobEndpoint": _required_https_endpoint(
                payload,
                resource_id=resource_id,
                path=("properties", "primaryEndpoints", "blob"),
            )
        }
    if state_key == "Microsoft.CognitiveServices/accounts/document-intelligence":
        return {
            "documentIntelligenceEndpoint": _required_https_endpoint(
                payload,
                resource_id=resource_id,
                path=("properties", "endpoint"),
            )
        }
    if state_key == "Microsoft.CognitiveServices/accounts/foundry":
        return {
            "foundryEndpoint": _required_https_endpoint(
                payload,
                resource_id=resource_id,
                path=("properties", "endpoints", "AI Foundry API"),
            ),
            "foundryOpenAIEndpoint": _required_https_endpoint(
                payload,
                resource_id=resource_id,
                path=(
                    "properties",
                    "endpoints",
                    "OpenAI Language Model Instance API",
                ),
            ),
        }
    if state_key == "Microsoft.CognitiveServices/accounts/projects":
        return {
            "foundryProjectEndpoint": _required_https_endpoint_from_paths(
                payload,
                resource_id=resource_id,
                paths=(
                    ("properties", "endpoint"),
                    ("properties", "endpoints", "AI Foundry API"),
                ),
            )
        }
    if state_key == "Microsoft.Search/searchServices":
        return {
            "searchEndpoint": _required_https_endpoint(
                payload,
                resource_id=resource_id,
                path=("properties", "endpoint"),
            )
        }
    if state_key == "Microsoft.ContainerRegistry/registries":
        return {
            "containerRegistryLoginServer": _required_registry_login_server(
                payload,
                resource_id=resource_id,
            )
        }
    return {}


def _arm_authority_record(
    *,
    state_key: str,
    resource_id: str,
    resource_type: str,
    payload: dict[str, Any],
    runtime_outputs: dict[str, str],
) -> dict[str, Any]:
    """Select only mutation-relevant, non-secret ARM resource properties."""
    record: dict[str, Any] = {
        "resource_id": resource_id,
        "state_key": state_key,
        "resource_type": resource_type,
        "reported_type": payload.get("type"),
        "kind": payload.get("kind"),
        "location": payload.get("location"),
        "runtime_outputs": runtime_outputs,
    }
    sku = payload.get("sku")
    if isinstance(sku, dict):
        record["sku"] = {
            key: sku.get(key)
            for key in ("name", "tier", "capacity")
            if sku.get(key) is not None
        }
    if state_key.startswith(
        "Microsoft.CognitiveServices/accounts/deployments/"
    ):
        model = _property_path(payload, ("properties", "model"))
        if not isinstance(model, dict) or not all(
            isinstance(model.get(key), str) and model.get(key).strip()
            for key in ("name", "version")
        ):
            raise ValueError(
                f"Connected resource '{resource_id}' is missing authoritative "
                "model name/version at 'properties.model'."
            )
        record["model"] = {
            key: model.get(key)
            for key in ("format", "name", "version")
            if model.get(key) is not None
        }
    return record


def _read_connected_azure_resource(
    runner: CommandRunner,
    manifest: InfraManifest,
    *,
    resource_type: str,
    resource_name: str,
) -> tuple[str, str, dict[str, str], dict[str, Any]]:
    adoption = _azure_adoptions(manifest).get(
        (resource_type, resource_name)
    )
    if adoption is None:
        raise ValueError(
            f"No connect configuration found for {resource_type}/"
            f"{resource_name}."
        )
    state_key, resource_id = adoption
    if resource_type == "Microsoft.Resources/resourceGroups":
        result = runner.run([
            "az", "group", "show",
            "--subscription", manifest.azure.subscription_id,
            "--name", resource_name,
            "--output", "json",
        ])
    else:
        result = runner.run([
            "az", "resource", "show",
            "--subscription", manifest.azure.subscription_id,
            "--ids", resource_id,
            "--output", "json",
        ])
    if not result.succeeded:
        raise CommandError(
            f"Cannot connect {resource_name}: {result.stderr}",
            result=result,
        )
    try:
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise ValueError("response is not a JSON object")
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CommandError(
            f"Connected resource '{resource_id}' returned invalid JSON: {exc}",
            result=result,
        ) from exc
    actual_type = payload.get("type")
    if (
        isinstance(actual_type, str)
        and actual_type.strip()
        and actual_type.strip().lower() != resource_type.lower()
    ):
        raise ValueError(
            f"Connected resource '{resource_id}' type mismatch: expected "
            f"'{resource_type}', ARM returned '{actual_type}'."
        )
    outputs = (
        {}
        if resource_type == "Microsoft.Resources/resourceGroups"
        else _arm_runtime_outputs(
            state_key=state_key,
            resource_id=resource_id,
            payload=payload,
        )
    )
    return state_key, resource_id, outputs, payload


def resolve_connected_arm_authority(
    manifest: InfraManifest,
    runner: CommandRunner,
) -> dict[str, Any]:
    """Read every manifest-connected Azure resource without persisting state."""
    outputs: dict[str, str] = {}
    resources: list[dict[str, Any]] = []
    for resource_type, resource_name in sorted(_azure_adoptions(manifest)):
        state_key, resource_id, resource_outputs, payload = (
            _read_connected_azure_resource(
                runner,
                manifest,
                resource_type=resource_type,
                resource_name=resource_name,
            )
        )
        outputs.update(resource_outputs)
        resources.append(_arm_authority_record(
            state_key=state_key,
            resource_id=resource_id,
            resource_type=resource_type,
            payload=payload,
            runtime_outputs=resource_outputs,
        ))
    return {
        "resources": resources,
        "runtime_outputs": outputs,
    }


def _validate_and_record_azure_adoption(
    runner: CommandRunner,
    manifest: InfraManifest,
    item: Any,
    state: InfraState,
    build_root: Path,
    connected_outputs: dict[str, str] | None = None,
) -> InfraState:
    state_key, resource_id, resource_outputs, _ = (
        _read_connected_azure_resource(
            runner,
            manifest,
            resource_type=item.resource_type,
            resource_name=item.resource_name,
        )
    )
    if connected_outputs is not None:
        connected_outputs.update(resource_outputs)
    adopted = dict(state.adopted_resource_ids)
    adopted[state_key] = resource_id
    state = state.model_copy(update={"adopted_resource_ids": adopted})
    save_state(state, build_root)
    return state


def _managed_arm_ids_from_outputs(
    raw_outputs: dict,
    manifest: InfraManifest,
) -> dict[str, str]:
    managed = _extract_arm_ids_from_outputs(raw_outputs)
    if manifest.resources.storage.mode == ResourceMode.CONNECT:
        managed.pop("Microsoft.Storage/storageAccounts", None)
    if manifest.resources.document_intelligence.mode == ResourceMode.CONNECT:
        managed.pop(
            "Microsoft.CognitiveServices/accounts/document-intelligence", None
        )
    if manifest.resources.foundry.mode == ResourceMode.CONNECT:
        managed.pop("Microsoft.CognitiveServices/accounts/foundry", None)
        managed.pop("Microsoft.CognitiveServices/accounts/projects", None)
        managed.pop(
            "Microsoft.CognitiveServices/accounts/deployments/chat", None
        )
        managed.pop(
            "Microsoft.CognitiveServices/accounts/deployments/embedding", None
        )
    if manifest.resources.search.mode == ResourceMode.CONNECT:
        managed.pop("Microsoft.Search/searchServices", None)
    if manifest.resources.container_registry.mode == ResourceMode.CONNECT:
        managed.pop("Microsoft.ContainerRegistry/registries", None)
    return managed


def _fabric_name(config: Any, fallback: str) -> str:
    return config.display_name or config.name or fallback


def _record_fabric_item(
    *,
    state: InfraState,
    build_root: Path,
    resource_type: str,
    item_id: str,
    mode: ResourceMode,
    output_key: str,
    outputs: dict[str, Any],
) -> InfraState:
    if not item_id:
        raise ValueError(f"{resource_type} returned an empty item ID.")
    field_name = (
        "managed_resource_ids"
        if mode == ResourceMode.CREATE
        else "adopted_resource_ids"
    )
    ids = dict(getattr(state, field_name))
    ids[resource_type] = item_id
    outputs[output_key] = item_id
    state_outputs = dict(state.outputs)
    state_outputs.update({output_key: item_id})
    state = state.model_copy(
        update={field_name: ids, "outputs": state_outputs}
    )
    save_state(state, build_root)
    return state


def _apply_fabric_items(
    manifest: InfraManifest,
    items: list[Any],
    transport: Any,
    state: InfraState,
    build_root: Path,
    outputs: dict[str, Any],
) -> tuple[InfraState, int, int, list[str]]:
    from .fabric_client import (
        FabricGraphModelClient,
        FabricLakehouseClient,
        FabricOntologyClient,
        FabricWorkspaceClient,
    )

    succeeded = 0
    failed = 0
    errors: list[str] = []
    workspace_id = (
        state.managed_resource_ids.get("Fabric/Workspace")
        or state.adopted_resource_ids.get("Fabric/Workspace")
    )

    for item in items:
        try:
            if item.resource_type == "Fabric/Workspace":
                config = manifest.fabric.workspace
                result = FabricWorkspaceClient(
                    transport
                ).create_or_connect_workspace(
                    _fabric_name(config, "kg-workspace"),
                    manifest.fabric.capacity_id,
                    mode=config.mode.value,
                    item_id=config.item_id,
                )
                workspace_id = str(result.get("id") or "")
                state = _record_fabric_item(
                    state=state,
                    build_root=build_root,
                    resource_type=item.resource_type,
                    item_id=workspace_id,
                    mode=config.mode,
                    output_key="fabricWorkspaceId",
                    outputs=outputs,
                )
            else:
                if not workspace_id:
                    raise ValueError(
                        f"{item.resource_type} requires a persisted Fabric "
                        "workspace ID."
                    )
                if item.resource_type == "Fabric/Lakehouse":
                    config = manifest.fabric.lakehouse
                    result = FabricLakehouseClient(
                        transport, workspace_id
                    ).create_or_connect_lakehouse(
                        _fabric_name(config, "kg"),
                        enable_schemas=config.enable_schemas,
                        mode=config.mode.value,
                        item_id=config.item_id,
                    )
                    output_key = "fabricLakehouseId"
                elif item.resource_type == "Fabric/Ontology":
                    config = manifest.fabric.ontology
                    client = FabricOntologyClient(transport, workspace_id)
                    if config.mode == ResourceMode.CONNECT:
                        result = client.connect(
                            item_id=config.item_id,
                            display_name=_fabric_name(config, "KG Ontology"),
                        )
                    else:
                        capability = client.probe_capability()
                        if not capability.preview_available:
                            raise ValueError(" ".join(capability.warnings))
                        if not capability.ok:
                            raise ValueError(" ".join(capability.errors))
                        result = client.create_ontology(
                            _fabric_name(config, "KG Ontology")
                        )
                    output_key = "fabricOntologyId"
                elif item.resource_type == "Fabric/GraphModel":
                    config = manifest.fabric.graph_model
                    client = FabricGraphModelClient(transport, workspace_id)
                    if config.mode == ResourceMode.CONNECT:
                        result = client.connect(
                            item_id=config.item_id,
                            display_name=_fabric_name(config, "KG Graph"),
                        )
                    else:
                        discovery = client.discover()
                        if not discovery.automated_create_available:
                            outputs["fabricGraphModelState"] = (
                                "guided-connect-required"
                            )
                            outputs["fabricGraphModelGuidance"] = (
                                discovery.guidance or ""
                            )
                            raise ValueError(
                                (discovery.guidance or "")
                                + " "
                                + " ".join(discovery.warnings)
                            )
                        result = client.create_graph_model(
                            _fabric_name(config, "KG Graph")
                        )
                    output_key = "fabricGraphModelId"
                else:
                    raise ValueError(
                        f"Unsupported Fabric resource type: {item.resource_type}"
                    )
                state = _record_fabric_item(
                    state=state,
                    build_root=build_root,
                    resource_type=item.resource_type,
                    item_id=str(result.get("id") or ""),
                    mode=config.mode,
                    output_key=output_key,
                    outputs=outputs,
                )
            succeeded += 1
        except Exception as exc:
            failed += 1
            errors.append(f"{item.resource_name}: {exc}")

    return state, succeeded, failed, errors


# ---------------------------------------------------------------------------
# Main apply entry point
# ---------------------------------------------------------------------------


def apply_plan(
    manifest: InfraManifest,
    plan: InfraPlan,
    runner: CommandRunner,
    *,
    dry_run: bool = False,
    build_root: Path = Path("build"),
    infra_dir: Path = Path("infra"),
    fabric_transport: Any | None = None,
) -> ApplyStatus:
    """Apply the infrastructure plan.

    Correctness guarantees:
    - ``managed_resource_ids`` is updated ONLY when Bicep deployment succeeds
      and actual ARM IDs are present in deployment outputs.
    - Failed apply writes ``last_operation_status: failed`` without touching
      managed resources, so a retry re-attempts all CREATE items.
    - Parameters are generated from the manifest (not a hand-maintained file).
    - Dry-run runs ``az deployment group what-if`` (Azure resources only) and
      returns without persisting state.

    SPEC-006 §4.1 / INF-013.
    """
    operation_id = str(uuid.uuid4())
    errors: list[str] = []
    skipped = 0
    succeeded = 0
    failed = 0
    attempted = 0

    state = load_state(build_root, manifest.environment)
    state = state.model_copy(update={
        "last_operation": "apply",
        "last_operation_id": operation_id,
        "last_operation_status": "in_progress",
    })
    flat_outputs: dict[str, Any] = load_outputs(
        build_root, manifest.environment
    )
    flat_outputs.update(_connected_runtime_outputs(manifest))
    connected_outputs: dict[str, str] = {}

    # --- Generate Bicep parameters from manifest (always, even in dry-run) ---
    params_path = save_bicep_parameters(manifest, build_root)
    bicep_main = infra_dir / "main.bicep"

    if not dry_run:
        save_state(state, build_root)

    # --- Classify plan items ---
    no_op_items = [i for i in plan.items if i.action == PlanAction.NO_OP]
    skipped = len(no_op_items)
    actionable_items = [i for i in plan.items if i.action != PlanAction.NO_OP]
    attempted = len(actionable_items)
    adoption_map = _azure_adoptions(manifest)
    azure_connected_items = [
        item
        for item in plan.items
        if (item.resource_type, item.resource_name) in adoption_map
    ]
    planned_connected_keys = {
        (item.resource_type, item.resource_name)
        for item in azure_connected_items
    }
    for resource_type, resource_name in sorted(adoption_map):
        state_key, _ = adoption_map[(resource_type, resource_name)]
        if (
            state_key == "Microsoft.ContainerRegistry/registries"
            and (resource_type, resource_name) not in planned_connected_keys
        ):
            azure_connected_items.append(PlanItem(
                resource_type=resource_type,
                resource_name=resource_name,
                action=PlanAction.NO_OP,
            ))
    resource_group_items = [
        i for i in actionable_items
        if i.resource_type == "Microsoft.Resources/resourceGroups"
        and (i.resource_type, i.resource_name) not in adoption_map
    ]
    azure_creates = [
        i for i in plan.items
        if not i.resource_type.startswith("Fabric/")
        and i.resource_type != "Microsoft.Resources/resourceGroups"
        and i.action in (PlanAction.CREATE, PlanAction.UPDATE)
    ]
    fabric_items = [
        i for i in plan.items
        if i.resource_type.startswith("Fabric/")
        and i.action in (PlanAction.CREATE, PlanAction.ADOPT)
    ]

    if dry_run:
        # Group-level what-if requires an existing resource group. When the
        # resource group itself is planned for creation, the plan is the only
        # non-mutating preview available.
        rg_is_create = any(
            item.action == PlanAction.CREATE for item in resource_group_items
        )
        if azure_creates and bicep_main.exists() and not rg_is_create:
            try:
                _run_bicep_what_if(
                    runner,
                    manifest.azure.subscription_id,
                    manifest.azure.resource_group.name
                    or f"rg-{manifest.environment}",
                    str(bicep_main),
                    str(params_path),
                )
            except CommandError as exc:
                errors.append(f"Bicep what-if: {exc}")
                failed = len(azure_creates)
        skipped += len(actionable_items) - failed
        return ApplyStatus(
            operation_id=operation_id,
            environment=manifest.environment,
            dry_run=True,
            items_attempted=attempted,
            items_succeeded=0,
            items_skipped=skipped,
            items_failed=failed,
            errors=errors,
        )

    # --- Refresh every connected Azure resource, including NO_OP resources ---
    connected_endpoint_keys = {
        "Microsoft.Storage/storageAccounts": {"blobEndpoint"},
        "Microsoft.CognitiveServices/accounts/document-intelligence": {
            "documentIntelligenceEndpoint"
        },
        "Microsoft.CognitiveServices/accounts/foundry": {
            "foundryEndpoint",
            "foundryOpenAIEndpoint",
        },
        "Microsoft.CognitiveServices/accounts/projects": {
            "foundryProjectEndpoint"
        },
        "Microsoft.Search/searchServices": {"searchEndpoint"},
        "Microsoft.ContainerRegistry/registries": {
            "containerRegistryLoginServer"
        },
    }
    for item in azure_connected_items:
        state_key, _ = adoption_map[
            (item.resource_type, item.resource_name)
        ]
        for output_key in connected_endpoint_keys.get(state_key, set()):
            flat_outputs.pop(output_key, None)
        try:
            state = _validate_and_record_azure_adoption(
                runner,
                manifest,
                item,
                state,
                build_root,
                connected_outputs,
            )
            if item.action != PlanAction.NO_OP:
                succeeded += 1
        except Exception as exc:
            failed += 1
            errors.append(f"{item.resource_name}: {exc}")

    # --- Ensure or create the resource group first ---
    for item in resource_group_items:
        try:
            tags = [
                f"{key}={value}"
                for key, value in manifest.azure.tags.items()
            ]
            command = [
                "az", "group", "create",
                "--subscription", manifest.azure.subscription_id,
                "--name", item.resource_name,
                "--location", item.location
                or manifest.azure.default_location,
                "--output", "json",
            ]
            if tags:
                command.extend(["--tags", *tags])
            result = runner.run(command)
            if not result.succeeded:
                raise CommandError(
                    f"Resource group create failed: {result.stderr}",
                    result=result,
                )
            managed = dict(state.managed_resource_ids)
            managed["Microsoft.Resources/resourceGroups"] = (
                _resource_group_id(manifest)
            )
            state = state.model_copy(
                update={"managed_resource_ids": managed}
            )
            save_state(state, build_root)
            succeeded += 1
        except Exception as exc:
            failed += 1
            errors.append(f"{item.resource_name}: {exc}")

    # --- Full Bicep deployment for Azure resources ---
    raw_outputs: dict = {}
    deploy_succeeded = False

    if azure_creates and not errors:
        if not bicep_main.exists():
            errors.append(
                f"Bicep template not found: '{bicep_main}'. "
                "Ensure the infra/ directory is present."
            )
            failed += len(azure_creates)
        else:
            deployment_name = f"kg-{manifest.environment}-{operation_id[:8]}"
            try:
                raw_outputs = _run_bicep_deploy(
                    runner,
                    manifest.azure.subscription_id,
                    manifest.azure.resource_group.name or f"rg-{manifest.environment}",
                    str(bicep_main),
                    str(params_path),
                    deployment_name,
                )
                deploy_succeeded = True
                succeeded += len(azure_creates)
            except CommandError as exc:
                errors.append(f"Bicep deployment: {exc}")
                failed += len(azure_creates)
    elif azure_creates and errors:
        failed += len(azure_creates)
        errors.append(
            "Azure create operations were not started because a prerequisite "
            "resource connect/create operation failed."
        )

    # --- Persist successful ARM outputs before Fabric operations ---
    if deploy_succeeded:
        arm_ids = _managed_arm_ids_from_outputs(raw_outputs, manifest)
        if arm_ids:
            managed = dict(state.managed_resource_ids)
            managed.update(arm_ids)
            state = state.model_copy(update={"managed_resource_ids": managed})
            save_state(state, build_root)
        flat_outputs.update({
            key: (value.get("value") if isinstance(value, dict) else value)
            for key, value in raw_outputs.items()
        })
    # A mixed CREATE/CONNECT Bicep deployment can emit values for existing
    # declarations.  ARM reads remain authoritative for every connected
    # resource, so apply them after deployment outputs.
    flat_outputs.update(connected_outputs)

    # --- Execute Fabric create/connect operations sequentially ---
    if fabric_items and not errors:
        if fabric_transport is None:
            from .runner import RealCommandRunner

            if isinstance(runner, RealCommandRunner):
                from .fabric_client import (
                    DefaultAzureCredentialFabricTransport,
                )

                fabric_transport = DefaultAzureCredentialFabricTransport()
        if fabric_transport is None:
            failed += len(fabric_items)
            errors.append(
                "Fabric apply requires an authenticated HttpTransport. "
                "The CLI supplies DefaultAzureCredential automatically; tests "
                "must inject FakeHttpTransport."
            )
        else:
            (
                state,
                fabric_succeeded,
                fabric_failed,
                fabric_errors,
            ) = _apply_fabric_items(
                manifest,
                fabric_items,
                fabric_transport,
                state,
                build_root,
                flat_outputs,
            )
            succeeded += fabric_succeeded
            failed += fabric_failed
            errors.extend(fabric_errors)
    elif fabric_items and errors:
        failed += len(fabric_items)
        errors.append(
            "Fabric operations were not started because Azure provisioning "
            "did not complete successfully."
        )

    state_path: Optional[Path] = None
    outputs_path: Optional[Path] = None

    # Fabric item IDs are discovered after ARM/Bicep output collection. Merge
    # durable state outputs so runtime sync receives Azure and Fabric values in
    # one apply receipt.
    flat_outputs.update(state.outputs)
    if flat_outputs:
        try:
            outputs_path = save_outputs(
                flat_outputs, build_root, manifest.environment
            )
        except ValueError as exc:
            failed += 1
            errors.append(f"Infrastructure outputs: {exc}")
            _outputs_path(build_root, manifest.environment).unlink(
                missing_ok=True
            )

    # Always persist state (even on failure — records partial progress for retry).
    final_status = "succeeded" if not errors else "failed"
    state = state.model_copy(update={"last_operation_status": final_status})
    state_path = save_state(state, build_root)

    return ApplyStatus(
        operation_id=operation_id,
        environment=manifest.environment,
        dry_run=dry_run,
        items_attempted=attempted,
        items_succeeded=succeeded,
        items_skipped=skipped,
        items_failed=failed,
        errors=errors,
        state_path=state_path,
        outputs_path=outputs_path,
    )


# ---------------------------------------------------------------------------
# Status command
# ---------------------------------------------------------------------------


def get_apply_status(
    build_root: Path,
    environment: str,
) -> dict:
    """Return a summary dict of the last apply operation for *environment*."""
    state = load_state(build_root, environment)
    outputs = load_outputs(build_root, environment)
    return {
        "environment": environment,
        "last_operation": state.last_operation,
        "last_operation_id": state.last_operation_id,
        "last_operation_status": state.last_operation_status,
        "managed_resources": dict(state.managed_resource_ids),
        "adopted_resources": dict(state.adopted_resource_ids),
        "output_count": len(outputs),
        "outputs_path": str(_outputs_path(build_root, environment)),
    }
