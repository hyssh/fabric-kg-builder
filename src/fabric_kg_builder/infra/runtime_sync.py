"""Synchronize non-secret infrastructure outputs into runtime configuration."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from .schema import InfraManifest
from .schema import ResourceMode
from .apply import canonicalize_https_endpoint


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(temp_name, path)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected an object in {path}.")
    return payload


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a mapping in {path}.")
    return payload


def _set_if_present(target: dict[str, Any], key: str, value: Any) -> None:
    if value not in (None, ""):
        target[key] = value


def _resource_name(resource_id: str | None) -> str:
    return str(resource_id or "").rstrip("/").rsplit("/", 1)[-1]


def sync_runtime_configuration(
    *,
    environment: str,
    manifest: InfraManifest,
    outputs: dict[str, Any],
    fabric_environment_path: Path,
    agent_metadata_path: Path,
) -> dict[str, str]:
    """Write non-secret Azure/Fabric outputs to both runtime configuration files."""
    outputs = dict(outputs)
    for key, value in list(outputs.items()):
        if key.lower().endswith("endpoint") and value not in (None, ""):
            outputs[key] = canonicalize_https_endpoint(
                value,
                authority=key,
            )
    required_connected_outputs: list[tuple[str, tuple[str, ...]]] = []
    if manifest.resources.storage.mode == ResourceMode.CONNECT:
        required_connected_outputs.append(("storage", ("blobEndpoint",)))
    if manifest.resources.document_intelligence.mode == ResourceMode.CONNECT:
        required_connected_outputs.append(
            ("document_intelligence", ("documentIntelligenceEndpoint",))
        )
    if manifest.resources.foundry.mode == ResourceMode.CONNECT:
        required_connected_outputs.append(
            (
                "foundry",
                (
                    "foundryEndpoint",
                    "foundryOpenAIEndpoint",
                    "foundryProjectEndpoint",
                ),
            )
        )
    if manifest.resources.search.mode == ResourceMode.CONNECT:
        required_connected_outputs.append(("search", ("searchEndpoint",)))
    if manifest.resources.container_registry.mode == ResourceMode.CONNECT:
        required_connected_outputs.append(
            ("container_registry", ("containerRegistryLoginServer",))
        )
    elif manifest.features.reference_app:
        required_connected_outputs.append(
            ("container_registry", ("containerRegistryLoginServer",))
        )
    for resource_name, keys in required_connected_outputs:
        missing = [
            key
            for key in keys
            if not isinstance(outputs.get(key), str)
            or not str(outputs.get(key)).strip()
        ]
        if missing:
            raise ValueError(
                f"Connected {resource_name} runtime outputs are missing "
                f"authoritative ARM values: {', '.join(missing)}."
            )

    fabric_config = _load_json(fabric_environment_path)
    fabric = fabric_config.setdefault("fabric", {})
    search = fabric_config.setdefault("ai_search", {})
    blob = fabric_config.setdefault("blob_storage", {})
    document_intelligence = fabric_config.setdefault("document_intelligence", {})
    foundry = fabric_config.setdefault("foundry", {})
    identity = fabric_config.setdefault("identity", {})
    container_registry = fabric_config.setdefault("container_registry", {})

    _set_if_present(fabric, "workspace_id", outputs.get("fabricWorkspaceId"))
    _set_if_present(fabric, "lakehouse_item_id", outputs.get("fabricLakehouseId"))
    _set_if_present(fabric, "ontology_item_id", outputs.get("fabricOntologyId"))
    _set_if_present(fabric, "graph_model_item_id", outputs.get("fabricGraphModelId"))
    _set_if_present(
        fabric,
        "graph_model_display_name",
        manifest.fabric.graph_model.display_name
        or manifest.fabric.graph_model.name,
    )
    fabric.setdefault("schema_name", "dbo")

    _set_if_present(search, "endpoint", outputs.get("searchEndpoint"))
    _set_if_present(search, "service_name", outputs.get("searchServiceName"))
    search.setdefault("index_prefix", f"kg-{environment}-")
    search.setdefault("index_chunks", "kg-chunks")
    search.setdefault("index_document_elements", "kg-document-elements")
    search.setdefault("index_visual_assets", "kg-visual-assets")

    _set_if_present(blob, "account_name", outputs.get("storageAccountName"))
    _set_if_present(blob, "container", outputs.get("containerName"))
    _set_if_present(blob, "endpoint", outputs.get("blobEndpoint"))
    _set_if_present(
        document_intelligence,
        "endpoint",
        outputs.get("documentIntelligenceEndpoint"),
    )
    _set_if_present(
        foundry,
        "endpoint",
        outputs.get("foundryProjectEndpoint") or outputs.get("foundryEndpoint"),
    )
    _set_if_present(foundry, "account_endpoint", outputs.get("foundryEndpoint"))
    _set_if_present(
        foundry,
        "openai_endpoint",
        outputs.get("foundryOpenAIEndpoint"),
    )
    _set_if_present(
        foundry,
        "project_endpoint",
        outputs.get("foundryProjectEndpoint"),
    )
    _set_if_present(foundry, "project_name", outputs.get("foundryProjectName"))
    _set_if_present(foundry, "project", outputs.get("foundryProjectName"))
    _set_if_present(
        foundry,
        "chat_deployment",
        outputs.get("chatDeploymentName"),
    )
    _set_if_present(
        foundry,
        "embedding_deployment",
        outputs.get("embeddingDeploymentName"),
    )
    _set_if_present(identity, "resource_id", outputs.get("identityId"))
    _set_if_present(identity, "client_id", outputs.get("identityClientId"))
    _set_if_present(identity, "principal_id", outputs.get("identityPrincipalId"))
    _set_if_present(
        container_registry,
        "login_server",
        outputs.get("containerRegistryLoginServer"),
    )
    azure = fabric_config.setdefault("azure", {})
    _set_if_present(azure, "subscription_id", manifest.azure.subscription_id)
    _set_if_present(
        azure,
        "resource_group",
        manifest.azure.resource_group.name,
    )

    _atomic_write_text(
        fabric_environment_path,
        json.dumps(fabric_config, indent=2, sort_keys=True) + "\n",
    )

    metadata = _load_yaml(agent_metadata_path)
    metadata.setdefault("schemaVersion", "1.0")
    metadata.setdefault("defaultEnvironment", environment)
    metadata.setdefault("agentName", "fabric-kg-agent")
    metadata.setdefault("model", {"deploymentName": "gpt-4.1"})
    metadata.setdefault(
        "promptAgent",
        {
            "systemPromptVersion": "v1.3",
            "instructionsVariant": "search-ontology-mixed",
            "temperature": 0.0,
            "maxTokens": 2048,
            "topP": 1.0,
        },
    )
    environments = metadata.setdefault("environments", {})
    env_config = environments.setdefault(environment, {})
    _set_if_present(env_config, "projectEndpoint", outputs.get("foundryProjectEndpoint"))
    _set_if_present(
        env_config,
        "modelDeployment",
        outputs.get("chatDeploymentName"),
    )
    _set_if_present(env_config, "subscriptionId", manifest.azure.subscription_id)
    _set_if_present(env_config, "resourceGroup", manifest.azure.resource_group.name)

    acr = env_config.setdefault("acr", {})
    login_server = outputs.get("containerRegistryLoginServer")
    if login_server:
        acr["loginServer"] = login_server
    else:
        acr.pop("loginServer", None)
    acr.setdefault("repository", "fabric-kg")

    deployments = env_config.setdefault("deployments", {})
    _set_if_present(deployments, "chat", outputs.get("chatDeploymentName"))
    _set_if_present(
        deployments,
        "embedding",
        outputs.get("embeddingDeploymentName"),
    )

    connections = env_config.setdefault("connections", {})
    _set_if_present(
        connections,
        "search",
        outputs.get("foundrySearchConnectionId"),
    )

    knowledge = env_config.setdefault("knowledge", {})
    index_name = (
        f"{search.get('index_prefix', '')}"
        f"{search.get('index_chunks', 'kg-chunks')}"
    )
    _set_if_present(knowledge, "searchIndexName", index_name)
    knowledge.setdefault("queryType", "semantic")
    knowledge.setdefault("topK", 5)

    _set_if_present(
        env_config,
        "foundryAccountName",
        outputs.get("foundryAccountName")
        or _resource_name(outputs.get("foundryAccountId")),
    )
    _set_if_present(
        env_config,
        "foundryProjectName",
        outputs.get("foundryProjectName"),
    )

    _atomic_write_text(
        agent_metadata_path,
        yaml.safe_dump(metadata, sort_keys=False, allow_unicode=False),
    )
    return {
        "fabric_environment": str(fabric_environment_path),
        "agent_metadata": str(agent_metadata_path),
    }
