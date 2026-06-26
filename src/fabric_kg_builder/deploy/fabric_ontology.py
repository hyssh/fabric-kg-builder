"""deploy.fabric_ontology — create or get a Fabric Ontology item via REST,
and populate it via updateDefinition.

Fabric items API pattern (verified 2026-06-24):
  GET  /v1/workspaces/{ws}/items                       → list; find displayName+type match
  POST /v1/workspaces/{ws}/items                       → create (201 sync | 202 LRO)
  POST /v1/workspaces/{ws}/items/{id}/updateDefinition → populate ontology (200 | 202 LRO)

Token scope: https://api.fabric.microsoft.com/.default
Auth:        DefaultAzureCredential (az login in dev; SPN in CI via .env).

The real Fabric Ontology format is produced by fabric_def.build_ontology_parts()
which returns the EXACT decoded format (EntityType, DataBinding, RelationshipType,
Contextualization, .platform) from a working ontology. This module base64-encodes
each part and POSTs via updateDefinition to POPULATE the graph.

Usage (mock — no network)::

    result = create_or_get_ontology_item(
        workspace_id="9802a28a-...",
        name="kg_ontology",
        mock=True,
    )
    # {"item_id": "mock-ontology-item-id", "created": False, "note": "MOCK: ..."}

Usage (live)::

    result = create_or_get_ontology_item(
        workspace_id="9802a28a-...",
        name="kg_ontology",
        mock=False,
    )
    # {"item_id": "<guid>", "created": True, "note": "Created new Ontology item."}
    # or {"item_id": "<guid>", "created": False, "note": "Reused existing Ontology item."}

Then populate::

    update_result = update_ontology_definition(
        workspace_id="9802a28a-...",
        ontology_item_id="<guid>",
        parts=build_ontology_parts(...),
        mock=False,
    )
"""

from __future__ import annotations

import base64
import json
import logging
import sys
from typing import Any, Callable

logger = logging.getLogger(__name__)

_FABRIC_API_BASE = "https://api.fabric.microsoft.com/v1"
_FABRIC_TOKEN_SCOPE = "https://api.fabric.microsoft.com/.default"

_MOCK_ITEM_ID = "mock-ontology-item-id"
_NOTE_DEFINITION_API = (
    "NOTE: The graph is populated via updateDefinition using the REAL Fabric "
    "ontology format (EntityType/DataBinding/RelationshipType/Contextualization). "
    "Run deploy-ontology --no-mock to push the definition to Fabric."
)


def _default_token_provider() -> str:
    """Obtain a Bearer token via DefaultAzureCredential."""
    try:
        from azure.identity import DefaultAzureCredential  # noqa: PLC0415
    except ImportError as exc:
        logger.error(
            "[fabric_ontology] azure-identity is not installed: %s. "
            "Run: pip install azure-identity",
            exc,
        )
        sys.exit(6)

    try:
        cred = DefaultAzureCredential()
        token = cred.get_token(_FABRIC_TOKEN_SCOPE).token
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[fabric_ontology] Authentication failed (DefaultAzureCredential): %s. "
            "Run 'az login' (dev) or set FABRIC_CLIENT_ID/SECRET/TENANT_ID (CI).",
            exc,
        )
        sys.exit(6)

    return token


def create_or_get_ontology_item(
    workspace_id: str,
    name: str,
    mock: bool = False,
    token_provider: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Create a Fabric Ontology item idempotently, or return the existing one.

    Parameters
    ----------
    workspace_id:
        Fabric workspace GUID (from ontology/environments/{env}.json).
    name:
        Display name for the Ontology item (e.g. "kg_ontology").
    mock:
        When ``True``, no network call is made; returns a planned-action dict.
    token_provider:
        Callable that returns a Bearer token string.  Defaults to
        ``DefaultAzureCredential`` with Fabric API scope.  Inject in tests.

    Returns
    -------
    dict with keys:
        item_id (str)  — Fabric item GUID (or mock sentinel).
        created (bool) — True if a new item was created; False if reused.
        note (str)     — Human-readable status + definition-API caveat.

    Raises / exits
    --------------
    SystemExit(6)  on authentication failure.
    SystemExit(1)  on other errors (HTTP failures, unexpected responses).
    """
    if mock:
        note = (
            f"MOCK: would create-or-get Ontology item '{name}' in workspace "
            f"{workspace_id}. No network call made. " + _NOTE_DEFINITION_API
        )
        return {"item_id": _MOCK_ITEM_ID, "created": False, "note": note}

    import requests  # noqa: PLC0415 — lazy import keeps offline mode working

    tp = token_provider or _default_token_provider
    token = tp()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # --- IDEMPOTENCY: check whether the item already exists ---
    list_url = f"{_FABRIC_API_BASE}/workspaces/{workspace_id}/items"
    try:
        resp = requests.get(list_url, headers=headers, timeout=30)
    except requests.RequestException as exc:
        logger.error("[fabric_ontology] GET %s failed: %s", list_url, exc)
        sys.exit(1)

    if resp.status_code == 401:
        logger.error(
            "[fabric_ontology] 401 Unauthorized listing workspace items. "
            "Check your credentials (az login / SPN)."
        )
        sys.exit(6)

    if not resp.ok:
        logger.error(
            "[fabric_ontology] GET items returned %s: %s",
            resp.status_code,
            resp.text[:500],
        )
        sys.exit(1)

    items: list[dict[str, Any]] = resp.json().get("value", [])
    existing = next(
        (
            item
            for item in items
            if item.get("displayName") == name and item.get("type") == "Ontology"
        ),
        None,
    )

    if existing:
        item_id: str = existing["id"]
        note = (
            f"Reused existing Ontology item '{name}' (id={item_id}). "
            + _NOTE_DEFINITION_API
        )
        logger.info("[fabric_ontology] REUSE existing Ontology item id=%s", item_id)
        return {"item_id": item_id, "created": False, "note": note}

    # --- CREATE: item does not exist ---
    create_url = f"{_FABRIC_API_BASE}/workspaces/{workspace_id}/items"
    payload = {"displayName": name, "type": "Ontology"}
    try:
        create_resp = requests.post(create_url, headers=headers, json=payload, timeout=60)
    except requests.RequestException as exc:
        logger.error("[fabric_ontology] POST %s failed: %s", create_url, exc)
        sys.exit(1)

    if create_resp.status_code == 401:
        logger.error(
            "[fabric_ontology] 401 Unauthorized creating Ontology item. "
            "Check your credentials."
        )
        sys.exit(6)

    if create_resp.status_code == 201:
        # Synchronous creation — item is in response body
        body = create_resp.json()
        item_id = body.get("id", "")
        note = (
            f"Created new Ontology item '{name}' (id={item_id}, 201 sync). "
            + _NOTE_DEFINITION_API
        )
        logger.info("[fabric_ontology] CREATED Ontology item id=%s (201)", item_id)
        return {"item_id": item_id, "created": True, "note": note}

    if create_resp.status_code == 202:
        # Long-running operation — report "creating" with Location header
        location = create_resp.headers.get("Location") or create_resp.headers.get(
            "x-ms-operation-id", ""
        )
        note = (
            f"Ontology item '{name}' creation in progress (202 LRO). "
            f"Operation location: {location or '(not provided)'}. "
            "Poll the Location URL to retrieve the item id once provisioning completes. "
            + _NOTE_DEFINITION_API
        )
        logger.info(
            "[fabric_ontology] CREATING Ontology item (202 LRO), location=%s", location
        )
        # Return a placeholder — the item id is not available until LRO completes
        return {"item_id": f"lro:{location}", "created": True, "note": note}

    # Any other status is an error
    logger.error(
        "[fabric_ontology] Unexpected status %s creating Ontology item: %s",
        create_resp.status_code,
        create_resp.text[:500],
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# update_ontology_definition — push the REAL Fabric format to populate graph
# ---------------------------------------------------------------------------


def update_ontology_definition(
    workspace_id: str,
    ontology_item_id: str,
    parts: list[dict],
    mock: bool = False,
    token_provider: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Push the Fabric ontology definition to populate nodes + edges.

    Encodes each part's ``payload_json`` dict as base64 JSON and calls:
      POST /v1/workspaces/{ws}/items/{id}/updateDefinition

    Parameters
    ----------
    workspace_id:
        Fabric workspace GUID.
    ontology_item_id:
        Fabric Ontology item GUID (from create_or_get_ontology_item).
    parts:
        List of part dicts from ``build_ontology_parts()`` — each has
        ``path`` (str) and ``payload_json`` (dict).
    mock:
        When ``True``, no network call is made; returns a summary dict.
    token_provider:
        Callable returning a Bearer token. Defaults to DefaultAzureCredential.

    Returns
    -------
    dict with keys:
        parts_count (int)  — number of parts sent.
        status (str)       — "mock", "ok-200", "ok-202", or "error".
        note (str)         — human-readable message.
    """
    # Build the encoded parts list (base64 JSON)
    encoded_parts = []
    for part in parts:
        raw_json = json.dumps(part["payload_json"], ensure_ascii=False)
        b64 = base64.b64encode(raw_json.encode("utf-8")).decode("ascii")
        encoded_parts.append(
            {
                "path": part["path"],
                "payload": b64,
                "payloadType": "InlineBase64",
            }
        )

    parts_count = len(encoded_parts)
    paths = [p["path"] for p in encoded_parts]

    if mock:
        note = (
            f"MOCK: would call updateDefinition for item '{ontology_item_id}' "
            f"in workspace {workspace_id} with {parts_count} parts: {paths}. "
            "No network call made."
        )
        logger.info("[fabric_ontology] MOCK updateDefinition: %d parts", parts_count)
        return {"parts_count": parts_count, "status": "mock", "note": note}

    import requests  # noqa: PLC0415 — lazy import for offline compatibility

    tp = token_provider or _default_token_provider
    token = tp()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    url = (
        f"{_FABRIC_API_BASE}/workspaces/{workspace_id}"
        f"/items/{ontology_item_id}/updateDefinition"
    )
    body = {"definition": {"parts": encoded_parts}}

    try:
        resp = requests.post(url, headers=headers, json=body, timeout=120)
    except requests.RequestException as exc:
        logger.error("[fabric_ontology] POST updateDefinition failed: %s", exc)
        sys.exit(1)

    if resp.status_code == 401:
        logger.error(
            "[fabric_ontology] 401 Unauthorized on updateDefinition. "
            "Check credentials (az login / SPN)."
        )
        sys.exit(6)

    if resp.status_code == 200:
        note = (
            f"updateDefinition succeeded (200) for item '{ontology_item_id}'. "
            f"{parts_count} parts pushed. Graph should now be POPULATED."
        )
        logger.info("[fabric_ontology] updateDefinition 200 OK, %d parts", parts_count)
        return {"parts_count": parts_count, "status": "ok-200", "note": note}

    if resp.status_code == 202:
        location = resp.headers.get("Location") or resp.headers.get("x-ms-operation-id", "")
        note = (
            f"updateDefinition accepted (202 LRO) for item '{ontology_item_id}'. "
            f"Operation: {location or '(no location)'}. {parts_count} parts submitted."
        )
        logger.info(
            "[fabric_ontology] updateDefinition 202 LRO, location=%s", location
        )
        return {"parts_count": parts_count, "status": "ok-202", "note": note, "location": location}

    logger.error(
        "[fabric_ontology] updateDefinition returned unexpected status %s: %s",
        resp.status_code,
        resp.text[:500],
    )
    sys.exit(1)
