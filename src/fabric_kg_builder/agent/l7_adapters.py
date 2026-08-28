"""Azure/Fabric production adapters for the L7 deployment authority."""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import os
import secrets
import time
from typing import Any, Mapping, Protocol
from urllib.parse import urljoin, urlsplit

from azure.core.exceptions import (
    AzureError,
    HttpResponseError,
    ServiceRequestError,
    ServiceResponseError,
)
from requests import RequestException

from fabric_kg_builder.agent.l6_integration import L6CanonicalAgentDefinition
from fabric_kg_builder.agent.l7_deployment import (
    L7DeploymentAction,
    L7ConnectionOwnershipReceipt,
    L7DeploymentConfig,
    L7DeploymentError,
    L7FabricItemTarget,
    L7ObservedIdentity,
    L7OwnershipAuthorityObservation,
    L7RemoteReadinessObservation,
    L7ResourceReadback,
    L7ResourceResult,
)
from fabric_kg_builder.agent.project_connections import (
    FoundryProjectConnectionClient,
    ProjectConnectionError,
)
from fabric_kg_builder.contracts.base import canonical_sha256


_ARM_SCOPE = "https://management.azure.com/.default"
_FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
_FABRIC_BASE = "https://api.fabric.microsoft.com/v1"


def _add_exception_note(exc: BaseException, note: str) -> None:
    add_note = getattr(exc, "add_note", None)
    if callable(add_note):
        add_note(note)


class L7FoundryAgentBackend(Protocol):
    def desired_hash(
        self,
        *,
        config: L7DeploymentConfig,
        definition: L6CanonicalAgentDefinition,
    ) -> str: ...

    def get(
        self,
        *,
        project_resource_id: str,
        agent_name: str,
    ) -> L7ResourceReadback: ...

    def upsert(
        self,
        *,
        config: L7DeploymentConfig,
        definition: L6CanonicalAgentDefinition,
        expected_etag: str | None,
        create_only: bool,
    ) -> L7ResourceReadback: ...

    def delete_created(
        self,
        *,
        config: L7DeploymentConfig,
        definition: L6CanonicalAgentDefinition,
        expected_etag: str,
    ) -> None: ...

    def restore_updated(
        self,
        *,
        config: L7DeploymentConfig,
        definition: L6CanonicalAgentDefinition,
        expected_etag: str,
    ) -> L7ResourceReadback: ...

    def rollback_pending(
        self,
        *,
        config: L7DeploymentConfig,
        definition: L6CanonicalAgentDefinition,
    ) -> None: ...


class L7ConnectionOwnershipAuthority(Protocol):
    def observe(self) -> L7OwnershipAuthorityObservation: ...

    def read_verified(
        self,
        *,
        connection_id: str,
    ) -> L7ConnectionOwnershipReceipt | None: ...

    def issue_attempt_created(
        self,
        *,
        connection_id: str,
        connection_etag: str,
        workspace_id: str,
        data_agent_id: str,
    ) -> L7ConnectionOwnershipReceipt: ...

    def delete_attempt_created(
        self,
        *,
        connection_id: str,
        connection_etag: str,
    ) -> None: ...


def _is_control_flow(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (KeyboardInterrupt, SystemExit, asyncio.CancelledError),
    )


class SDKL7FoundryAgentBackend:
    """Versioned Foundry prompt-agent adapter bound to canonical L6 OpenAPI."""

    def __init__(
        self,
        *,
        project_endpoint: str,
        credential: Any,
        reconciliation_timeout_seconds: float = 10.0,
        reconciliation_poll_seconds: float = 0.25,
    ) -> None:
        try:
            from azure.ai.projects import AIProjectClient
        except ImportError as exc:
            raise L7DeploymentError(
                "azure-ai-projects>=2.3.0 is required for L7 Foundry deployment"
            ) from exc
        try:
            self._project = AIProjectClient(
                endpoint=project_endpoint,
                credential=credential,
                allow_preview=True,
            )
        except (AzureError, TypeError, ValueError) as exc:
            raise L7DeploymentError(
                "Foundry project client construction failed"
            ) from exc
        if (
            reconciliation_timeout_seconds <= 0
            or reconciliation_poll_seconds <= 0
            or reconciliation_poll_seconds > reconciliation_timeout_seconds
        ):
            raise ValueError("Foundry reconciliation timing is invalid")
        self._reconciliation_timeout = reconciliation_timeout_seconds
        self._reconciliation_poll = reconciliation_poll_seconds
        self._pending_attempts: dict[str, tuple[str | None, str]] = {}

    @staticmethod
    def _value(value: Any, key: str, default: Any = "") -> Any:
        if isinstance(value, Mapping):
            return value.get(key, default)
        return getattr(value, key, default)

    @classmethod
    def _plain(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): cls._plain(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [cls._plain(item) for item in value]
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return cls._plain(model_dump(mode="json"))
        return value

    def _latest(self, agent_name: str) -> tuple[Any | None, str]:
        try:
            matches = tuple(
                agent
                for agent in self._project.agents.list()
                if str(self._value(agent, "name")) == agent_name
            )
        except AzureError as exc:
            raise L7DeploymentError("Foundry agent list failed") from exc
        if not matches:
            return None, ""
        if len(matches) != 1:
            raise L7DeploymentError("Foundry returned duplicate agent identities")
        agent = matches[0]
        versions = self._value(agent, "versions", {})
        latest = self._value(versions, "latest", {})
        latest_version = str(
            self._value(latest, "version")
            or self._value(agent, "version")
            or ""
        )
        if not latest_version:
            raise L7DeploymentError("Foundry agent omitted latest version identity")
        return agent, latest_version

    def _build_prompt_definition(
        self,
        *,
        config: L7DeploymentConfig,
        definition: L6CanonicalAgentDefinition,
    ) -> Any:
        try:
            from azure.ai.projects.models import (
                FabricDataAgentToolParameters,
                MicrosoftFabricPreviewTool,
                OpenApiFunctionDefinition,
                OpenApiProjectConnectionAuthDetails,
                OpenApiTool,
                PromptAgentDefinition,
                ToolProjectConnection,
            )
        except ImportError as exc:
            raise L7DeploymentError(
                "installed azure-ai-projects lacks required OpenAPI/Fabric tools"
            ) from exc
        from fabric_kg_builder.agent.l7_remote_tool import build_l6_openapi_spec

        remote_connection_id = str(
            definition["connections"]["l6_remote_tool"]["project_connection_id"]
        )
        fabric_connection_id = str(
            definition["connections"]["fabric_data_agent"][
                "project_connection_id"
            ]
        )
        auth = OpenApiProjectConnectionAuthDetails(
            security_scheme={
                "project_connection_id": remote_connection_id,
            }
        )
        openapi_tool = OpenApiTool(
            openapi=OpenApiFunctionDefinition(
                name="fabric_kg_canonical_l6",
                spec=build_l6_openapi_spec(
                    endpoint=config.remote_tool_endpoint,
                    max_body_bytes=config.remote_tool_max_body_bytes,
                    timeout_seconds=config.remote_tool_timeout_seconds,
                ),
                description=(
                    "Canonical L6 evidence tools with strict schemas and zero synthesis."
                ),
                auth=auth,
            )
        )
        fabric_tool = MicrosoftFabricPreviewTool(
            fabric_dataagent_preview=FabricDataAgentToolParameters(
                project_connections=[
                    ToolProjectConnection(
                        project_connection_id=fabric_connection_id
                    )
                ]
            )
        )
        return PromptAgentDefinition(
            model=config.model_deployment,
            instructions=str(definition["instructions"]),
            temperature=0.0,
            top_p=1.0,
            tools=[openapi_tool, fabric_tool],
        )

    def desired_hash(
        self,
        *,
        config: L7DeploymentConfig,
        definition: L6CanonicalAgentDefinition,
    ) -> str:
        return canonical_sha256(
            self._plain(
                self._build_prompt_definition(
                    config=config,
                    definition=definition,
                )
            )
        )

    def get(
        self,
        *,
        project_resource_id: str,
        agent_name: str,
    ) -> L7ResourceReadback:
        stable_id = f"{project_resource_id}/agents/{agent_name}"
        latest, latest_version = self._latest(agent_name)
        if latest is None:
            return L7ResourceReadback(
                resource_kind="foundry_agent",
                stable_id=stable_id,
                exists=False,
            )

        get_version = getattr(self._project.agents, "get_version", None)
        if not callable(get_version):
            raise L7DeploymentError(
                "installed Foundry SDK cannot read an exact agent version"
            )
        try:
            detail = get_version(
                agent_name=agent_name,
                agent_version=latest_version,
            )
        except AzureError as exc:
            raise L7DeploymentError("Foundry agent version GET failed") from exc
        metadata = self._value(detail, "metadata", {})
        if not isinstance(metadata, Mapping):
            metadata = {}
        actual_definition = self._value(detail, "definition", None)
        if actual_definition is None:
            raise L7DeploymentError(
                "Foundry exact agent version omitted its effective definition"
            )
        actual_definition_hash = canonical_sha256(
            self._plain(actual_definition)
        )
        return L7ResourceReadback(
            resource_kind="foundry_agent",
            stable_id=stable_id,
            exists=True,
            etag=latest_version or None,
            resource_type="PromptAgent",
            properties_hash=actual_definition_hash,
            definition_hash=(
                str(metadata.get("l6_definition_hash"))
                if metadata.get("l6_definition_hash")
                else None
            ),
        )

    def upsert(
        self,
        *,
        config: L7DeploymentConfig,
        definition: L6CanonicalAgentDefinition,
        expected_etag: str | None,
        create_only: bool,
    ) -> L7ResourceReadback:
        if type(definition) is not L6CanonicalAgentDefinition:
            raise L7DeploymentError("Foundry adapter requires canonical L6 definition")
        current = self.get(
            project_resource_id=config.foundry_project_resource_id,
            agent_name=definition.agent_name,
        )
        if create_only and current.exists:
            raise L7DeploymentError("Foundry agent appeared after planning")
        if not create_only and (
            not current.exists or current.etag != expected_etag
        ):
            raise L7DeploymentError("Foundry agent version changed after planning")
        prompt_definition = self._build_prompt_definition(
            config=config,
            definition=definition,
        )
        desired_hash = canonical_sha256(self._plain(prompt_definition))
        attempt_id = "op-sha256:" + secrets.token_hex(32)
        pending_attempts = getattr(self, "_pending_attempts", None)
        if pending_attempts is None:
            pending_attempts = {}
            self._pending_attempts = pending_attempts
        pending_attempts[definition.agent_name] = (
            current.etag,
            attempt_id,
        )
        try:
            created = self._project.agents.create_version(
                definition.agent_name,
                definition=prompt_definition,
                metadata={
                    "l6_definition_hash": definition.definition_hash,
                    "l6_instructions_hash": str(definition["instructions_hash"]),
                    "l6_toolset_version": str(definition["toolset_version"]),
                    "l7_attempt_id": attempt_id,
                },
                description="Fabric KG canonical L6 evidence-first agent",
            )
            version = str(self._value(created, "version") or "")
            if not version:
                raise L7DeploymentError(
                    "Foundry create_version omitted version identity"
                )
        except BaseException as exc:
            try:
                self._reconcile_uncertain_version(
                    config=config,
                    definition=definition,
                    previous_etag=current.etag,
                    attempt_id=attempt_id,
                )
            except BaseException as rollback_exc:
                _add_exception_note(
                    exc,
                    "uncertain Foundry version reconciliation failed: "
                    f"{type(rollback_exc).__name__}",
                )
            else:
                pending_attempts.pop(definition.agent_name, None)
            if _is_control_flow(exc):
                raise
            raise L7DeploymentError(
                "Foundry create_version outcome was reconciled"
            ) from exc
        try:
            readback = self.get(
                project_resource_id=config.foundry_project_resource_id,
                agent_name=definition.agent_name,
            )
            if (
                readback.etag != version
                or readback.properties_hash != desired_hash
                or readback.definition_hash != definition.definition_hash
                or not self._version_has_attempt(
                    agent_name=definition.agent_name,
                    agent_version=version,
                    attempt_id=attempt_id,
                )
            ):
                raise L7DeploymentError("Foundry agent exact readback failed")
        except BaseException as exc:
            try:
                self._project.agents.delete_version(
                    agent_name=definition.agent_name,
                    agent_version=version,
                )
            except BaseException as rollback_exc:
                _add_exception_note(
                    exc,
                    "conditional Foundry version rollback also failed: "
                    f"{type(rollback_exc).__name__}",
                )
            else:
                pending_attempts.pop(definition.agent_name, None)
            if _is_control_flow(exc):
                raise
            if isinstance(exc, L7DeploymentError):
                raise
            raise L7DeploymentError(
                "Foundry agent readback failed; created version was rolled back"
            ) from exc
        pending_attempts.pop(definition.agent_name, None)
        return readback

    def _reconcile_uncertain_version(
        self,
        *,
        config: L7DeploymentConfig,
        definition: L6CanonicalAgentDefinition,
        previous_etag: str | None,
        attempt_id: str,
    ) -> None:
        del config
        list_versions = getattr(self._project.agents, "list_versions", None)
        if not callable(list_versions):
            raise L7DeploymentError(
                "Foundry SDK cannot reconcile uncertain version ownership"
            )
        deadline = time.monotonic() + getattr(
            self,
            "_reconciliation_timeout",
            10.0,
        )
        matches: tuple[str, ...] = ()
        latest_version = ""
        last_error: BaseException | None = None
        while time.monotonic() < deadline:
            try:
                versions = tuple(list_versions(agent_name=definition.agent_name))
                matches = tuple(
                    str(self._value(item, "version") or "")
                    for item in versions
                    if isinstance(self._value(item, "metadata", {}), Mapping)
                    and self._value(item, "metadata", {}).get("l7_attempt_id")
                    == attempt_id
                )
                matches = tuple(version for version in matches if version)
                if not matches:
                    _, latest_version = self._latest(definition.agent_name)
                last_error = None
            except (
                AzureError,
                ConnectionError,
                TimeoutError,
            ) as exc:
                last_error = exc
            if matches:
                break
            time.sleep(
                min(
                    getattr(self, "_reconciliation_poll", 0.25),
                    max(0.0, deadline - time.monotonic()),
                )
            )
        if last_error is not None:
            raise L7DeploymentError(
                "uncertain Foundry version list failed throughout "
                "the reconciliation window"
            ) from last_error
        if len(matches) > 1:
            raise L7DeploymentError(
                "uncertain Foundry attempt identity is ambiguous"
            )
        if not matches:
            if latest_version == (previous_etag or "") or not latest_version:
                return
            raise L7DeploymentError(
                "uncertain Foundry attempt version was not found"
            )
        try:
            self._project.agents.delete_version(
                agent_name=definition.agent_name,
                agent_version=matches[0],
            )
        except AzureError as exc:
            raise L7DeploymentError(
                "uncertain Foundry version rollback failed"
            ) from exc

    def _version_has_attempt(
        self,
        *,
        agent_name: str,
        agent_version: str,
        attempt_id: str,
    ) -> bool:
        get_version = getattr(self._project.agents, "get_version", None)
        if not callable(get_version):
            return False
        try:
            detail = get_version(
                agent_name=agent_name,
                agent_version=agent_version,
            )
        except AzureError as exc:
            raise L7DeploymentError(
                "Foundry attempt metadata readback failed"
            ) from exc
        metadata = self._value(detail, "metadata", {})
        return bool(
            isinstance(metadata, Mapping)
            and metadata.get("l7_attempt_id") == attempt_id
        )

    def rollback_pending(
        self,
        *,
        config: L7DeploymentConfig,
        definition: L6CanonicalAgentDefinition,
    ) -> None:
        pending = getattr(self, "_pending_attempts", {}).get(
            definition.agent_name
        )
        if pending is None:
            return
        previous_etag, attempt_id = pending
        self._reconcile_uncertain_version(
            config=config,
            definition=definition,
            previous_etag=previous_etag,
            attempt_id=attempt_id,
        )
        self._pending_attempts.pop(definition.agent_name, None)

    def delete_created(
        self,
        *,
        config: L7DeploymentConfig,
        definition: L6CanonicalAgentDefinition,
        expected_etag: str,
    ) -> None:
        _, latest_version = self._latest(definition.agent_name)
        if latest_version != expected_etag:
            raise L7DeploymentError(
                "Foundry agent changed before conditional rollback"
            )
        try:
            self._project.agents.delete_version(
                agent_name=definition.agent_name,
                agent_version=expected_etag,
            )
        except AzureError as exc:
            raise L7DeploymentError(
                "Foundry conditional version rollback failed"
            ) from exc

    def restore_updated(
        self,
        *,
        config: L7DeploymentConfig,
        definition: L6CanonicalAgentDefinition,
        expected_etag: str,
    ) -> L7ResourceReadback:
        self.delete_created(
            config=config,
            definition=definition,
            expected_etag=expected_etag,
        )
        return self.get(
            project_resource_id=config.foundry_project_resource_id,
            agent_name=definition.agent_name,
        )


def _decode_access_token_claims(token: str) -> Mapping[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise L7DeploymentError("Azure credential returned a non-JWT access token")
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        value = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise L7DeploymentError("Azure access token claims are unreadable") from exc
    if not isinstance(value, dict):
        raise L7DeploymentError("Azure access token claims are invalid")
    return value


class AzureL7ReadOnlyProbe:
    """GET-only probe for identity, Fabric items, connections, and Foundry agents."""

    def __init__(
        self,
        *,
        config: L7DeploymentConfig,
        credential: Any,
        remote_probe_credential: Any,
        connection_client: FoundryProjectConnectionClient,
        foundry_backend: L7FoundryAgentBackend,
        ownership_authority: L7ConnectionOwnershipAuthority,
        session: Any | None = None,
    ) -> None:
        if session is None:
            import requests

            session = requests.Session()
        self._config = config
        self._credential = credential
        self._remote_probe_credential = remote_probe_credential
        self._connections = connection_client
        self._foundry = foundry_backend
        self._ownership = ownership_authority
        self._session = session

    def _token(self, scope: str) -> str:
        try:
            token = self._credential.get_token(scope).token
        except (HttpResponseError, ServiceRequestError, TimeoutError) as exc:
            raise L7DeploymentError("Azure credential token acquisition failed") from exc
        if not token:
            raise L7DeploymentError("Azure credential returned an empty token")
        return token

    def _remote_probe_token(self, scope: str) -> str:
        try:
            token = self._remote_probe_credential.get_token(scope).token
        except (HttpResponseError, ServiceRequestError, TimeoutError) as exc:
            raise L7DeploymentError(
                "Foundry caller readiness token acquisition failed"
            ) from exc
        if not token:
            raise L7DeploymentError(
                "Foundry caller readiness credential returned an empty token"
            )
        return token

    def current_identity(self) -> L7ObservedIdentity:
        token = self._token(_ARM_SCOPE)
        claims = _decode_access_token_claims(token)
        tenant_id = str(claims.get("tid") or "")
        principal_id = str(claims.get("oid") or claims.get("sub") or "")
        if not tenant_id or not principal_id:
            raise L7DeploymentError(
                "Azure credential token omits tenant or principal identity"
            )
        return L7ObservedIdentity(
            tenant_id=tenant_id,
            principal_id=principal_id,
        )

    def probe_remote_readiness(
        self,
        *,
        config: L7DeploymentConfig,
        definition: L6CanonicalAgentDefinition,
    ) -> L7RemoteReadinessObservation:
        scope = config.remote_tool_audience.rstrip("/") + "/.default"
        token = self._remote_probe_token(scope)
        claims = _decode_access_token_claims(token)
        tenant_id = str(claims.get("tid") or "")
        caller_object_id = str(claims.get("oid") or "")
        roles = claims.get("roles") or ()
        if (
            tenant_id.casefold() != config.tenant_id.casefold()
            or caller_object_id
            not in config.remote_tool_allowed_caller_object_ids
            or not isinstance(roles, (list, tuple))
            or config.remote_tool_required_app_role not in roles
        ):
            raise L7DeploymentError(
                "RemoteTool probe credential lacks exact tenant/caller/app role"
            )
        try:
            response = self._session.request(
                "GET",
                config.remote_tool_endpoint.rstrip("/") + "/ready",
                headers={"Authorization": f"Bearer {token}"},
                timeout=(5, 15),
            )
        except RequestException as exc:
            raise L7DeploymentError(
                "authenticated RemoteTool readiness request failed"
            ) from exc
        if response.status_code != 200:
            raise L7DeploymentError(
                f"authenticated RemoteTool readiness failed with HTTP "
                f"{response.status_code}"
            )
        try:
            observation = L7RemoteReadinessObservation.model_validate_json(
                response.text
            )
        except (ValueError, TypeError) as exc:
            raise L7DeploymentError(
                "RemoteTool readiness response is invalid"
            ) from exc
        from fabric_kg_builder.agent.l7_remote_tool import build_l6_openapi_spec

        expected_schema_hash = canonical_sha256(
            build_l6_openapi_spec(
                endpoint=config.remote_tool_endpoint,
                max_body_bytes=config.remote_tool_max_body_bytes,
                timeout_seconds=config.remote_tool_timeout_seconds,
            )
        )
        if (
            observation.endpoint != config.remote_tool_endpoint
            or observation.tenant_id.casefold() != config.tenant_id.casefold()
            or observation.audience != config.remote_tool_audience
            or observation.caller_object_id != caller_object_id
            or observation.app_role != config.remote_tool_required_app_role
            or observation.openapi_schema_hash != expected_schema_hash
            or observation.l6_definition_hash != definition.definition_hash
            or observation.authority_backend != "azure_blob"
            or observation.authority_version
            != config.l6_authority_backend_version
        ):
            raise L7DeploymentError(
                "RemoteTool readiness authority differs from deployment config"
            )
        return observation

    def probe_ownership_authority(
        self,
        *,
        config: L7DeploymentConfig,
    ) -> L7OwnershipAuthorityObservation:
        try:
            observation = self._ownership.observe()
        except (HttpResponseError, ServiceRequestError, TypeError, ValueError) as exc:
            raise L7DeploymentError(
                "Fabric connection ownership authority probe failed"
            ) from exc
        if (
            observation.authority_id
            != config.fabric_connection_ownership_authority_id
        ):
            raise L7DeploymentError(
                "Fabric connection ownership authority mismatch"
            )
        return observation

    def get_fabric_connection_ownership(
        self,
        *,
        config: L7DeploymentConfig,
        readback: L7ResourceReadback,
        data_agent_id: str,
    ) -> L7ConnectionOwnershipReceipt | None:
        if not readback.exists:
            return None
        try:
            receipt = self._ownership.read_verified(
                connection_id=readback.stable_id
            )
        except (HttpResponseError, ServiceRequestError, TypeError, ValueError) as exc:
            raise L7DeploymentError(
                "Fabric connection ownership receipt read failed"
            ) from exc
        if receipt is None:
            return None
        if (
            receipt.connection_etag != readback.etag
            or receipt.workspace_id != config.fabric_workspace_id
            or receipt.data_agent_id != data_agent_id
        ):
            raise L7DeploymentError(
                "Fabric connection ownership receipt is stale or mismatched"
            )
        return receipt

    def _fabric_request(
        self,
        method: str,
        path_or_url: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        token = self._token(_FABRIC_SCOPE)
        url = (
            path_or_url
            if path_or_url.startswith("https://")
            else f"{_FABRIC_BASE}{path_or_url}"
        )
        try:
            response = self._session.request(
                method,
                url,
                headers={"Authorization": f"Bearer {token}"},
                json=json_body,
                timeout=60,
            )
        except RequestException as exc:
            raise L7DeploymentError("Fabric readback request failed") from exc
        return response

    def _fabric_definition(
        self,
        *,
        workspace_id: str,
        item_id: str,
    ) -> dict[str, Any]:
        response = self._fabric_request(
            "POST",
            f"/workspaces/{workspace_id}/items/{item_id}/getDefinition",
            json_body={},
        )
        if response.status_code == 200:
            body = response.json()
            if not isinstance(body, dict):
                raise L7DeploymentError(
                    "Fabric definition readback returned invalid JSON"
                )
            return body
        if response.status_code != 202:
            raise L7DeploymentError(
                f"Fabric definition POST failed with HTTP {response.status_code}"
            )
        location = str(
            response.headers.get("Location")
            or response.headers.get("location")
            or ""
        )
        operation_url = urljoin(f"{_FABRIC_BASE}/", location)
        parsed = urlsplit(operation_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.fabric.microsoft.com"
        ):
            raise L7DeploymentError(
                "Fabric definition operation location is invalid"
            )
        operation: dict[str, Any] | None = None
        for _ in range(60):
            polled = self._fabric_request("GET", operation_url)
            if polled.status_code not in {200, 202}:
                raise L7DeploymentError(
                    "Fabric definition operation polling failed"
                )
            body = polled.json()
            if not isinstance(body, dict):
                raise L7DeploymentError(
                    "Fabric definition operation returned invalid JSON"
                )
            status = str(body.get("status") or "").casefold()
            if status == "succeeded":
                operation = body
                break
            if status in {"failed", "cancelled", "canceled"}:
                raise L7DeploymentError(
                    "Fabric definition operation failed"
                )
            retry_after = polled.headers.get("Retry-After", "1")
            try:
                wait_seconds = min(max(float(retry_after), 0.0), 10.0)
            except (TypeError, ValueError):
                wait_seconds = 1.0
            time.sleep(wait_seconds)
        if operation is None:
            raise L7DeploymentError(
                "Fabric definition operation exceeded bounded polling"
            )
        candidate = operation.get("definition")
        if isinstance(candidate, dict):
            return {"definition": candidate}
        result = operation.get("result")
        if isinstance(result, dict):
            candidate = result.get("definition")
            if isinstance(candidate, dict):
                return {"definition": candidate}
        result_url = (
            operation_url
            if operation_url.rstrip("/").endswith("/result")
            else operation_url.rstrip("/") + "/result"
        )
        result_response = self._fabric_request("GET", result_url)
        if result_response.status_code != 200:
            raise L7DeploymentError(
                "Fabric definition operation result is unavailable"
            )
        result_body = result_response.json()
        if not isinstance(result_body, dict):
            raise L7DeploymentError(
                "Fabric definition result returned invalid JSON"
            )
        return result_body

    def get_fabric_item(
        self,
        *,
        workspace_id: str,
        item: L7FabricItemTarget,
    ) -> L7ResourceReadback:
        stable_id = f"fabric:workspace/{workspace_id}/item/{item.item_id}"
        response = self._fabric_request(
            "GET",
            f"/workspaces/{workspace_id}/items/{item.item_id}"
        )
        if response.status_code == 404:
            return L7ResourceReadback(
                resource_kind="fabric_item",
                stable_id=stable_id,
                exists=False,
            )
        if response.status_code != 200:
            raise L7DeploymentError(
                f"Fabric item GET failed with HTTP {response.status_code}"
            )
        try:
            body = response.json()
        except (TypeError, ValueError) as exc:
            raise L7DeploymentError("Fabric item GET returned invalid JSON") from exc
        if (
            not isinstance(body, dict)
            or str(body.get("id") or "").casefold() != item.item_id.casefold()
            or str(body.get("workspaceId") or workspace_id).casefold()
            != workspace_id.casefold()
        ):
            raise L7DeploymentError("Fabric item stable ID readback mismatch")
        definition_hash = None
        try:
            definition_body = self._fabric_definition(
                workspace_id=workspace_id,
                item_id=item.item_id,
            )
        except (TypeError, ValueError) as exc:
            raise L7DeploymentError(
                "Fabric item definition readback returned invalid JSON"
            ) from exc
        definition_hash = canonical_sha256(definition_body)
        etag = str(
            response.headers.get("ETag")
            or response.headers.get("etag")
            or ""
        ) or None
        return L7ResourceReadback(
            resource_kind="fabric_item",
            stable_id=stable_id,
            exists=True,
            etag=etag,
            resource_type=str(body.get("type") or ""),
            properties_hash=canonical_sha256(
                {
                    "id": str(body.get("id") or ""),
                    "workspaceId": str(body.get("workspaceId") or workspace_id),
                    "type": str(body.get("type") or ""),
                    "displayName": str(body.get("displayName") or ""),
                }
            ),
            definition_hash=definition_hash,
        )

    def get_connection(self, *, resource_id: str) -> L7ResourceReadback:
        prefix = f"{self._config.foundry_project_resource_id}/connections/"
        if not resource_id.casefold().startswith(prefix.casefold()):
            raise L7DeploymentError("connection resource ID is outside the project")
        name = resource_id[len(prefix):]
        try:
            connection = self._connections.get(name)
        except (ProjectConnectionError, L7DeploymentError) as exc:
            raise L7DeploymentError("Foundry connection GET failed") from exc
        if connection is None:
            return L7ResourceReadback(
                resource_kind="foundry_connection",
                stable_id=resource_id,
                exists=False,
            )
        return L7ResourceReadback(
            resource_kind="foundry_connection",
            stable_id=connection.resource_id,
            exists=True,
            etag=connection.etag or None,
            resource_type=connection.category,
            properties_hash=connection.properties_hash,
        )

    def get_agent(
        self,
        *,
        project_resource_id: str,
        agent_name: str,
    ) -> L7ResourceReadback:
        try:
            return self._foundry.get(
                project_resource_id=project_resource_id,
                agent_name=agent_name,
            )
        except (
            HttpResponseError,
            ServiceRequestError,
            ConnectionError,
            TimeoutError,
            TypeError,
            ValueError,
        ) as exc:
            raise L7DeploymentError("Foundry agent GET failed") from exc

    def desired_agent_hash(
        self,
        *,
        config: L7DeploymentConfig,
        definition: L6CanonicalAgentDefinition,
    ) -> str:
        try:
            return self._foundry.desired_hash(
                config=config,
                definition=definition,
            )
        except (
            HttpResponseError,
            ServiceRequestError,
            ConnectionError,
            TimeoutError,
            TypeError,
            ValueError,
        ) as exc:
            raise L7DeploymentError(
                "Foundry desired agent definition failed"
            ) from exc


class AzureL7MutationAdapter:
    """Apply only approved connection/agent actions; Fabric is verification-only."""

    def __init__(
        self,
        *,
        probe: AzureL7ReadOnlyProbe,
        connection_client: FoundryProjectConnectionClient,
        foundry_backend: L7FoundryAgentBackend,
        ownership_authority: L7ConnectionOwnershipAuthority,
    ) -> None:
        self._probe = probe
        self._connections = connection_client
        self._foundry = foundry_backend
        self._ownership = ownership_authority
        self._definitions: dict[str, L6CanonicalAgentDefinition] = {}
        self._started_connections: dict[str, Any] = {}

    @staticmethod
    def _result(
        action: L7DeploymentAction,
        readback: L7ResourceReadback,
        *,
        ownership_receipt_hash: str | None = None,
    ) -> L7ResourceResult:
        if action.action in {"adopt", "verify"}:
            performed = "adopted" if action.action == "adopt" else "verified"
        else:
            performed = "created" if action.action == "create" else "updated"
        return L7ResourceResult(
            stable_id=action.stable_id,
            action=performed,
            before_etag=action.expected_etag,
            after_etag=readback.etag,
            readback_hash=canonical_sha256(readback.model_dump(mode="json")),
            ownership_receipt_hash=ownership_receipt_hash,
            rollback_status=(
                "pending" if action.rollback.action != "none" else "not_required"
            ),
        )

    @staticmethod
    def _require_approved_readback(
        action: L7DeploymentAction,
        readback: L7ResourceReadback,
    ) -> None:
        observed_hash = canonical_sha256(readback.model_dump(mode="json"))
        if observed_hash != action.expected_readback_hash:
            raise L7DeploymentError(
                f"{action.resource_kind} changed after final preflight"
            )

    def apply(
        self,
        action: L7DeploymentAction,
        *,
        config: L7DeploymentConfig,
        definition: L6CanonicalAgentDefinition,
    ) -> L7ResourceResult:
        if action.action == "unsupported":
            raise L7DeploymentError("unsupported action reached mutation phase")
        if action.resource_kind == "fabric_item":
            item = next(
                target
                for target in config.fabric_items
                if action.stable_id.endswith(f"/{target.item_id}")
            )
            readback = self._probe.get_fabric_item(
                workspace_id=config.fabric_workspace_id,
                item=item,
            )
            self._require_approved_readback(action, readback)
            return self._result(action, readback)
        if action.resource_kind in {
            "fabric_connection",
            "remote_tool_connection",
        }:
            if action.action == "adopt":
                readback = self._probe.get_connection(resource_id=action.stable_id)
                self._require_approved_readback(action, readback)
                ownership_receipt_hash = None
                if action.resource_kind == "fabric_connection":
                    receipt = self._ownership.read_verified(
                        connection_id=action.stable_id
                    )
                    if (
                        receipt is None
                        or receipt.connection_etag != readback.etag
                    ):
                        raise L7DeploymentError(
                            "Fabric connection ownership changed after preflight"
                        )
                    ownership_receipt_hash = receipt.receipt_hash
                return self._result(
                    action,
                    readback,
                    ownership_receipt_hash=ownership_receipt_hash,
                )
            ownership_receipt = None
            try:
                if action.resource_kind == "fabric_connection":
                    data_agent = next(
                        item
                        for item in config.fabric_items
                        if item.item_type == "DataAgent"
                    )
                    connection = self._connections.upsert_fabric_data_agent(
                        name=config.fabric_connection_name,
                        workspace_id=config.fabric_workspace_id,
                        data_agent_id=data_agent.item_id,
                        expected_etag=action.expected_etag,
                        create_only=action.action == "create",
                    )
                else:
                    connection = self._connections.upsert_remote_tool(
                        name=config.remote_tool_connection_name,
                        target=config.remote_tool_endpoint,
                        audience=config.remote_tool_audience,
                        expected_etag=action.expected_etag,
                        create_only=action.action == "create",
                    )
            except (
                ProjectConnectionError,
                HttpResponseError,
                ServiceRequestError,
                ConnectionError,
                TimeoutError,
                TypeError,
                ValueError,
            ) as exc:
                raise L7DeploymentError(
                    "Foundry connection mutation failed"
                ) from exc
            self._started_connections[action.stable_id] = connection
            if (
                action.resource_kind == "fabric_connection"
                and action.action == "create"
            ):
                data_agent = next(
                    item
                    for item in config.fabric_items
                    if item.item_type == "DataAgent"
                )
                try:
                    ownership_receipt = self._ownership.issue_attempt_created(
                        connection_id=connection.resource_id,
                        connection_etag=connection.etag,
                        workspace_id=config.fabric_workspace_id,
                        data_agent_id=data_agent.item_id,
                    )
                except BaseException as ownership_exc:
                    rollback_succeeded = False
                    if getattr(
                        ownership_exc,
                        "_l7_connection_rollback_safe",
                        True,
                    ):
                        try:
                            self._connections.delete_if_attempt_owned(
                                name=config.fabric_connection_name,
                                attempt_owned=True,
                                expected_etag=connection.etag,
                            )
                            rollback_succeeded = True
                        except BaseException as rollback_exc:
                            _add_exception_note(
                                ownership_exc,
                                "conditional connection rollback also failed: "
                                f"{type(rollback_exc).__name__}",
                            )
                    else:
                        _add_exception_note(
                            ownership_exc,
                            "connection retained because ownership receipt "
                            "cleanup is uncertain",
                        )
                    if rollback_succeeded:
                        self._started_connections.pop(
                            action.stable_id,
                            None,
                        )
                    if _is_control_flow(ownership_exc):
                        raise
                    raise L7DeploymentError(
                        "ownership receipt issuance failed"
                    ) from ownership_exc
            readback = L7ResourceReadback(
                resource_kind="foundry_connection",
                stable_id=connection.resource_id,
                exists=True,
                etag=connection.etag or None,
                resource_type=connection.category,
                properties_hash=connection.properties_hash,
            )
            result = self._result(
                action,
                readback,
                ownership_receipt_hash=(
                    ownership_receipt.receipt_hash
                    if ownership_receipt is not None
                    else None
                ),
            )
            self._started_connections.pop(action.stable_id, None)
            return result
        if action.resource_kind == "foundry_agent":
            self._definitions[action.stable_id] = definition
            try:
                if action.action == "adopt":
                    readback = self._probe.get_agent(
                        project_resource_id=config.foundry_project_resource_id,
                        agent_name=definition.agent_name,
                    )
                    self._require_approved_readback(action, readback)
                else:
                    readback = self._foundry.upsert(
                        config=config,
                        definition=definition,
                        expected_etag=action.expected_etag,
                        create_only=action.action == "create",
                    )
            except (
                HttpResponseError,
                ServiceRequestError,
                ConnectionError,
                TimeoutError,
                TypeError,
                ValueError,
            ) as exc:
                raise L7DeploymentError(
                    "Foundry agent mutation boundary failed"
                ) from exc
            if readback.properties_hash != action.desired_hash:
                raise L7DeploymentError("Foundry agent exact readback mismatch")
            return self._result(action, readback)
        raise L7DeploymentError("unknown L7 deployment action")

    def rollback(
        self,
        action: L7DeploymentAction,
        result: L7ResourceResult,
        *,
        config: L7DeploymentConfig,
    ) -> L7ResourceResult:
        if action.ownership == "preexisting" and action.action == "adopt":
            return result
        name = action.stable_id.rsplit("/", 1)[-1]
        if action.resource_kind in {
            "fabric_connection",
            "remote_tool_connection",
        }:
            try:
                if action.action == "create":
                    if action.resource_kind == "fabric_connection":
                        self._ownership.delete_attempt_created(
                            connection_id=action.stable_id,
                            connection_etag=result.after_etag or "",
                        )
                    self._connections.delete_if_attempt_owned(
                        name=name,
                        attempt_owned=True,
                        expected_etag=result.after_etag or "",
                    )
                else:
                    self._connections.restore_if_attempt_owned(
                        name=name,
                        attempt_owned=True,
                        expected_etag=result.after_etag or "",
                    )
            except (ProjectConnectionError, L7DeploymentError) as exc:
                raise L7DeploymentError(
                    "Foundry connection conditional rollback failed"
                ) from exc
        elif action.resource_kind == "foundry_agent":
            definition = self._definitions.get(action.stable_id)
            if definition is None:
                raise L7DeploymentError(
                    "Foundry rollback lacks canonical attempt authority"
                )
            try:
                if action.action == "create":
                    self._foundry.delete_created(
                        config=config,
                        definition=definition,
                        expected_etag=result.after_etag or "",
                    )
                else:
                    self._foundry.restore_updated(
                        config=config,
                        definition=definition,
                        expected_etag=result.after_etag or "",
                    )
            except (
                HttpResponseError,
                ServiceRequestError,
                ConnectionError,
                TimeoutError,
                TypeError,
                ValueError,
            ) as exc:
                raise L7DeploymentError(
                    "Foundry agent conditional rollback failed"
                ) from exc
        return result.model_copy(update={"rollback_status": "succeeded"})

    def verify_postconditions(
        self,
        *,
        config: L7DeploymentConfig,
        definition: L6CanonicalAgentDefinition,
        results: tuple[L7ResourceResult, ...],
    ) -> None:
        del definition
        connection_id = config.connection_resource_id(
            config.fabric_connection_name
        )
        matching = tuple(
            result for result in results if result.stable_id == connection_id
        )
        if len(matching) != 1:
            raise L7DeploymentError(
                "Fabric connection result is missing or ambiguous"
            )
        try:
            receipt = self._ownership.read_verified(
                connection_id=connection_id
            )
        except (
            HttpResponseError,
            ServiceRequestError,
            ConnectionError,
            TimeoutError,
            TypeError,
            ValueError,
        ) as exc:
            raise L7DeploymentError(
                "ownership postcondition read failed"
            ) from exc
        data_agent = next(
            item for item in config.fabric_items if item.item_type == "DataAgent"
        )
        if (
            receipt is None
            or receipt.connection_etag != matching[0].after_etag
            or receipt.receipt_hash
            != matching[0].ownership_receipt_hash
            or receipt.workspace_id != config.fabric_workspace_id
            or receipt.data_agent_id != data_agent.item_id
        ):
            raise L7DeploymentError(
                "Fabric connection ownership postcondition failed"
            )

    def rollback_started(
        self,
        action: L7DeploymentAction,
        *,
        config: L7DeploymentConfig,
        definition: L6CanonicalAgentDefinition,
    ) -> None:
        if action.resource_kind in {
            "fabric_connection",
            "remote_tool_connection",
        }:
            name = action.stable_id.rsplit("/", 1)[-1]
            started = self._started_connections.get(action.stable_id)
            try:
                if started is not None:
                    if action.action == "create":
                        if action.resource_kind == "fabric_connection":
                            self._ownership.delete_attempt_created(
                                connection_id=action.stable_id,
                                connection_etag=started.etag,
                            )
                        self._connections.delete_if_attempt_owned(
                            name=name,
                            attempt_owned=True,
                            expected_etag=started.etag,
                        )
                    else:
                        self._connections.restore_if_attempt_owned(
                            name=name,
                            attempt_owned=True,
                            expected_etag=started.etag,
                        )
                    self._started_connections.pop(action.stable_id, None)
                else:
                    self._connections.rollback_pending(name)
            except (ProjectConnectionError, L7DeploymentError) as exc:
                raise L7DeploymentError(
                    "started connection mutation reconciliation failed"
                ) from exc
            return
        if action.resource_kind == "foundry_agent":
            try:
                self._foundry.rollback_pending(
                    config=config,
                    definition=definition,
                )
            except (
                HttpResponseError,
                ServiceRequestError,
                ServiceResponseError,
                ConnectionError,
                TimeoutError,
                TypeError,
                ValueError,
            ) as exc:
                raise L7DeploymentError(
                    "started Foundry mutation reconciliation failed"
                ) from exc


def build_azure_l7_adapters(
    *,
    config: L7DeploymentConfig,
    foundry_backend: L7FoundryAgentBackend,
    ownership_authority: L7ConnectionOwnershipAuthority,
    remote_probe_credential: Any,
    credential: Any | None = None,
    request: Any | None = None,
    session: Any | None = None,
) -> tuple[AzureL7ReadOnlyProbe, AzureL7MutationAdapter]:
    if credential is None:
        from azure.identity import DefaultAzureCredential

        credential = DefaultAzureCredential(
            additionally_allowed_tenants=[config.tenant_id]
        )
    connections = FoundryProjectConnectionClient(
        subscription_id=config.subscription_id,
        resource_group=config.resource_group,
        account_name=config.foundry_account_name,
        project_name=config.foundry_project_name,
        tenant_id=config.tenant_id,
        credential=credential,
        transport=request,
    )
    probe = AzureL7ReadOnlyProbe(
        config=config,
        credential=credential,
        remote_probe_credential=remote_probe_credential,
        connection_client=connections,
        foundry_backend=foundry_backend,
        ownership_authority=ownership_authority,
        session=session,
    )
    return probe, AzureL7MutationAdapter(
        probe=probe,
        connection_client=connections,
        foundry_backend=foundry_backend,
        ownership_authority=ownership_authority,
    )


def build_default_azure_l7_adapters(
    config: L7DeploymentConfig,
) -> tuple[AzureL7ReadOnlyProbe, AzureL7MutationAdapter]:
    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential(
        additionally_allowed_tenants=[config.tenant_id]
    )
    backend = SDKL7FoundryAgentBackend(
        project_endpoint=config.foundry_project_endpoint,
        credential=credential,
    )
    remote_factory_path = os.environ.get(
        "FABRIC_KG_L7_REMOTE_PROBE_CREDENTIAL_FACTORY",
        "",
    )
    remote_module, remote_separator, remote_attribute = (
        remote_factory_path.partition(":")
    )
    if (
        not remote_separator
        or not remote_module
        or not remote_attribute
    ):
        raise L7DeploymentError(
            "FABRIC_KG_L7_REMOTE_PROBE_CREDENTIAL_FACTORY=module:callable "
            "is required to prove the actual Foundry caller identity"
        )
    try:
        remote_factory = getattr(
            importlib.import_module(remote_module),
            remote_attribute,
        )
        remote_probe_credential = remote_factory(
            config=config,
            credential=credential,
        )
    except (ImportError, AttributeError, TypeError) as exc:
        raise L7DeploymentError(
            "Foundry caller readiness credential factory failed"
        ) from exc
    factory_path = os.environ.get("FABRIC_KG_L7_OWNERSHIP_FACTORY", "")
    module_name, separator, attribute = factory_path.partition(":")
    if not separator or not module_name or not attribute:
        raise L7DeploymentError(
            "FABRIC_KG_L7_OWNERSHIP_FACTORY=module:callable is required; "
            "deployment is NO-GO without signed durable ownership authority"
        )
    try:
        factory = getattr(importlib.import_module(module_name), attribute)
        ownership_authority = factory(config=config, credential=credential)
    except (ImportError, AttributeError, TypeError) as exc:
        raise L7DeploymentError(
            "signed durable ownership authority factory failed"
        ) from exc
    return build_azure_l7_adapters(
        config=config,
        foundry_backend=backend,
        ownership_authority=ownership_authority,
        remote_probe_credential=remote_probe_credential,
        credential=credential,
    )
