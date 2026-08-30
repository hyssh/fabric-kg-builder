"""Foundry project connection lifecycle with exact non-secret readback."""

from __future__ import annotations

from dataclasses import dataclass
import inspect
import secrets
import time
from typing import Any, Callable, Mapping
from urllib.parse import urljoin, urlsplit

from fabric_kg_builder.contracts.base import canonical_sha256


_MANAGEMENT_SCOPE = "https://management.azure.com/.default"
_API_VERSION = "2025-04-01-preview"
_PROVIDER_PROPERTY_ALLOWLIST = {
    "createdAt",
    "createdBy",
    "lastModifiedAt",
    "provisioningState",
}
_SECURITY_PROPERTY_KEYS = {
    "authType",
    "audience",
    "category",
    "credentials",
    "group",
    "isSharedToAll",
    "metadata",
    "target",
}
_METADATA_KEYS = {"ApiType", "bindingHash", "l7AttemptId", "type"}
_MANAGEMENT_ORIGIN = "https://management.azure.com"


def _validated_arm_url(candidate: str, *, base_url: str) -> str:
    resolved = urljoin(base_url, candidate)
    parsed = urlsplit(resolved)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "management.azure.com"
        or parsed.username
        or parsed.password
        or parsed.port is not None
    ):
        raise ProjectConnectionError(
            "Foundry project connection operation URL origin validation failed."
        )
    return resolved


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
    attempt_id: str = ""
    normalized_properties: Mapping[str, Any] | None = None


def normalize_connection_properties(
    properties: Mapping[str, Any],
) -> dict[str, Any]:
    unexpected = set(properties) - (
        _SECURITY_PROPERTY_KEYS | _PROVIDER_PROPERTY_ALLOWLIST
    )
    if unexpected:
        raise ProjectConnectionError(
            "Foundry project connection returned unexpected properties."
        )
    metadata = properties.get("metadata")
    if metadata is None:
        metadata_values: dict[str, Any] = {}
    elif isinstance(metadata, Mapping):
        metadata_values = dict(metadata)
    else:
        raise ProjectConnectionError(
            "Foundry project connection metadata is invalid."
        )
    if set(metadata_values) - _METADATA_KEYS:
        raise ProjectConnectionError(
            "Foundry project connection returned unexpected metadata."
        )
    normalized: dict[str, Any] = {
        "authType": str(properties.get("authType") or ""),
        "category": str(properties.get("category") or ""),
        "target": str(properties.get("target") or ""),
        "audience": str(properties.get("audience") or ""),
        "isSharedToAll": bool(properties.get("isSharedToAll", False)),
        "group": str(properties.get("group") or ""),
        "metadata": {
            key: metadata_values[key]
            for key in sorted(metadata_values)
        },
    }
    credentials = properties.get("credentials")
    if credentials is not None:
        if not isinstance(credentials, Mapping) or set(credentials) != {"keys"}:
            raise ProjectConnectionError(
                "Foundry project connection credentials are invalid."
            )
        keys = credentials.get("keys")
        if not isinstance(keys, Mapping):
            raise ProjectConnectionError(
                "Foundry project connection CustomKeys readback is redacted "
                "without key-name evidence."
            )
        if set(keys) != {"workspace-id", "artifact-id"}:
            raise ProjectConnectionError(
                "Foundry project connection CustomKeys names differ."
            )
        visible = {
            key: isinstance(keys.get(key), str) and bool(keys.get(key))
            for key in ("workspace-id", "artifact-id")
        }
        if all(visible.values()):
            binding_hash = canonical_sha256(
                {
                    "workspace_id": str(keys["workspace-id"]),
                    "data_agent_id": str(keys["artifact-id"]),
                }
            )
        elif not any(visible.values()):
            binding_hash = str(metadata_values.get("bindingHash") or "")
            if not binding_hash or not metadata_values.get("l7AttemptId"):
                raise ProjectConnectionError(
                    "Foundry project connection CustomKeys are redacted "
                    "without an attempt-bound binding."
                )
        else:
            raise ProjectConnectionError(
                "Foundry project connection CustomKeys readback has mixed "
                "visible and redacted values."
            )
        if binding_hash != str(metadata_values.get("bindingHash") or ""):
            raise ProjectConnectionError(
                "Foundry project connection CustomKeys binding differs."
            )
        normalized["credentials"] = {
            "keyNames": ["artifact-id", "workspace-id"],
            "bindingHash": binding_hash,
        }
    return normalized


def fabric_data_agent_connection_properties(
    *,
    workspace_id: str,
    data_agent_id: str,
    attempt_id: str,
) -> dict[str, Any]:
    return {
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
            "l7AttemptId": attempt_id,
        },
    }


def search_connection_properties(
    *,
    endpoint: str,
    attempt_id: str,
) -> dict[str, Any]:
    return {
        "authType": "ProjectManagedIdentity",
        "category": "CognitiveSearch",
        "target": endpoint.rstrip("/"),
        "isSharedToAll": True,
        "metadata": {"l7AttemptId": attempt_id},
    }


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
        self._legacy_put_request = False
        if request is not None:
            try:
                parameters = tuple(inspect.signature(request).parameters)
                self._legacy_put_request = bool(parameters) and (
                    parameters[0] != "method"
                )
            except (TypeError, ValueError):
                pass

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

                response = requests.request(
                    method,
                    url,
                    headers=request_headers,
                    json=body,
                    timeout=60,
                    allow_redirects=False,
                )
            else:
                try:
                    response = self._request(
                        method,
                        url,
                        headers=request_headers,
                        json=body,
                        timeout=60,
                        allow_redirects=False,
                    )
                except TypeError:
                    if method != "PUT":
                        raise
                    response = self._request(
                        url,
                        headers=request_headers,
                        json=body,
                        timeout=60,
                        allow_redirects=False,
                    )
            if 300 <= response.status_code < 400:
                raise ProjectConnectionError(
                    "Foundry project connection redirect was refused."
                )
            return response
        except Exception as exc:
            if isinstance(exc, ProjectConnectionError):
                raise
            raise ProjectConnectionError(
                f"Foundry project connection {method} transport failed."
            ) from exc

    def _poll_lro(self, location: str, *, base_url: str) -> None:
        operation_url = _validated_arm_url(location, base_url=base_url)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            request_headers = self._headers()
            try:
                if self._request is None:
                    import requests

                    response = requests.request(
                        "GET",
                        operation_url,
                        headers=request_headers,
                        timeout=60,
                        allow_redirects=False,
                    )
                else:
                    response = self._request(
                        "GET",
                        operation_url,
                        headers=request_headers,
                        timeout=60,
                        allow_redirects=False,
                    )
            except Exception as exc:
                raise ProjectConnectionError(
                    "Foundry project connection LRO transport failed."
                ) from exc
            if 300 <= response.status_code < 400:
                raise ProjectConnectionError(
                    "Foundry project connection LRO redirect was refused."
                )
            if response.status_code >= 400:
                raise ProjectConnectionError(
                    "Foundry project connection LRO failed."
                )
            body = self._response_json(response, "connection LRO")
            next_location = str(response.headers.get("Location") or "")
            if next_location:
                operation_url = _validated_arm_url(
                    next_location, base_url=operation_url
                )
            status = str(body.get("status") or "").casefold()
            if status in {"succeeded", "completed"}:
                return
            if status in {"failed", "cancelled", "canceled"}:
                raise ProjectConnectionError(
                    "Foundry project connection LRO reported failure."
                )
            time.sleep(2)
        raise ProjectConnectionError(
            "Foundry project connection LRO timed out."
        )

    @staticmethod
    def _response_json(response: Any, operation: str) -> dict[str, Any]:
        try:
            body = response.json()
        except (TypeError, ValueError) as exc:
            raise ProjectConnectionError(
                f"Foundry project connection {operation} returned non-JSON."
            ) from exc
        if not isinstance(body, dict):
            raise ProjectConnectionError(
                f"Foundry project connection {operation} returned invalid JSON."
            )
        return body

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
        normalized = normalize_connection_properties(properties)
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
            properties_hash=canonical_sha256(normalized),
            binding_hash=str(
                (
                    properties.get("metadata")
                    if isinstance(properties.get("metadata"), Mapping)
                    else {}
                ).get("bindingHash")
                or ""
            ),
            attempt_id=str(
                (
                    properties.get("metadata")
                    if isinstance(properties.get("metadata"), Mapping)
                    else {}
                ).get("l7AttemptId")
                or ""
            ),
            normalized_properties=normalized,
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

    def _legacy_put(
        self, name: str, properties: dict[str, Any]
    ) -> ProjectConnection:
        """Preserve the pre-0.2.4 injected URL-first PUT transport contract."""
        assert self._request is not None
        resource_id = self.connection_id(name)
        url = (
            f"https://management.azure.com{resource_id}"
            f"?api-version={_API_VERSION}"
        )
        try:
            response = self._request(
                url,
                headers=self._headers(),
                json={"name": name, "properties": properties},
                timeout=60,
            )
        except Exception as exc:
            raise ProjectConnectionError(
                "Foundry project connection PUT transport failed."
            ) from exc
        if response.status_code not in (200, 201):
            raise ProjectConnectionError(
                f"Foundry project connection '{name}' failed with HTTP "
                f"{response.status_code}."
            )
        normalized = normalize_connection_properties(properties)
        metadata = normalized.get("metadata")
        return ProjectConnection(
            name=name,
            resource_id=resource_id,
            category=str(normalized["category"]),
            target=str(normalized.get("target", "")),
            audience=str(normalized.get("audience", "")),
            etag=str(getattr(response, "headers", {}).get("ETag") or ""),
            properties_hash=canonical_sha256(normalized),
            binding_hash=str(
                metadata.get("bindingHash", "")
                if isinstance(metadata, Mapping)
                else ""
            ),
            attempt_id=str(
                metadata.get("l7AttemptId", "")
                if isinstance(metadata, Mapping)
                else ""
            ),
            normalized_properties=normalized,
        )

    def _put(
        self,
        name: str,
        properties: dict[str, Any],
        *,
        create_only: bool = False,
        attempt_id: str | None = None,
    ) -> ProjectConnection:
        if (
            self._legacy_put_request
            and not create_only
        ):
            return self._legacy_put(name, properties)
        existing = self.get(name)
        if create_only and existing is not None:
            raise ProjectConnectionError(
                f"Foundry project connection '{name}' already exists; "
                "release-owned connection adoption is forbidden."
            )
        resource_id = self.connection_id(name)
        operation_attempt_id = attempt_id or ("op-" + secrets.token_hex(32))
        if not (
            operation_attempt_id.startswith("op-")
            and len(operation_attempt_id) == 67
        ):
            raise ValueError("connection attempt ID is invalid.")
        attempt_properties = dict(properties)
        metadata = (
            dict(properties.get("metadata"))
            if isinstance(properties.get("metadata"), Mapping)
            else {}
        )
        metadata["l7AttemptId"] = operation_attempt_id
        attempt_properties["metadata"] = metadata
        expected_properties = normalize_connection_properties(
            attempt_properties
        )
        expected_hash = canonical_sha256(expected_properties)
        try:
            response = self._send(
                "PUT",
                resource_id,
                body={"name": name, "properties": attempt_properties},
                headers={"If-None-Match": "*"} if create_only else None,
            )
        except ProjectConnectionError:
            if create_only:
                try:
                    observed = self.get(name)
                    if (
                        observed is not None
                        and observed.attempt_id == operation_attempt_id
                        and observed.properties_hash == expected_hash
                        and observed.etag
                    ):
                        self.delete_created(name, expected_etag=observed.etag)
                except ProjectConnectionError:
                    pass
            raise
        if response.status_code == 202:
            location = str(response.headers.get("Location") or "")
            if not location:
                self._reconcile_failed_create(
                    name,
                    operation_attempt_id,
                    expected_hash,
                )
            try:
                self._poll_lro(
                    location,
                    base_url=(
                        f"{_MANAGEMENT_ORIGIN}{resource_id}"
                        f"?api-version={_API_VERSION}"
                    ),
                )
            except ProjectConnectionError:
                if create_only:
                    self._reconcile_failed_create(
                        name,
                        operation_attempt_id,
                        expected_hash,
                    )
                raise ProjectConnectionError(
                    f"Foundry project connection '{name}' update LRO "
                    "requires external reconciliation."
                )
            parsed = self.get(name)
            if (
                parsed is None
                or parsed.attempt_id != operation_attempt_id
                or parsed.properties_hash != expected_hash
                or not parsed.etag
            ):
                if create_only:
                    self._reconcile_failed_create(
                        name,
                        operation_attempt_id,
                        expected_hash,
                    )
                raise ProjectConnectionError(
                    f"Foundry project connection '{name}' LRO readback mismatch."
                )
            return parsed
        if response.status_code not in (200, 201):
            if create_only:
                self._reconcile_failed_create(
                    name,
                    operation_attempt_id,
                    expected_hash,
                )
            raise ProjectConnectionError(
                f"Foundry project connection '{name}' update outcome "
                "requires external reconciliation."
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
            expected_metadata = attempt_properties["metadata"]
            if (
                parsed.category != str(properties["category"])
                or parsed.target != str(properties.get("target", ""))
                or parsed.audience != str(properties.get("audience", ""))
                or parsed.binding_hash
                != str(expected_metadata.get("bindingHash") or "")
                or parsed.attempt_id != operation_attempt_id
                or parsed.properties_hash != expected_hash
                or parsed.normalized_properties != expected_properties
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
                if (
                    observed is not None
                    and observed.attempt_id == operation_attempt_id
                    and observed.properties_hash == expected_hash
                ):
                    etag = mutation_etag or observed.etag
                    if etag:
                        self.delete_created(name, expected_etag=etag)
            raise

    def _reconcile_failed_create(
        self,
        name: str,
        attempt_id: str,
        expected_hash: str,
    ) -> None:
        observed = self.get(name)
        if (
            observed is not None
            and observed.attempt_id == attempt_id
            and observed.properties_hash == expected_hash
            and observed.etag
        ):
            self.delete_created(name, expected_etag=observed.etag)
            raise ProjectConnectionError(
                f"Foundry project connection '{name}' ambiguous create "
                "was reconciled and rolled back."
            )
        raise ProjectConnectionError(
            f"Foundry project connection '{name}' create outcome is unconfirmed."
        )

    def upsert_fabric_data_agent(
        self,
        *,
        name: str,
        workspace_id: str,
        data_agent_id: str,
        create_only: bool = False,
        attempt_id: str | None = None,
    ) -> ProjectConnection:
        """Create the CustomKeys connection required by MicrosoftFabricPreviewTool."""
        if not workspace_id or not data_agent_id:
            raise ValueError("workspace_id and data_agent_id are required.")
        resolved_attempt = attempt_id or ("op-" + secrets.token_hex(32))
        return self._put(
            name,
            fabric_data_agent_connection_properties(
                workspace_id=workspace_id,
                data_agent_id=data_agent_id,
                attempt_id=resolved_attempt,
            ),
            create_only=create_only,
            attempt_id=resolved_attempt,
        )

    def upsert_search(
        self,
        *,
        name: str,
        endpoint: str,
        create_only: bool = False,
        attempt_id: str | None = None,
    ) -> ProjectConnection:
        """Create a managed-identity Azure AI Search project connection."""
        if not endpoint.startswith("https://"):
            raise ValueError("Search target must be an HTTPS URL.")
        resolved_attempt = attempt_id or ("op-" + secrets.token_hex(32))
        return self._put(
            name,
            search_connection_properties(
                endpoint=endpoint,
                attempt_id=resolved_attempt,
            ),
            create_only=create_only,
            attempt_id=resolved_attempt,
        )
