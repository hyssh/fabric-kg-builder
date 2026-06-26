"""deploy.search_deployer — create/update Azure AI Search indexes and upload docs.

Uses the Azure AI Search REST API directly (no SDK dependency) with
DefaultAzureCredential for OAuth Bearer token auth.

Schema fields starting with ``"_"`` (e.g. ``_comment``, ``_schema_version``)
are stripped before any REST call.

Vector field handling
---------------------
If the schema already contains a ``vectorSearch`` section it is passed through
as-is (the kg-chunks index.schema.json already has one).  If any field has a
``"dimensions"`` attribute but the schema lacks a ``vectorSearch`` section, a
minimal HNSW profile is injected automatically so the PUT doesn't fail.

Usage (mock / test — no network)::

    result = deploy_index(
        endpoint="https://example-search.search.windows.net",
        index_name="kg-dev-chunks",
        schema_dict=json.loads(Path("build/search/kg-chunks/index.schema.json").read_text()),
        docs=[],
        mock=True,
    )
    # {"index_name": "kg-dev-chunks", "schema_pushed": True, "docs_pushed": 0, "mock": True, ...}

Usage (live)::

    result = deploy_index(
        endpoint="https://example-search.search.windows.net",
        index_name="kg-dev-chunks",
        schema_dict=schema,
        docs=docs,
    )
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_API_VERSION = "2024-07-01"
_BATCH_SIZE = 1000

# Minimal HNSW vectorSearch config injected when schema has vector fields but
# no vectorSearch section.  Should not normally be needed — our real schema
# already ships a full vectorSearch block.
_DEFAULT_VECTOR_SEARCH = {
    "algorithms": [
        {
            "name": "hnsw-config",
            "kind": "hnsw",
            "hnswParameters": {
                "m": 4,
                "efConstruction": 400,
                "efSearch": 500,
                "metric": "cosine",
            },
        }
    ],
    "profiles": [
        {
            "name": "default-hnsw",
            "algorithm": "hnsw-config",
        }
    ],
}


def _strip_underscore_keys(obj: Any) -> Any:
    """Recursively remove all keys starting with ``'_'`` from dicts."""
    if isinstance(obj, dict):
        return {
            k: _strip_underscore_keys(v)
            for k, v in obj.items()
            if not k.startswith("_")
        }
    if isinstance(obj, list):
        return [_strip_underscore_keys(item) for item in obj]
    return obj


def _ensure_vector_search(schema: dict) -> dict:
    """Inject a minimal vectorSearch section when schema has vector fields but none defined."""
    fields = schema.get("fields", [])
    has_vector_field = any(f.get("dimensions") for f in fields)
    if not has_vector_field:
        return schema
    if "vectorSearch" in schema:
        return schema

    logger.warning(
        "[search_deployer] Schema has vector fields but no vectorSearch section; "
        "injecting default HNSW profile."
    )
    patched = dict(schema)
    patched["vectorSearch"] = _DEFAULT_VECTOR_SEARCH
    # Assign the default profile to vector fields that lack an explicit one
    patched_fields = []
    for f in fields:
        if f.get("dimensions") and not f.get("vectorSearchProfile"):
            f = dict(f)
            f["vectorSearchProfile"] = "default-hnsw"
        patched_fields.append(f)
    patched["fields"] = patched_fields
    return patched


def _sanitize_for_rest(schema: dict) -> dict:
    """Repair schema to be valid for the Azure AI Search REST API 2024-07-01.

    Two classes of issues are fixed:

    1. **Semantic configuration** — the generator historically emitted
       ``contentFields`` / ``keywordsFields`` inside ``prioritizedFields``.
       The REST API requires ``prioritizedContentFields`` /
       ``prioritizedKeywordsFields`` (plural, prefixed).  Both the top-level
       ``semantic.configurations`` shape and the legacy ``semanticConfiguration``
       singular key are handled.  Bare-string field-list items are wrapped as
       ``{"fieldName": item}``.

    2. **vectorSearch.vectorizers** — server-side vectorizers are not needed
       (we compute embeddings ourselves) and an incomplete entry causes a 400.
       The ``vectorizers`` list is dropped entirely.  Any ``vectorizer`` key on
       a profile entry is also removed.
    """
    schema = dict(schema)  # shallow copy — avoid mutating caller's dict

    # ------------------------------------------------------------------
    # 1. Fix semantic configuration
    # ------------------------------------------------------------------
    def _wrap_field(item: Any) -> dict:
        """Ensure a field item is {fieldName: ...}, not a bare string."""
        if isinstance(item, str):
            return {"fieldName": item}
        return item

    def _fix_prioritized_fields(pf: dict) -> dict:
        """Rename contentFields/keywordsFields to REST-valid names."""
        pf = dict(pf)
        # titleField — already correct in both old and new shape
        # contentFields → prioritizedContentFields
        if "contentFields" in pf and "prioritizedContentFields" not in pf:
            pf["prioritizedContentFields"] = pf.pop("contentFields")
        if "prioritizedContentFields" in pf:
            pf["prioritizedContentFields"] = [
                _wrap_field(f) for f in pf["prioritizedContentFields"]
            ]
        # keywordsFields → prioritizedKeywordsFields
        if "keywordsFields" in pf and "prioritizedKeywordsFields" not in pf:
            pf["prioritizedKeywordsFields"] = pf.pop("keywordsFields")
        if "prioritizedKeywordsFields" in pf:
            pf["prioritizedKeywordsFields"] = [
                _wrap_field(f) for f in pf["prioritizedKeywordsFields"]
            ]
        # titleField: wrap if bare string
        if "titleField" in pf:
            tf = pf["titleField"]
            if isinstance(tf, str):
                pf["titleField"] = {"fieldName": tf}
        return pf

    def _fix_configurations(configs: list) -> list:
        fixed = []
        for cfg in configs:
            cfg = dict(cfg)
            if "prioritizedFields" in cfg:
                cfg["prioritizedFields"] = _fix_prioritized_fields(cfg["prioritizedFields"])
            fixed.append(cfg)
        return fixed

    # Normalise to REST shape: top-level "semantic" with "configurations" list
    if "semanticConfiguration" in schema and "semantic" not in schema:
        # Legacy singular key emitted by some older generator versions
        raw = schema.pop("semanticConfiguration")
        if isinstance(raw, dict):
            schema["semantic"] = {
                "configurations": _fix_configurations(
                    raw.get("configurations", [raw])
                )
            }
    elif "semantic" in schema:
        sem = dict(schema["semantic"])
        if "configurations" in sem:
            sem["configurations"] = _fix_configurations(sem["configurations"])
        schema["semantic"] = sem

    # ------------------------------------------------------------------
    # 2. Drop vectorizers; clean profiles
    # ------------------------------------------------------------------
    if "vectorSearch" in schema:
        vs = dict(schema["vectorSearch"])
        vs.pop("vectorizers", None)
        if "profiles" in vs:
            vs["profiles"] = [
                {k: v for k, v in p.items() if k != "vectorizer"}
                for p in vs["profiles"]
            ]
        schema["vectorSearch"] = vs

    return schema


def _get_token() -> str:
    """Obtain a Bearer token for Azure AI Search using DefaultAzureCredential."""
    from azure.identity import DefaultAzureCredential  # type: ignore[import]

    cred = DefaultAzureCredential()
    return cred.get_token("https://search.azure.com/.default").token


def deploy_index(
    endpoint: str,
    index_name: str,
    schema_dict: dict,
    docs: list[dict],
    recreate: bool = False,
    mock: bool = False,
    token_provider: Any = None,
) -> dict:
    """Create/update an AI Search index and upload documents via REST API.

    Parameters
    ----------
    endpoint:
        AI Search service endpoint (e.g. ``"https://example-search.search.windows.net"``).
    index_name:
        Fully-qualified deployed index name (e.g. ``"kg-dev-chunks"``).
    schema_dict:
        Raw index schema dict as read from ``index.schema.json``.  ``"_"``-prefixed
        keys are stripped automatically before the PUT call.
    docs:
        Documents to upload (list of dicts).  May be empty.
    recreate:
        When ``True``, DELETE the index before PUT (drops all existing documents).
        Use with care.
    mock:
        When ``True``, log planned actions and return without any network call.
        Safe for offline use and unit tests.
    token_provider:
        Optional callable returning a Bearer token string.  Defaults to
        ``DefaultAzureCredential`` scoped to ``https://search.azure.com/.default``.

    Returns
    -------
    dict
        Keys: ``index_name``, ``schema_pushed`` (bool), ``docs_pushed`` (int),
        ``mock`` (bool), ``errors`` (list[str]).
    """
    clean_schema = _strip_underscore_keys(schema_dict)
    clean_schema = _ensure_vector_search(clean_schema)
    clean_schema = _sanitize_for_rest(clean_schema)
    # Override the schema name with the deployed (prefixed) name
    clean_schema["name"] = index_name

    result: dict[str, Any] = {
        "index_name": index_name,
        "schema_pushed": False,
        "docs_pushed": 0,
        "mock": mock,
        "errors": [],
    }

    if mock:
        logger.info(
            "[search_deployer] MOCK: would PUT index=%s, upload %d docs",
            index_name,
            len(docs),
        )
        result["schema_pushed"] = True
        result["docs_pushed"] = len(docs)
        return result

    import requests  # type: ignore[import]

    _tok_fn = token_provider if token_provider is not None else _get_token
    tok = _tok_fn()
    hdr = {
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json",
    }

    # Optionally delete the index first (recreate=True drops all docs)
    if recreate:
        del_url = f"{endpoint}/indexes/{index_name}?api-version={_API_VERSION}"
        try:
            resp = requests.delete(del_url, headers=hdr, timeout=30)
            if resp.status_code not in (200, 204, 404):
                logger.warning(
                    "[search_deployer] DELETE index returned %s", resp.status_code
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[search_deployer] DELETE index error: %s", exc)

    # PUT index schema (create or update)
    put_url = f"{endpoint}/indexes/{index_name}?api-version={_API_VERSION}"
    try:
        resp = requests.put(put_url, headers=hdr, json=clean_schema, timeout=60)
        resp.raise_for_status()
        result["schema_pushed"] = True
        logger.info(
            "[search_deployer] PUT index %s -> HTTP %s", index_name, resp.status_code
        )
    except Exception as exc:  # noqa: BLE001
        err = f"PUT index failed: {exc}"
        logger.error("[search_deployer] %s", err)
        result["errors"].append(err)
        return result

    if not docs:
        return result

    # Upload docs in batches of _BATCH_SIZE (AI Search max = 1000).
    # allowUnsafeKeys=true permits document keys containing characters like ':'
    # (our IDs are e.g. 'chunk:abc'); retrieval joins on entity_ids/canonical_key,
    # not the key, so readable keys are kept rather than base64-encoded.
    post_url = (
        f"{endpoint}/indexes/{index_name}/docs/index"
        f"?api-version={_API_VERSION}&allowUnsafeKeys=true"
    )
    total_pushed = 0
    for start in range(0, len(docs), _BATCH_SIZE):
        batch = docs[start : start + _BATCH_SIZE]
        actions = [{"@search.action": "mergeOrUpload", **doc} for doc in batch]
        try:
            resp = requests.post(
                post_url, headers=hdr, json={"value": actions}, timeout=120
            )
            resp.raise_for_status()
            body = resp.json()
            # Count confirmed successes; fall back to batch length if no value array
            pushed = sum(1 for v in body.get("value", []) if v.get("status", False))
            batch_pushed = pushed if pushed else len(batch)
            total_pushed += batch_pushed
            logger.info(
                "[search_deployer] batch %d–%d: pushed %d docs",
                start,
                start + len(batch),
                batch_pushed,
            )
        except Exception as exc:  # noqa: BLE001
            err = f"batch upload [{start}:{start + len(batch)}] failed: {exc}"
            logger.error("[search_deployer] %s", err)
            result["errors"].append(err)

    result["docs_pushed"] = total_pushed
    return result
