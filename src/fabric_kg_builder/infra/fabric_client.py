"""Typed Fabric REST API clients for workspace, Lakehouse, Ontology, and Graph model.

All clients use a ``HttpTransport`` protocol so they can be tested with a
``FakeHttpTransport`` without live network calls.

Recorded API versions are explicit; GA and preview features must not be mixed.

SPEC-006 §6.1 / INF-014, INF-015, INF-016, INF-017.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlencode

from .names import (
    validate_fabric_graph_model_name,
    validate_fabric_identifier_name,
)


# ---------------------------------------------------------------------------
# Recorded API versions
# ---------------------------------------------------------------------------

_FABRIC_API_VERSION = "2023-11-01"
_FABRIC_API_BASE = "https://api.fabric.microsoft.com/v1"
_FABRIC_TOKEN_SCOPE = "https://api.fabric.microsoft.com/.default"

# Fabric item types — exact ``type`` values returned by the Fabric REST API.
# These appear in RESPONSES; POST request bodies do NOT include a ``type`` field.
# Ontology:   https://learn.microsoft.com/rest/api/fabric/ontology/items
# GraphModel: https://learn.microsoft.com/rest/api/fabric/graphmodel/items
_ITEM_TYPE_LAKEHOUSE = "Lakehouse"
_ITEM_TYPE_ONTOLOGY = "Ontology"
_ITEM_TYPE_GRAPH_MODEL = "GraphModel"
_ITEM_TYPE_WORKSPACE = "Workspace"


# ---------------------------------------------------------------------------
# HTTP transport protocol
# ---------------------------------------------------------------------------


@dataclass
class HttpResponse:
    """Simplified HTTP response container."""
    status_code: int
    headers: dict[str, str]
    body: str

    def json(self) -> Any:
        return json.loads(self.body)

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def is_accepted(self) -> bool:
        return self.status_code == 202

    @property
    def operation_url(self) -> str | None:
        """LRO polling location from response headers."""
        return self.headers.get("Location") or self.headers.get("location")


@runtime_checkable
class HttpTransport(Protocol):
    """Minimal HTTP transport for Fabric REST calls."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict | None = None,
    ) -> HttpResponse:
        ...


class DefaultAzureCredentialFabricTransport:
    """Authenticated production transport for Fabric REST APIs."""

    def __init__(
        self,
        *,
        credential: Any | None = None,
        timeout_seconds: float = 60.0,
        session: Any | None = None,
    ) -> None:
        if credential is None:
            from fabric_kg_builder.azure_identity import (
                default_azure_credential,
            )

            credential = default_azure_credential()
        if session is None:
            import requests

            session = requests.Session()
        self._credential = credential
        self._timeout_seconds = timeout_seconds
        self._session = session

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict | None = None,
    ) -> HttpResponse:
        token = self._credential.get_token(_FABRIC_TOKEN_SCOPE).token
        request_headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        request_headers.update(headers or {})
        try:
            response = self._session.request(
                method=method,
                url=url,
                headers=request_headers,
                json=json_body,
                timeout=self._timeout_seconds,
            )
        except Exception as exc:
            raise FabricLROError(
                f"Fabric REST request failed for {method.upper()} {url}: {exc}"
            ) from exc
        return HttpResponse(
            status_code=int(response.status_code),
            headers=dict(response.headers),
            body=response.text or "",
        )


# ---------------------------------------------------------------------------
# Fake transport for unit tests
# ---------------------------------------------------------------------------


@dataclass
class FakeHttpTransport:
    """Scripted HTTP transport for deterministic unit tests."""

    responses: list[tuple[str, str, HttpResponse]] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    strict: bool = True

    def add_response(
        self,
        method: str,
        url_substring: str,
        *,
        status_code: int = 200,
        body: dict | str | None = None,
        headers: dict[str, str] | None = None,
    ) -> "FakeHttpTransport":
        body_str = json.dumps(body) if isinstance(body, dict) else (body or "")
        self.responses.append((
            method.upper(),
            url_substring,
            HttpResponse(
                status_code=status_code,
                headers=headers or {},
                body=body_str,
            ),
        ))
        return self

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict | None = None,
    ) -> HttpResponse:
        self.calls.append((method.upper(), url))
        matches = [
            (url_sub, response)
            for req_method, url_sub, response in self.responses
            if req_method == method.upper() and url_sub in url
        ]
        if matches:
            return max(
                matches,
                key=lambda match: (url.rfind(match[0]), len(match[0])),
            )[1]
        if self.strict:
            raise AssertionError(
                f"FakeHttpTransport has no response for {method.upper()} {url!r}.\n"
                f"Registered: {[(m, u) for m, u, _ in self.responses]}"
            )
        return HttpResponse(status_code=200, headers={}, body="{}")


# ---------------------------------------------------------------------------
# LRO polling helper
# ---------------------------------------------------------------------------


class FabricLROError(RuntimeError):
    """Long-running operation failed or timed out."""


def _require_item_id(item: dict, resource_type: str) -> dict:
    if not item.get("id"):
        raise FabricLROError(
            f"{resource_type} operation completed without returning an item ID."
        )
    return item


def _delete_item(
    transport: HttpTransport,
    url: str,
    resource_type: str,
    *,
    _sleep_fn=time.sleep,
) -> None:
    response = transport.request("DELETE", url)
    if response.status_code == 404:
        return
    if response.is_accepted:
        operation_url = response.operation_url
        if not operation_url:
            raise FabricLROError(
                f"{resource_type} delete returned 202 without a Location header."
            )
        _poll_lro(transport, operation_url, _sleep_fn=_sleep_fn)
        return
    if not response.ok:
        raise FabricLROError(
            f"Failed to delete {resource_type}: "
            f"{response.status_code} {response.body}"
        )


def _poll_lro(
    transport: HttpTransport,
    operation_url: str,
    *,
    max_attempts: int = 30,
    sleep_seconds: float = 2.0,
    _sleep_fn=time.sleep,
) -> dict:
    """Poll a Fabric LRO until succeeded/failed or timeout.

    Honors ``Retry-After`` from response headers.  Raises ``FabricLROError``
    on failure or timeout.
    """
    for _ in range(max_attempts):
        resp = transport.request("GET", operation_url)
        if resp.status_code == 429:
            # Rate-limited — honour Retry-After and stay in the polling loop.
            retry_after = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
            wait = float(retry_after) if retry_after and str(retry_after).isdigit() else sleep_seconds
            _sleep_fn(wait)
            continue
        if not resp.ok:
            raise FabricLROError(
                f"LRO poll failed with status {resp.status_code}: {resp.body}"
            )
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError):
            data = {}

        status = (data.get("status") or "").lower()
        if status in ("succeeded", "completed"):
            return data
        if status in ("failed", "cancelled"):
            error = data.get("error", {}) or {}
            raise FabricLROError(
                f"LRO operation failed: {error.get('message', data)}"
            )
        # Still running — wait.
        retry_after = resp.headers.get("Retry-After")
        wait = float(retry_after) if retry_after and retry_after.isdigit() else sleep_seconds
        _sleep_fn(wait)
    raise FabricLROError(
        f"LRO operation at '{operation_url}' did not complete after {max_attempts} attempts."
    )


# ---------------------------------------------------------------------------
# Workspace client (INF-014)
# ---------------------------------------------------------------------------


class FabricWorkspaceClient:
    """Typed Fabric workspace REST client.

    SPEC-006 / INF-014.
    """

    def __init__(self, transport: HttpTransport) -> None:
        self._t = transport

    def _url(self, path: str) -> str:
        return f"{_FABRIC_API_BASE}{path}"

    def list_workspaces(self) -> list[dict]:
        """Return all workspaces accessible to the caller."""
        resp = self._t.request("GET", self._url("/workspaces"))
        if not resp.ok:
            raise FabricLROError(
                f"Failed to list workspaces: {resp.status_code} {resp.body}"
            )
        data = resp.json()
        return data.get("value", [])

    def get_workspace(self, workspace_id: str) -> dict:
        """Return workspace metadata for *workspace_id*."""
        resp = self._t.request("GET", self._url(f"/workspaces/{workspace_id}"))
        if resp.status_code == 404:
            raise KeyError(f"Workspace '{workspace_id}' not found.")
        if not resp.ok:
            raise FabricLROError(
                f"Failed to get workspace '{workspace_id}': {resp.status_code} {resp.body}"
            )
        return resp.json()

    def create_workspace(
        self,
        display_name: str,
        capacity_id: str | None = None,
        *,
        _sleep_fn=time.sleep,
    ) -> dict:
        """Create a Fabric workspace and wait for LRO completion.

        Returns the workspace metadata dict.
        """
        body: dict = {"displayName": display_name}
        if capacity_id:
            body["capacityId"] = capacity_id
        resp = self._t.request(
            "POST",
            self._url("/workspaces"),
            json_body=body,
        )
        if resp.is_accepted:
            # Long-running operation
            op_url = resp.operation_url
            if not op_url:
                raise FabricLROError(
                    "Workspace create returned 202 but no Location header for LRO polling."
                )
            _poll_lro(self._t, op_url, _sleep_fn=_sleep_fn)
            # After LRO, retrieve the created workspace.
            workspaces = self.list_workspaces()
            for ws in workspaces:
                if ws.get("displayName") == display_name:
                    return _require_item_id(ws, "Workspace")
            raise FabricLROError(
                f"Workspace '{display_name}' was not visible after its create "
                "operation completed."
            )
        if resp.ok:
            return _require_item_id(resp.json(), "Workspace")
        raise FabricLROError(
            f"Failed to create workspace '{display_name}': {resp.status_code} {resp.body}"
        )

    def find_workspace_by_name(self, name: str) -> dict | None:
        """Find a workspace by display name; return None if not found."""
        for ws in self.list_workspaces():
            if ws.get("displayName") == name or ws.get("name") == name:
                return ws
        return None

    def create_or_connect_workspace(
        self,
        display_name: str,
        capacity_id: str | None = None,
        *,
        mode: str = "create",
        item_id: str | None = None,
    ) -> dict:
        """Create a new workspace or connect to an existing one."""
        if mode == "connect":
            if item_id:
                return self.get_workspace(item_id)
            existing = self.find_workspace_by_name(display_name)
            if existing is None:
                raise FabricLROError(
                    f"Workspace '{display_name}' not found for connect mode. "
                    "Verify the workspace exists and you have access."
                )
            return existing
        return self.create_workspace(display_name, capacity_id)

    def delete_workspace(self, workspace_id: str) -> None:
        _delete_item(
            self._t,
            self._url(f"/workspaces/{workspace_id}"),
            f"Workspace '{workspace_id}'",
        )


# ---------------------------------------------------------------------------
# Lakehouse client (INF-015)
# ---------------------------------------------------------------------------


class FabricLakehouseClient:
    """Typed Fabric Lakehouse REST client with schema support.

    SPEC-006 / INF-015.
    """

    def __init__(self, transport: HttpTransport, workspace_id: str) -> None:
        self._t = transport
        self._workspace_id = workspace_id

    def _url(self, path: str = "") -> str:
        return f"{_FABRIC_API_BASE}/workspaces/{self._workspace_id}/lakehouses{path}"

    def list_lakehouses(self) -> list[dict]:
        """Return all Lakehouse items in the workspace."""
        resp = self._t.request("GET", self._url())
        if not resp.ok:
            raise FabricLROError(
                f"Failed to list lakehouses: {resp.status_code} {resp.body}"
            )
        return resp.json().get("value", [])

    def get_lakehouse(self, lakehouse_id: str) -> dict:
        """Return metadata for a specific Lakehouse."""
        resp = self._t.request("GET", self._url(f"/{lakehouse_id}"))
        if resp.status_code == 404:
            raise KeyError(f"Lakehouse '{lakehouse_id}' not found.")
        if not resp.ok:
            raise FabricLROError(
                f"Failed to get lakehouse '{lakehouse_id}': {resp.status_code} {resp.body}"
            )
        return resp.json()

    def create_lakehouse(
        self,
        display_name: str,
        *,
        enable_schemas: bool = True,
        _sleep_fn=time.sleep,
    ) -> dict:
        """Create a schema-enabled (or classic) Lakehouse.

        Schema-enabled Lakehouse requires ``enableSchemas: true`` in the
        creation definition. This is not patchable after creation.
        """
        validate_fabric_identifier_name(display_name, "Lakehouse")
        body: dict = {
            "displayName": display_name,
            "type": _ITEM_TYPE_LAKEHOUSE,
        }
        if enable_schemas:
            body["creationPayload"] = {"enableSchemas": True}
        resp = self._t.request(
            "POST",
            self._url(),
            json_body=body,
        )
        if resp.is_accepted:
            op_url = resp.operation_url
            if not op_url:
                raise FabricLROError(
                    "Lakehouse create returned 202 but no Location header."
                )
            _poll_lro(self._t, op_url, _sleep_fn=_sleep_fn)
            # Retrieve after LRO
            for lh in self.list_lakehouses():
                if lh.get("displayName") == display_name:
                    return _require_item_id(lh, "Lakehouse")
            raise FabricLROError(
                f"Lakehouse '{display_name}' was not visible after its create "
                "operation completed."
            )
        if resp.ok:
            return _require_item_id(resp.json(), "Lakehouse")
        raise FabricLROError(
            f"Failed to create Lakehouse '{display_name}': {resp.status_code} {resp.body}"
        )

    def find_lakehouse_by_name(self, name: str) -> dict | None:
        """Find a Lakehouse by display name; return None if not found."""
        for lh in self.list_lakehouses():
            if lh.get("displayName") == name or lh.get("name") == name:
                return lh
        return None

    def create_or_connect_lakehouse(
        self,
        display_name: str,
        *,
        enable_schemas: bool = True,
        mode: str = "create",
        item_id: str | None = None,
    ) -> dict:
        """Create a new schema-enabled Lakehouse or connect to an existing one."""
        if mode == "connect":
            if item_id:
                return self.get_lakehouse(item_id)
            existing = self.find_lakehouse_by_name(display_name)
            if existing is None:
                raise FabricLROError(
                    f"Lakehouse '{display_name}' not found for connect mode."
                )
            return existing
        return self.create_lakehouse(display_name, enable_schemas=enable_schemas)

    def delete_lakehouse(self, lakehouse_id: str) -> None:
        _delete_item(
            self._t,
            self._url(f"/{lakehouse_id}"),
            f"Lakehouse '{lakehouse_id}'",
        )


# ---------------------------------------------------------------------------
# Ontology client (INF-016)
# ---------------------------------------------------------------------------
# Official preview API (item type returned is exactly "Ontology"):
#   POST https://api.fabric.microsoft.com/v1/workspaces/{workspaceId}/ontologies
#   GET  https://api.fabric.microsoft.com/v1/workspaces/{workspaceId}/ontologies
#   https://learn.microsoft.com/rest/api/fabric/ontology/items/create-ontology
#   https://learn.microsoft.com/rest/api/fabric/ontology/items/list-ontologies
#
# Request bodies do NOT include a ``type`` field; ``type`` appears only in
# responses.  Both 201 (sync) and 202 (LRO) are possible.  429 returns
# Retry-After and must be retried.


class OntologyCapabilityProbe:
    """Result of probing Ontology capability availability."""
    def __init__(
        self,
        *,
        preview_available: bool,
        capacity_ok: bool,
        tenant_setting_ok: bool,
        definition_capability: bool,
        warnings: list[str],
        errors: list[str],
    ) -> None:
        self.preview_available = preview_available
        self.capacity_ok = capacity_ok
        self.tenant_setting_ok = tenant_setting_ok
        self.definition_capability = definition_capability
        self.warnings = warnings
        self.errors = errors

    @property
    def ok(self) -> bool:
        return not self.errors


class FabricOntologyClient:
    """Typed Fabric Ontology REST client.

    Uses the dedicated ``/ontologies`` endpoint (preview) — NOT the generic
    ``/items`` endpoint.  A 200 response from GET /ontologies (even with an
    empty list) proves the capability is enabled in this tenant/capacity.
    A 404 or 501 means the preview is not available.

    SPEC-006 / INF-016.
    """

    def __init__(self, transport: HttpTransport, workspace_id: str) -> None:
        self._t = transport
        self._workspace_id = workspace_id

    def _ontologies_url(self, path: str = "") -> str:
        return f"{_FABRIC_API_BASE}/workspaces/{self._workspace_id}/ontologies{path}"

    def list_ontologies(self) -> list[dict]:
        """Return all Ontology items via GET /ontologies."""
        resp = self._t.request("GET", self._ontologies_url())
        if not resp.ok:
            raise FabricLROError(
                f"Failed to list ontologies: {resp.status_code} {resp.body}. "
                "The Ontology preview may not be enabled in this tenant."
            )
        return resp.json().get("value", [])

    def probe_capability(self) -> OntologyCapabilityProbe:
        """Probe whether the Ontology preview capability is available.

        Uses GET /ontologies.  A 200 (even with empty list) = capability
        enabled.  404/501 = not available in this tenant/capacity.
        """
        warnings: list[str] = []
        errors: list[str] = []

        resp = self._t.request("GET", self._ontologies_url())

        if resp.status_code in (404, 501):
            warnings.append(
                "Ontology preview capability is not available in this tenant or capacity "
                f"(GET /ontologies returned {resp.status_code}). "
                "Contact your Fabric admin to enable the Ontology preview feature."
            )
            return OntologyCapabilityProbe(
                preview_available=False,
                capacity_ok=False,
                tenant_setting_ok=False,
                definition_capability=False,
                warnings=warnings,
                errors=errors,
            )

        if not resp.ok:
            errors.append(
                f"Cannot probe Ontology capability: {resp.status_code} {resp.body}. "
                "Check Fabric workspace access."
            )
            return OntologyCapabilityProbe(
                preview_available=False,
                capacity_ok=False,
                tenant_setting_ok=False,
                definition_capability=False,
                warnings=warnings,
                errors=errors,
            )

        # 200 (any body, including empty list) = capability available
        return OntologyCapabilityProbe(
            preview_available=True,
            capacity_ok=True,
            tenant_setting_ok=True,
            definition_capability=True,
            warnings=warnings,
            errors=errors,
        )

    def create_ontology(
        self,
        display_name: str,
        *,
        description: str | None = None,
        _sleep_fn=time.sleep,
    ) -> dict:
        """Create an Ontology item via POST /ontologies (preview-gated).

        Request body contains only ``displayName`` (and optionally
        ``description``); ``type`` is absent per the documented contract
        and appears only in the response.
        """
        validate_fabric_identifier_name(display_name, "Ontology")
        body: dict = {"displayName": display_name}
        if description:
            body["description"] = description

        resp = self._t.request("POST", self._ontologies_url(), json_body=body)

        if resp.status_code == 429:
            raise FabricLROError(
                "Rate-limited (429) creating Ontology. "
                f"Retry-After: {resp.headers.get('Retry-After', 'unknown')}s."
            )
        if resp.is_accepted:
            op_url = resp.operation_url
            if not op_url:
                raise FabricLROError("Ontology create returned 202 but no Location header.")
            _poll_lro(self._t, op_url, _sleep_fn=_sleep_fn)
            found = self.find_by_name(display_name)
            if found is None:
                raise FabricLROError(
                    f"Ontology '{display_name}' was not visible after its create "
                    "operation completed."
                )
            return _require_item_id(found, "Ontology")
        if resp.ok:
            return _require_item_id(resp.json(), "Ontology")
        if resp.status_code in (404, 501):
            raise FabricLROError(
                f"Ontology item creation failed ({resp.status_code}): {resp.body}. "
                "This capability is not available in your tenant or is unsupported. "
                "Contact a Fabric admin to enable the Ontology preview feature."
            )
        raise FabricLROError(
            f"Failed to create Ontology '{display_name}': {resp.status_code} {resp.body}"
        )

    def find_by_name(self, name: str) -> dict | None:
        for item in self.list_ontologies():
            if item.get("displayName") == name or item.get("name") == name:
                return item
        return None

    def connect(
        self,
        *,
        item_id: str | None = None,
        display_name: str | None = None,
    ) -> dict:
        if item_id:
            resp = self._t.request("GET", self._ontologies_url(f"/{item_id}"))
            if resp.status_code == 404:
                raise KeyError(f"Ontology '{item_id}' not found.")
            if not resp.ok:
                raise FabricLROError(
                    f"Failed to get Ontology '{item_id}': "
                    f"{resp.status_code} {resp.body}"
                )
            return _require_item_id(resp.json(), "Ontology")
        if display_name:
            found = self.find_by_name(display_name)
            if found is None:
                raise FabricLROError(
                    f"Ontology '{display_name}' not found for connect."
                )
            return _require_item_id(found, "Ontology")
        raise ValueError("connect() requires item_id or display_name.")

    def delete_ontology(self, ontology_id: str) -> None:
        _delete_item(
            self._t,
            self._ontologies_url(f"/{ontology_id}"),
            f"Ontology '{ontology_id}'",
        )


# ---------------------------------------------------------------------------
# Graph model client (INF-017)
# ---------------------------------------------------------------------------
# Official preview API (item type returned is exactly "GraphModel"):
#   POST https://api.fabric.microsoft.com/v1/workspaces/{workspaceId}/graphModels
#   GET  https://api.fabric.microsoft.com/v1/workspaces/{workspaceId}/graphModels
#   https://learn.microsoft.com/rest/api/fabric/graphmodel/items/create-graph-model
#   https://learn.microsoft.com/rest/api/fabric/graphmodel/items/list-graph-models
#
# Request bodies do NOT include a ``type`` field; ``type`` appears only in
# responses.  A 404/501 from GET /graphModels means the preview API is not
# available in this tenant — return a typed guided-connect state, never
# misclassify as auth failure.


class GraphModelDiscovery:
    """Result of Graph model capability discovery."""

    def __init__(
        self,
        *,
        automated_create_available: bool,
        existing_items: list[dict],
        guidance: str | None = None,
        warnings: list[str],
    ) -> None:
        self.automated_create_available = automated_create_available
        self.existing_items = existing_items
        self.guidance = guidance
        self.warnings = warnings


class FabricGraphModelClient:
    """Typed Fabric Graph model REST client.

    Uses the dedicated ``/graphModels`` endpoint (preview) — NOT the generic
    ``/items`` endpoint.  When GET /graphModels returns 404 or 501 (API not
    available in this tenant/capacity), ``discover()`` returns a typed
    guided-connect state so the CLI can guide the operator through manual
    creation rather than misclassifying the failure.

    SPEC-006 §6.4 / INF-017.
    """

    _GUIDED_FALLBACK = (
        "To create a Graph model manually:\n"
        "1. Open the Fabric workspace in the browser.\n"
        "2. Select '+ New item' → 'Graph model' (preview).\n"
        "3. Name it and complete the setup wizard.\n"
        "4. Run 'fabric-kg infra connect --resource graph_model --id <item-id>' "
        "to register the item with this environment."
    )

    def __init__(self, transport: HttpTransport, workspace_id: str) -> None:
        self._t = transport
        self._workspace_id = workspace_id

    def _graph_models_url(self, path: str = "") -> str:
        return f"{_FABRIC_API_BASE}/workspaces/{self._workspace_id}/graphModels{path}"

    def list_graph_models(self) -> list[dict]:
        """Return all Graph model items via GET /graphModels."""
        resp = self._t.request("GET", self._graph_models_url())
        if not resp.ok:
            raise FabricLROError(
                f"Failed to list graph models: {resp.status_code} {resp.body}."
            )
        return resp.json().get("value", [])

    def discover(self) -> GraphModelDiscovery:
        """Probe whether the Graph model preview API is available for automation.

        Uses GET /graphModels.  A 200 (even empty list) = automated creation
        available.  Any non-200 status = API not available in this
        tenant/capacity; returns a typed guided-connect state.
        """
        warnings: list[str] = []

        resp = self._t.request("GET", self._graph_models_url())
        if resp.ok:
            existing = resp.json().get("value", [])
            return GraphModelDiscovery(
                automated_create_available=True,
                existing_items=existing,
                guidance=None,
                warnings=warnings,
            )

        warnings.append(
            f"Graph model preview API (/graphModels) is not available in this tenant or "
            f"capacity (status {resp.status_code}). "
            "Automated create is unavailable; use the guided connect workflow."
        )
        return GraphModelDiscovery(
            automated_create_available=False,
            existing_items=[],
            guidance=self._GUIDED_FALLBACK,
            warnings=warnings,
        )

    def create_graph_model(
        self,
        display_name: str,
        *,
        description: str | None = None,
        _sleep_fn=time.sleep,
    ) -> dict:
        """Create a Graph model item via POST /graphModels (preview-gated).

        Request body contains only ``displayName`` (and optionally
        ``description``); ``type`` is absent per the documented contract
        and appears only in the response.

        Raises ``FabricLROError`` with guided fallback instructions when the
        API returns 400/404/501.
        """
        validate_fabric_graph_model_name(display_name)
        body: dict = {"displayName": display_name}
        if description:
            body["description"] = description

        resp = self._t.request("POST", self._graph_models_url(), json_body=body)

        if resp.status_code == 429:
            raise FabricLROError(
                "Rate-limited (429) creating Graph model. "
                f"Retry-After: {resp.headers.get('Retry-After', 'unknown')}s."
            )
        if resp.is_accepted:
            op_url = resp.operation_url
            if not op_url:
                raise FabricLROError("Graph model create returned 202 but no Location header.")
            _poll_lro(self._t, op_url, _sleep_fn=_sleep_fn)
            found = self.find_by_name(display_name)
            if found is None:
                raise FabricLROError(
                    f"Graph model '{display_name}' was not visible after its "
                    "create operation completed."
                )
            return _require_item_id(found, "Graph model")
        if resp.ok:
            return _require_item_id(resp.json(), "Graph model")
        if resp.status_code in (400, 404, 501):
            raise FabricLROError(
                f"Graph model creation is not available via the API "
                f"(status {resp.status_code}). "
                f"{self._GUIDED_FALLBACK}"
            )
        raise FabricLROError(
            f"Failed to create Graph model '{display_name}': {resp.status_code} {resp.body}"
        )

    def find_by_name(self, name: str) -> dict | None:
        """Find a Graph model item by display name via GET /graphModels."""
        resp = self._t.request("GET", self._graph_models_url())
        if not resp.ok:
            return None
        for item in resp.json().get("value", []):
            if item.get("displayName") == name or item.get("name") == name:
                return item
        return None

    def connect(
        self,
        *,
        item_id: str | None = None,
        display_name: str | None = None,
    ) -> dict:
        """Connect to an existing Graph model item via GET /graphModels/{id}."""
        if item_id:
            resp = self._t.request("GET", self._graph_models_url(f"/{item_id}"))
            if resp.status_code == 404:
                raise KeyError(f"Graph model '{item_id}' not found.")
            if not resp.ok:
                raise FabricLROError(
                    f"Failed to get Graph model '{item_id}': {resp.status_code} {resp.body}"
                )
            return _require_item_id(resp.json(), "Graph model")
        if display_name:
            found = self.find_by_name(display_name)
            if found is None:
                raise FabricLROError(
                    f"Graph model '{display_name}' not found for connect. "
                    "Verify the item exists and try connecting by item ID."
                )
            return _require_item_id(found, "Graph model")
        raise ValueError("connect() requires item_id or display_name.")

    def delete_graph_model(self, graph_model_id: str) -> None:
        _delete_item(
            self._t,
            self._graph_models_url(f"/{graph_model_id}"),
            f"Graph model '{graph_model_id}'",
        )
