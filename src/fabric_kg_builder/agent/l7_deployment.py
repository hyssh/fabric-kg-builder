"""Production-safe L7 planning and deployment authority for canonical L6 agents."""

from __future__ import annotations

import json
import time
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
    definition_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )


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
    path.write_text(
        canonical_json(receipt.model_dump(mode="json")) + "\n",
        encoding="utf-8",
    )


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

        readbacks: list[L7ResourceReadback] = []
        actions: list[L7DeploymentAction] = []
        for item in config.fabric_items:
            readback = self._probe.get_fabric_item(
                workspace_id=config.fabric_workspace_id,
                item=item,
            )
            readbacks.append(readback)
            expected_hash = item.definition_hash or canonical_sha256(
                {"item_id": item.item_id, "item_type": item.item_type}
            )
            exact = (
                readback.exists
                and readback.resource_type == item.item_type
                and (
                    item.definition_hash is None
                    or readback.definition_hash == item.definition_hash
                )
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
            readbacks=tuple(readbacks),
            actions=tuple(actions),
            hosting_prerequisite=(
                "RemoteTool endpoint must already be hosted on an approved HTTPS "
                "compute surface with Entra audience enforcement; this deployment "
                "does not provision compute."
            ),
        )


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
        rollback_on_failure: bool = True,
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
        fresh = self._planner.build(config=config, definition=definition)
        if (
            fresh.tenant_id != plan.tenant_id
            or fresh.principal_id != plan.principal_id
            or fresh.readbacks != plan.readbacks
            or fresh.actions != plan.actions
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

        results: list[L7ResourceResult] = []
        calls = 0
        started_monotonic = time.monotonic()
        try:
            for action in plan.actions:
                calls += 1
                results.append(
                    self._mutations.apply(
                        action,
                        config=config,
                        definition=definition,
                    )
                )
        except L7DeploymentError as exc:
            rollback_failures = 0
            if rollback_on_failure:
                for action, result in reversed(tuple(zip(plan.actions, results))):
                    if action.rollback.action != "none":
                        try:
                            self._mutations.rollback(
                                action,
                                result,
                                config=config,
                            )
                        except L7DeploymentError:
                            rollback_failures += 1
            raise L7DeploymentError(
                "L7 deployment failed after "
                f"{calls} operations; rollback_failures={rollback_failures}"
            ) from exc
        duration_ref = "op-sha256:" + canonical_sha256(
            {
                "plan_hash": plan.plan_hash,
                "calls": calls,
                "duration_bucket_ms": int(
                    (time.monotonic() - started_monotonic) * 1000
                ),
            }
        )
        return L7DeploymentReceipt.seal(
            status="succeeded",
            plan_hash=plan.plan_hash,
            config_hash=config.config_hash,
            l6_definition_hash=definition.definition_hash,
            started_at=started_at,
            completed_at=self._clock(),
            resources=tuple(results),
            accounting=L7RemoteAccounting(
                calls=calls,
                request_bytes=0,
                response_bytes=0,
                retries=0,
                waits=0,
                operation_refs=(duration_ref,),
            ),
        )
