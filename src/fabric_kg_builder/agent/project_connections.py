"""Foundry project connection lifecycle with exact non-secret readback."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from fabric_kg_builder.contracts.base import canonical_sha256


_MANAGEMENT_SCOPE = "https://management.azure.com/.default"
_API_VERSION = "2025-04-01-preview"


class ProjectConnectionError(RuntimeError):
    """Raised when a Foundry project connection operation fails closed."""


@dataclass(frozen=True)
class ProjectConnection:
    name: str
    resource_id: str
    category: str
    target: str
    audience: str = ""
    etag: str = ""
    properties_hash: str = ""
    binding_hash: str = ""


class FoundryProjectConnectionClient:
    """Manage project connections without logging or persisting access tokens."""

    def __init__(
        self,
        *,
        subscription_id: str,
        resource_group: str,
        account_name: str,
        project_name: str,
        tenant_id: str | None = None,
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
        self.tenant_id = tenant_id
        self._credential = credential
        self._request = request

    def connection_id(self, name: str) -> str:
        if not name or "/" in name:
            raise ValueError("connection name must be one ARM path segment.")
        return (
            f"/subscriptions/{self.subscription_id}"
            f"/resourceGroups/{self.resource_group}"
            f"/providers/Microsoft.CognitiveServices/accounts/{self.account_name}"
            f"/projects/{self.project_name}/connections/{name}"
        )

    def _headers(self) -> dict[str, str]:
        if self._credential is None:
            from azure.identity import DefaultAzureCredential

            kwargs = (
                {"additionally_allowed_tenants": [self.tenant_id]}
                if self.tenant_id
                else {}
            )
            self._credential = DefaultAzureCredential(**kwargs)
        token = self._credential.get_token(_MANAGEMENT_SCOPE).token
        if not token:
            raise ProjectConnectionError("Azure credential returned an empty ARM token.")
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _send(
        self,
        method: str,
        resource_id: str,
        *,
        body: dict[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        url = f"https://management.azure.com{resource_id}?api-version={_API_VERSION}"
        request_headers = self._headers()
        request_headers.update(dict(headers or {}))
        try:
            if self._request is None:
                import requests

                return requests.request(
                    method,
                    url,
                    headers=request_headers,
                    json=body,
                    timeout=60,
                )
            try:
                return self._request(
                    method,
                    url,
                    headers=request_headers,
                    json=body,
                    timeout=60,
                )
            except TypeError:
                if method != "PUT":
                    raise
                return self._request(
                    url,
                    headers=request_headers,
                    json=body,
                    timeout=60,
                )
        except Exception as exc:
            raise ProjectConnectionError(
                f"Foundry project connection {method} transport failed."
            ) from exc

    @staticmethod
    def _parse(name: str, resource_id: str, response: Any) -> ProjectConnection:
        try:
            body = response.json()
        except (TypeError, ValueError) as exc:
            raise ProjectConnectionError(
                f"Foundry project connection '{name}' returned non-JSON."
            ) from exc
        if not isinstance(body, Mapping):
            raise ProjectConnectionError(
                f"Foundry project connection '{name}' returned an invalid body."
            )
        if str(body.get("id") or resource_id).casefold() != resource_id.casefold():
            raise ProjectConnectionError(
                f"Foundry project connection '{name}' stable ID mismatch."
            )
        properties = body.get("properties")
        if not isinstance(properties, Mapping):
            raise ProjectConnectionError(
                f"Foundry project connection '{name}' omitted properties."
            )
        return ProjectConnection(
            name=name,
            resource_id=resource_id,
            category=str(properties.get("category") or ""),
            target=str(properties.get("target") or ""),
            audience=str(properties.get("audience") or ""),
            etag=str(
                body.get("etag")
                or getattr(response, "headers", {}).get("ETag")
                or ""
            ),
            properties_hash=canonical_sha256(dict(properties)),
            binding_hash=str(
                (
                    properties.get("metadata")
                    if isinstance(properties.get("metadata"), Mapping)
                    else {}
                ).get("bindingHash")
                or ""
            ),
        )

    def get(self, name: str) -> ProjectConnection | None:
        resource_id = self.connection_id(name)
        response = self._send("GET", resource_id)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise ProjectConnectionError(
                f"Foundry project connection '{name}' GET failed with HTTP "
                f"{response.status_code}."
            )
        return self._parse(name, resource_id, response)

    def delete_created(self, name: str, *, expected_etag: str) -> None:
        """Conditionally delete only an exact attempt-created connection."""
        if not expected_etag:
            raise ProjectConnectionError("conditional delete requires an ETag.")
        resource_id = self.connection_id(name)
        response = self._send(
            "DELETE", resource_id, headers={"If-Match": expected_etag}
        )
        if response.status_code not in (200, 202, 204, 404):
            raise ProjectConnectionError(
                f"Foundry project connection '{name}' delete failed with HTTP "
                f"{response.status_code}."
            )
        if response.status_code == 404:
            return
        import time

        for _ in range(30):
            if self.get(name) is None:
                return
            time.sleep(1)
        raise ProjectConnectionError(
            f"Foundry project connection '{name}' delete readback timed out."
        )

    def _put(
        self,
        name: str,
        properties: dict[str, Any],
        *,
        create_only: bool = False,
    ) -> ProjectConnection:
        existing = self.get(name)
        if create_only and existing is not None:
            raise ProjectConnectionError(
                f"Foundry project connection '{name}' already exists; "
                "release-owned connection adoption is forbidden."
            )
        resource_id = self.connection_id(name)
        response = self._send(
            "PUT",
            resource_id,
            body={"name": name, "properties": properties},
            headers={"If-None-Match": "*"} if create_only else None,
        )
        if response.status_code not in (200, 201):
            raise ProjectConnectionError(
                f"Foundry project connection '{name}' failed with HTTP "
                f"{response.status_code}."
            )
        mutation_etag = str(
            getattr(response, "headers", {}).get("ETag") or ""
        )
        try:
            mutation = self._parse(name, resource_id, response)
            parsed = self.get(name)
            if parsed is None:
                raise ProjectConnectionError(
                    f"Foundry project connection '{name}' disappeared after mutation."
                )
            metadata = (
                properties.get("metadata")
                if isinstance(properties.get("metadata"), Mapping)
                else {}
            )
            if (
                parsed.category != str(properties["category"])
                or parsed.target != str(properties.get("target", ""))
                or parsed.audience != str(properties.get("audience", ""))
                or parsed.binding_hash != str(metadata.get("bindingHash") or "")
            ):
                raise ProjectConnectionError(
                    f"Foundry project connection '{name}' readback mismatch."
                )
            if mutation.etag and parsed.etag and mutation.etag != parsed.etag:
                raise ProjectConnectionError(
                    f"Foundry project connection '{name}' ETag readback mismatch."
                )
            return parsed
        except ProjectConnectionError:
            if create_only:
                observed = self.get(name)
                etag = (observed.etag if observed else "") or mutation_etag
                if observed is not None and etag:
                    self.delete_created(name, expected_etag=etag)
            raise

    def upsert_fabric_data_agent(
        self,
        *,
        name: str,
        workspace_id: str,
        data_agent_id: str,
        create_only: bool = False,
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
                "metadata": {
                    "type": "fabric_dataagent_preview",
                    "bindingHash": canonical_sha256(
                        {
                            "workspace_id": workspace_id,
                            "data_agent_id": data_agent_id,
                        }
                    ),
                },
            },
            create_only=create_only,
        )

    def upsert_search(
        self,
        *,
        name: str,
        endpoint: str,
        create_only: bool = False,
    ) -> ProjectConnection:
        """Create a managed-identity Azure AI Search project connection."""
        if not endpoint.startswith("https://"):
            raise ValueError("Search target must be an HTTPS URL.")
        return self._put(
            name,
            {
                "authType": "ProjectManagedIdentity",
                "category": "CognitiveSearch",
                "target": endpoint.rstrip("/"),
                "isSharedToAll": True,
            },
            create_only=create_only,
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
