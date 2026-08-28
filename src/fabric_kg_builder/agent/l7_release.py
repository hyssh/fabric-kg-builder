"""Strict, file-driven L7 release planning and execution for 0.2.4."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from fabric_kg_builder.contracts.base import canonical_sha256


RELEASE_VERSION = "0.2.4"
_FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
_MANAGEMENT_SCOPE = "https://management.azure.com/.default"
_SEARCH_SCOPE = "https://search.azure.com/.default"
_FABRIC_BASE = "https://api.fabric.microsoft.com/v1"
_FABRIC_TYPES = {
    "DataAgent": "dataAgents",
    "GraphModel": "graphModels",
    "Ontology": "ontologies",
    "SemanticModel": "semanticModels",
}


class L7ReleaseError(RuntimeError):
    """Raised when an L7 release gate fails closed."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactBinding(_StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class FabricDefinitionTarget(_StrictModel):
    name: str = Field(min_length=1)
    item_id: str = Field(min_length=1)
    item_type: Literal["DataAgent", "GraphModel", "Ontology", "SemanticModel"]
    artifact: ArtifactBinding


class SearchTarget(_StrictModel):
    endpoint: str = Field(pattern=r"^https://[^/]+/?$")
    index_name: str = Field(pattern=r"^fabric-kg-024-[a-z0-9-]+$")
    index_schema: ArtifactBinding
    documents: ArtifactBinding
    knowledge_source_name: str = Field(pattern=r"^fabric-kg-024-[a-z0-9-]+$")
    knowledge_base_name: str = Field(pattern=r"^fabric-kg-024-[a-z0-9-]+$")
    api_version: str = "2025-11-01-preview"
    foundry_role_assignment_id: str = ""
    foundry_role_principal_id: str = ""
    foundry_role_definition_id: str = ""

    @field_validator("endpoint")
    @classmethod
    def _trusted_search_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").casefold()
        suffixes = (
            ".search.windows.net",
            ".search.azure.us",
            ".search.azure.cn",
        )
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or not any(host.endswith(suffix) for suffix in suffixes)
            or host.count(".") < 3
        ):
            raise ValueError("endpoint must be a trusted Azure AI Search service URL")
        return f"https://{host}"

    @model_validator(mode="after")
    def _complete_role_evidence(self) -> "SearchTarget":
        values = (
            self.foundry_role_assignment_id,
            self.foundry_role_principal_id,
            self.foundry_role_definition_id,
        )
        if any(values) and not all(values):
            raise ValueError(
                "Foundry Search role evidence requires assignment, principal, "
                "and role-definition stable IDs"
            )
        return self


class FoundryTarget(_StrictModel):
    account_name: str = Field(min_length=1)
    project_name: str = Field(min_length=1)
    search_connection_name: str = Field(pattern=r"^fabric-kg-024-[a-z0-9-]+$")
    fabric_connection_name: str = Field(pattern=r"^fabric-kg-024-[a-z0-9-]+$")
    data_agent_id: str = Field(min_length=1)
    deploy_builtin_agent: bool = False


class L7ReleaseConfig(_StrictModel):
    release: Literal["0.2.4"] = RELEASE_VERSION
    tenant_id: str = Field(min_length=1)
    subscription_id: str = Field(min_length=1)
    resource_group: str = Field(min_length=1)
    expected_principal_id: str = Field(min_length=1)
    fabric_workspace_id: str = Field(min_length=1)
    authority_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    l5a_definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    l5b_definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    l6_definition: ArtifactBinding
    fabric_definitions: tuple[FabricDefinitionTarget, ...]
    search: SearchTarget
    foundry: FoundryTarget
    plan_ttl_seconds: int = Field(default=900, ge=60, le=3600)

    @model_validator(mode="after")
    def _unique_targets(self) -> "L7ReleaseConfig":
        names = [item.name for item in self.fabric_definitions]
        ids = [item.item_id.casefold() for item in self.fabric_definitions]
        if len(names) != len(set(names)) or len(ids) != len(set(ids)):
            raise ValueError("Fabric target names and IDs must be unique")
        if self.foundry.data_agent_id.casefold() not in {
            item.item_id.casefold()
            for item in self.fabric_definitions
            if item.item_type == "DataAgent"
        }:
            raise ValueError("Foundry data_agent_id must bind a configured DataAgent")
        return self

    @property
    def config_hash(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class ObservedIdentity(_StrictModel):
    tenant_id: str
    principal_id: str


class ResourceReadback(_StrictModel):
    resource_id: str
    exists: bool
    resource_type: str
    name: str
    etag: str = ""
    definition_hash: str = ""
    token: str = ""
    properties_hash: str = ""


class L7Observation(_StrictModel):
    identity: ObservedIdentity
    resources: tuple[ResourceReadback, ...]
    capabilities: Mapping[str, bool]
    observed_at: datetime


class RollbackStep(_StrictModel):
    action: Literal["delete-created", "restore-definition", "none"]
    resource_id: str
    expected_etag: str = ""


class DeploymentAction(_StrictModel):
    order: int = Field(ge=1)
    component: str
    resource_id: str
    resource_type: str
    name: str
    action: Literal["create", "update", "noop", "deferred", "no-go"]
    desired_hash: str
    observed_hash: str = ""
    observed_etag: str = ""
    readback_expectation: Mapping[str, Any]
    rollback: RollbackStep
    reason: str = ""


class L7DeploymentPlan(_StrictModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    release: Literal["0.2.4"] = RELEASE_VERSION
    config_hash: str
    tenant_id: str
    principal_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_hash: str
    l5a_definition_hash: str
    l5b_definition_hash: str
    l6_definition_hash: str
    l6_hosting: Literal["generated-local-deferred"] = "generated-local-deferred"
    created_at: datetime
    expires_at: datetime
    observation_hash: str
    actions: tuple[DeploymentAction, ...]
    plan_hash: str = ""

    @model_validator(mode="after")
    def _hash_matches(self) -> "L7DeploymentPlan":
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"plan_hash"})
        )
        if self.plan_hash != expected:
            raise ValueError("L7 plan hash mismatch")
        return self

    @classmethod
    def seal(cls, **values: Any) -> "L7DeploymentPlan":
        provisional = cls.model_construct(**values, plan_hash="")
        values["plan_hash"] = canonical_sha256(
            provisional.model_dump(mode="json", exclude={"plan_hash"})
        )
        return cls.model_validate(values)


class JournalEntry(_StrictModel):
    sequence: int
    phase: Literal["before", "after", "rollback-before", "rollback-after"]
    component: str
    action: str
    resource_id: str
    status: str
    observed_etag: str = ""


class L7DeploymentReceipt(_StrictModel):
    release: Literal["0.2.4"] = RELEASE_VERSION
    plan_hash: str
    status: Literal["succeeded", "rolled-back", "failed"]
    completed_at: datetime
    journal: tuple[JournalEntry, ...]
    deferred_components: tuple[str, ...]
    receipt_hash: str = ""

    @model_validator(mode="after")
    def _hash_matches(self) -> "L7DeploymentReceipt":
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"receipt_hash"})
        )
        if self.receipt_hash != expected:
            raise ValueError("L7 receipt hash mismatch")
        return self

    @classmethod
    def seal(cls, **values: Any) -> "L7DeploymentReceipt":
        provisional = cls.model_construct(**values, receipt_hash="")
        values["receipt_hash"] = canonical_sha256(
            provisional.model_dump(mode="json", exclude={"receipt_hash"})
        )
        return cls.model_validate(values)


class L7Backend(Protocol):
    def observe(self, config: L7ReleaseConfig) -> L7Observation: ...

    def apply(
        self, config: L7ReleaseConfig, action: DeploymentAction
    ) -> ResourceReadback: ...

    def rollback(
        self, config: L7ReleaseConfig, action: DeploymentAction
    ) -> ResourceReadback: ...


def _canonical_json_hash_bytes(payload: bytes, path: Path) -> str:
    try:
        value = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise L7ReleaseError(f"invalid JSON artifact: {path}") from exc
    return canonical_sha256(value)


def _validated_artifact_bytes(binding: ArtifactBinding, base: Path) -> bytes:
    path = (base / binding.path).resolve()
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise L7ReleaseError(f"cannot read bound artifact: {binding.path}") from exc
    if hashlib.sha256(payload).hexdigest() != binding.sha256:
        raise L7ReleaseError(f"artifact byte hash mismatch: {binding.path}")
    if _canonical_json_hash_bytes(payload, path) != binding.canonical_hash:
        raise L7ReleaseError(f"artifact canonical hash mismatch: {binding.path}")
    return payload


def _validate_artifact(binding: ArtifactBinding, base: Path) -> Path:
    _validated_artifact_bytes(binding, base)
    return (base / binding.path).resolve()


def _artifact_value(binding: ArtifactBinding, base: Path) -> Any:
    path = (base / binding.path).resolve()
    try:
        return json.loads(_validated_artifact_bytes(binding, base))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise L7ReleaseError(f"invalid JSON artifact: {binding.path}") from exc


def _definition_value(binding: ArtifactBinding, base: Path) -> dict[str, Any]:
    value = _artifact_value(binding, base)
    if not isinstance(value, dict):
        raise L7ReleaseError("Fabric definition artifact must be a JSON object")
    nested = value.get("definition")
    return nested if isinstance(nested, dict) else value


def _document_values(binding: ArtifactBinding, base: Path) -> list[dict[str, Any]]:
    value = _artifact_value(binding, base)
    if isinstance(value, dict):
        value = value.get("documents", value.get("value"))
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise L7ReleaseError("Search documents artifact must contain JSON objects")
    return [dict(item) for item in value]


def load_l7_config(path: Path) -> L7ReleaseConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return L7ReleaseConfig.model_validate(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise L7ReleaseError(f"invalid L7 configuration: {path}") from exc


def load_observation(path: Path) -> L7Observation:
    try:
        return L7Observation.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise L7ReleaseError(f"invalid L7 observation: {path}") from exc


def _write_immutable(path: Path, payload: bytes) -> None:
    if path.exists():
        existing = path.read_bytes()
        if existing != payload:
            raise L7ReleaseError(f"refusing to replace immutable file: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_bytes(payload)
    os.chmod(temp, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    temp.replace(path)


def persist_plan(path: Path, plan: L7DeploymentPlan) -> None:
    _write_immutable(
        path,
        (json.dumps(plan.model_dump(mode="json"), indent=2, sort_keys=True) + "\n").encode(),
    )


def load_plan(path: Path) -> L7DeploymentPlan:
    try:
        return L7DeploymentPlan.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise L7ReleaseError(f"invalid L7 plan: {path}") from exc


def persist_receipt(path: Path, receipt: L7DeploymentReceipt) -> None:
    _write_immutable(
        path,
        (
            json.dumps(receipt.model_dump(mode="json"), indent=2, sort_keys=True)
            + "\n"
        ).encode(),
    )


def _readback_map(observation: L7Observation) -> dict[str, ResourceReadback]:
    result: dict[str, ResourceReadback] = {}
    for item in observation.resources:
        key = item.resource_id.casefold()
        if key in result:
            raise L7ReleaseError("observation contains duplicate stable resource IDs")
        result[key] = item
    return result


def _require_identity(config: L7ReleaseConfig, identity: ObservedIdentity) -> None:
    if identity.tenant_id.casefold() != config.tenant_id.casefold():
        raise L7ReleaseError("observed tenant differs from configured tenant")
    if identity.principal_id.casefold() != config.expected_principal_id.casefold():
        raise L7ReleaseError("observed identity differs from configured principal")


class L7Planner:
    def __init__(self, backend: L7Backend) -> None:
        self.backend = backend

    def build(
        self, config: L7ReleaseConfig, *, config_path: Path
    ) -> L7DeploymentPlan:
        base = config_path.resolve().parent
        _validate_artifact(config.l6_definition, base)
        fabric_desired_hashes = {
            item.item_id: canonical_sha256(
                _definition_value(item.artifact, base)
            )
            for item in config.fabric_definitions
        }
        observation = self.backend.observe(config)
        _require_identity(config, observation.identity)
        reads = _readback_map(observation)
        actions: list[DeploymentAction] = []
        order = 1

        for target in config.fabric_definitions:
            desired_definition_hash = fabric_desired_hashes[target.item_id]
            resource_id = (
                f"/workspaces/{config.fabric_workspace_id}/"
                f"{_FABRIC_TYPES[target.item_type]}/{target.item_id}"
            )
            observed = reads.get(resource_id.casefold())
            capable = observation.capabilities.get(
                f"fabric.{target.item_type}.definition", False
            )
            if not capable:
                mutation = "no-go"
                reason = "exact definition mutation and getDefinition readback unavailable"
            elif observed is None or not observed.exists:
                mutation = "no-go"
                reason = "configured stable Fabric item is absent; implicit name create forbidden"
            elif observed.resource_type != target.item_type or observed.name != target.name:
                mutation = "no-go"
                reason = "Fabric stable ID/type/name readback mismatch"
            elif not observed.etag:
                mutation = "no-go"
                reason = "Fabric definition mutation lacks conditional ETag authority"
            elif observed.definition_hash == desired_definition_hash:
                mutation = "noop"
                reason = ""
            else:
                mutation = "update"
                reason = ""
            actions.append(
                DeploymentAction(
                    order=order,
                    component=f"fabric-{target.item_type.casefold()}",
                    resource_id=resource_id,
                    resource_type=target.item_type,
                    name=target.name,
                    action=mutation,
                    desired_hash=desired_definition_hash,
                    observed_hash=observed.definition_hash if observed else "",
                    observed_etag=observed.etag if observed else "",
                    readback_expectation={
                        "stable_id": target.item_id,
                        "type": target.item_type,
                        "name": target.name,
                        "definition_hash": desired_definition_hash,
                    },
                    rollback=RollbackStep(
                        action="restore-definition" if mutation == "update" else "none",
                        resource_id=resource_id,
                        expected_etag=observed.etag if observed else "",
                    ),
                    reason=reason,
                )
            )
            order += 1

        index_schema = _artifact_value(config.search.index_schema, base)
        if not isinstance(index_schema, dict):
            raise L7ReleaseError("Search index schema must be a JSON object")
        index_schema = dict(index_schema)
        index_schema["name"] = config.search.index_name
        documents = _document_values(config.search.documents, base)
        index_desired_hash = canonical_sha256(
            {
                "schema": index_schema,
                "documents": sorted(documents, key=canonical_sha256),
            }
        )
        search_resources = (
            (
                "search-index",
                f"{config.search.endpoint.rstrip('/')}/indexes/{config.search.index_name}",
                config.search.index_name,
                index_desired_hash,
                "search.index",
            ),
            (
                "search-knowledge-source",
                f"{config.search.endpoint.rstrip('/')}/knowledgesources/"
                f"{config.search.knowledge_source_name}",
                config.search.knowledge_source_name,
                canonical_sha256(
                    {
                        "index": config.search.index_name,
                        "name": config.search.knowledge_source_name,
                    }
                ),
                "search.knowledge-source",
            ),
            (
                "search-knowledge-base",
                f"{config.search.endpoint.rstrip('/')}/knowledgebases/"
                f"{config.search.knowledge_base_name}",
                config.search.knowledge_base_name,
                canonical_sha256(
                    {
                        "sources": [config.search.knowledge_source_name],
                        "name": config.search.knowledge_base_name,
                    }
                ),
                "search.knowledge-base",
            ),
        )
        for component, resource_id, name, desired_hash, capability in search_resources:
            observed = reads.get(resource_id.casefold())
            if not observation.capabilities.get(capability, False):
                action = "no-go"
                reason = "required Search capability or managed-identity role unavailable"
            elif observed is not None and observed.exists:
                action = "no-go"
                reason = "release-owned Search name collision; adoption is forbidden"
            else:
                action = "create"
                reason = ""
            actions.append(
                DeploymentAction(
                    order=order,
                    component=component,
                    resource_id=resource_id,
                    resource_type=component,
                    name=name,
                    action=action,
                    desired_hash=desired_hash,
                    readback_expectation={
                        "name": name,
                        "hash": desired_hash,
                        "exact_schema_docs_count_acl": True,
                    },
                    rollback=RollbackStep(
                        action="delete-created" if action == "create" else "none",
                        resource_id=resource_id,
                    ),
                    reason=reason,
                )
            )
            order += 1

        project_id = (
            f"/subscriptions/{config.subscription_id}/resourceGroups/"
            f"{config.resource_group}/providers/Microsoft.CognitiveServices/accounts/"
            f"{config.foundry.account_name}/projects/{config.foundry.project_name}"
        )
        for kind, name, desired_hash in (
            (
                "foundry-search-connection",
                config.foundry.search_connection_name,
                canonical_sha256(
                    {"category": "CognitiveSearch", "target": config.search.endpoint}
                ),
            ),
            (
                "foundry-fabric-connection",
                config.foundry.fabric_connection_name,
                canonical_sha256(
                    {
                        "category": "CustomKeys",
                        "workspace_id": config.fabric_workspace_id,
                        "data_agent_id": config.foundry.data_agent_id,
                    }
                ),
            ),
        ):
            resource_id = f"{project_id}/connections/{name}"
            observed = reads.get(resource_id.casefold())
            if not observation.capabilities.get("foundry.project-connections", False):
                action = "no-go"
                reason = "exact connection create/readback/conditional delete unavailable"
            elif observed is not None and observed.exists:
                action = "no-go"
                reason = "release-owned connection collision; redacted keys are not adoptable"
            else:
                action = "create"
                reason = ""
            actions.append(
                DeploymentAction(
                    order=order,
                    component=kind,
                    resource_id=resource_id,
                    resource_type="ProjectConnection",
                    name=name,
                    action=action,
                    desired_hash=desired_hash,
                    readback_expectation={
                        "stable_id": resource_id,
                        "name": name,
                        "exact_category_target_audience_binding": True,
                    },
                    rollback=RollbackStep(
                        action="delete-created" if action == "create" else "none",
                        resource_id=resource_id,
                    ),
                    reason=reason,
                )
            )
            order += 1

        agent_action: Literal["deferred", "no-go"]
        if config.foundry.deploy_builtin_agent:
            agent_action = "no-go"
            reason = (
                "built-in Foundry agent exact definition rollback is not available "
                "through the current supported adapter"
            )
        else:
            agent_action = "deferred"
            reason = (
                "canonical L6 five-tool RemoteTool hosting is deferred; built-in "
                "Search and Fabric Data Agent connections are prepared only"
            )
        actions.append(
            DeploymentAction(
                order=order,
                component="foundry-built-in-agent",
                resource_id=f"{project_id}/agents",
                resource_type="FoundryAgent",
                name="fabric-kg-024-agent",
                action=agent_action,
                desired_hash=config.l6_definition.canonical_hash,
                readback_expectation={"deferred_scope_is_explicit": True},
                rollback=RollbackStep(action="none", resource_id=f"{project_id}/agents"),
                reason=reason,
            )
        )
        now = datetime.now(timezone.utc)
        observation_hash = canonical_sha256(
            observation.model_dump(mode="json", exclude={"observed_at"})
        )
        return L7DeploymentPlan.seal(
            config_hash=config.config_hash,
            tenant_id=config.tenant_id,
            principal_hash=canonical_sha256(
                {"principal_id": observation.identity.principal_id}
            ),
            authority_hash=config.authority_hash,
            l5a_definition_hash=config.l5a_definition_hash,
            l5b_definition_hash=config.l5b_definition_hash,
            l6_definition_hash=config.l6_definition.canonical_hash,
            created_at=now,
            expires_at=now + timedelta(seconds=config.plan_ttl_seconds),
            observation_hash=observation_hash,
            actions=tuple(actions),
        )


class ObservationBackend:
    """GET-free local backend for deterministic dry-run smoke tests only."""

    def __init__(self, observation: L7Observation) -> None:
        self.observation = observation

    def observe(self, config: L7ReleaseConfig) -> L7Observation:
        return self.observation

    def apply(self, config: L7ReleaseConfig, action: DeploymentAction) -> ResourceReadback:
        raise L7ReleaseError("local observation backend cannot mutate resources")

    def rollback(self, config: L7ReleaseConfig, action: DeploymentAction) -> ResourceReadback:
        raise L7ReleaseError("local observation backend cannot rollback resources")


def _decode_identity(token: str) -> ObservedIdentity:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return ObservedIdentity(
            tenant_id=str(claims.get("tid") or ""),
            principal_id=str(claims.get("oid") or claims.get("sub") or ""),
        )
    except (IndexError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise L7ReleaseError("Azure access token omitted required identity claims") from exc


class AzureL7Backend:
    """Live readback backend. Mutations are limited to proven-safe adapters."""

    def __init__(self, artifact_base: Path) -> None:
        from azure.identity import DefaultAzureCredential

        self.credential = DefaultAzureCredential()
        self.artifact_base = artifact_base.resolve()
        self._rollback_definitions: dict[str, dict[str, Any]] = {}
        self._created_etags: dict[str, str] = {}
        self._mutation_confirmed: set[str] = set()

    def _artifact_json(self, binding: ArtifactBinding) -> Any:
        return _artifact_value(binding, self.artifact_base)

    @staticmethod
    def _definition(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise L7ReleaseError("Fabric definition artifact must be a JSON object")
        nested = value.get("definition")
        if isinstance(nested, dict):
            return nested
        return value

    def _token(self, scope: str) -> str:
        try:
            token = self.credential.get_token(scope).token
        except Exception as exc:
            raise L7ReleaseError("Azure identity token acquisition failed") from exc
        if not token:
            raise L7ReleaseError("Azure credential returned an empty token")
        return token

    @staticmethod
    def _request(
        method: str,
        url: str,
        *,
        token: str,
        body: dict[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        import requests

        merged = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        merged.update(dict(headers or {}))
        try:
            return requests.request(
                method, url, headers=merged, json=body, timeout=60
            )
        except requests.RequestException as exc:
            raise L7ReleaseError(f"{method} transport failed for declared resource") from exc

    @staticmethod
    def _json(response: Any, operation: str) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise L7ReleaseError(f"{operation} returned non-JSON") from exc
        if not isinstance(body, dict):
            raise L7ReleaseError(f"{operation} returned invalid JSON shape")
        return body

    def _fabric_definition(
        self, config: L7ReleaseConfig, target: FabricDefinitionTarget
    ) -> ResourceReadback:
        collection = _FABRIC_TYPES[target.item_type]
        item_url = (
            f"{_FABRIC_BASE}/workspaces/{config.fabric_workspace_id}/items/"
            f"{target.item_id}"
        )
        token = self._token(_FABRIC_SCOPE)
        item_response = self._request("GET", item_url, token=token)
        resource_id = (
            f"/workspaces/{config.fabric_workspace_id}/{collection}/{target.item_id}"
        )
        if item_response.status_code == 404:
            return ResourceReadback(
                resource_id=resource_id,
                exists=False,
                resource_type=target.item_type,
                name=target.name,
            )
        if item_response.status_code != 200:
            raise L7ReleaseError(
                f"Fabric item readback failed with HTTP {item_response.status_code}"
            )
        item = self._json(item_response, "Fabric item readback")
        definition_url = (
            f"{_FABRIC_BASE}/workspaces/{config.fabric_workspace_id}/{collection}/"
            f"{target.item_id}/getDefinition"
        )
        response = self._request("POST", definition_url, token=token, body={})
        if response.status_code not in (200, 202):
            raise L7ReleaseError(
                f"{target.item_type} getDefinition capability unavailable "
                f"(HTTP {response.status_code})"
            )
        if response.status_code == 202:
            location = str(response.headers.get("Location") or "")
            if not location:
                raise L7ReleaseError("Fabric getDefinition returned 202 without Location")
            body = self._wait_lro(location, token)
            definition = body.get("definition")
            if not isinstance(definition, dict):
                result = self._request(
                    "GET", f"{location.rstrip('/')}/result", token=token
                )
                if result.status_code != 200:
                    raise L7ReleaseError("Fabric getDefinition result readback failed")
                definition = self._json(
                    result, "Fabric getDefinition result"
                ).get("definition")
        else:
            definition = self._json(
                response, "Fabric getDefinition"
            ).get("definition")
        if not isinstance(definition, dict):
            raise L7ReleaseError("Fabric getDefinition omitted definition")
        self._rollback_definitions.setdefault(resource_id.casefold(), definition)
        return ResourceReadback(
            resource_id=resource_id,
            exists=True,
            resource_type=str(item.get("type") or ""),
            name=str(item.get("displayName") or ""),
            etag=str(response.headers.get("ETag") or item_response.headers.get("ETag") or ""),
            definition_hash=canonical_sha256(definition),
        )

    def observe(self, config: L7ReleaseConfig) -> L7Observation:
        identity = _decode_identity(self._token(_MANAGEMENT_SCOPE))
        resources: list[ResourceReadback] = []
        capabilities: dict[str, bool] = {
            "search.index": True,
            "search.knowledge-source": False,
            "search.knowledge-base": False,
            "foundry.project-connections": True,
        }
        if config.search.foundry_role_assignment_id:
            role_url = (
                f"https://management.azure.com/subscriptions/{config.subscription_id}"
                f"/resourceGroups/{config.resource_group}/providers/Microsoft.Authorization"
                f"/roleAssignments/{config.search.foundry_role_assignment_id}"
                "?api-version=2022-04-01"
            )
            role_response = self._request(
                "GET", role_url, token=self._token(_MANAGEMENT_SCOPE)
            )
            if role_response.status_code == 200:
                role_body = self._json(role_response, "Search role assignment")
                properties = (
                    role_body.get("properties")
                    if isinstance(role_body.get("properties"), dict)
                    else {}
                )
                role_present = (
                    str(properties.get("principalId") or "").casefold()
                    == config.search.foundry_role_principal_id.casefold()
                    and str(properties.get("roleDefinitionId") or "").casefold()
                    == config.search.foundry_role_definition_id.casefold()
                )
            else:
                role_present = False
            capabilities["search.knowledge-source"] = role_present
            capabilities["search.knowledge-base"] = role_present
        for target in config.fabric_definitions:
            try:
                resources.append(self._fabric_definition(config, target))
                capabilities[f"fabric.{target.item_type}.definition"] = True
            except L7ReleaseError:
                capabilities[f"fabric.{target.item_type}.definition"] = False

        search_token = self._token(_SEARCH_SCOPE)
        for kind, name in (
            ("indexes", config.search.index_name),
            ("knowledgesources", config.search.knowledge_source_name),
            ("knowledgebases", config.search.knowledge_base_name),
        ):
            url = (
                f"{config.search.endpoint.rstrip('/')}/{kind}/{name}"
                f"?api-version={config.search.api_version}"
            )
            response = self._request("GET", url, token=search_token)
            if response.status_code not in (200, 404):
                raise L7ReleaseError(
                    f"Search {kind} readback failed with HTTP {response.status_code}"
                )
            resources.append(
                ResourceReadback(
                    resource_id=url.split("?")[0],
                    exists=response.status_code == 200,
                    resource_type=f"search-{kind}",
                    name=name,
                    etag=str(response.headers.get("ETag") or ""),
                    properties_hash=(
                        canonical_sha256(self._json(response, f"Search {kind}"))
                        if response.status_code == 200
                        else ""
                    ),
                )
            )

        from fabric_kg_builder.agent.project_connections import (
            FoundryProjectConnectionClient,
        )

        client = FoundryProjectConnectionClient(
            subscription_id=config.subscription_id,
            resource_group=config.resource_group,
            account_name=config.foundry.account_name,
            project_name=config.foundry.project_name,
            tenant_id=config.tenant_id,
            credential=self.credential,
        )
        for name in (
            config.foundry.search_connection_name,
            config.foundry.fabric_connection_name,
        ):
            try:
                item = client.get(name)
            except Exception as exc:
                raise L7ReleaseError(
                    "Foundry project connection readback failed"
                ) from exc
            resources.append(
                ResourceReadback(
                    resource_id=client.connection_id(name),
                    exists=item is not None,
                    resource_type="ProjectConnection",
                    name=name,
                    etag=item.etag if item else "",
                    properties_hash=item.properties_hash if item else "",
                )
            )
        return L7Observation(
            identity=identity,
            resources=tuple(resources),
            capabilities=capabilities,
            observed_at=datetime.now(timezone.utc),
        )

    def _target_for_action(
        self, config: L7ReleaseConfig, action: DeploymentAction
    ) -> FabricDefinitionTarget:
        target = next(
            (
                item
                for item in config.fabric_definitions
                if item.item_id.casefold()
                == action.resource_id.casefold().rsplit("/", 1)[-1]
            ),
            None,
        )
        if target is None:
            raise L7ReleaseError("Fabric action has no exact configured target")
        return target

    def _search_url(self, config: L7ReleaseConfig, path: str) -> str:
        return (
            f"{config.search.endpoint.rstrip('/')}/{path}"
            f"?api-version={config.search.api_version}"
        )

    def _search_create_put(
        self,
        config: L7ReleaseConfig,
        action: DeploymentAction,
        path: str,
        body: dict[str, Any],
        token: str,
    ) -> tuple[Any, dict[str, Any]]:
        marker = "fabric-kg-024-attempt:" + secrets.token_hex(32)
        desired = {**body, "description": marker}
        try:
            response = self._request(
                "PUT",
                self._search_url(config, path),
                token=token,
                body=desired,
                headers={"If-None-Match": "*"},
            )
        except L7ReleaseError:
            observed = self._request(
                "GET", self._search_url(config, path), token=token
            )
            if observed.status_code == 200:
                observed_body = self._json(
                    observed, f"{action.component} uncertain readback"
                )
                etag = str(observed.headers.get("ETag") or "")
                if observed_body.get("description") == marker and etag:
                    cleanup = self._request(
                        "DELETE",
                        self._search_url(config, path),
                        token=token,
                        headers={"If-Match": etag},
                    )
                    if cleanup.status_code not in (200, 202, 204, 404):
                        raise L7ReleaseError(
                            f"{action.component} uncertain cleanup failed"
                        )
            raise
        if response.status_code in (200, 201):
            key = action.resource_id.casefold()
            self._mutation_confirmed.add(key)
            etag = str(response.headers.get("ETag") or "")
            if etag:
                self._created_etags[key] = etag
        return response, desired

    def _search_create(
        self, config: L7ReleaseConfig, action: DeploymentAction
    ) -> ResourceReadback:
        token = self._token(_SEARCH_SCOPE)
        if action.component == "search-index":
            schema = self._artifact_json(config.search.index_schema)
            if not isinstance(schema, dict):
                raise L7ReleaseError("Search index schema must be a JSON object")
            schema = dict(schema)
            schema["name"] = config.search.index_name
            path = f"indexes/{config.search.index_name}"
            response, schema = self._search_create_put(
                config, action, path, schema, token
            )
            if response.status_code not in (200, 201):
                raise L7ReleaseError(
                    f"Search index create failed with HTTP {response.status_code}"
                )
            documents = _document_values(
                config.search.documents, self.artifact_base
            )
            if documents:
                upload = self._request(
                    "POST",
                    self._search_url(
                        config, f"indexes/{config.search.index_name}/docs/index"
                    ),
                    token=token,
                    body={
                        "value": [
                            {"@search.action": "upload", **item}
                            for item in documents
                            if isinstance(item, dict)
                        ]
                    },
                )
                if upload.status_code not in (200, 201):
                    raise L7ReleaseError(
                        f"Search document upload failed with HTTP {upload.status_code}"
                    )
            count = self._request(
                "GET",
                self._search_url(
                    config, f"indexes/{config.search.index_name}/docs/$count"
                ),
                token=token,
            )
            if count.status_code != 200:
                raise L7ReleaseError(
                    f"Search count readback failed with HTTP {count.status_code}"
                )
            try:
                observed_count = int(count.text)
            except (TypeError, ValueError) as exc:
                raise L7ReleaseError("Search count readback was invalid") from exc
            if observed_count != len(documents):
                raise L7ReleaseError("Search document count readback mismatch")
        elif action.component == "search-knowledge-source":
            path = f"knowledgesources/{config.search.knowledge_source_name}"
            body = {
                "name": config.search.knowledge_source_name,
                "kind": "searchIndex",
                "searchIndexParameters": {
                    "searchIndexName": config.search.index_name,
                    "sourceDataFields": [],
                    "searchFields": [],
                },
            }
            response, body = self._search_create_put(
                config, action, path, body, token
            )
        else:
            path = f"knowledgebases/{config.search.knowledge_base_name}"
            body = {
                "name": config.search.knowledge_base_name,
                "knowledgeSources": [
                    {"name": config.search.knowledge_source_name}
                ],
            }
            response, body = self._search_create_put(
                config, action, path, body, token
            )
        if response.status_code not in (200, 201):
            raise L7ReleaseError(
                f"{action.component} create failed with HTTP {response.status_code}"
            )
        etag = str(response.headers.get("ETag") or "")
        if etag:
            self._created_etags[action.resource_id.casefold()] = etag
        readback = self._request(
            "GET", self._search_url(config, path), token=token
        )
        if readback.status_code != 200:
            raise L7ReleaseError(
                f"{action.component} readback failed with HTTP {readback.status_code}"
            )
        readback_etag = str(readback.headers.get("ETag") or etag)
        if readback_etag:
            self._created_etags[action.resource_id.casefold()] = readback_etag
        body = self._json(readback, f"{action.component} readback")
        if str(body.get("name") or "") != action.name:
            raise L7ReleaseError(f"{action.component} exact-name readback mismatch")
        if action.component == "search-index":
            if {key: body.get(key) for key in schema} != schema:
                raise L7ReleaseError("Search index schema readback mismatch")
            search_response = self._request(
                "POST",
                self._search_url(
                    config, f"indexes/{config.search.index_name}/docs/search"
                ),
                token=token,
                body={"search": "*", "top": len(documents) + 1},
            )
            if search_response.status_code != 200:
                raise L7ReleaseError("Search document readback failed")
            observed_documents = self._json(
                search_response, "Search document readback"
            ).get("value")
            if not isinstance(observed_documents, list):
                raise L7ReleaseError("Search document readback omitted values")
            clean_documents = [
                {
                    key: value
                    for key, value in item.items()
                    if not key.startswith("@search.")
                }
                for item in observed_documents
                if isinstance(item, dict)
            ]
            if sorted(clean_documents, key=canonical_sha256) != sorted(
                documents, key=canonical_sha256
            ):
                raise L7ReleaseError("Search document hash/ACL readback mismatch")
        elif action.component == "search-knowledge-source":
            parameters = (
                body.get("searchIndexParameters")
                if isinstance(body.get("searchIndexParameters"), dict)
                else {}
            )
            if (
                body.get("kind") != "searchIndex"
                or parameters.get("searchIndexName") != config.search.index_name
            ):
                raise L7ReleaseError("Search knowledge source readback mismatch")
        elif [
            item.get("name")
            for item in body.get("knowledgeSources", [])
            if isinstance(item, dict)
        ] != [config.search.knowledge_source_name]:
            raise L7ReleaseError("Search knowledge base readback mismatch")
        return ResourceReadback(
            resource_id=action.resource_id,
            exists=True,
            resource_type=action.resource_type,
            name=action.name,
            etag=readback_etag,
            properties_hash=action.desired_hash,
        )

    def _connection_client(self, config: L7ReleaseConfig) -> Any:
        from fabric_kg_builder.agent.project_connections import (
            FoundryProjectConnectionClient,
        )

        return FoundryProjectConnectionClient(
            subscription_id=config.subscription_id,
            resource_group=config.resource_group,
            account_name=config.foundry.account_name,
            project_name=config.foundry.project_name,
            tenant_id=config.tenant_id,
            credential=self.credential,
        )

    def apply(self, config: L7ReleaseConfig, action: DeploymentAction) -> ResourceReadback:
        if action.component.startswith("fabric-"):
            target = self._target_for_action(config, action)
            definition = self._definition(self._artifact_json(target.artifact))
            collection = _FABRIC_TYPES[target.item_type]
            url = (
                f"{_FABRIC_BASE}/workspaces/{config.fabric_workspace_id}/"
                f"{collection}/{target.item_id}/updateDefinition"
            )
            response = self._request(
                "POST",
                url,
                token=self._token(_FABRIC_SCOPE),
                body={"definition": definition},
                headers={"If-Match": action.observed_etag}
                if action.observed_etag
                else None,
            )
            if response.status_code not in (200, 202):
                raise L7ReleaseError(
                    f"{target.item_type} updateDefinition failed with HTTP "
                    f"{response.status_code}"
                )
            self._mutation_confirmed.add(action.resource_id.casefold())
            response_etag = str(response.headers.get("ETag") or "")
            if response_etag:
                self._created_etags[action.resource_id.casefold()] = response_etag
            if response.status_code == 202:
                location = str(response.headers.get("Location") or "")
                if not location:
                    raise L7ReleaseError(
                        f"{target.item_type} update returned 202 without Location"
                    )
                self._wait_lro(location, self._token(_FABRIC_SCOPE))
            observed = self._fabric_definition(config, target)
            desired = canonical_sha256(definition)
            if observed.definition_hash != desired:
                raise L7ReleaseError(
                    f"{target.item_type} getDefinition hash mismatch"
                )
            self._created_etags[action.resource_id.casefold()] = observed.etag
            return observed
        if action.component.startswith("search-"):
            return self._search_create(config, action)
        if action.component == "foundry-search-connection":
            try:
                item = self._connection_client(config).upsert_search(
                    name=config.foundry.search_connection_name,
                    endpoint=config.search.endpoint,
                    create_only=True,
                )
            except Exception as exc:
                raise L7ReleaseError(
                    "Foundry Search connection mutation failed"
                ) from exc
        elif action.component == "foundry-fabric-connection":
            try:
                item = self._connection_client(config).upsert_fabric_data_agent(
                    name=config.foundry.fabric_connection_name,
                    workspace_id=config.fabric_workspace_id,
                    data_agent_id=config.foundry.data_agent_id,
                    create_only=True,
                )
            except Exception as exc:
                raise L7ReleaseError(
                    "Foundry Fabric connection mutation failed"
                ) from exc
        else:
            raise L7ReleaseError(
                f"live mutation adapter for {action.component} is unavailable"
            )
        if not item.etag:
            raise L7ReleaseError("Foundry connection create omitted ETag")
        self._mutation_confirmed.add(action.resource_id.casefold())
        self._created_etags[action.resource_id.casefold()] = item.etag
        return ResourceReadback(
            resource_id=action.resource_id,
            exists=True,
            resource_type="ProjectConnection",
            name=action.name,
            etag=item.etag,
            properties_hash=action.desired_hash,
        )

    def rollback(self, config: L7ReleaseConfig, action: DeploymentAction) -> ResourceReadback:
        if action.rollback.action == "restore-definition":
            target = self._target_for_action(config, action)
            previous = self._rollback_definitions.get(action.resource_id.casefold())
            if previous is None:
                raise L7ReleaseError("Fabric rollback definition is unavailable")
            current = self._fabric_definition(config, target)
            if current.definition_hash == action.observed_hash:
                return current
            if current.definition_hash != action.desired_hash:
                raise L7ReleaseError(
                    "Fabric rollback refused concurrent definition drift"
                )
            if not current.etag:
                raise L7ReleaseError(
                    "Fabric rollback lacks conditional ETag authority"
                )
            collection = _FABRIC_TYPES[target.item_type]
            response = self._request(
                "POST",
                (
                    f"{_FABRIC_BASE}/workspaces/{config.fabric_workspace_id}/"
                    f"{collection}/{target.item_id}/updateDefinition"
                ),
                token=self._token(_FABRIC_SCOPE),
                body={"definition": previous},
                headers={"If-Match": current.etag},
            )
            if response.status_code not in (200, 202):
                raise L7ReleaseError("Fabric definition rollback failed")
            if response.status_code == 202:
                location = str(response.headers.get("Location") or "")
                if not location:
                    raise L7ReleaseError(
                        "Fabric rollback returned 202 without Location"
                    )
                self._wait_lro(location, self._token(_FABRIC_SCOPE))
            restored = self._fabric_definition(config, target)
            if restored.definition_hash != action.observed_hash:
                raise L7ReleaseError(
                    "Fabric rollback definition hash readback mismatch"
                )
            return restored
        if action.rollback.action != "delete-created":
            raise L7ReleaseError("unsupported rollback action")
        if action.resource_id.casefold() not in self._mutation_confirmed:
            raise L7ReleaseError(
                "mutation outcome was not confirmed; unsafe unconditional "
                "rollback was refused"
            )
        etag = self._created_etags.get(action.resource_id.casefold(), "")
        if not etag:
            raise L7ReleaseError("conditional rollback requires a confirmed ETag")
        if action.component.startswith("search-"):
            segment = {
                "search-index": f"indexes/{config.search.index_name}",
                "search-knowledge-source": (
                    f"knowledgesources/{config.search.knowledge_source_name}"
                ),
                "search-knowledge-base": (
                    f"knowledgebases/{config.search.knowledge_base_name}"
                ),
            }[action.component]
            response = self._request(
                "DELETE",
                self._search_url(config, segment),
                token=self._token(_SEARCH_SCOPE),
                headers={"If-Match": etag} if etag else None,
            )
            if response.status_code not in (200, 202, 204, 404):
                raise L7ReleaseError(f"{action.component} rollback delete failed")
            if response.status_code == 202:
                location = str(response.headers.get("Location") or "")
                if not location:
                    raise L7ReleaseError(
                        f"{action.component} delete returned 202 without Location"
                    )
                self._wait_lro(location, self._token(_SEARCH_SCOPE))
            check = self._request(
                "GET",
                self._search_url(config, segment),
                token=self._token(_SEARCH_SCOPE),
            )
            if check.status_code != 404:
                raise L7ReleaseError(
                    f"{action.component} rollback deletion readback mismatch"
                )
        else:
            try:
                self._connection_client(config).delete_created(
                    action.name, expected_etag=etag
                )
            except Exception as exc:
                raise L7ReleaseError(
                    "Foundry connection rollback failed"
                ) from exc
        return ResourceReadback(
            resource_id=action.resource_id,
            exists=False,
            resource_type=action.resource_type,
            name=action.name,
        )

    def _wait_lro(self, location: str, token: str) -> dict[str, Any]:
        import time

        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            response = self._request("GET", location, token=token)
            if response.status_code >= 400:
                raise L7ReleaseError(
                    f"Fabric operation failed with HTTP {response.status_code}"
                )
            body = self._json(response, "Fabric operation")
            status = str(body.get("status") or "").casefold()
            if status in {"succeeded", "completed"}:
                return body
            if status in {"failed", "cancelled", "canceled"}:
                raise L7ReleaseError("Fabric operation reported failure")
            time.sleep(2)
        raise L7ReleaseError("Fabric operation timed out")


class L7Executor:
    def __init__(self, planner: L7Planner, backend: L7Backend) -> None:
        self.planner = planner
        self.backend = backend

    def execute(
        self,
        *,
        config: L7ReleaseConfig,
        config_path: Path,
        plan: L7DeploymentPlan,
        approval: str,
        receipt_path: Path,
    ) -> L7DeploymentReceipt:
        if approval != plan.plan_hash:
            raise L7ReleaseError("--approve-live must exactly equal the plan hash")
        if config.config_hash != plan.config_hash:
            raise L7ReleaseError("current configuration differs from approved plan")
        if datetime.now(timezone.utc) >= plan.expires_at:
            raise L7ReleaseError("approved plan has expired")
        fresh = self.planner.build(config, config_path=config_path)
        comparable = ("tenant_id", "principal_hash", "config_hash", "observation_hash")
        if any(getattr(fresh, key) != getattr(plan, key) for key in comparable):
            raise L7ReleaseError("immediate live drift check differs from approved plan")
        if fresh.actions != plan.actions:
            raise L7ReleaseError("planned actions changed before live execution")
        blockers = [item for item in plan.actions if item.action == "no-go"]
        if blockers:
            names = ", ".join(item.component for item in blockers)
            raise L7ReleaseError(f"capability NO-GO; no mutations performed: {names}")

        journal: list[JournalEntry] = []
        applied: list[DeploymentAction] = []
        sequence = 1
        try:
            for action in plan.actions:
                if action.action in {"noop", "deferred"}:
                    continue
                if datetime.now(timezone.utc) >= plan.expires_at:
                    raise L7ReleaseError("plan expired immediately before mutation")
                journal.append(
                    JournalEntry(
                        sequence=sequence,
                        phase="before",
                        component=action.component,
                        action=action.action,
                        resource_id=action.resource_id,
                        status="pending",
                        observed_etag=action.observed_etag,
                    )
                )
                sequence += 1
                applied.append(action)
                observed = self.backend.apply(config, action)
                if (
                    observed.resource_id.casefold() != action.resource_id.casefold()
                    or (
                        action.desired_hash
                        not in {observed.definition_hash, observed.properties_hash}
                    )
                ):
                    raise L7ReleaseError(
                        f"post-mutation readback mismatch: {action.component}"
                    )
                journal.append(
                    JournalEntry(
                        sequence=sequence,
                        phase="after",
                        component=action.component,
                        action=action.action,
                        resource_id=action.resource_id,
                        status="verified",
                        observed_etag=observed.etag,
                    )
                )
                sequence += 1
        except BaseException as exc:
            rollback_errors: list[str] = []
            for action in reversed(applied):
                journal.append(
                    JournalEntry(
                        sequence=sequence,
                        phase="rollback-before",
                        component=action.component,
                        action=action.rollback.action,
                        resource_id=action.resource_id,
                        status="pending",
                    )
                )
                sequence += 1
                try:
                    observed = self.backend.rollback(config, action)
                    status = "verified"
                    etag = observed.etag
                except BaseException as rollback_exc:
                    rollback_errors.append(
                        f"{action.component}:{type(rollback_exc).__name__}"
                    )
                    status = "failed"
                    etag = ""
                journal.append(
                    JournalEntry(
                        sequence=sequence,
                        phase="rollback-after",
                        component=action.component,
                        action=action.rollback.action,
                        resource_id=action.resource_id,
                        status=status,
                        observed_etag=etag,
                    )
                )
                sequence += 1
            receipt = L7DeploymentReceipt.seal(
                plan_hash=plan.plan_hash,
                status="failed" if rollback_errors else "rolled-back",
                completed_at=datetime.now(timezone.utc),
                journal=tuple(journal),
                deferred_components=tuple(
                    item.component for item in plan.actions if item.action == "deferred"
                ),
            )
            persist_receipt(receipt_path, receipt)
            detail = (
                f"; rollback failures: {', '.join(rollback_errors)}"
                if rollback_errors
                else ""
            )
            raise L7ReleaseError(
                f"live deployment failed and rollback completed{detail}"
            ) from exc

        receipt = L7DeploymentReceipt.seal(
            plan_hash=plan.plan_hash,
            status="succeeded",
            completed_at=datetime.now(timezone.utc),
            journal=tuple(journal),
            deferred_components=tuple(
                item.component for item in plan.actions if item.action == "deferred"
            ),
        )
        persist_receipt(receipt_path, receipt)
        return receipt
