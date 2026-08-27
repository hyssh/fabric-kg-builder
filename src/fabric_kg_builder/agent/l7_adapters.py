"""Azure/Fabric production adapters for the L7 deployment authority."""

from __future__ import annotations

import base64
import json
import time
from typing import Any, Mapping, Protocol
from urllib.parse import urljoin, urlsplit

from azure.core.exceptions import HttpResponseError
from requests import RequestException

from fabric_kg_builder.agent.l6_integration import L6CanonicalAgentDefinition
from fabric_kg_builder.agent.l7_deployment import (
    L7DeploymentAction,
    L7DeploymentConfig,
    L7DeploymentError,
    L7FabricItemTarget,
    L7ObservedIdentity,
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


class SDKL7FoundryAgentBackend:
    """Versioned Foundry prompt-agent adapter bound to canonical L6 OpenAPI."""

    def __init__(self, *, project_endpoint: str, credential: Any) -> None:
        try:
            from azure.ai.projects import AIProjectClient
        except ImportError as exc:
            raise L7DeploymentError(
                "azure-ai-projects>=2.3.0 is required for L7 Foundry deployment"
            ) from exc
        self._project = AIProjectClient(
            endpoint=project_endpoint,
            credential=credential,
            allow_preview=True,
        )

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
        except HttpResponseError as exc:
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
                    endpoint=config.remote_tool_endpoint
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
        except HttpResponseError as exc:
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
        try:
            created = self._project.agents.create_version(
                definition.agent_name,
                definition=prompt_definition,
                metadata={
                    "l6_definition_hash": definition.definition_hash,
                    "l6_instructions_hash": str(definition["instructions_hash"]),
                    "l6_toolset_version": str(definition["toolset_version"]),
                },
                description="Fabric KG canonical L6 evidence-first agent",
            )
        except HttpResponseError as exc:
            raise L7DeploymentError("Foundry create_version failed") from exc
        version = str(self._value(created, "version") or "")
        if not version:
            raise L7DeploymentError("Foundry create_version omitted version identity")
        try:
            readback = self.get(
                project_resource_id=config.foundry_project_resource_id,
                agent_name=definition.agent_name,
            )
            if (
                readback.etag != version
                or readback.properties_hash != desired_hash
                or readback.definition_hash != definition.definition_hash
            ):
                raise L7DeploymentError("Foundry agent exact readback failed")
        except (L7DeploymentError, HttpResponseError, TypeError, ValueError) as exc:
            try:
                self._project.agents.delete_version(
                    agent_name=definition.agent_name,
                    agent_version=version,
                )
            except HttpResponseError as rollback_exc:
                raise L7DeploymentError(
                    "Foundry agent readback and conditional rollback failed"
                ) from rollback_exc
            raise L7DeploymentError(
                "Foundry agent readback failed; created version was rolled back"
            ) from exc
        return readback

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
        except HttpResponseError as exc:
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
        connection_client: FoundryProjectConnectionClient,
        foundry_backend: L7FoundryAgentBackend,
        session: Any | None = None,
    ) -> None:
        if session is None:
            import requests

            session = requests.Session()
        self._config = config
        self._credential = credential
        self._connections = connection_client
        self._foundry = foundry_backend
        self._session = session

    def current_identity(self) -> L7ObservedIdentity:
        token = self._credential.get_token(_ARM_SCOPE).token
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

    def _fabric_request(
        self,
        method: str,
        path_or_url: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        token = self._credential.get_token(_FABRIC_SCOPE).token
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
        if item.definition_hash:
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
        except ProjectConnectionError as exc:
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
        return self._foundry.get(
            project_resource_id=project_resource_id,
            agent_name=agent_name,
        )

    def desired_agent_hash(
        self,
        *,
        config: L7DeploymentConfig,
        definition: L6CanonicalAgentDefinition,
    ) -> str:
        return self._foundry.desired_hash(
            config=config,
            definition=definition,
        )


class AzureL7MutationAdapter:
    """Apply only approved connection/agent actions; Fabric is verification-only."""

    def __init__(
        self,
        *,
        probe: AzureL7ReadOnlyProbe,
        connection_client: FoundryProjectConnectionClient,
        foundry_backend: L7FoundryAgentBackend,
    ) -> None:
        self._probe = probe
        self._connections = connection_client
        self._foundry = foundry_backend
        self._definitions: dict[str, L6CanonicalAgentDefinition] = {}

    @staticmethod
    def _result(
        action: L7DeploymentAction,
        readback: L7ResourceReadback,
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
                return self._result(action, readback)
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
            except ProjectConnectionError as exc:
                raise L7DeploymentError(
                    "Foundry connection mutation failed"
                ) from exc
            readback = L7ResourceReadback(
                resource_kind="foundry_connection",
                stable_id=connection.resource_id,
                exists=True,
                etag=connection.etag or None,
                resource_type=connection.category,
                properties_hash=connection.properties_hash,
            )
            return self._result(action, readback)
        if action.resource_kind == "foundry_agent":
            self._definitions[action.stable_id] = definition
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
            except ProjectConnectionError as exc:
                raise L7DeploymentError(
                    "Foundry connection conditional rollback failed"
                ) from exc
        elif action.resource_kind == "foundry_agent":
            definition = self._definitions.get(action.stable_id)
            if definition is None:
                raise L7DeploymentError(
                    "Foundry rollback lacks canonical attempt authority"
                )
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
        return result.model_copy(update={"rollback_status": "succeeded"})


def build_azure_l7_adapters(
    *,
    config: L7DeploymentConfig,
    foundry_backend: L7FoundryAgentBackend,
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
        connection_client=connections,
        foundry_backend=foundry_backend,
        session=session,
    )
    return probe, AzureL7MutationAdapter(
        probe=probe,
        connection_client=connections,
        foundry_backend=foundry_backend,
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
    return build_azure_l7_adapters(
        config=config,
        foundry_backend=backend,
        credential=credential,
    )
