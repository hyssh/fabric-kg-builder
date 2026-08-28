"""Production-safe L7 planning and deployment authority for canonical L6 agents."""

from __future__ import annotations

import json
import os
import secrets
import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from fabric_kg_builder.agent.l6_integration import L6CanonicalAgentDefinition
from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256


L7_DEPLOYMENT_SCHEMA_VERSION = "1.0.0"
L7_DEPLOYMENT_CODE_VERSION = "0.2.3"


class L7DeploymentError(RuntimeError):
    """Raised when planning, drift validation, deployment, or rollback fails."""


def _add_exception_note(exc: BaseException, note: str) -> None:
    add_note = getattr(exc, "add_note", None)
    if callable(add_note):
        add_note(note)


class _L7Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class L7FabricItemTarget(_L7Model):
    item_id: str = Field(min_length=1)
    item_type: Literal["DataAgent", "Lakehouse", "Ontology", "GraphModel"]
    definition_path: str = Field(min_length=1)
    definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    definition_bytes_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class L7DeploymentConfig(_L7Model):
    """Non-secret exact target configuration for one L7 deployment."""

    tenant_id: str = Field(min_length=1)
    subscription_id: str = Field(min_length=1)
    resource_group: str = Field(min_length=1)
    expected_principal_id: str = Field(min_length=1)
    foundry_account_name: str = Field(min_length=1)
    foundry_project_name: str = Field(min_length=1)
    foundry_project_endpoint: str = Field(min_length=1)
    model_deployment: str = Field(min_length=1)
    fabric_workspace_id: str = Field(min_length=1)
    fabric_items: tuple[L7FabricItemTarget, ...]
    fabric_connection_name: str = Field(min_length=1)
    remote_tool_connection_name: str = Field(min_length=1)
    remote_tool_endpoint: str = Field(min_length=1)
    remote_tool_audience: str = Field(min_length=1)
    remote_tool_allowed_caller_object_ids: tuple[str, ...]
    remote_tool_required_app_role: str = Field(min_length=1)
    remote_tool_max_body_bytes: int = Field(
        default=1_048_576,
        ge=1024,
        le=16_777_216,
    )
    remote_tool_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        le=300,
    )
    fabric_connection_ownership_authority_id: str = Field(
        pattern=r"^gxra-sha256:[0-9a-f]{64}$"
    )
    l6_authority_backend_version: str = Field(min_length=1)
    l5a_definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    l5b_definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_ttl_seconds: int = Field(default=900, ge=60, le=3600)

    @field_validator("foundry_project_endpoint", "remote_tool_endpoint")
    @classmethod
    def _https_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "deployment endpoints must be unsigned HTTPS URLs without userinfo"
            )
        return value.rstrip("/")

    @field_validator("remote_tool_audience")
    @classmethod
    def _audience(cls, value: str) -> str:
        if "?" in value or "#" in value or "@" in value:
            raise ValueError("RemoteTool audience must be a stable unsigned identifier")
        return value

    @field_validator(
        "fabric_connection_name",
        "remote_tool_connection_name",
        "foundry_account_name",
        "foundry_project_name",
        "resource_group",
    )
    @classmethod
    def _arm_name(cls, value: str) -> str:
        if value != value.strip() or "/" in value:
            raise ValueError("resource names must be exact ARM path segments")
        return value

    @model_validator(mode="after")
    def _items_are_unique(self) -> "L7DeploymentConfig":
        identities = tuple(item.item_id.casefold() for item in self.fabric_items)
        if len(identities) != len(set(identities)):
            raise ValueError("Fabric item IDs must be unique")
        data_agents = tuple(
            item for item in self.fabric_items if item.item_type == "DataAgent"
        )
        if len(data_agents) != 1:
            raise ValueError("exactly one Fabric DataAgent item is required")
        if not self.remote_tool_allowed_caller_object_ids:
            raise ValueError("RemoteTool requires at least one allowed caller object ID")
        if len(self.remote_tool_allowed_caller_object_ids) != len(
            set(self.remote_tool_allowed_caller_object_ids)
        ):
            raise ValueError("RemoteTool allowed caller object IDs must be unique")
        endpoint = urlsplit(self.foundry_project_endpoint)
        expected_hosts = {
            f"{self.foundry_account_name}.services.ai.azure.com".casefold(),
            f"{self.foundry_account_name}.ai.azure.com".casefold(),
        }
        if (
            str(endpoint.hostname or "").casefold() not in expected_hosts
            or endpoint.path.rstrip("/")
            != f"/api/projects/{self.foundry_project_name}"
        ):
            raise ValueError(
                "Foundry project endpoint differs from configured account/project"
            )
        return self

    @property
    def foundry_project_resource_id(self) -> str:
        return (
            f"/subscriptions/{self.subscription_id}"
            f"/resourceGroups/{self.resource_group}"
            f"/providers/Microsoft.CognitiveServices/accounts/"
            f"{self.foundry_account_name}/projects/{self.foundry_project_name}"
        )

    def connection_resource_id(self, name: str) -> str:
        return f"{self.foundry_project_resource_id}/connections/{name}"

    @property
    def config_hash(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class L7ObservedIdentity(_L7Model):
    tenant_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)


class L7RemoteReadinessObservation(_L7Model):
    endpoint: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    audience: str = Field(min_length=1)
    caller_object_id: str = Field(min_length=1)
    app_role: str = Field(min_length=1)
    openapi_schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    l6_definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_backend: str = Field(min_length=1)
    authority_version: str = Field(min_length=1)
    checked_at: datetime
    expires_at: datetime
    readiness_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _sealed(self) -> "L7RemoteReadinessObservation":
        if self.expires_at <= self.checked_at:
            raise ValueError("RemoteTool readiness expiry must follow observation")
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"readiness_hash"})
        )
        if self.readiness_hash != expected:
            raise ValueError("RemoteTool readiness hash mismatch")
        return self

    @property
    def binding_hash(self) -> str:
        return canonical_sha256(
            self.model_dump(
                mode="json",
                exclude={"checked_at", "expires_at", "readiness_hash"},
            )
        )


class L7OwnershipAuthorityObservation(_L7Model):
    backend: Literal["azure_blob"]
    authority_id: str = Field(pattern=r"^gxra-sha256:[0-9a-f]{64}$")
    snapshot_version: int = Field(ge=1)
    checked_at: datetime
    expires_at: datetime
    observation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _sealed(self) -> "L7OwnershipAuthorityObservation":
        if self.expires_at <= self.checked_at:
            raise ValueError("ownership authority observation is expired")
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"observation_hash"})
        )
        if self.observation_hash != expected:
            raise ValueError("ownership authority observation hash mismatch")
        return self

    @property
    def binding_hash(self) -> str:
        return canonical_sha256(
            self.model_dump(
                mode="json",
                exclude={"checked_at", "expires_at", "observation_hash"},
            )
        )


class L7ConnectionOwnershipReceipt(_L7Model):
    connection_id: str = Field(min_length=1)
    connection_etag: str = Field(min_length=1)
    category: Literal["CustomKeys"]
    target: Literal["-"]
    audience: Literal[""] = ""
    workspace_id: str = Field(min_length=1)
    data_agent_id: str = Field(min_length=1)
    authority_id: str = Field(pattern=r"^gxra-sha256:[0-9a-f]{64}$")
    authority_version: int = Field(ge=1)
    issued_at: datetime
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _sealed(self) -> "L7ConnectionOwnershipReceipt":
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"receipt_hash"})
        )
        if self.receipt_hash != expected:
            raise ValueError("connection ownership receipt hash mismatch")
        return self

    @property
    def signing_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode="json",
            exclude={"signature", "receipt_hash"},
        )


class L7ResourceReadback(_L7Model):
    resource_kind: Literal[
        "fabric_item",
        "fabric_definition",
        "foundry_connection",
        "foundry_agent",
    ]
    stable_id: str = Field(min_length=1)
    exists: bool
    etag: str | None = None
    resource_type: str | None = None
    properties_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    definition_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )


class L7RollbackAction(_L7Model):
    action: Literal["delete_if_created", "restore_if_updated", "none"]
    expected_etag: str | None = None


class L7DeploymentAction(_L7Model):
    resource_kind: Literal[
        "fabric_item",
        "fabric_connection",
        "remote_tool_connection",
        "foundry_agent",
    ]
    stable_id: str = Field(min_length=1)
    action: Literal["create", "update", "adopt", "verify", "unsupported"]
    ownership: Literal["attempt_created", "attempt_updated", "preexisting", "absent"]
    expected_etag: str | None = None
    expected_readback_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    desired_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_definition_bytes_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    rollback: L7RollbackAction
    capability_reason: str | None = None


class L7DeploymentPlan(_L7Model):
    schema_version: Literal["1.0.0"] = L7_DEPLOYMENT_SCHEMA_VERSION
    code_version: Literal["0.2.3"] = L7_DEPLOYMENT_CODE_VERSION
    created_at: datetime
    expires_at: datetime
    tenant_id: str
    principal_id: str
    subscription_id: str
    resource_group: str
    foundry_project_resource_id: str
    fabric_workspace_id: str
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    l5a_definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    l5b_definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    l6_definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    remote_readiness: L7RemoteReadinessObservation
    ownership_authority: L7OwnershipAuthorityObservation
    fabric_connection_ownership: L7ConnectionOwnershipReceipt | None = None
    readbacks: tuple[L7ResourceReadback, ...]
    actions: tuple[L7DeploymentAction, ...]
    hosting_prerequisite: str
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _sealed(self) -> "L7DeploymentPlan":
        if self.expires_at <= self.created_at:
            raise ValueError("deployment plan expiry must follow creation")
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"plan_hash"})
        )
        if self.plan_hash != expected:
            raise ValueError("deployment plan hash mismatch")
        stable_ids = tuple(action.stable_id for action in self.actions)
        if len(stable_ids) != len(set(stable_ids)):
            raise ValueError("deployment actions must target unique stable IDs")
        return self

    @classmethod
    def seal(cls, **values: Any) -> "L7DeploymentPlan":
        values.pop("plan_hash", None)
        provisional = cls.model_construct(**values, plan_hash="0" * 64)
        values["plan_hash"] = canonical_sha256(
            provisional.model_dump(mode="json", exclude={"plan_hash"})
        )
        return cls.model_validate(values)


class L7RemoteAccounting(_L7Model):
    calls: int = Field(ge=0)
    request_bytes: int = Field(ge=0)
    response_bytes: int = Field(ge=0)
    retries: int = Field(ge=0)
    waits: int = Field(ge=0)
    operation_refs: tuple[str, ...] = ()


class L7ResourceResult(_L7Model):
    stable_id: str
    action: Literal["created", "updated", "adopted", "verified"]
    before_etag: str | None = None
    after_etag: str | None = None
    readback_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    ownership_receipt_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    rollback_status: Literal["not_required", "pending", "succeeded"] = "not_required"


class L7DeploymentReceipt(_L7Model):
    schema_version: Literal["1.0.0"] = L7_DEPLOYMENT_SCHEMA_VERSION
    status: Literal["succeeded", "failed"]
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    l6_definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    started_at: datetime
    completed_at: datetime
    resources: tuple[L7ResourceResult, ...]
    accounting: L7RemoteAccounting
    failure_code: str | None = None
    receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _sealed(self) -> "L7DeploymentReceipt":
        if (self.status == "failed") != bool(self.failure_code):
            raise ValueError("failed receipts require a safe failure code")
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"receipt_hash"})
        )
        if self.receipt_hash != expected:
            raise ValueError("deployment receipt hash mismatch")
        return self

    @classmethod
    def seal(cls, **values: Any) -> "L7DeploymentReceipt":
        values.pop("receipt_hash", None)
        provisional = cls.model_construct(**values, receipt_hash="0" * 64)
        values["receipt_hash"] = canonical_sha256(
            provisional.model_dump(mode="json", exclude={"receipt_hash"})
        )
        return cls.model_validate(values)


class L7ReadOnlyProbe(Protocol):
    """All methods must be GET/read-only and return exact stable readback."""

    def current_identity(self) -> L7ObservedIdentity: ...

    def probe_remote_readiness(
        self,
        *,
        config: L7DeploymentConfig,
        definition: L6CanonicalAgentDefinition,
    ) -> L7RemoteReadinessObservation: ...

    def probe_ownership_authority(
        self,
        *,
        config: L7DeploymentConfig,
    ) -> L7OwnershipAuthorityObservation: ...

    def get_fabric_connection_ownership(
        self,
        *,
        config: L7DeploymentConfig,
        readback: L7ResourceReadback,
        data_agent_id: str,
    ) -> L7ConnectionOwnershipReceipt | None: ...

    def get_fabric_item(
        self,
        *,
        workspace_id: str,
        item: L7FabricItemTarget,
    ) -> L7ResourceReadback: ...

    def get_connection(
        self,
        *,
        resource_id: str,
    ) -> L7ResourceReadback: ...

    def get_agent(
        self,
        *,
        project_resource_id: str,
        agent_name: str,
    ) -> L7ResourceReadback: ...

    def desired_agent_hash(
        self,
        *,
        config: L7DeploymentConfig,
        definition: L6CanonicalAgentDefinition,
    ) -> str: ...


class L7MutationAdapter(Protocol):
    def apply(
        self,
        action: L7DeploymentAction,
        *,
        config: L7DeploymentConfig,
        definition: L6CanonicalAgentDefinition,
    ) -> L7ResourceResult: ...

    def rollback(
        self,
        action: L7DeploymentAction,
        result: L7ResourceResult,
        *,
        config: L7DeploymentConfig,
    ) -> L7ResourceResult: ...

    def verify_postconditions(
        self,
        *,
        config: L7DeploymentConfig,
        definition: L6CanonicalAgentDefinition,
        results: tuple[L7ResourceResult, ...],
    ) -> None: ...

    def rollback_started(
        self,
        action: L7DeploymentAction,
        *,
        config: L7DeploymentConfig,
        definition: L6CanonicalAgentDefinition,
    ) -> None: ...


@dataclass
class _L7MutationJournalEntry:
    action: L7DeploymentAction
    phase: Literal["started", "completed"] = "started"
    result: L7ResourceResult | None = None


def require_canonical_l6_definition(
    path: Path,
) -> L6CanonicalAgentDefinition:
    """Load a persisted definition only when it equals rebuilt L6 authority bytes."""
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise L7DeploymentError("L6 definition file is not valid canonical JSON") from exc
    if not isinstance(value, dict):
        raise L7DeploymentError("L6 definition file must contain an object")
    try:
        connections = value["connections"]
        canonical = L6CanonicalAgentDefinition(
            agent_name=str(value["agent_name"]),
            fabric_data_agent_connection_id=str(
                connections["fabric_data_agent"]["project_connection_id"]
            ),
            foundry_remote_tool_connection_id=str(
                connections["l6_remote_tool"]["project_connection_id"]
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise L7DeploymentError(
            "L6 definition lacks trusted canonical construction fields"
        ) from exc
    if raw != canonical.canonical_bytes:
        raise L7DeploymentError(
            "L6 definition differs from rebuilt canonical authority"
        )
    return canonical


def load_l7_config(path: Path) -> L7DeploymentConfig:
    try:
        return L7DeploymentConfig.model_validate_json(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise L7DeploymentError("L7 deployment config is invalid") from exc


def persist_l7_plan(path: Path, plan: L7DeploymentPlan) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json(plan.model_dump(mode="json")) + "\n"
    path.write_text(payload, encoding="utf-8")
    if L7DeploymentPlan.model_validate_json(path.read_text("utf-8")) != plan:
        raise L7DeploymentError("persisted L7 plan readback mismatch")


def load_l7_plan(path: Path) -> L7DeploymentPlan:
    try:
        return L7DeploymentPlan.model_validate_json(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise L7DeploymentError("L7 deployment plan is invalid") from exc


def persist_l7_receipt(path: Path, receipt: L7DeploymentReceipt) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        path.name + f".{secrets.token_hex(16)}.tmp"
    )
    payload = canonical_json(receipt.model_dump(mode="json")) + "\n"
    linked = False
    owned_inode: int | None = None
    try:
        with temporary.open("xb") as stream:
            stream.write(payload.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        if (
            L7DeploymentReceipt.model_validate_json(
                temporary.read_text("utf-8")
            )
            != receipt
        ):
            raise L7DeploymentError(
                "temporary L7 receipt readback mismatch"
            )
        owned_inode = temporary.stat().st_ino
        os.link(temporary, path)
        linked = True
        temporary.unlink()
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_fd = os.open(str(path.parent), directory_flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if (
            L7DeploymentReceipt.model_validate_json(path.read_text("utf-8"))
            != receipt
        ):
            raise L7DeploymentError(
                "persisted L7 receipt readback mismatch"
            )
    except (OSError, ValueError, L7DeploymentError) as exc:
        temporary.unlink(missing_ok=True)
        if linked and owned_inode is not None:
            try:
                if path.stat().st_ino == owned_inode:
                    path.unlink()
                    directory_flags = os.O_RDONLY | getattr(
                        os,
                        "O_DIRECTORY",
                        0,
                    )
                    directory_fd = os.open(str(path.parent), directory_flags)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            except FileNotFoundError:
                pass
        if isinstance(exc, L7DeploymentError):
            raise
        raise L7DeploymentError(
            "L7 deployment receipt persistence failed"
        ) from exc


def _connection_desired_hash(
    *,
    auth_type: str,
    category: str,
    target: str,
    audience: str = "",
    binding_hash: str = "",
    group: str = "",
    is_shared_to_all: bool = True,
    metadata: Mapping[str, Any] | None = None,
) -> str:
    return canonical_sha256(
        {
            "authType": auth_type,
            "category": category,
            "group": group,
            "target": target,
            "isSharedToAll": is_shared_to_all,
            "audience": audience,
            "metadata": dict(metadata or {}),
            "binding_hash": binding_hash,
        }
    )


def _action_for_mutable(
    *,
    kind: Literal[
        "fabric_connection",
        "remote_tool_connection",
        "foundry_agent",
    ],
    readback: L7ResourceReadback,
    desired_hash: str,
) -> L7DeploymentAction:
    if not readback.exists:
        return L7DeploymentAction(
            resource_kind=kind,
            stable_id=readback.stable_id,
            action="create",
            ownership="attempt_created",
            expected_readback_hash=canonical_sha256(
                readback.model_dump(mode="json")
            ),
            desired_hash=desired_hash,
            rollback=L7RollbackAction(action="delete_if_created"),
        )
    if readback.properties_hash == desired_hash:
        return L7DeploymentAction(
            resource_kind=kind,
            stable_id=readback.stable_id,
            action="adopt",
            ownership="preexisting",
            expected_etag=readback.etag,
            expected_readback_hash=canonical_sha256(
                readback.model_dump(mode="json")
            ),
            desired_hash=desired_hash,
            rollback=L7RollbackAction(action="none"),
        )
    return L7DeploymentAction(
        resource_kind=kind,
        stable_id=readback.stable_id,
        action="update",
        ownership="attempt_updated",
        expected_etag=readback.etag,
        expected_readback_hash=canonical_sha256(
            readback.model_dump(mode="json")
        ),
        desired_hash=desired_hash,
        rollback=L7RollbackAction(
            action="restore_if_updated",
            expected_etag=readback.etag,
        ),
    )


def _validate_expected_fabric_definitions(
    config: L7DeploymentConfig,
) -> None:
    for item in config.fabric_items:
        try:
            expected_bytes = Path(item.definition_path).read_bytes()
            expected_json = json.loads(expected_bytes)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise L7DeploymentError(
                "expected Fabric definition artifact is unreadable"
            ) from exc
        canonical_bytes = (canonical_json(expected_json) + "\n").encode("utf-8")
        if (
            expected_bytes != canonical_bytes
            or canonical_sha256(expected_json) != item.definition_hash
            or hashlib.sha256(expected_bytes).hexdigest()
            != item.definition_bytes_hash
        ):
            raise L7DeploymentError(
                "expected Fabric definition bytes/hash are not canonical"
            )


class L7DeploymentPlanner:
    """Construct an immutable plan using only read-only probes."""

    def __init__(
        self,
        probe: L7ReadOnlyProbe,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._probe = probe
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def build(
        self,
        *,
        config: L7DeploymentConfig,
        definition: L6CanonicalAgentDefinition,
    ) -> L7DeploymentPlan:
        if type(definition) is not L6CanonicalAgentDefinition:
            raise L7DeploymentError("L7 accepts only L6CanonicalAgentDefinition")
        if (
            definition.fabric_data_agent_connection_id.casefold()
            != config.connection_resource_id(
                config.fabric_connection_name
            ).casefold()
            or definition.foundry_remote_tool_connection_id.casefold()
            != config.connection_resource_id(
                config.remote_tool_connection_name
            ).casefold()
        ):
            raise L7DeploymentError(
                "canonical L6 connection IDs differ from configured resources"
            )
        identity = self._probe.current_identity()
        if (
            identity.tenant_id.casefold() != config.tenant_id.casefold()
            or identity.principal_id.casefold()
            != config.expected_principal_id.casefold()
        ):
            raise L7DeploymentError("deployment identity differs from configuration")
        remote_readiness = self.probe_remote_readiness(
            config=config,
            definition=definition,
        )
        ownership_authority = self._probe.probe_ownership_authority(
            config=config
        )
        if (
            ownership_authority.authority_id
            != config.fabric_connection_ownership_authority_id
            or ownership_authority.expires_at <= self._clock()
        ):
            raise L7DeploymentError(
                "Fabric connection ownership authority is unavailable"
            )

        readbacks: list[L7ResourceReadback] = []
        actions: list[L7DeploymentAction] = []
        _validate_expected_fabric_definitions(config)
        for item in config.fabric_items:
            readback = self._probe.get_fabric_item(
                workspace_id=config.fabric_workspace_id,
                item=item,
            )
            readbacks.append(readback)
            expected_hash = item.definition_hash
            exact = (
                readback.exists
                and readback.resource_type == item.item_type
                and readback.definition_hash == item.definition_hash
            )
            actions.append(
                L7DeploymentAction(
                    resource_kind="fabric_item",
                    stable_id=readback.stable_id,
                    action="verify" if exact else "unsupported",
                    ownership="preexisting" if readback.exists else "absent",
                    expected_etag=readback.etag,
                    expected_readback_hash=canonical_sha256(
                        readback.model_dump(mode="json")
                    ),
                    desired_hash=expected_hash,
                    expected_definition_bytes_hash=(
                        item.definition_bytes_hash
                    ),
                    rollback=L7RollbackAction(action="none"),
                    capability_reason=(
                        None
                        if exact
                        else "Fabric mutation is unsupported; exact item readback is required"
                    ),
                )
            )

        data_agent = next(
            item for item in config.fabric_items if item.item_type == "DataAgent"
        )
        fabric_id = config.connection_resource_id(config.fabric_connection_name)
        fabric_readback = self._probe.get_connection(resource_id=fabric_id)
        readbacks.append(fabric_readback)
        fabric_ownership = self._probe.get_fabric_connection_ownership(
            config=config,
            readback=fabric_readback,
            data_agent_id=data_agent.item_id,
        )
        fabric_binding_hash = canonical_sha256(
            {
                "workspace_id": config.fabric_workspace_id,
                "data_agent_id": data_agent.item_id,
            }
        )
        fabric_connection_action = _action_for_mutable(
            kind="fabric_connection",
            readback=fabric_readback,
            desired_hash=_connection_desired_hash(
                auth_type="CustomKeys",
                category="CustomKeys",
                target="-",
                group="AzureAI",
                metadata={
                    "type": "fabric_dataagent_preview",
                    "bindingHash": fabric_binding_hash,
                },
                binding_hash=fabric_binding_hash,
            ),
        )
        if fabric_connection_action.action == "update":
            fabric_connection_action = fabric_connection_action.model_copy(
                update={
                    "action": "unsupported",
                    "capability_reason": (
                        "Foundry redacts CustomKeys credentials; a preexisting "
                        "mismatched Fabric connection cannot be restored safely"
                    ),
                }
            )
        elif fabric_connection_action.action == "adopt":
            if (
                fabric_ownership is None
                or fabric_ownership.connection_id.casefold()
                != fabric_id.casefold()
                or fabric_ownership.connection_etag != fabric_readback.etag
                or fabric_ownership.category != "CustomKeys"
                or fabric_ownership.target != "-"
                or fabric_ownership.audience != ""
                or fabric_ownership.workspace_id
                != config.fabric_workspace_id
                or fabric_ownership.data_agent_id != data_agent.item_id
                or fabric_ownership.authority_id
                != config.fabric_connection_ownership_authority_id
            ):
                fabric_connection_action = fabric_connection_action.model_copy(
                    update={
                        "action": "unsupported",
                        "capability_reason": (
                            "preexisting redacted CustomKeys connection lacks "
                            "an exact signed durable ownership receipt"
                        ),
                    }
                )
        actions.append(fabric_connection_action)

        remote_id = config.connection_resource_id(
            config.remote_tool_connection_name
        )
        remote_readback = self._probe.get_connection(resource_id=remote_id)
        readbacks.append(remote_readback)
        actions.append(
            _action_for_mutable(
                kind="remote_tool_connection",
                readback=remote_readback,
                desired_hash=_connection_desired_hash(
                    auth_type="ProjectManagedIdentity",
                    category="RemoteTool",
                    target=config.remote_tool_endpoint,
                    audience=config.remote_tool_audience,
                    metadata={"ApiType": "Azure"},
                ),
            )
        )

        agent_id = (
            f"{config.foundry_project_resource_id}/agents/{definition.agent_name}"
        )
        agent_readback = self._probe.get_agent(
            project_resource_id=config.foundry_project_resource_id,
            agent_name=definition.agent_name,
        )
        if agent_readback.stable_id != agent_id:
            raise L7DeploymentError("Foundry agent stable ID readback mismatch")
        readbacks.append(agent_readback)
        agent_hash = self._probe.desired_agent_hash(
            config=config,
            definition=definition,
        )
        actions.append(
            _action_for_mutable(
                kind="foundry_agent",
                readback=agent_readback,
                desired_hash=agent_hash,
            )
        )
        now = self._clock()
        return L7DeploymentPlan.seal(
            created_at=now,
            expires_at=now + timedelta(seconds=config.plan_ttl_seconds),
            tenant_id=config.tenant_id,
            principal_id=config.expected_principal_id,
            subscription_id=config.subscription_id,
            resource_group=config.resource_group,
            foundry_project_resource_id=config.foundry_project_resource_id,
            fabric_workspace_id=config.fabric_workspace_id,
            config_hash=config.config_hash,
            l5a_definition_hash=config.l5a_definition_hash,
            l5b_definition_hash=config.l5b_definition_hash,
            l6_definition_hash=definition.definition_hash,
            remote_readiness=remote_readiness,
            ownership_authority=ownership_authority,
            fabric_connection_ownership=fabric_ownership,
            readbacks=tuple(readbacks),
            actions=tuple(actions),
            hosting_prerequisite=(
                "RemoteTool endpoint must already be hosted on an approved HTTPS "
                "compute surface with Entra audience enforcement; this deployment "
                "does not provision compute."
            ),
        )

    def probe_remote_readiness(
        self,
        *,
        config: L7DeploymentConfig,
        definition: L6CanonicalAgentDefinition,
    ) -> L7RemoteReadinessObservation:
        observation = self._probe.probe_remote_readiness(
            config=config,
            definition=definition,
        )
        now = self._clock()
        if observation.expires_at <= now:
            raise L7DeploymentError("RemoteTool readiness observation expired")
        return observation


class L7DeploymentExecutor:
    """Validate an approved plan completely before performing its first mutation."""

    def __init__(
        self,
        *,
        planner: L7DeploymentPlanner,
        mutations: L7MutationAdapter,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._planner = planner
        self._mutations = mutations
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def execute(
        self,
        *,
        plan: L7DeploymentPlan,
        approve_live: str,
        config: L7DeploymentConfig,
        definition: L6CanonicalAgentDefinition,
        receipt_path: Path,
    ) -> L7DeploymentReceipt:
        started_at = self._clock()
        if approve_live != plan.plan_hash:
            raise L7DeploymentError("live approval must equal the exact plan hash")
        if started_at >= plan.expires_at:
            raise L7DeploymentError("approved deployment plan has expired")
        if plan.config_hash != config.config_hash:
            raise L7DeploymentError("deployment config changed since planning")
        if plan.l6_definition_hash != definition.definition_hash:
            raise L7DeploymentError("L6 canonical definition changed since planning")
        if receipt_path.exists():
            raise L7DeploymentError(
                "L7 deployment receipt path already exists"
            )
        fresh = self._planner.build(config=config, definition=definition)
        if (
            fresh.tenant_id != plan.tenant_id
            or fresh.principal_id != plan.principal_id
            or fresh.readbacks != plan.readbacks
            or fresh.actions != plan.actions
            or fresh.remote_readiness.binding_hash
            != plan.remote_readiness.binding_hash
            or fresh.ownership_authority.binding_hash
            != plan.ownership_authority.binding_hash
            or fresh.fabric_connection_ownership
            != plan.fabric_connection_ownership
            or fresh.l5a_definition_hash != plan.l5a_definition_hash
            or fresh.l5b_definition_hash != plan.l5b_definition_hash
        ):
            raise L7DeploymentError(
                "tenant, identity, resource ETag, audience, or definition drift detected"
            )
        unsupported = tuple(
            action for action in plan.actions if action.action == "unsupported"
        )
        if unsupported:
            raise L7DeploymentError(
                "plan contains unsupported Fabric capabilities; no mutations performed"
            )
        pre_mutation_readiness = self._planner.probe_remote_readiness(
            config=config,
            definition=definition,
        )
        if (
            pre_mutation_readiness.binding_hash
            != plan.remote_readiness.binding_hash
        ):
            raise L7DeploymentError(
                "RemoteTool readiness changed immediately before mutation"
            )
        _validate_expected_fabric_definitions(config)
        if self._clock() >= plan.expires_at:
            raise L7DeploymentError(
                "approved deployment plan expired immediately before mutation"
            )

        journal: list[_L7MutationJournalEntry] = []
        calls = 0
        started_monotonic = time.monotonic()
        original_error: BaseException | None = None
        rollback_failures: list[str] = []
        receipt: L7DeploymentReceipt | None = None
        try:
            for action in plan.actions:
                if self._clock() >= plan.expires_at:
                    raise L7DeploymentError(
                        "approved deployment plan expired between mutations"
                    )
                calls += 1
                entry = _L7MutationJournalEntry(action=action)
                journal.append(entry)
                entry.result = self._mutations.apply(
                    action,
                    config=config,
                    definition=definition,
                )
                entry.phase = "completed"
            completed_results = tuple(
                entry.result
                for entry in journal
                if entry.phase == "completed" and entry.result is not None
            )
            self._mutations.verify_postconditions(
                config=config,
                definition=definition,
                results=completed_results,
            )
            final_readiness = self._planner.probe_remote_readiness(
                config=config,
                definition=definition,
            )
            if (
                final_readiness.binding_hash
                != plan.remote_readiness.binding_hash
            ):
                raise L7DeploymentError(
                    "RemoteTool readiness changed before deployment success"
                )
            completion_time = self._clock()
            if completion_time >= plan.expires_at:
                raise L7DeploymentError(
                    "approved deployment plan expired before success"
                )
            results = tuple(
                entry.result.model_copy(
                    update={"rollback_status": "not_required"}
                )
                for entry in journal
                if entry.phase == "completed" and entry.result is not None
            )
            duration_ref = "op-sha256:" + canonical_sha256(
                {
                    "plan_hash": plan.plan_hash,
                    "calls": calls,
                    "duration_bucket_ms": int(
                        (time.monotonic() - started_monotonic) * 1000
                    ),
                }
            )
            receipt = L7DeploymentReceipt.seal(
                status="succeeded",
                plan_hash=plan.plan_hash,
                config_hash=config.config_hash,
                l6_definition_hash=definition.definition_hash,
                started_at=started_at,
                completed_at=completion_time,
                resources=results,
                accounting=L7RemoteAccounting(
                    calls=calls,
                    request_bytes=0,
                    response_bytes=0,
                    retries=0,
                    waits=0,
                    operation_refs=(duration_ref,),
                ),
            )
            persist_l7_receipt(receipt_path, receipt)
        except BaseException as exc:
            original_error = exc
        finally:
            if original_error is not None:
                for entry in reversed(journal):
                    if entry.action.rollback.action == "none":
                        continue
                    try:
                        if (
                            entry.phase == "completed"
                            and entry.result is not None
                        ):
                            self._mutations.rollback(
                                entry.action,
                                entry.result,
                                config=config,
                            )
                        else:
                            self._mutations.rollback_started(
                                entry.action,
                                config=config,
                                definition=definition,
                            )
                    except BaseException as rollback_exc:
                        rollback_failures.append(
                            type(rollback_exc).__name__
                        )
        if original_error is not None:
            detail = (
                "L7 deployment failed after "
                f"{calls} operations; rollback_failures="
                f"{len(rollback_failures)}"
            )
            if isinstance(
                original_error,
                (KeyboardInterrupt, SystemExit),
            ):
                _add_exception_note(original_error, detail)
                raise original_error
            try:
                import asyncio

                cancelled = isinstance(
                    original_error,
                    asyncio.CancelledError,
                )
            except ImportError:
                cancelled = False
            if cancelled:
                _add_exception_note(original_error, detail)
                raise original_error
            raise L7DeploymentError(detail) from original_error
        if receipt is None:
            raise L7DeploymentError(
                "L7 deployment completed without a receipt"
            )
        return receipt
