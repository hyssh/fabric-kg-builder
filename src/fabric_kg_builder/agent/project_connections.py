"""Foundry project connection lifecycle with exact readback and CAS."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable, Literal, Mapping

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
    binding_hash: str = ""
    etag: str = ""
    properties_hash: str = ""
    action: Literal["created", "updated", "adopted", "read"] = "read"


class FoundryProjectConnectionClient:
    """Manage project connections through ARM without persisting credentials."""

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
        transport: Callable[..., Any] | None = None,
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
        self._legacy_put_request = request
        self._transport = transport
        self._read_properties: dict[str, dict[str, Any]] = {}
        self._rollback_properties: dict[str, dict[str, Any]] = {}

    def connection_id(self, name: str) -> str:
        if not name or "/" in name:
            raise ValueError("connection name must be a non-empty ARM path segment")
        return (
            f"/subscriptions/{self.subscription_id}"
            f"/resourceGroups/{self.resource_group}"
            f"/providers/Microsoft.CognitiveServices/accounts/{self.account_name}"
            f"/projects/{self.project_name}/connections/{name}"
        )

    def _authorization_headers(self) -> dict[str, str]:
        if self._credential is None:
            from azure.identity import DefaultAzureCredential

            credential_kwargs = (
                {"additionally_allowed_tenants": [self.tenant_id]}
                if self.tenant_id
                else {}
            )
            self._credential = DefaultAzureCredential(**credential_kwargs)
        credential_tenant = str(
            getattr(self._credential, "tenant_id", "")
            or getattr(self._credential, "_tenant_id", "")
        )
        if (
            self.tenant_id
            and credential_tenant
            and credential_tenant.casefold() != self.tenant_id.casefold()
        ):
            raise ProjectConnectionError(
                "credential tenant differs from the configured deployment tenant"
            )
        token = self._credential.get_token(_MANAGEMENT_SCOPE).token
        if not token:
            raise ProjectConnectionError(
                "credential returned an empty Azure Resource Manager token"
            )
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _send(
        self,
        method: str,
        resource_id: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        if self._transport is None and (
            self._legacy_put_request is None
        ):
            import requests

            sender = requests.request
        elif self._transport is not None:
            sender = self._transport
        else:
            sender = self._legacy_put_request
        request_headers = self._authorization_headers()
        request_headers.update(dict(headers or {}))
        url = (
            f"https://management.azure.com{resource_id}"
            f"?api-version={_API_VERSION}"
        )
        if sender is self._legacy_put_request:
            return sender(
                url,
                method=method,
                headers=request_headers,
                json=json_body,
                timeout=60,
            )
        return sender(
            method,
            url,
            headers=request_headers,
            json=json_body,
            timeout=60,
        )

    @staticmethod
    def _response_json(response: Any, operation: str) -> dict[str, Any]:
        try:
            body = response.json()
        except (TypeError, ValueError) as exc:
            raise ProjectConnectionError(
                f"{operation} returned a non-JSON response"
            ) from exc
        if not isinstance(body, dict):
            raise ProjectConnectionError(f"{operation} returned an invalid response")
        return body

    @staticmethod
    def _desired_hash(properties: Mapping[str, Any]) -> tuple[str, str]:
        credentials = properties.get("credentials")
        keys = (
            credentials.get("keys")
            if isinstance(credentials, Mapping)
            else None
        )
        binding_hash = ""
        if isinstance(keys, Mapping):
            binding_hash = canonical_sha256(
                {
                    "workspace_id": str(keys.get("workspace-id") or ""),
                    "data_agent_id": str(keys.get("artifact-id") or ""),
                }
            )
        metadata = (
            dict(properties.get("metadata"))
            if isinstance(properties.get("metadata"), Mapping)
            else {}
        )
        committed_binding_hash = str(metadata.get("bindingHash") or "")
        if not binding_hash and re.fullmatch(
            r"[0-9a-f]{64}",
            committed_binding_hash,
        ):
            binding_hash = committed_binding_hash
        return (
            canonical_sha256(
                {
                    "authType": str(properties.get("authType") or ""),
                    "category": str(properties.get("category") or ""),
                    "group": str(properties.get("group") or ""),
                    "target": str(properties.get("target") or ""),
                    "isSharedToAll": bool(
                        properties.get("isSharedToAll", False)
                    ),
                    "audience": str(properties.get("audience") or ""),
                    "metadata": metadata,
                    "binding_hash": binding_hash,
                }
            ),
            binding_hash,
        )

    def _parse(
        self,
        *,
        name: str,
        resource_id: str,
        body: Mapping[str, Any],
        response: Any,
        action: Literal["created", "updated", "adopted", "read"],
    ) -> ProjectConnection:
        properties = body.get("properties")
        if not isinstance(properties, Mapping):
            raise ProjectConnectionError(
                f"Foundry project connection '{name}' omitted properties"
            )
        stable_id = str(body.get("id") or resource_id)
        if stable_id.casefold() != resource_id.casefold():
            raise ProjectConnectionError(
                f"Foundry project connection '{name}' stable ID mismatch"
            )
        properties_hash, binding_hash = self._desired_hash(properties)
        etag = str(
            body.get("etag")
            or getattr(response, "headers", {}).get("ETag")
            or getattr(response, "headers", {}).get("etag")
            or ""
        )
        return ProjectConnection(
            name=name,
            resource_id=resource_id,
            category=str(properties.get("category") or ""),
            target=str(properties.get("target") or ""),
            audience=str(properties.get("audience") or ""),
            binding_hash=binding_hash,
            etag=etag,
            properties_hash=properties_hash,
            action=action,
        )

    def get(self, name: str) -> ProjectConnection | None:
        resource_id = self.connection_id(name)
        response = self._send("GET", resource_id)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise ProjectConnectionError(
                f"Foundry project connection GET failed with HTTP "
                f"{response.status_code}"
            )
        body = self._response_json(response, "connection GET")
        properties = body.get("properties")
        if isinstance(properties, Mapping):
            self._read_properties[name] = dict(properties)
        return self._parse(
            name=name,
            resource_id=resource_id,
            body=body,
            response=response,
            action="read",
        )

    def _put(
        self,
        name: str,
        properties: dict[str, Any],
        *,
        expected_etag: str | None = None,
        create_only: bool = False,
    ) -> ProjectConnection:
        resource_id = self.connection_id(name)
        desired_hash, _ = self._desired_hash(properties)
        existing = self.get(name)
        if create_only and existing is not None:
            raise ProjectConnectionError(
                f"Foundry project connection '{name}' appeared since planning"
            )
        if existing is not None and existing.properties_hash == desired_hash:
            return ProjectConnection(
                **{
                    **existing.__dict__,
                    "action": "adopted",
                }
            )
        if existing is not None:
            self._rollback_properties[name] = dict(
                self._read_properties.get(name, {})
            )
            rollback_credentials = self._rollback_properties[name].get(
                "credentials"
            )
            if (
                properties.get("authType") == "CustomKeys"
                and not isinstance(rollback_credentials, Mapping)
            ):
                raise ProjectConnectionError(
                    f"Foundry project connection '{name}' has redacted "
                    "credentials and cannot be updated with safe rollback"
                )
            if not expected_etag or existing.etag != expected_etag:
                raise ProjectConnectionError(
                    f"Foundry project connection '{name}' changed since planning"
                )
            action: Literal["created", "updated"] = "updated"
            conditional_headers = {"If-Match": expected_etag}
        else:
            if expected_etag:
                raise ProjectConnectionError(
                    f"Foundry project connection '{name}' disappeared since planning"
                )
            action = "created"
            conditional_headers = {"If-None-Match": "*"}
        response = self._send(
            "PUT",
            resource_id,
            headers=conditional_headers,
            json_body={"name": name, "properties": properties},
        )
        if response.status_code not in (200, 201):
            raise ProjectConnectionError(
                f"Foundry project connection '{name}' failed with HTTP "
                f"{response.status_code}"
            )
        mutation_body = self._response_json(response, "connection PUT")
        after_etag = str(
            mutation_body.get("etag")
            or getattr(response, "headers", {}).get("ETag")
            or getattr(response, "headers", {}).get("etag")
            or ""
        )
        if not after_etag:
            raise ProjectConnectionError(
                f"Foundry project connection '{name}' mutation omitted ETag"
            )
        try:
            readback = self.get(name)
            if (
                readback is None
                or readback.category != str(properties["category"])
                or readback.target != str(properties.get("target", ""))
                or readback.audience != str(properties.get("audience", ""))
                or readback.properties_hash != desired_hash
                or readback.etag != after_etag
            ):
                raise ProjectConnectionError(
                    f"Foundry project connection '{name}' readback mismatch"
                )
        except ProjectConnectionError as exc:
            if action == "created":
                rollback = self._send(
                    "DELETE",
                    resource_id,
                    headers={"If-Match": after_etag},
                )
            else:
                previous = self._rollback_properties.get(name)
                if not previous:
                    raise ProjectConnectionError(
                        f"Foundry project connection '{name}' lacks rollback state"
                    ) from exc
                rollback = self._send(
                    "PUT",
                    resource_id,
                    headers={"If-Match": after_etag},
                    json_body={"name": name, "properties": previous},
                )
            if rollback.status_code not in (200, 201, 202, 204):
                raise ProjectConnectionError(
                    f"Foundry project connection '{name}' readback and rollback failed"
                ) from exc
            raise
        return ProjectConnection(
            **{
                **readback.__dict__,
                "action": action,
            }
        )

    def upsert_fabric_data_agent(
        self,
        *,
        name: str,
        workspace_id: str,
        data_agent_id: str,
        expected_etag: str | None = None,
        create_only: bool = False,
    ) -> ProjectConnection:
        """Create the CustomKeys connection required by MicrosoftFabricPreviewTool."""
        if not workspace_id or not data_agent_id:
            raise ValueError("workspace_id and data_agent_id are required.")
        binding_hash = canonical_sha256(
            {
                "workspace_id": workspace_id,
                "data_agent_id": data_agent_id,
            }
        )
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
                    "bindingHash": binding_hash,
                },
            },
            expected_etag=expected_etag,
            create_only=create_only,
        )

    def upsert_remote_tool(
        self,
        *,
        name: str,
        target: str,
        audience: str,
        expected_etag: str | None = None,
        create_only: bool = False,
    ) -> ProjectConnection:
        """Create a managed-identity RemoteTool connection for an HTTPS endpoint."""
        if not target.startswith("https://"):
            raise ValueError("RemoteTool target must be an HTTPS URL.")
        if not audience:
            raise ValueError("RemoteTool audience is required.")
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
            expected_etag=expected_etag,
            create_only=create_only,
        )

    def delete_if_attempt_owned(
        self,
        *,
        name: str,
        attempt_owned: bool,
        expected_etag: str,
    ) -> bool:
        if not attempt_owned:
            return False
        current = self.get(name)
        if current is None:
            return True
        if current.etag != expected_etag:
            raise ProjectConnectionError(
                f"Foundry project connection '{name}' changed before rollback"
            )
        response = self._send(
            "DELETE",
            self.connection_id(name),
            headers={"If-Match": expected_etag},
        )
        if response.status_code not in (200, 202, 204, 404):
            raise ProjectConnectionError(
                f"Foundry project connection rollback failed with HTTP "
                f"{response.status_code}"
            )
        return True

    def restore_if_attempt_owned(
        self,
        *,
        name: str,
        previous_properties: dict[str, Any] | None = None,
        attempt_owned: bool,
        expected_etag: str,
    ) -> ProjectConnection | None:
        if not attempt_owned:
            return None
        restore_properties = (
            previous_properties
            if previous_properties is not None
            else self._rollback_properties.get(name)
        )
        if not restore_properties:
            raise ProjectConnectionError(
                f"Foundry project connection '{name}' lacks rollback state"
            )
        return self._put(
            name,
            restore_properties,
            expected_etag=expected_etag,
        )
