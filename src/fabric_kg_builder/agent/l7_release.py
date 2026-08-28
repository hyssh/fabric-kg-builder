"""Strict, file-driven L7 release planning and execution for 0.2.4."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol
from urllib.parse import urlsplit
from urllib.parse import urljoin

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.version import RELEASE_VERSION


_FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
_MANAGEMENT_SCOPE = "https://management.azure.com/.default"
_SEARCH_SCOPE = "https://search.azure.com/.default"
_COGNITIVE_SERVICES_USER_ROLE_ID = "a97b65f3-24c7-4388-baec-2e87135dc908"
_FABRIC_BASE = "https://api.fabric.microsoft.com/v1"
_FABRIC_ORIGIN = "https://api.fabric.microsoft.com"
_ARM_ORIGIN = "https://management.azure.com"
_FABRIC_TYPES = {
    "DataAgent": "dataAgents",
    "GraphModel": "graphModels",
    "Ontology": "ontologies",
    "SemanticModel": "semanticModels",
}
_RELEASE_NAME_PATTERN = r"^fabric-kg-024-[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"


class L7ReleaseError(RuntimeError):
    """Raised when an L7 release gate fails closed."""


@dataclass(frozen=True)
class L7LroOutcome:
    body: dict[str, Any]
    status_url: str
    result_url: str | None


def _validated_service_url(
    candidate: str,
    *,
    expected_origin: str,
    base_url: str | None = None,
) -> str:
    expected = urlsplit(expected_origin)
    resolved = urljoin(base_url or f"{expected_origin.rstrip('/')}/", candidate)
    parsed = urlsplit(resolved)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port is not None
        or parsed.hostname is None
        or parsed.hostname.casefold() != (expected.hostname or "").casefold()
        or parsed.scheme.casefold() != expected.scheme.casefold()
    ):
        raise L7ReleaseError("service operation URL origin validation failed")
    return resolved


def _is_result_url(url: str) -> bool:
    return urlsplit(url).path.rstrip("/").casefold().endswith("/result")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactBinding(_StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def _validate_release_name(value: str) -> str:
    import re

    if not re.fullmatch(_RELEASE_NAME_PATTERN, value):
        raise ValueError(
            "resource name must use bounded release-owned fabric-kg-024-* grammar"
        )
    return value


def _effective_uid() -> int:
    getter = getattr(os, "geteuid", None)
    if not callable(getter):
        raise L7ReleaseError(
            "L7 live ownership validation requires POSIX owner semantics"
        )
    return int(getter())


class FabricOwnershipReceipt(_StrictModel):
    release: Literal["0.2.4"] = RELEASE_VERSION
    attempt_id: str = Field(pattern=r"^op-[0-9a-f]{64}$")
    authority_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    item_id: str = Field(min_length=1)
    item_type: Literal["DataAgent", "GraphModel", "Ontology", "SemanticModel"]
    name: str
    definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    etag: str = Field(min_length=1)
    created_at: datetime
    receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("name")
    @classmethod
    def _owned_name(cls, value: str) -> str:
        return _validate_release_name(value)

    @model_validator(mode="after")
    def _sealed(self) -> "FabricOwnershipReceipt":
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"receipt_hash"})
        )
        if self.receipt_hash != expected:
            raise ValueError("Fabric ownership receipt hash mismatch")
        return self


class FabricDefinitionTarget(_StrictModel):
    mode: Literal["create", "managed"]
    name: str
    item_id: str | None = None
    item_type: Literal["DataAgent", "GraphModel", "Ontology", "SemanticModel"]
    artifact: ArtifactBinding
    ownership_receipt: ArtifactBinding | None = None
    ownership_receipt_output: str | None = None

    @field_validator("name")
    @classmethod
    def _owned_name(cls, value: str) -> str:
        return _validate_release_name(value)

    @model_validator(mode="after")
    def _intent_fields(self) -> "FabricDefinitionTarget":
        if self.mode == "create":
            if self.item_id is not None or self.ownership_receipt is not None:
                raise ValueError(
                    "Fabric create intent forbids item_id and ownership_receipt"
                )
            if not self.ownership_receipt_output:
                raise ValueError(
                    "Fabric create intent requires ownership_receipt_output"
                )
            if not Path(self.ownership_receipt_output).is_absolute():
                raise ValueError(
                    "Fabric ownership_receipt_output must be absolute"
                )
        else:
            if not self.item_id or self.ownership_receipt is None:
                raise ValueError(
                    "Fabric managed intent requires item_id and ownership_receipt"
                )
            if not self.ownership_receipt_output:
                raise ValueError(
                    "Fabric managed intent requires ownership_receipt_output"
                )
            if not Path(self.ownership_receipt_output).is_absolute():
                raise ValueError(
                    "Fabric ownership_receipt_output must be absolute"
                )
        return self


class SearchTarget(_StrictModel):
    endpoint: str = Field(pattern=r"^https://[^/]+/?$")
    index_name: str
    index_schema: ArtifactBinding
    documents: ArtifactBinding
    knowledge_source_name: str
    knowledge_base_name: str
    api_version: str = "2025-11-01-preview"
    foundry_role_assignment_id: str = ""
    search_managed_identity_principal_id: str = ""
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

    @field_validator("index_name", "knowledge_source_name", "knowledge_base_name")
    @classmethod
    def _owned_names(cls, value: str) -> str:
        return _validate_release_name(value)

    @model_validator(mode="after")
    def _complete_role_evidence(self) -> "SearchTarget":
        values = (
            self.foundry_role_assignment_id,
            self.search_managed_identity_principal_id,
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
    search_connection_name: str
    fabric_connection_name: str
    data_agent_id: str = ""
    deploy_builtin_agent: bool = False

    @field_validator("search_connection_name", "fabric_connection_name")
    @classmethod
    def _owned_names(cls, value: str) -> str:
        return _validate_release_name(value)


class L7ReleaseConfig(_StrictModel):
    release: Literal["0.2.4"] = RELEASE_VERSION
    tenant_id: str = Field(min_length=1)
    subscription_id: str = Field(min_length=1)
    resource_group: str = Field(min_length=1)
    expected_principal_id: str = Field(min_length=1)
    fabric_workspace_id: str = Field(min_length=1)
    ownership_registry_output: str | None = None
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
        ids = [
            item.item_id.casefold()
            for item in self.fabric_definitions
            if item.item_id is not None
        ]
        if len(names) != len(set(names)) or len(ids) != len(set(ids)):
            raise ValueError("Fabric target names and IDs must be unique")
        data_agents = [
            item
            for item in self.fabric_definitions
            if item.item_type == "DataAgent"
        ]
        if len(data_agents) > 1:
            raise ValueError("at most one Fabric DataAgent target is allowed")
        if data_agents:
            data_agent = data_agents[0]
            if data_agent.mode == "managed":
                if self.foundry.data_agent_id.casefold() != str(
                    data_agent.item_id
                ).casefold():
                    raise ValueError(
                        "Foundry data_agent_id must bind the managed DataAgent"
                    )
            elif self.foundry.data_agent_id:
                raise ValueError(
                    "Foundry data_agent_id must be empty for DataAgent create intent"
                )
        elif self.foundry.data_agent_id or self.foundry.deploy_builtin_agent:
            raise ValueError(
                "Foundry Data Agent binding requires a Fabric DataAgent target"
            )
        if self.fabric_definitions:
            if not self.ownership_registry_output:
                raise ValueError("Fabric targets require ownership_registry_output")
            if not Path(self.ownership_registry_output).is_absolute():
                raise ValueError(
                    "ownership_registry_output must be absolute"
                )
        modes = {item.mode for item in self.fabric_definitions}
        if len(modes) > 1:
            raise ValueError(
                "Fabric create and managed targets require separate transactions"
            )
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
    properties_hash: str = ""
    stable_id: str = ""


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
    ownership_marker: str = ""
    readback_expectation: Mapping[str, Any]
    rollback: RollbackStep
    reason: str = ""


class L7DeploymentPlan(_StrictModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    release: Literal["0.2.4"] = RELEASE_VERSION
    attempt_id: str = Field(pattern=r"^op-[0-9a-f]{64}$")
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
    attempt_id: str = Field(pattern=r"^op-[0-9a-f]{64}$")
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


def _search_document_batches(
    documents: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    max_bytes = 15 * 1024 * 1024
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for document in documents:
        action = {"@search.action": "upload", **document}
        candidate = [*current, action]
        encoded_size = len(
            json.dumps(
                {"value": candidate},
                ensure_ascii=True,
                allow_nan=False,
            ).encode()
        )
        if len(candidate) <= 1000 and encoded_size <= max_bytes:
            current = candidate
            continue
        if not current:
            raise L7ReleaseError(
                "Search document exceeds the safe indexing payload limit"
            )
        batches.append(current)
        current = [action]
        if (
            len(
                json.dumps(
                    {"value": current},
                    ensure_ascii=True,
                    allow_nan=False,
                ).encode()
            )
            > max_bytes
        ):
            raise L7ReleaseError(
                "Search document exceeds the safe indexing payload limit"
            )
    if current:
        batches.append(current)
    return batches


def _ownership_receipt(
    target: FabricDefinitionTarget,
    config: L7ReleaseConfig,
    base: Path,
) -> FabricOwnershipReceipt:
    try:
        receipt = FabricOwnershipReceipt.model_validate(
            _artifact_value(target.ownership_receipt, base)
        )
    except ValueError as exc:
        raise L7ReleaseError(
            f"invalid Fabric ownership receipt for {target.name}"
        ) from exc
    if (
        receipt.item_id.casefold() != target.item_id.casefold()
        or receipt.item_type != target.item_type
        or receipt.name != target.name
        or receipt.authority_hash != config.authority_hash
    ):
        raise L7ReleaseError(
            f"Fabric ownership receipt binding mismatch for {target.name}"
        )
    registry_path_text = os.environ.get("FABRIC_KG_OWNERSHIP_REGISTRY", "")
    registry_hash = os.environ.get("FABRIC_KG_OWNERSHIP_REGISTRY_SHA256", "")
    if (
        not registry_path_text
        or not Path(registry_path_text).is_absolute()
        or len(registry_hash) != 64
    ):
        raise L7ReleaseError(
            "Fabric ownership requires an absolute registry path and pinned hash"
        )
    registry_path = Path(registry_path_text)
    try:
        registry_bytes = registry_path.read_bytes()
        registry_stat = registry_path.stat()
    except OSError as exc:
        raise L7ReleaseError("Fabric ownership registry is unavailable") from exc
    if (
        hashlib.sha256(registry_bytes).hexdigest() != registry_hash
        or registry_stat.st_uid != _effective_uid()
        or stat.S_IMODE(registry_stat.st_mode) & 0o222
    ):
        raise L7ReleaseError(
            "Fabric ownership registry is not pinned, owner-controlled, and read-only"
        )
    try:
        registry = json.loads(registry_bytes)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise L7ReleaseError("Fabric ownership registry is invalid") from exc
    registry_key = (
        f"{config.tenant_id.casefold()}/{config.fabric_workspace_id.casefold()}/"
        f"{target.item_type.casefold()}/{target.item_id.casefold()}"
    )
    receipts = (
        registry.get("receipts")
        if isinstance(registry, dict)
        and isinstance(registry.get("receipts"), dict)
        else {}
    )
    if receipts.get(registry_key) != receipt.receipt_hash:
        raise L7ReleaseError(
            f"Fabric ownership registry mismatch for {target.name}"
        )
    return receipt


def _pinned_ownership_entries() -> dict[str, str]:
    registry_path_text = os.environ.get("FABRIC_KG_OWNERSHIP_REGISTRY", "")
    registry_hash = os.environ.get("FABRIC_KG_OWNERSHIP_REGISTRY_SHA256", "")
    if (
        not registry_path_text
        or not Path(registry_path_text).is_absolute()
        or len(registry_hash) != 64
    ):
        raise L7ReleaseError(
            "Fabric ownership requires an absolute registry path and pinned hash"
        )
    registry_path = Path(registry_path_text)
    try:
        registry_bytes = registry_path.read_bytes()
        registry_stat = registry_path.stat()
    except OSError as exc:
        raise L7ReleaseError("Fabric ownership registry is unavailable") from exc
    if (
        hashlib.sha256(registry_bytes).hexdigest() != registry_hash
        or registry_stat.st_uid != _effective_uid()
        or stat.S_IMODE(registry_stat.st_mode) & 0o222
    ):
        raise L7ReleaseError(
            "Fabric ownership registry is not pinned, owner-controlled, and read-only"
        )
    try:
        registry = json.loads(registry_bytes)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise L7ReleaseError("Fabric ownership registry is invalid") from exc
    receipts = (
        registry.get("receipts")
        if isinstance(registry, dict)
        and isinstance(registry.get("receipts"), dict)
        else None
    )
    if receipts is None or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in receipts.items()
    ):
        raise L7ReleaseError("Fabric ownership registry receipts are invalid")
    return dict(receipts)


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


def _secure_output_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = path.parent.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != _effective_uid()
        or stat.S_IMODE(parent.st_mode) & 0o022
    ):
        raise L7ReleaseError(
            "immutable output directory must be owner-controlled and not "
            "group/world writable"
        )


@dataclass(frozen=True)
class _OwnedPublication:
    device: int
    inode: int
    directory: int
    descriptor: int


def _write_immutable(
    path: Path,
    payload: bytes,
    *,
    retain_descriptors: bool = False,
) -> tuple[int, int] | _OwnedPublication | None:
    _secure_output_parent(path)
    directory = os.open(path.parent, os.O_RDONLY)

    def existing_identity() -> tuple[int, int] | None:
        try:
            descriptor = os.open(
                path.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise L7ReleaseError(
                f"immutable path cannot be opened safely: {path}"
            ) from exc
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != _effective_uid()
                or stat.S_IMODE(opened.st_mode) & 0o222
            ):
                raise L7ReleaseError(
                    f"immutable path has unsafe ownership or mode: {path}"
                )
            existing = stream.read()
        if existing != payload:
            raise L7ReleaseError(f"refusing to replace immutable file: {path}")
        return opened.st_dev, opened.st_ino

    try:
        if existing_identity() is not None:
            os.close(directory)
            return None
    except BaseException:
        os.close(directory)
        raise
    temp_name = f".{path.name}.{os.getpid()}.tmp"
    try:
        descriptor = os.open(
            temp_name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=directory,
        )
    except BaseException:
        os.close(directory)
        raise
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    except BaseException:
        os.close(descriptor)
        os.unlink(temp_name, dir_fd=directory)
        os.close(directory)
        raise
    staged = os.fstat(descriptor)
    try:
        os.link(
            temp_name,
            path.name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
            follow_symlinks=False,
        )
    except FileExistsError:
        try:
            matched = existing_identity() is not None
        finally:
            os.close(descriptor)
            os.unlink(temp_name, dir_fd=directory)
            os.close(directory)
        if matched:
            return None
        raise
    except BaseException:
        os.close(descriptor)
        os.unlink(temp_name, dir_fd=directory)
        os.close(directory)
        raise
    os.unlink(temp_name, dir_fd=directory)
    try:
        os.fsync(directory)
    except BaseException:
        try:
            linked = os.stat(
                path.name, dir_fd=directory, follow_symlinks=False
            )
            if (linked.st_dev, linked.st_ino) == (
                staged.st_dev,
                staged.st_ino,
            ):
                os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
                os.unlink(path.name, dir_fd=directory)
        except OSError:
            pass
        os.close(descriptor)
        os.close(directory)
        raise
    published = os.stat(
        path.name, dir_fd=directory, follow_symlinks=False
    )
    if (published.st_dev, published.st_ino) != (staged.st_dev, staged.st_ino):
        os.close(descriptor)
        os.close(directory)
        raise L7ReleaseError("immutable publication inode changed")
    if retain_descriptors:
        os.lseek(descriptor, 0, os.SEEK_SET)
        return _OwnedPublication(
            device=published.st_dev,
            inode=published.st_ino,
            directory=directory,
            descriptor=descriptor,
        )
    os.close(descriptor)
    os.close(directory)
    return published.st_dev, published.st_ino


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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


class _ReceiptReservation:
    """Reserve both durable receipt outcomes before the first mutation."""

    def __init__(self, path: Path, plan_hash: str, attempt_id: str) -> None:
        self.path = path
        self.plan_hash = plan_hash
        self.attempt_id = attempt_id
        self.success_pending = path.with_name(f".{path.name}.pending")
        self.failure_path = path.with_name(f"{path.name}.failure.json")
        self.failure_pending = path.with_name(f".{path.name}.failure.pending")
        self.reserved = False
        self._success_fd = -1
        self._failure_fd = -1
        self._marker = b""

    @staticmethod
    def _entry_exists(path: Path) -> bool:
        return os.path.lexists(path)

    @staticmethod
    def _secure_parent(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent = path.parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != _effective_uid()
            or stat.S_IMODE(parent.st_mode) & 0o022
        ):
            raise L7ReleaseError(
                "receipt directory must be owner-controlled and not group/world writable"
            )

    @staticmethod
    def _create(path: Path, payload: bytes) -> int:
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            path.unlink(missing_ok=True)
            raise
        return descriptor

    def reserve(self) -> None:
        self._secure_parent(self.path)
        if self._entry_exists(self.path) or self._entry_exists(self.failure_path):
            raise L7ReleaseError(
                "preexisting receipt destination blocks a new transaction"
            )
        self._marker = (
            json.dumps(
                {
                    "attempt_id": self.attempt_id,
                    "plan_hash": self.plan_hash,
                    "status": "reserved",
                },
                sort_keys=True,
            )
            + "\n"
        ).encode()
        try:
            self._success_fd = self._create(
                self.success_pending, self._marker
            )
            try:
                self._failure_fd = self._create(
                    self.failure_pending, self._marker
                )
            except BaseException:
                os.close(self._success_fd)
                self._success_fd = -1
                self.success_pending.unlink(missing_ok=True)
                raise
        except FileExistsError as exc:
            raise L7ReleaseError(
                "receipt transaction already reserved; reconcile before retry"
            ) from exc
        self.reserved = True
        try:
            _fsync_parent(self.path)
        except BaseException:
            self.cancel_before_mutation()
            raise
        if self._entry_exists(self.path) or self._entry_exists(self.failure_path):
            self.cancel_before_mutation()
            raise L7ReleaseError(
                "receipt destination changed during reservation"
            )

    @staticmethod
    def _payload(receipt: L7DeploymentReceipt) -> bytes:
        return (
            json.dumps(receipt.model_dump(mode="json"), indent=2, sort_keys=True)
            + "\n"
        ).encode()

    @staticmethod
    def _rewrite(descriptor: int, path: Path, payload: bytes) -> None:
        opened = os.fstat(descriptor)
        current = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise L7ReleaseError("receipt reservation inode changed")
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

    def _close_descriptors(self) -> None:
        for attribute in ("_success_fd", "_failure_fd"):
            descriptor = getattr(self, attribute)
            if descriptor >= 0:
                os.close(descriptor)
                setattr(self, attribute, -1)

    def commit_success(self, receipt: L7DeploymentReceipt) -> None:
        if not self.reserved or receipt.attempt_id != self.attempt_id:
            raise L7ReleaseError("success receipt does not own the reservation")
        self._rewrite(
            self._success_fd, self.success_pending, self._payload(receipt)
        )
        success_identity = os.fstat(self._success_fd)
        try:
            os.close(self._failure_fd)
            self._failure_fd = -1
            self.failure_pending.unlink()
            os.link(self.success_pending, self.path, follow_symlinks=False)
            final_identity = self.path.stat(follow_symlinks=False)
            if (
                final_identity.st_dev,
                final_identity.st_ino,
            ) != (
                success_identity.st_dev,
                success_identity.st_ino,
            ):
                raise L7ReleaseError("success receipt link inode mismatch")
            self.success_pending.unlink()
            os.close(self._success_fd)
            self._success_fd = -1
            _fsync_parent(self.path)
        except BaseException:
            if self._entry_exists(self.path):
                final_identity = self.path.stat(follow_symlinks=False)
                if (
                    final_identity.st_dev,
                    final_identity.st_ino,
                ) == (
                    success_identity.st_dev,
                    success_identity.st_ino,
                ):
                    self.path.unlink(missing_ok=True)
            if self._success_fd >= 0:
                os.close(self._success_fd)
                self._success_fd = -1
            self.success_pending.unlink(missing_ok=True)
            if self._failure_fd >= 0:
                os.close(self._failure_fd)
                self._failure_fd = -1
            self.failure_pending.unlink(missing_ok=True)
            self._failure_fd = self._create(
                self.failure_pending, self._marker
            )
            try:
                _fsync_parent(self.path)
            except OSError:
                pass
            raise
        self.reserved = False

    def commit_failure(self, receipt: L7DeploymentReceipt) -> None:
        if not self.reserved or receipt.attempt_id != self.attempt_id:
            raise L7ReleaseError("failure receipt does not own the reservation")
        self._rewrite(
            self._failure_fd, self.failure_pending, self._payload(receipt)
        )
        failure_identity = os.fstat(self._failure_fd)
        if self._success_fd >= 0:
            os.close(self._success_fd)
            self._success_fd = -1
        self.success_pending.unlink(missing_ok=True)
        os.link(self.failure_pending, self.failure_path, follow_symlinks=False)
        final_identity = self.failure_path.stat(follow_symlinks=False)
        if (
            final_identity.st_dev,
            final_identity.st_ino,
        ) != (
            failure_identity.st_dev,
            failure_identity.st_ino,
        ):
            raise L7ReleaseError("failure receipt link inode mismatch")
        self.failure_pending.unlink()
        os.close(self._failure_fd)
        self._failure_fd = -1
        _fsync_parent(self.failure_path)
        self.reserved = False

    def cancel_before_mutation(self) -> None:
        if not self.reserved:
            return
        self._close_descriptors()
        self.success_pending.unlink(missing_ok=True)
        self.failure_pending.unlink(missing_ok=True)
        try:
            _fsync_parent(self.path)
        except OSError:
            pass
        self.reserved = False


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
        self,
        config: L7ReleaseConfig,
        *,
        config_path: Path,
        attempt_id: str | None = None,
    ) -> L7DeploymentPlan:
        deployment_attempt_id = attempt_id or ("op-" + secrets.token_hex(32))
        if not (
            deployment_attempt_id.startswith("op-")
            and len(deployment_attempt_id) == 67
        ):
            raise L7ReleaseError("deployment attempt ID is invalid")
        ownership_marker = f"fabric-kg-024-attempt:{deployment_attempt_id}"
        base = config_path.resolve().parent
        _validate_artifact(config.l6_definition, base)
        fabric_desired_hashes = {
            item.name: canonical_sha256(
                _definition_value(item.artifact, base)
            )
            for item in config.fabric_definitions
        }
        ownership_receipts = {
            str(item.item_id): _ownership_receipt(item, config, base)
            for item in config.fabric_definitions
            if item.mode == "managed"
        }
        observation = self.backend.observe(config)
        _require_identity(config, observation.identity)
        reads = _readback_map(observation)
        actions: list[DeploymentAction] = []
        order = 1

        for target in config.fabric_definitions:
            desired_definition_hash = fabric_desired_hashes[target.name]
            resource_id = (
                f"/workspaces/{config.fabric_workspace_id}/"
                f"{_FABRIC_TYPES[target.item_type]}/"
                + (
                    str(target.item_id)
                    if target.mode == "managed"
                    else f"by-name/{target.name}"
                )
            )
            observed = reads.get(resource_id.casefold())
            if target.mode == "create":
                capable = observation.capabilities.get(
                    f"fabric.{target.item_type}.create", False
                )
                if not capable:
                    mutation = "no-go"
                    reason = "exact Fabric create/getDefinition/delete capability unavailable"
                elif observed is not None and observed.exists:
                    mutation = "no-go"
                    reason = "release-owned Fabric create name collision"
                else:
                    mutation = "create"
                    reason = ""
            else:
                capable = observation.capabilities.get(
                    f"fabric.{target.item_type}.definition", False
                )
                receipt = ownership_receipts[str(target.item_id)]
                if not capable:
                    mutation = "no-go"
                    reason = "exact definition mutation and getDefinition readback unavailable"
                elif observed is None or not observed.exists:
                    mutation = "no-go"
                    reason = "configured managed Fabric item is absent"
                elif observed.resource_type != target.item_type or observed.name != target.name:
                    mutation = "no-go"
                    reason = "Fabric stable ID/type/name readback mismatch"
                elif (
                    observed.definition_hash != receipt.definition_hash
                    or observed.etag != receipt.etag
                ):
                    mutation = "no-go"
                    reason = "Fabric ownership receipt definition/ETag drift"
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
                    ownership_marker=ownership_marker,
                    observed_hash=observed.definition_hash if observed else "",
                    observed_etag=observed.etag if observed else "",
                    readback_expectation={
                        "stable_id": target.item_id,
                        "type": target.item_type,
                        "name": target.name,
                        "definition_hash": desired_definition_hash,
                        "ownership_receipt_hash": (
                            ownership_receipts[str(target.item_id)].receipt_hash
                            if target.mode == "managed"
                            else ""
                        ),
                    },
                    rollback=RollbackStep(
                        action=(
                            "restore-definition"
                            if mutation == "update"
                            else (
                                "delete-created"
                                if mutation == "create"
                                else "none"
                            )
                        ),
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
        index_schema["description"] = ownership_marker
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
                        "name": config.search.knowledge_source_name,
                        "kind": "searchIndex",
                        "searchIndexParameters": {
                            "searchIndexName": config.search.index_name,
                            "sourceDataFields": [],
                            "searchFields": [],
                        },
                        "description": ownership_marker,
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
                        "name": config.search.knowledge_base_name,
                        "knowledgeSources": [
                            {"name": config.search.knowledge_source_name}
                        ],
                        "description": ownership_marker,
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
                    ownership_marker=ownership_marker,
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
        from fabric_kg_builder.agent.project_connections import (
            fabric_data_agent_connection_properties,
            normalize_connection_properties,
            search_connection_properties,
        )

        if config.foundry.deploy_builtin_agent:
            search_connection_hash = canonical_sha256(
                normalize_connection_properties(
                    search_connection_properties(
                        endpoint=config.search.endpoint,
                        attempt_id=deployment_attempt_id,
                    )
                )
            )
            fabric_connection_hash = canonical_sha256(
                normalize_connection_properties(
                    fabric_data_agent_connection_properties(
                        workspace_id=config.fabric_workspace_id,
                        data_agent_id=config.foundry.data_agent_id,
                        attempt_id=deployment_attempt_id,
                    )
                )
            )
        else:
            search_connection_hash = canonical_sha256(
                {"deferred": config.foundry.search_connection_name}
            )
            fabric_connection_hash = canonical_sha256(
                {"deferred": config.foundry.fabric_connection_name}
            )
        for kind, name, desired_hash in (
            (
                "foundry-search-connection",
                config.foundry.search_connection_name,
                search_connection_hash,
            ),
            (
                "foundry-fabric-connection",
                config.foundry.fabric_connection_name,
                fabric_connection_hash,
            ),
        ):
            resource_id = f"{project_id}/connections/{name}"
            observed = reads.get(resource_id.casefold())
            if not config.foundry.deploy_builtin_agent:
                action = "deferred"
                reason = "Foundry built-in agent deployment disabled by config"
            elif not observation.capabilities.get("foundry.project-connections", False):
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
                    ownership_marker=ownership_marker,
                    readback_expectation={
                        "stable_id": resource_id,
                        "name": name,
                        "exact_security_properties": True,
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
                "Foundry agent deployment is disabled; local L6 and built-in "
                "Search/Fabric Data Agent inputs are prepared only"
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
            attempt_id=deployment_attempt_id,
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
        self._created_fabric_ids: dict[str, str] = {}
        self._ownership_outputs: dict[
            Path, tuple[bytes, int, int, int, int]
        ] = {}

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
        expected_origin: str | None = None,
        base_url: str | None = None,
    ) -> Any:
        import requests

        parsed = urlsplit(urljoin(base_url or "", url))
        origin = expected_origin or (
            f"{parsed.scheme}://{parsed.hostname}" if parsed.hostname else ""
        )
        safe_url = _validated_service_url(
            url,
            expected_origin=origin,
            base_url=base_url,
        )
        merged = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        merged.update(dict(headers or {}))
        try:
            response = requests.request(
                method,
                safe_url,
                headers=merged,
                json=body,
                timeout=60,
                allow_redirects=False,
            )
            if 300 <= response.status_code < 400:
                redirect = str(response.headers.get("Location") or "")
                if redirect:
                    _validated_service_url(
                        redirect,
                        expected_origin=origin,
                        base_url=safe_url,
                    )
                raise L7ReleaseError("service redirect was refused")
            return response
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
            outcome = self._wait_lro(
                location,
                token,
                expected_origin=_FABRIC_ORIGIN,
                base_url=definition_url,
            )
            definition = outcome.body.get("definition")
            if not isinstance(definition, dict):
                if outcome.result_url:
                    result_url = outcome.result_url
                else:
                    operation_parts = urlsplit(outcome.status_url)
                    status_path = operation_parts.path.rstrip("/")
                    result_path = (
                        status_path
                        if status_path.casefold().endswith("/result")
                        else f"{status_path}/result"
                    )
                    result_url = operation_parts._replace(
                        path=result_path,
                        query="",
                        fragment="",
                    ).geturl()
                result = self._request(
                    "GET",
                    result_url,
                    token=token,
                    expected_origin=_FABRIC_ORIGIN,
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
            stable_id=str(target.item_id or ""),
            etag=str(response.headers.get("ETag") or item_response.headers.get("ETag") or ""),
            definition_hash=canonical_sha256(definition),
            properties_hash=canonical_sha256(
                {"description": str(item.get("description") or "")}
            ),
        )

    def _list_fabric_items(
        self, config: L7ReleaseConfig
    ) -> list[dict[str, Any]]:
        token = self._token(_FABRIC_SCOPE)
        url = f"{_FABRIC_BASE}/workspaces/{config.fabric_workspace_id}/items"
        items: list[dict[str, Any]] = []
        for _ in range(100):
            response = self._request(
                "GET",
                url,
                token=token,
                expected_origin=_FABRIC_ORIGIN,
            )
            if response.status_code != 200:
                raise L7ReleaseError(
                    f"Fabric item listing failed with HTTP {response.status_code}"
                )
            body = self._json(response, "Fabric item listing")
            values = body.get("value")
            if not isinstance(values, list):
                raise L7ReleaseError("Fabric item listing omitted value array")
            items.extend(item for item in values if isinstance(item, dict))
            continuation = str(body.get("continuationUri") or "")
            if continuation:
                url = _validated_service_url(
                    continuation,
                    expected_origin=_FABRIC_ORIGIN,
                    base_url=url,
                )
                continue
            continuation_token = str(body.get("continuationToken") or "")
            if continuation_token:
                from urllib.parse import quote

                url = (
                    f"{_FABRIC_BASE}/workspaces/{config.fabric_workspace_id}/"
                    f"items?continuationToken={quote(continuation_token, safe='')}"
                )
                continue
            return items
        raise L7ReleaseError("Fabric item listing exceeded 100 pages")

    def observe(self, config: L7ReleaseConfig) -> L7Observation:
        identity = _decode_identity(self._token(_MANAGEMENT_SCOPE))
        resources: list[ResourceReadback] = []
        capabilities: dict[str, bool] = {
            "search.index": True,
            "search.knowledge-source": False,
            "search.knowledge-base": False,
            # ARM preview exposes create-or-update PUT without documented
            # atomic create-only/CAS semantics. Do not infer ownership from
            # mutable metadata or conditionally delete an overwritten item.
            "foundry.project-connections": False,
        }
        if config.search.foundry_role_assignment_id:
            foundry_scope = (
                f"/subscriptions/{config.subscription_id}/resourceGroups/"
                f"{config.resource_group}/providers/Microsoft.CognitiveServices/"
                f"accounts/{config.foundry.account_name}"
            )
            role_url = (
                f"https://management.azure.com{foundry_scope}"
                f"/providers/Microsoft.Authorization"
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
                    == config.search.search_managed_identity_principal_id.casefold()
                    and str(properties.get("roleDefinitionId") or "").casefold()
                    == config.search.foundry_role_definition_id.casefold()
                    and str(role_body.get("id") or "").casefold()
                    == (
                        f"{foundry_scope}/providers/Microsoft.Authorization/"
                        f"roleAssignments/{config.search.foundry_role_assignment_id}"
                    ).casefold()
                    and config.search.foundry_role_definition_id.casefold().endswith(
                        f"/roledefinitions/{_COGNITIVE_SERVICES_USER_ROLE_ID}"
                    )
                )
            else:
                role_present = False
            capabilities["search.knowledge-source"] = role_present
            capabilities["search.knowledge-base"] = role_present
        create_items = (
            self._list_fabric_items(config)
            if any(item.mode == "create" for item in config.fabric_definitions)
            else []
        )
        for target in config.fabric_definitions:
            if target.mode == "create":
                collection = _FABRIC_TYPES[target.item_type]
                resource_id = (
                    f"/workspaces/{config.fabric_workspace_id}/{collection}/"
                    f"by-name/{target.name}"
                )
                collision = next(
                    (
                        item
                        for item in create_items
                        if item.get("displayName") == target.name
                    ),
                    None,
                )
                resources.append(
                    ResourceReadback(
                        resource_id=resource_id,
                        exists=collision is not None,
                        resource_type=str((collision or {}).get("type") or target.item_type),
                        name=target.name,
                        stable_id=str((collision or {}).get("id") or ""),
                        etag=str((collision or {}).get("etag") or ""),
                    )
                )
                # Fabric create/get/delete contracts do not currently expose
                # documented ETag + conditional-delete CAS authority. Keep
                # first-run lifecycle planned but block live mutation.
                capabilities[f"fabric.{target.item_type}.create"] = False
                continue
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

        if config.foundry.deploy_builtin_agent:
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
                if (
                    item.mode == "managed"
                    and str(item.item_id).casefold()
                    == action.resource_id.casefold().rsplit("/", 1)[-1]
                )
                or (
                    item.mode == "create"
                    and item.name == action.name
                )
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
        marker = action.ownership_marker
        if not marker.startswith("fabric-kg-024-attempt:op-"):
            raise L7ReleaseError("Search action omitted approved ownership marker")
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
            self._reconcile_search_create(
                config,
                action,
                path,
                desired,
                token,
                keep=False,
            )
            raise
        if response.status_code == 202:
            location = str(response.headers.get("Location") or "")
            if not location:
                self._reconcile_search_create(
                    config, action, path, desired, token, keep=False
                )
            try:
                self._wait_lro(
                    location,
                    token,
                    expected_origin=config.search.endpoint,
                    base_url=self._search_url(config, path),
                )
            except L7ReleaseError:
                self._reconcile_search_create(
                    config, action, path, desired, token, keep=False
                )
            response = self._reconcile_search_create(
                config, action, path, desired, token, keep=True
            )
        elif response.status_code not in (200, 201):
            self._reconcile_search_create(
                config, action, path, desired, token, keep=False
            )
        if response.status_code in (200, 201):
            key = action.resource_id.casefold()
            self._mutation_confirmed.add(key)
            etag = str(response.headers.get("ETag") or "")
            if etag:
                self._created_etags[key] = etag
        return response, desired

    def _reconcile_search_create(
        self,
        config: L7ReleaseConfig,
        action: DeploymentAction,
        path: str,
        desired: dict[str, Any],
        token: str,
        *,
        keep: bool,
    ) -> Any:
        resource_url = self._search_url(config, path)
        observed = self._request("GET", resource_url, token=token)
        if observed.status_code != 200:
            raise L7ReleaseError(
                f"{action.component} create outcome is unconfirmed"
            )
        observed_body = {
            key: value
            for key, value in self._json(
                observed, f"{action.component} uncertain readback"
            ).items()
            if not key.startswith("@odata.")
        }
        if observed_body != desired:
            if observed_body.get("description") == action.ownership_marker:
                etag = str(observed.headers.get("ETag") or "")
                if etag:
                    self._mutation_confirmed.add(
                        action.resource_id.casefold()
                    )
                    self._created_etags[
                        action.resource_id.casefold()
                    ] = etag
                    self.rollback(config, action)
            raise L7ReleaseError(
                f"{action.component} collision has a foreign attempt or binding"
            )
        etag = str(observed.headers.get("ETag") or "")
        if not etag:
            raise L7ReleaseError(
                f"{action.component} reconciliation omitted ETag"
            )
        key = action.resource_id.casefold()
        self._mutation_confirmed.add(key)
        self._created_etags[key] = etag
        if keep:
            return observed
        cleanup = self._request(
            "DELETE",
            resource_url,
            token=token,
            headers={"If-Match": etag},
        )
        if cleanup.status_code == 202:
            location = str(cleanup.headers.get("Location") or "")
            if not location:
                raise L7ReleaseError(
                    f"{action.component} cleanup returned 202 without Location"
                )
            self._wait_lro(
                location,
                token,
                expected_origin=config.search.endpoint,
                base_url=resource_url,
            )
        elif cleanup.status_code not in (200, 204, 404):
            raise L7ReleaseError(
                f"{action.component} uncertain cleanup failed"
            )
        readback = self._request("GET", resource_url, token=token)
        if readback.status_code != 404:
            raise L7ReleaseError(
                f"{action.component} uncertain cleanup readback failed"
            )
        raise L7ReleaseError(
            f"{action.component} ambiguous create was reconciled and rolled back"
        )

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
            body = schema
            if response.status_code not in (200, 201):
                raise L7ReleaseError(
                    f"Search index create failed with HTTP {response.status_code}"
                )
            documents = _document_values(
                config.search.documents, self.artifact_base
            )
            for batch in _search_document_batches(documents):
                upload = self._request(
                    "POST",
                    self._search_url(
                        config, f"indexes/{config.search.index_name}/docs/index"
                    ),
                    token=token,
                    body={"value": batch},
                )
                if upload.status_code not in (200, 201):
                    raise L7ReleaseError(
                        f"Search document upload failed with HTTP {upload.status_code}"
                    )
                upload_result = self._json(
                    upload, "Search document upload readback"
                ).get("value")
                if (
                    not isinstance(upload_result, list)
                    or len(upload_result) != len(batch)
                    or any(
                        not isinstance(item, dict)
                        or item.get("status") is not True
                        for item in upload_result
                    )
                ):
                    raise L7ReleaseError(
                        "Search document upload item readback mismatch"
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
        expected_body = body
        if response.status_code not in (200, 201):
            raise L7ReleaseError(
                f"{action.component} create failed with HTTP {response.status_code}"
            )
        etag = str(response.headers.get("ETag") or "")
        if not etag:
            raise L7ReleaseError(
                f"{action.component} create omitted rollback ETag"
            )
        self._created_etags[action.resource_id.casefold()] = etag
        readback = self._request(
            "GET", self._search_url(config, path), token=token
        )
        if readback.status_code != 200:
            raise L7ReleaseError(
                f"{action.component} readback failed with HTTP {readback.status_code}"
            )
        readback_etag = str(readback.headers.get("ETag") or etag)
        if readback_etag != etag:
            raise L7ReleaseError(
                f"{action.component} ETag changed before readback"
            )
        self._created_etags[action.resource_id.casefold()] = readback_etag
        body = {
            key: value
            for key, value in self._json(
                readback, f"{action.component} readback"
            ).items()
            if not key.startswith("@odata.")
        }
        if str(body.get("name") or "") != action.name:
            raise L7ReleaseError(f"{action.component} exact-name readback mismatch")
        if action.component == "search-index":
            observed_schema = body
            if observed_schema != schema:
                raise L7ReleaseError("Search index schema readback mismatch")
            search_url = self._search_url(
                config, f"indexes/{config.search.index_name}/docs/search"
            )
            page_request: dict[str, Any] = {
                "search": "*",
                "top": min(1000, len(documents) + 1),
            }
            observed_documents: list[Any] = []
            continuation_hashes: set[str] = set()
            while True:
                search_response = self._request(
                    "POST",
                    search_url,
                    token=token,
                    body=page_request,
                )
                if search_response.status_code != 200:
                    raise L7ReleaseError("Search document readback failed")
                page = self._json(
                    search_response, "Search document readback"
                )
                values = page.get("value")
                if not isinstance(values, list):
                    raise L7ReleaseError(
                        "Search document readback omitted values"
                    )
                observed_documents.extend(values)
                next_link = page.get("@odata.nextLink")
                next_parameters = page.get("@search.nextPageParameters")
                if not next_link and next_parameters is None:
                    break
                if (
                    not isinstance(next_link, str)
                    or not isinstance(next_parameters, dict)
                    or not next_parameters
                ):
                    raise L7ReleaseError(
                        "Search document continuation was invalid"
                    )
                if len(observed_documents) > len(documents):
                    raise L7ReleaseError(
                        "Search document continuation exceeded expected count"
                    )
                continuation_hash = canonical_sha256(next_parameters)
                if continuation_hash in continuation_hashes:
                    raise L7ReleaseError(
                        "Search document continuation repeated"
                    )
                continuation_hashes.add(continuation_hash)
                page_request = dict(next_parameters)
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
            actual_hash = canonical_sha256(
                {
                    "schema": observed_schema,
                    "documents": sorted(
                        clean_documents, key=canonical_sha256
                    ),
                }
            )
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
            observed_body = body
            if observed_body != expected_body:
                raise L7ReleaseError(
                    "Search knowledge source exact readback mismatch"
                )
            actual_hash = canonical_sha256(observed_body)
        else:
            if [
                item.get("name")
                for item in body.get("knowledgeSources", [])
                if isinstance(item, dict)
            ] != [config.search.knowledge_source_name]:
                raise L7ReleaseError("Search knowledge base readback mismatch")
            observed_body = body
            if observed_body != expected_body:
                raise L7ReleaseError(
                    "Search knowledge base exact readback mismatch"
                )
            actual_hash = canonical_sha256(observed_body)
        if actual_hash != action.desired_hash:
            raise L7ReleaseError(
                f"{action.component} deployed hash differs from approved plan"
            )
        return ResourceReadback(
            resource_id=action.resource_id,
            exists=True,
            resource_type=action.resource_type,
            name=action.name,
            etag=readback_etag,
            properties_hash=actual_hash,
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

    def _created_target(
        self,
        target: FabricDefinitionTarget,
        item_id: str,
    ) -> FabricDefinitionTarget:
        return target.model_copy(
            update={
                "mode": "managed",
                "item_id": item_id,
                "ownership_receipt": target.artifact,
                "ownership_receipt_output": None,
            }
        )

    def _create_fabric(
        self,
        config: L7ReleaseConfig,
        target: FabricDefinitionTarget,
        action: DeploymentAction,
    ) -> ResourceReadback:
        if not action.ownership_marker.startswith(
            "fabric-kg-024-attempt:op-"
        ):
            raise L7ReleaseError("Fabric create omitted approved attempt marker")
        collection = _FABRIC_TYPES[target.item_type]
        create_url = (
            f"{_FABRIC_BASE}/workspaces/{config.fabric_workspace_id}/{collection}"
        )
        definition = self._definition(self._artifact_json(target.artifact))
        token = self._token(_FABRIC_SCOPE)
        try:
            response = self._request(
                "POST",
                create_url,
                token=token,
                body={
                    "displayName": target.name,
                    "description": action.ownership_marker,
                    "definition": definition,
                },
                expected_origin=_FABRIC_ORIGIN,
            )
        except L7ReleaseError:
            self._reconcile_fabric_create(
                config, target, action, keep=False
            )
            raise
        item_id = ""
        if response.status_code == 201:
            body = self._json(response, f"{target.item_type} create")
            item_id = str(body.get("id") or body.get("itemId") or "")
        elif response.status_code == 202:
            location = str(response.headers.get("Location") or "")
            if not location:
                self._reconcile_fabric_create(
                    config, target, action, keep=False
                )
            try:
                outcome = self._wait_lro(
                    location,
                    token,
                    expected_origin=_FABRIC_ORIGIN,
                    base_url=create_url,
                )
            except L7ReleaseError:
                self._reconcile_fabric_create(
                    config, target, action, keep=False
                )
                raise
            result = outcome.body.get("result")
            item_id = str(
                outcome.body.get("id")
                or outcome.body.get("itemId")
                or (
                    result.get("id")
                    if isinstance(result, dict)
                    else ""
                )
                or ""
            )
        else:
            self._reconcile_fabric_create(
                config, target, action, keep=False
            )
        if not item_id:
            return self._reconcile_fabric_create(
                config, target, action, keep=True
            )
        if not item_id:
            raise L7ReleaseError(f"{target.item_type} create omitted item ID")
        logical_key = action.resource_id.casefold()
        self._created_fabric_ids[logical_key] = item_id
        self._mutation_confirmed.add(logical_key)
        managed_target = self._created_target(target, item_id)
        observed = self._fabric_definition(config, managed_target)
        if observed.etag:
            self._created_etags[logical_key] = observed.etag
        if (
            observed.name != target.name
            or observed.resource_type != target.item_type
            or observed.definition_hash != action.desired_hash
            or observed.properties_hash
            != canonical_sha256(
                {"description": action.ownership_marker}
            )
            or not observed.etag
        ):
            raise L7ReleaseError(
                f"{target.item_type} create readback mismatch"
            )
        return ResourceReadback(
            resource_id=action.resource_id,
            stable_id=item_id,
            exists=True,
            resource_type=target.item_type,
            name=target.name,
            etag=observed.etag,
            definition_hash=observed.definition_hash,
        )

    def _reconcile_fabric_create(
        self,
        config: L7ReleaseConfig,
        target: FabricDefinitionTarget,
        action: DeploymentAction,
        *,
        keep: bool,
    ) -> ResourceReadback:
        if not action.ownership_marker.startswith(
            "fabric-kg-024-attempt:op-"
        ):
            raise L7ReleaseError("Fabric create omitted approved attempt marker")
        matches: list[dict[str, Any]] = []
        import time

        for attempt in range(5):
            matches = [
                item
                for item in self._list_fabric_items(config)
                if item.get("displayName") == target.name
                and item.get("type") == target.item_type
                and item.get("description") == action.ownership_marker
            ]
            if len(matches) == 1:
                break
            if len(matches) > 1:
                raise L7ReleaseError(
                    f"{target.item_type} create reconciliation is ambiguous"
                )
            if attempt < 4:
                time.sleep(2)
        if len(matches) != 1:
            raise L7ReleaseError(
                f"{target.item_type} create outcome is unconfirmed"
            )
        item_id = str(matches[0].get("id") or "")
        if not item_id:
            raise L7ReleaseError(
                f"{target.item_type} create reconciliation omitted item ID"
            )
        logical_key = action.resource_id.casefold()
        self._created_fabric_ids[logical_key] = item_id
        self._mutation_confirmed.add(logical_key)
        observed = self._fabric_definition(
            config, self._created_target(target, item_id)
        )
        if observed.etag:
            self._created_etags[logical_key] = observed.etag
        if (
            observed.name != target.name
            or observed.resource_type != target.item_type
            or observed.definition_hash != action.desired_hash
            or observed.properties_hash
            != canonical_sha256(
                {"description": action.ownership_marker}
            )
            or not observed.etag
        ):
            raise L7ReleaseError(
                f"{target.item_type} create reconciliation readback mismatch"
            )
        reconciled = ResourceReadback(
            resource_id=action.resource_id,
            stable_id=item_id,
            exists=True,
            resource_type=target.item_type,
            name=target.name,
            etag=observed.etag,
            definition_hash=observed.definition_hash,
        )
        if keep:
            return reconciled
        self.rollback(config, action)
        raise L7ReleaseError(
            f"{target.item_type} ambiguous create was reconciled and rolled back"
        )

    def finalize_ownership(
        self,
        config: L7ReleaseConfig,
        plan: L7DeploymentPlan,
        mutations: list[tuple[DeploymentAction, ResourceReadback]],
    ) -> None:
        receipt_hashes: dict[str, str] = (
            _pinned_ownership_entries()
            if any(
                target.mode == "managed"
                for target in config.fabric_definitions
            )
            else {}
        )
        fabric_mutations = {
            action.name: (action, observed)
            for action, observed in mutations
            if action.component.startswith("fabric-")
        }
        for target in config.fabric_definitions:
            mutation = fabric_mutations.get(target.name)
            if mutation is None:
                if target.mode != "managed":
                    continue
                existing = _ownership_receipt(
                    target, config, self.artifact_base
                )
                registry_key = (
                    f"{config.tenant_id.casefold()}/"
                    f"{config.fabric_workspace_id.casefold()}/"
                    f"{target.item_type.casefold()}/"
                    f"{str(target.item_id).casefold()}"
                )
                receipt_hashes[registry_key] = existing.receipt_hash
                continue
            action, observed = mutation
            values: dict[str, Any] = {
                "release": RELEASE_VERSION,
                "attempt_id": plan.attempt_id,
                "authority_hash": config.authority_hash,
                "item_id": observed.stable_id,
                "item_type": target.item_type,
                "name": target.name,
                "definition_hash": observed.definition_hash,
                "etag": observed.etag,
                "created_at": datetime.now(timezone.utc),
            }
            provisional = FabricOwnershipReceipt.model_construct(
                **values,
                receipt_hash="0" * 64,
            )
            values["receipt_hash"] = canonical_sha256(
                provisional.model_dump(
                    mode="json", exclude={"receipt_hash"}
                )
            )
            receipt = FabricOwnershipReceipt.model_validate(values)
            payload = (
                json.dumps(
                    receipt.model_dump(mode="json"),
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode()
            path = Path(str(target.ownership_receipt_output))
            publication = _write_immutable(
                path, payload, retain_descriptors=True
            )
            if isinstance(publication, _OwnedPublication):
                self._ownership_outputs[path] = (
                    payload,
                    publication.device,
                    publication.inode,
                    publication.directory,
                    publication.descriptor,
                )
            registry_key = (
                f"{config.tenant_id.casefold()}/"
                f"{config.fabric_workspace_id.casefold()}/"
                f"{target.item_type.casefold()}/"
                f"{observed.stable_id.casefold()}"
            )
            receipt_hashes[registry_key] = receipt.receipt_hash
        if receipt_hashes:
            registry_path = Path(str(config.ownership_registry_output))
            registry_payload = (
                json.dumps(
                    {"version": "1", "receipts": receipt_hashes},
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode()
            publication = _write_immutable(
                registry_path,
                registry_payload,
                retain_descriptors=True,
            )
            if isinstance(publication, _OwnedPublication):
                self._ownership_outputs[registry_path] = (
                    registry_payload,
                    publication.device,
                    publication.inode,
                    publication.directory,
                    publication.descriptor,
                )

    def apply(self, config: L7ReleaseConfig, action: DeploymentAction) -> ResourceReadback:
        if action.component.startswith("fabric-"):
            target = self._target_for_action(config, action)
            if action.action == "create":
                return self._create_fabric(config, target, action)
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
                self._wait_lro(
                    location,
                    self._token(_FABRIC_SCOPE),
                    expected_origin=_FABRIC_ORIGIN,
                    base_url=url,
                )
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
            attempt_id = action.ownership_marker.rsplit(":", 1)[-1]
            try:
                item = self._connection_client(config).upsert_search(
                    name=config.foundry.search_connection_name,
                    endpoint=config.search.endpoint,
                    create_only=True,
                    attempt_id=attempt_id,
                )
            except Exception as exc:
                raise L7ReleaseError(
                    "Foundry Search connection mutation failed"
                ) from exc
        elif action.component == "foundry-fabric-connection":
            attempt_id = action.ownership_marker.rsplit(":", 1)[-1]
            try:
                item = self._connection_client(config).upsert_fabric_data_agent(
                    name=config.foundry.fabric_connection_name,
                    workspace_id=config.fabric_workspace_id,
                    data_agent_id=config.foundry.data_agent_id,
                    create_only=True,
                    attempt_id=attempt_id,
                )
            except Exception as exc:
                raise L7ReleaseError(
                    "Foundry Fabric connection mutation failed"
                ) from exc
        else:
            raise L7ReleaseError(
                f"live mutation adapter for {action.component} is unavailable"
            )
        self._mutation_confirmed.add(action.resource_id.casefold())
        if not item.etag:
            raise L7ReleaseError("Foundry connection create omitted ETag")
        self._created_etags[action.resource_id.casefold()] = item.etag
        return ResourceReadback(
            resource_id=action.resource_id,
            exists=True,
            resource_type="ProjectConnection",
            name=action.name,
            etag=item.etag,
            properties_hash=item.properties_hash,
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
            rollback_url = (
                f"{_FABRIC_BASE}/workspaces/{config.fabric_workspace_id}/"
                f"{collection}/{target.item_id}/updateDefinition"
            )
            response = self._request(
                "POST",
                rollback_url,
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
                self._wait_lro(
                    location,
                    self._token(_FABRIC_SCOPE),
                    expected_origin=_FABRIC_ORIGIN,
                    base_url=rollback_url,
                )
            restored = self._fabric_definition(config, target)
            if restored.definition_hash != action.observed_hash:
                raise L7ReleaseError(
                    "Fabric rollback definition hash readback mismatch"
                )
            return restored
        if action.rollback.action != "delete-created":
            raise L7ReleaseError("unsupported rollback action")
        if action.component.startswith("fabric-"):
            target = self._target_for_action(config, action)
            item_id = self._created_fabric_ids.get(
                action.resource_id.casefold(), ""
            )
            etag = self._created_etags.get(
                action.resource_id.casefold(), ""
            )
            if not item_id or not etag:
                raise L7ReleaseError(
                    "Fabric create rollback lacks stable ID/ETag authority"
                )
            collection = _FABRIC_TYPES[target.item_type]
            delete_url = (
                f"{_FABRIC_BASE}/workspaces/{config.fabric_workspace_id}/"
                f"{collection}/{item_id}"
            )
            response = self._request(
                "DELETE",
                delete_url,
                token=self._token(_FABRIC_SCOPE),
                headers={"If-Match": etag},
                expected_origin=_FABRIC_ORIGIN,
            )
            if response.status_code == 202:
                location = str(response.headers.get("Location") or "")
                if not location:
                    raise L7ReleaseError(
                        "Fabric delete returned 202 without Location"
                    )
                self._wait_lro(
                    location,
                    self._token(_FABRIC_SCOPE),
                    expected_origin=_FABRIC_ORIGIN,
                    base_url=delete_url,
                )
            elif response.status_code not in (200, 204, 404):
                raise L7ReleaseError("Fabric create rollback delete failed")
            check = self._request(
                "GET",
                f"{_FABRIC_BASE}/workspaces/{config.fabric_workspace_id}/"
                f"items/{item_id}",
                token=self._token(_FABRIC_SCOPE),
                expected_origin=_FABRIC_ORIGIN,
            )
            if check.status_code != 404:
                raise L7ReleaseError(
                    "Fabric create rollback deletion readback mismatch"
                )
            return ResourceReadback(
                resource_id=action.resource_id,
                stable_id=item_id,
                exists=False,
                resource_type=target.item_type,
                name=target.name,
            )
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
            delete_url = self._search_url(config, segment)
            response = self._request(
                "DELETE",
                delete_url,
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
                self._wait_lro(
                    location,
                    self._token(_SEARCH_SCOPE),
                    expected_origin=config.search.endpoint,
                    base_url=delete_url,
                )
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

    def _cleanup_ownership_outputs(self) -> None:
        for path, owned in list(self._ownership_outputs.items()):
            payload, device, inode, directory, descriptor = owned
            try:
                current = os.fstat(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                if (
                    (current.st_dev, current.st_ino) != (device, inode)
                    or os.read(descriptor, len(payload) + 1) != payload
                ):
                    raise L7ReleaseError(
                        "ownership output changed before rollback"
                    )
                linked = os.stat(
                    path.name,
                    dir_fd=directory,
                    follow_symlinks=False,
                )
                if (linked.st_dev, linked.st_ino) != (device, inode):
                    raise L7ReleaseError(
                        "ownership output inode changed before rollback"
                    )
                os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
                os.unlink(path.name, dir_fd=directory)
                os.fsync(directory)
            except OSError as exc:
                raise L7ReleaseError(
                    "ownership output rollback failed"
                ) from exc
            finally:
                os.close(descriptor)
                os.close(directory)
            self._ownership_outputs.pop(path, None)

    def finalize_rollback(self) -> None:
        """Remove generated ownership state after all resource rollbacks succeed."""
        self._cleanup_ownership_outputs()

    def finalize_success(self) -> None:
        """Release ownership descriptors after the success receipt is durable."""
        for path, owned in list(self._ownership_outputs.items()):
            _payload, _device, _inode, directory, descriptor = owned
            for retained in (descriptor, directory):
                try:
                    os.close(retained)
                except OSError:
                    pass
            self._ownership_outputs.pop(path, None)

    def _wait_lro(
        self,
        location: str,
        token: str,
        *,
        expected_origin: str,
        base_url: str,
    ) -> L7LroOutcome:
        import time

        operation_url = _validated_service_url(
            location,
            expected_origin=expected_origin,
            base_url=base_url,
        )
        result_url: str | None = (
            operation_url if _is_result_url(operation_url) else None
        )
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            response = self._request(
                "GET",
                operation_url,
                token=token,
                expected_origin=expected_origin,
            )
            if 300 <= response.status_code < 400:
                raise L7ReleaseError("service operation redirect was refused")
            if response.status_code >= 400:
                raise L7ReleaseError(
                    f"Fabric operation failed with HTTP {response.status_code}"
                )
            body = self._json(response, "Fabric operation")
            next_location = str(response.headers.get("Location") or "")
            if next_location:
                validated_next = _validated_service_url(
                    next_location,
                    expected_origin=expected_origin,
                    base_url=operation_url,
                )
                if _is_result_url(validated_next):
                    result_url = validated_next
                else:
                    operation_url = validated_next
            status = str(body.get("status") or "").casefold()
            if status in {"succeeded", "completed"} or (
                not status and result_url == operation_url
            ):
                return L7LroOutcome(
                    body=body,
                    status_url=operation_url,
                    result_url=result_url,
                )
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
        reservation = _ReceiptReservation(
            receipt_path, plan.plan_hash, plan.attempt_id
        )
        if datetime.now(timezone.utc) >= plan.expires_at:
            raise L7ReleaseError("approved plan has expired")
        fresh = self.planner.build(
            config,
            config_path=config_path,
            attempt_id=plan.attempt_id,
        )
        comparable = ("tenant_id", "principal_hash", "config_hash", "observation_hash")
        if any(getattr(fresh, key) != getattr(plan, key) for key in comparable):
            raise L7ReleaseError("immediate live drift check differs from approved plan")
        if fresh.actions != plan.actions:
            raise L7ReleaseError("planned actions changed before live execution")
        blockers = [item for item in plan.actions if item.action == "no-go"]
        if blockers:
            names = ", ".join(item.component for item in blockers)
            raise L7ReleaseError(f"capability NO-GO; no mutations performed: {names}")

        reservation.reserve()
        journal: list[JournalEntry] = []
        applied: list[DeploymentAction] = []
        created_readbacks: list[
            tuple[DeploymentAction, ResourceReadback]
        ] = []
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
                if action.action in {"create", "update"}:
                    created_readbacks.append((action, observed))
            finalizer = getattr(self.backend, "finalize_ownership", None)
            if callable(finalizer):
                finalizer(config, plan, created_readbacks)
            receipt = L7DeploymentReceipt.seal(
                attempt_id=reservation.attempt_id,
                plan_hash=plan.plan_hash,
                status="succeeded",
                completed_at=datetime.now(timezone.utc),
                journal=tuple(journal),
                deferred_components=tuple(
                    item.component
                    for item in plan.actions
                    if item.action == "deferred"
                ),
            )
            reservation.commit_success(receipt)
            success_finalizer = getattr(self.backend, "finalize_success", None)
            if callable(success_finalizer):
                success_finalizer()
            return receipt
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
            if not rollback_errors:
                rollback_finalizer = getattr(
                    self.backend, "finalize_rollback", None
                )
                if callable(rollback_finalizer):
                    try:
                        rollback_finalizer()
                    except BaseException as rollback_exc:
                        rollback_errors.append(
                            "rollback-finalizer:"
                            f"{type(rollback_exc).__name__}"
                        )
            receipt = L7DeploymentReceipt.seal(
                attempt_id=reservation.attempt_id,
                plan_hash=plan.plan_hash,
                status="failed" if rollback_errors else "rolled-back",
                completed_at=datetime.now(timezone.utc),
                journal=tuple(journal),
                deferred_components=tuple(
                    item.component for item in plan.actions if item.action == "deferred"
                ),
            )
            try:
                reservation.commit_failure(receipt)
            except BaseException as persistence_exc:
                rollback_errors.append(
                    f"failure-receipt:{type(persistence_exc).__name__}"
                )
            detail = (
                f"; rollback failures: {', '.join(rollback_errors)}"
                if rollback_errors
                else ""
            )
            raise L7ReleaseError(
                f"live deployment failed and rollback completed{detail}"
            ) from exc
