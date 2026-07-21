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
import time
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


def _operation_location(headers: Any) -> str:
    location = headers.get("Location") or headers.get("location")
    if location:
        return str(location)
    operation_id = (
        headers.get("x-ms-operation-id")
        or headers.get("X-Ms-Operation-Id")
        or headers.get("x-ms-operationid")
    )
    if not operation_id:
        return ""
    operation_id = str(operation_id)
    if operation_id.startswith(("http://", "https://")):
        return operation_id
    return f"{_FABRIC_API_BASE}/operations/{operation_id}"


def _poll_lro(
    requests_module: Any,
    location: str,
    headers: dict[str, str],
    *,
    initial_retry_after: float = 5,
    max_attempts: int = 60,
    timeout_s: float = 300,
    sleep_fn: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Poll a Fabric operation to a terminal state or fail closed."""
    import time
    from urllib.parse import urljoin

    if not location:
        raise RuntimeError("Fabric returned 202 without an operation location.")
    operation_url = urljoin(f"{_FABRIC_API_BASE}/", location)
    sleeper = sleep_fn or time.sleep
    wait = max(0.0, float(initial_retry_after))
    started = time.monotonic()

    for attempt in range(max_attempts):
        if time.monotonic() - started > timeout_s:
            raise RuntimeError(
                f"Fabric LRO timed out after {timeout_s:g}s: {operation_url}"
            )
        try:
            response = requests_module.get(
                operation_url,
                headers=headers,
                timeout=30,
            )
        except requests_module.RequestException as exc:
            raise RuntimeError(
                f"Fabric LRO poll request failed at attempt {attempt + 1}: {exc}"
            ) from exc

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", wait or 5)
            try:
                wait = max(0.0, float(retry_after))
            except (TypeError, ValueError):
                wait = 5.0
            sleeper(wait)
            continue
        if response.status_code not in {200, 202}:
            raise RuntimeError(
                "Fabric LRO poll failed at attempt "
                f"{attempt + 1}: HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )

        try:
            body = response.json()
        except (TypeError, ValueError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        status = str(body.get("status", "")).lower()
        if status == "succeeded":
            return body
        if status in {"failed", "canceled", "cancelled"}:
            raise RuntimeError(
                f"Fabric LRO ended in {status}: {body.get('error', body)}"
            )

        retry_after = response.headers.get("Retry-After", wait or 5)
        try:
            wait = max(0.0, float(retry_after))
        except (TypeError, ValueError):
            wait = 5.0
        sleeper(wait)

    raise RuntimeError(
        f"Fabric LRO did not complete after {max_attempts} polls: {operation_url}"
    )


def _created_item_id(operation: dict[str, Any]) -> str:
    result = operation.get("result")
    candidates = [
        operation.get("id"),
        operation.get("itemId"),
        result.get("id") if isinstance(result, dict) else None,
        result.get("itemId") if isinstance(result, dict) else None,
    ]
    return next((str(value) for value in candidates if value), "")


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
    *,
    _lro_sleep: Callable[[float], None] | None = None,
    _lro_max_attempts: int = 60,
    _lro_timeout_s: float = 300,
    _create_retry_timeout_s: float = 300,
    _create_retry_sleep: Callable[[float], None] | None = None,
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
    retry_started = time.monotonic()
    retry_sleep = _create_retry_sleep or time.sleep
    while True:
        try:
            create_resp = requests.post(
                create_url,
                headers=headers,
                json=payload,
                timeout=60,
            )
        except requests.RequestException as exc:
            logger.error("[fabric_ontology] POST %s failed: %s", create_url, exc)
            sys.exit(1)

        if create_resp.status_code != 409:
            break
        try:
            error_code = str(create_resp.json().get("errorCode", ""))
        except (TypeError, ValueError, AttributeError):
            error_code = ""
        if error_code != "ItemDisplayNameNotAvailableYet":
            break
        if time.monotonic() - retry_started >= _create_retry_timeout_s:
            logger.error(
                "[fabric_ontology] Timed out after %gs waiting for deleted Ontology "
                "name '%s' to become available.",
                _create_retry_timeout_s,
                name,
            )
            sys.exit(1)
        retry_after = create_resp.headers.get("Retry-After", "5")
        try:
            wait = max(0.0, float(retry_after))
        except (TypeError, ValueError):
            wait = 5.0
        logger.info(
            "[fabric_ontology] Ontology name '%s' is pending deletion; retrying "
            "create in %gs.",
            name,
            wait,
        )
        retry_sleep(wait)

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
        location = _operation_location(create_resp.headers)
        retry_after = create_resp.headers.get("Retry-After", "5")
        try:
            operation = _poll_lro(
                requests,
                location,
                headers,
                initial_retry_after=float(retry_after),
                max_attempts=_lro_max_attempts,
                timeout_s=_lro_timeout_s,
                sleep_fn=_lro_sleep,
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            logger.error("[fabric_ontology] Ontology create LRO failed: %s", exc)
            sys.exit(1)

        item_id = _created_item_id(operation)
        if not item_id:
            try:
                refresh_resp = requests.get(list_url, headers=headers, timeout=30)
            except requests.RequestException as exc:
                logger.error(
                    "[fabric_ontology] Could not resolve completed Ontology LRO: %s",
                    exc,
                )
                sys.exit(1)
            if not refresh_resp.ok:
                logger.error(
                    "[fabric_ontology] Completed Ontology LRO but item lookup "
                    "returned %s: %s",
                    refresh_resp.status_code,
                    refresh_resp.text[:500],
                )
                sys.exit(1)
            refreshed_items = refresh_resp.json().get("value", [])
            created_item = next(
                (
                    item
                    for item in refreshed_items
                    if item.get("displayName") == name
                    and item.get("type") == "Ontology"
                ),
                None,
            )
            item_id = str((created_item or {}).get("id", ""))
        if not item_id:
            logger.error(
                "[fabric_ontology] Ontology create LRO succeeded but no item id "
                "was returned or discoverable."
            )
            sys.exit(1)

        note = (
            f"Created new Ontology item '{name}' (id={item_id}, 202 LRO completed). "
            + _NOTE_DEFINITION_API
        )
        logger.info(
            "[fabric_ontology] CREATED Ontology item id=%s (202 LRO completed)",
            item_id,
        )
        return {
            "item_id": item_id,
            "created": True,
            "note": note,
            "operation_location": location,
        }

    # Any other status is an error
    logger.error(
        "[fabric_ontology] Unexpected status %s creating Ontology item: %s",
        create_resp.status_code,
        create_resp.text[:500],
    )
    sys.exit(1)


def delete_ontology_item(
    workspace_id: str,
    ontology_item_id: str,
    *,
    mock: bool = False,
    token_provider: Callable[[], str] | None = None,
    _lro_sleep: Callable[[float], None] | None = None,
    _lro_timeout_s: float = 300,
) -> None:
    """Delete one configured Ontology item and wait for its Fabric LRO."""
    if mock:
        return

    import requests  # noqa: PLC0415

    token = (token_provider or _default_token_provider)()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    url = (
        f"{_FABRIC_API_BASE}/workspaces/{workspace_id}"
        f"/ontologies/{ontology_item_id}"
    )
    try:
        response = requests.delete(url, headers=headers, timeout=60)
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Failed to delete Ontology '{ontology_item_id}': {exc}"
        ) from exc
    if response.status_code == 404:
        return
    if response.status_code == 401:
        raise PermissionError(
            f"Unauthorized deleting Ontology '{ontology_item_id}'."
        )
    if response.status_code in {200, 204}:
        return
    if response.status_code == 202:
        _poll_lro(
            requests,
            _operation_location(response.headers),
            headers,
            timeout_s=_lro_timeout_s,
            sleep_fn=_lro_sleep,
        )
        return
    raise RuntimeError(
        f"Failed to delete Ontology '{ontology_item_id}': "
        f"HTTP {response.status_code}: {response.text[:500]}"
    )


# ---------------------------------------------------------------------------
# update_ontology_definition — push the REAL Fabric format to populate graph
# ---------------------------------------------------------------------------


def update_ontology_definition(
    workspace_id: str,
    ontology_item_id: str,
    parts: list[dict],
    mock: bool = False,
    token_provider: Callable[[], str] | None = None,
    *,
    _lro_sleep: Callable[[float], None] | None = None,
    _lro_max_attempts: int = 60,
    _lro_timeout_s: float = 300,
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
        f"/ontologies/{ontology_item_id}/updateDefinition?updateMetadata=true"
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
        location = _operation_location(resp.headers)
        retry_after = resp.headers.get("Retry-After", "5")
        try:
            operation = _poll_lro(
                requests,
                location,
                headers,
                initial_retry_after=float(retry_after),
                max_attempts=_lro_max_attempts,
                timeout_s=_lro_timeout_s,
                sleep_fn=_lro_sleep,
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            logger.error(
                "[fabric_ontology] updateDefinition LRO failed: %s",
                exc,
            )
            sys.exit(1)
        note = (
            f"updateDefinition completed (202 LRO) for item '{ontology_item_id}'. "
            f"{parts_count} parts pushed."
        )
        logger.info(
            "[fabric_ontology] updateDefinition 202 LRO completed, location=%s",
            location,
        )
        return {
            "parts_count": parts_count,
            "status": "ok-202",
            "note": note,
            "location": location,
            "retry_after": retry_after,
            "operation": operation,
        }

    logger.error(
        "[fabric_ontology] updateDefinition returned unexpected status %s: %s",
        resp.status_code,
        resp.text[:500],
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# get_ontology_definition — persisted definition read-back
# ---------------------------------------------------------------------------


def get_ontology_definition(
    workspace_id: str,
    ontology_item_id: str,
    *,
    token_provider: Callable[[], str] | None = None,
    requests_module: Any | None = None,
    _lro_sleep: Callable[[float], None] | None = None,
    _lro_max_attempts: int = 60,
    _lro_timeout_s: float = 300,
) -> dict[str, Any]:
    """Read the persisted Fabric Ontology definition.

    A successful updateDefinition response is not deployment evidence. This
    function calls the item-specific getDefinition endpoint and follows a 202
    operation to terminal completion before returning the persisted definition.
    """
    if not workspace_id or not ontology_item_id:
        raise ValueError(
            "workspace_id and ontology_item_id are required for Ontology read-back."
        )
    if requests_module is None:
        import requests as requests_module  # type: ignore[no-redef]

    tp = token_provider or _default_token_provider
    token = tp()
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
    }
    url = (
        f"{_FABRIC_API_BASE}/workspaces/{workspace_id}"
        f"/ontologies/{ontology_item_id}/getDefinition"
    )
    try:
        response = requests_module.post(
            url,
            headers=headers,
            json={},
            timeout=120,
        )
    except requests_module.RequestException as exc:
        raise RuntimeError(
            f"Ontology getDefinition request failed: {exc}"
        ) from exc

    if response.status_code in {401, 403}:
        raise PermissionError(
            "Fabric rejected Ontology getDefinition. Check workspace and "
            "Ontology permissions."
        )
    if response.status_code == 429:
        raise RuntimeError(
            "Fabric rate limited Ontology getDefinition. Retry-After: "
            f"{response.headers.get('Retry-After', '60')}s."
        )

    if response.status_code == 200:
        try:
            body = response.json()
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "Ontology getDefinition returned malformed JSON."
            ) from exc
        if not isinstance(body, dict):
            raise RuntimeError(
                "Ontology getDefinition returned a non-object response."
            )
        definition = body.get("definition", body)
        if not isinstance(definition, dict):
            raise RuntimeError(
                "Ontology getDefinition response does not contain a definition."
            )
        return definition

    if response.status_code == 202:
        operation_location = _operation_location(response.headers)
        operation = _poll_lro(
            requests_module,
            operation_location,
            headers,
            initial_retry_after=float(
                response.headers.get("Retry-After", "5")
            ),
            max_attempts=_lro_max_attempts,
            timeout_s=_lro_timeout_s,
            sleep_fn=_lro_sleep,
        )
        definition = operation.get("definition")
        if not isinstance(definition, dict):
            result = operation.get("result")
            definition = (
                result.get("definition", result)
                if isinstance(result, dict)
                else None
            )
        if isinstance(definition, dict):
            return definition

        result_location = (
            operation_location
            if operation_location.rstrip("/").endswith("/result")
            else operation_location.rstrip("/") + "/result"
        )
        try:
            result_response = requests_module.get(
                result_location,
                headers=headers,
                timeout=120,
            )
        except requests_module.RequestException as exc:
            raise RuntimeError(
                f"Ontology getDefinition result request failed: {exc}"
            ) from exc
        if result_response.status_code in {401, 403}:
            raise PermissionError(
                "Fabric rejected the Ontology getDefinition result request."
            )
        if result_response.status_code != 200:
            raise RuntimeError(
                "Ontology getDefinition LRO completed but its result could "
                f"not be read: HTTP {result_response.status_code}: "
                f"{result_response.text[:500]}"
            )
        try:
            result_body = result_response.json()
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "Ontology getDefinition result returned malformed JSON."
            ) from exc
        if not isinstance(result_body, dict):
            raise RuntimeError(
                "Ontology getDefinition result returned a non-object response."
            )
        definition = result_body.get("definition", result_body)
        if not isinstance(definition, dict):
            raise RuntimeError(
                "Ontology getDefinition result does not contain a definition."
            )
        return definition

    raise RuntimeError(
        f"Ontology getDefinition failed: HTTP {response.status_code}: "
        f"{response.text[:500]}"
    )


# ---------------------------------------------------------------------------
# M6 SRV-008: Ontology refresh operation and guided/manual blocking state
# ---------------------------------------------------------------------------


def get_ontology_refresh_state(
    workspace_id: str,
    ontology_item_id: str,
    *,
    mock: bool = False,
    token_provider: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Query the current refresh/definition state of an Ontology item.

    Returns a dict with:
        item_id (str)   — the Fabric Ontology item GUID
        state (str)     — "available" | "refreshing" | "stale" | "unknown" | "mock"
        last_modified (str | None)  — ISO-8601 timestamp of last updateDefinition
        capability (str) — "available" | "absent" (if endpoint not supported)
        note (str)       — human-readable message

    When ``capability`` is "absent", callers must use guided-manual or block
    until the capability becomes available (no success-shaped fallback).
    """
    if mock:
        return {
            "item_id": ontology_item_id,
            "state": "mock",
            "last_modified": None,
            "capability": "available",
            "note": f"MOCK: would query refresh state for item '{ontology_item_id}'.",
        }

    import requests  # noqa: PLC0415

    tp = token_provider or _default_token_provider
    token = tp()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # Try to get the item metadata — this gives last-modified and status
    url = f"{_FABRIC_API_BASE}/workspaces/{workspace_id}/items/{ontology_item_id}"
    try:
        resp = requests.get(url, headers=headers, timeout=30)
    except requests.RequestException as exc:
        logger.error("[fabric_ontology] GET item state failed: %s", exc)
        sys.exit(1)

    if resp.status_code == 404:
        # Capability absent or item not found — explicit manual blocking
        logger.warning(
            "[fabric_ontology] Ontology item '%s' not found (404). "
            "Refresh state query requires an existing item. "
            "Create the item first or wait for capability rollout.",
            ontology_item_id,
        )
        return {
            "item_id": ontology_item_id,
            "state": "not-found",
            "last_modified": None,
            "capability": "absent",
            "note": (
                f"Ontology item '{ontology_item_id}' not found or endpoint unavailable (404). "
                "Create the item first or await feature rollout. "
                "Manual deployment required until capability is present."
            ),
        }

    if resp.status_code == 401:
        logger.error("[fabric_ontology] 401 querying ontology state.")
        sys.exit(6)

    if resp.ok:
        body = resp.json()
        # Extract last-modified from common fields
        last_mod = (
            body.get("lastModifiedDate")
            or body.get("modifiedDate")
            or body.get("updatedAt")
        )
        return {
            "item_id": ontology_item_id,
            "state": "available",
            "last_modified": last_mod,
            "capability": "available",
            "note": f"Ontology item '{ontology_item_id}' is available. Last modified: {last_mod}.",
        }

    logger.error(
        "[fabric_ontology] Unexpected status %s querying ontology state.",
        resp.status_code,
    )
    sys.exit(1)
