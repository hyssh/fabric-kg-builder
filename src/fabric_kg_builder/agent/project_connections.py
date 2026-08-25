"""Foundry project connection lifecycle for grounded agent tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


_MANAGEMENT_SCOPE = "https://management.azure.com/.default"
_API_VERSION = "2025-04-01-preview"


class ProjectConnectionError(RuntimeError):
    """Raised when a Foundry project connection cannot be created."""


@dataclass(frozen=True)
class ProjectConnection:
    name: str
    resource_id: str
    category: str
    target: str


class FoundryProjectConnectionClient:
    """Create idempotent project connections through the ARM REST API."""

    def __init__(
        self,
        *,
        subscription_id: str,
        resource_group: str,
        account_name: str,
        project_name: str,
        credential: Any | None = None,
        request: Callable[..., Any] | None = None,
    ) -> None:
        if not all((subscription_id, resource_group, account_name, project_name)):
            raise ValueError(
                "subscription_id, resource_group, account_name, and project_name "
                "are required."
            )
        self.subscription_id = subscription_id
        self.resource_group = resource_group
        self.account_name = account_name
        self.project_name = project_name
        self._credential = credential
        self._request = request

    def connection_id(self, name: str) -> str:
        return (
            f"/subscriptions/{self.subscription_id}"
            f"/resourceGroups/{self.resource_group}"
            f"/providers/Microsoft.CognitiveServices/accounts/{self.account_name}"
            f"/projects/{self.project_name}/connections/{name}"
        )

    def _put(self, name: str, properties: dict[str, Any]) -> ProjectConnection:
        if self._credential is None:
            from azure.identity import DefaultAzureCredential

            from fabric_kg_builder.azure_identity import (
                default_azure_credential,
            )

            self._credential = default_azure_credential()
        if self._request is None:
            import requests

            self._request = requests.put

        resource_id = self.connection_id(name)
        token = self._credential.get_token(_MANAGEMENT_SCOPE).token
        response = self._request(
            f"https://management.azure.com{resource_id}?api-version={_API_VERSION}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"name": name, "properties": properties},
            timeout=60,
        )
        if response.status_code not in (200, 201):
            body = getattr(response, "text", "")
            raise ProjectConnectionError(
                f"Foundry project connection '{name}' failed with HTTP "
                f"{response.status_code}: {str(body)[:500]}"
            )
        return ProjectConnection(
            name=name,
            resource_id=resource_id,
            category=str(properties["category"]),
            target=str(properties.get("target", "")),
        )

    def upsert_fabric_data_agent(
        self,
        *,
        name: str,
        workspace_id: str,
        data_agent_id: str,
    ) -> ProjectConnection:
        """Create the CustomKeys connection required by MicrosoftFabricPreviewTool."""
        if not workspace_id or not data_agent_id:
            raise ValueError("workspace_id and data_agent_id are required.")
        return self._put(
            name,
            {
                "authType": "CustomKeys",
                "category": "CustomKeys",
                "group": "AzureAI",
                "target": "-",
                "isSharedToAll": True,
                "credentials": {
                    "keys": {
                        "workspace-id": workspace_id,
                        "artifact-id": data_agent_id,
                    }
                },
                "metadata": {"type": "fabric_dataagent_preview"},
            },
        )

    def upsert_remote_tool(
        self,
        *,
        name: str,
        target: str,
        audience: str,
    ) -> ProjectConnection:
        """Create a managed-identity RemoteTool connection for an MCP endpoint."""
        if not target.startswith("https://"):
            raise ValueError("RemoteTool target must be an HTTPS URL.")
        return self._put(
            name,
            {
                "authType": "ProjectManagedIdentity",
                "category": "RemoteTool",
                "target": target,
                "isSharedToAll": True,
                "audience": audience,
                "metadata": {"ApiType": "Azure"},
            },
        )
