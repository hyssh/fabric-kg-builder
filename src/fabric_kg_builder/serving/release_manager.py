"""serving.release_manager — typed Search REST release manager.

M6 SRV-005: Typed release manager for Azure AI Search index lifecycle.

Provides:
  - get_or_create_index     : idempotent index creation; returns stored schema on reuse
  - validate_stored_schema  : compare stored index schema against expected config
  - count_probe             : verify document count; malformed body → failure (not 0)
  - text_query_probe        : BM25 text-only smoke-test (alias: hybrid_query_probe)
  - vector_query_probe      : true vector/hybrid search with an actual query vector
  - citation_sample_probe   : sample citation/lineage fields; empty or missing → failure
  - atomic_alias_cutover    : swap stable alias; unexpected GET → fail; verifies target exists
  - rollback                : point alias back to previous version on post-cutover failure
  - ReleaseResult           : typed container with explicit partial failures

Pre-cutover gate contract:
  count_probe → text_query_probe → vector_query_probe → citation_sample_probe
  All must pass BEFORE alias_cutover is called.  If any fails the alias is not
  touched.  On post-cutover verification failure, rollback() is called and both
  the original failure and rollback result are surfaced.

Auth header contract:
  When token_provider is set, every request carries ``Authorization: Bearer <token>``.
  Tests verify the header key and prefix without exposing token values.

429/Retry-After (M6 scope):
  If the server returns 429, the result carries a structured
  ``partial_failures`` entry with ``"probe": "rate_limited"`` and
  ``"retry_after"`` seconds so callers can back off.  Broader LRO retry
  is M9 scope.

All network calls go through an injectable ``SearchTransport`` protocol so tests
run without any real Azure AI Search endpoint.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

logger = logging.getLogger(__name__)

_API_VERSION = "2024-07-01"
_ALIAS_API_VERSION = "2026-04-01"

# Canonical citation/lineage fields required in Search docs (SRV-002).
CANONICAL_LINEAGE_FIELDS: list[str] = [
    "project_id",
    "asset_id",
    "asset_version_id",
    "run_id",
    "source_file_id",
    "source_locator_json",
    "schema_version",
    "domain_hash",
]


def _project_schema_to_expected_shape(
    stored: Any,
    expected: Any,
) -> Any:
    """Remove Azure AI Search response defaults before schema comparison."""
    if isinstance(expected, dict) and isinstance(stored, dict):
        return {
            key: _project_schema_to_expected_shape(stored[key], value)
            for key, value in expected.items()
            if key in stored
        }
    if isinstance(expected, list) and isinstance(stored, list):
        if expected and all(isinstance(item, dict) and "name" in item for item in expected):
            stored_by_name = {
                item.get("name"): item
                for item in stored
                if isinstance(item, dict) and "name" in item
            }
            return [
                _project_schema_to_expected_shape(stored_by_name[item["name"]], item)
                for item in expected
                if item["name"] in stored_by_name
            ]
        return [
            _project_schema_to_expected_shape(actual, desired)
            for actual, desired in zip(stored, expected)
        ]
    return stored


# ---------------------------------------------------------------------------
# Transport protocol — injectable for tests
# ---------------------------------------------------------------------------


class SearchTransport(Protocol):
    """Minimal transport protocol for Azure AI Search REST calls."""

    def get(self, url: str, headers: dict[str, str]) -> "_Response":
        ...

    def put(self, url: str, headers: dict[str, str], json: dict) -> "_Response":
        ...

    def post(self, url: str, headers: dict[str, str], json: dict) -> "_Response":
        ...

    def delete(self, url: str, headers: dict[str, str]) -> "_Response":
        ...


@dataclass
class _Response:
    status_code: int
    body: Any = None

    @property
    def ok(self) -> bool:
        return self.status_code < 400


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class ReleaseResult:
    """Typed result of a release manager operation.

    Attributes
    ----------
    ok:
        True iff the operation succeeded (no errors, no partial failures).
    index_name:
        Physical index name that was operated on.
    alias:
        Alias that was updated (or None when not applicable).
    docs_found:
        Number of documents found in the index (probe operations).
    errors:
        List of error messages — non-empty means partial or full failure.
    partial_failures:
        Structured list of partial failure dicts (probe failures, etc.).
    metadata:
        Arbitrary extra data from the operation.
    """

    ok: bool
    index_name: str
    alias: Optional[str] = None
    docs_found: int = 0
    errors: list[str] = field(default_factory=list)
    partial_failures: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Fake transport for tests
# ---------------------------------------------------------------------------


class FakeSearchTransport:
    """In-memory fake transport — no network calls.

    Maintains a dict of ``{index_name: {schema, docs}}`` and alias mappings.
    ``_last_search_body`` holds the most recent POST /docs/search body for inspection.
    ``_override_count`` overrides the $count response body.  Use ``_COUNT_NOT_SET``
    sentinel (the default) to mean "not overridden".  Set to any value (including
    ``None`` or a non-int string) to inject a specific body that will be returned
    verbatim — useful for testing malformed-body failure paths.
    """

    _COUNT_NOT_SET: object = object()

    def __init__(self) -> None:
        self._indexes: dict[str, dict[str, Any]] = {}
        self._aliases: dict[str, str] = {}
        self._call_log: list[tuple[str, str]] = []
        self._last_search_body: dict[str, Any] = {}
        # Set to any value (including None) to override $count response body.
        # Leave as _COUNT_NOT_SET to return the real count from _indexes.
        self._override_count: Any = FakeSearchTransport._COUNT_NOT_SET

    def get(self, url: str, headers: dict[str, str]) -> _Response:
        self._call_log.append(("GET", url))
        # Check $count before general index GET (both contain "/indexes/")
        if "/docs/$count" in url:
            index_name = url.split("/indexes/")[1].split("/docs/$count")[0]
            if self._override_count is not FakeSearchTransport._COUNT_NOT_SET:
                return _Response(200, self._override_count)
            count = len(self._indexes.get(index_name, {}).get("docs", []))
            return _Response(200, count)
        if "/indexes/" in url and "/docs/search" not in url:
            # GET index — return 200 with schema if index exists, 404 otherwise
            index_name = url.split("/indexes/")[1].split("?")[0]
            if index_name in self._indexes:
                return _Response(200, {"name": index_name, **self._indexes[index_name].get("schema", {})})
            return _Response(404, {"error": {"code": "IndexNotFound"}})
        if "/aliases/" in url:
            alias_name = url.split("/aliases/")[1].split("?")[0]
            if alias_name in self._aliases:
                return _Response(200, {"name": alias_name, "indexes": [self._aliases[alias_name]]})
            return _Response(404, {"error": {"code": "AliasNotFound"}})
        if "/operations/" in url:
            return _Response(200, {"status": "Succeeded", "resourceId": "fake-graph-model-id"})
        return _Response(200, {})

    def put(self, url: str, headers: dict[str, str], json: dict) -> _Response:
        self._call_log.append(("PUT", url))
        if "/indexes/" in url:
            index_name = url.split("/indexes/")[1].split("?")[0]
            self._indexes.setdefault(index_name, {"docs": []})["schema"] = json
            return _Response(201, {"name": index_name})
        if "/aliases/" in url:
            alias_name = url.split("/aliases/")[1].split("?")[0]
            self._aliases[alias_name] = json.get("indexes", [None])[0]
            return _Response(200, {"name": alias_name})
        return _Response(200, {})

    def post(self, url: str, headers: dict[str, str], json: dict) -> _Response:
        self._call_log.append(("POST", url))
        if "/docs/search" in url:
            index_name = url.split("/indexes/")[1].split("/docs/search")[0]
            self._last_search_body = json
            docs = self._indexes.get(index_name, {}).get("docs", [])
            hits = docs[:3] if docs else []
            return _Response(200, {"value": hits})
        if "/docs/index" in url:
            index_name = url.split("/indexes/")[1].split("/docs/index")[0]
            values = json.get("value", [])
            self._indexes.setdefault(index_name, {"schema": {}})["docs"] = (
                self._indexes[index_name].get("docs", []) + values
            )
            return _Response(
                200,
                {"value": [{"key": v.get("chunk_id", str(i)), "status": True} for i, v in enumerate(values)]},
            )
        return _Response(200, {})

    def delete(self, url: str, headers: dict[str, str]) -> _Response:
        self._call_log.append(("DELETE", url))
        if "/indexes/" in url:
            index_name = url.split("/indexes/")[1].split("?")[0]
            self._indexes.pop(index_name, None)
            return _Response(204)
        if "/aliases/" in url:
            alias_name = url.split("/aliases/")[1].split("?")[0]
            self._aliases.pop(alias_name, None)
            return _Response(204)
        return _Response(204)

    def index_exists(self, name: str) -> bool:
        return name in self._indexes

    def alias_target(self, alias: str) -> Optional[str]:
        return self._aliases.get(alias)


# ---------------------------------------------------------------------------
# Real transport (uses requests)
# ---------------------------------------------------------------------------


class _RequestsTransport:
    def __init__(self, timeout: int = 60) -> None:
        self._timeout = timeout

    def _parse(self, r: Any) -> tuple[int, Any]:
        try:
            body = r.json()
        except ValueError:  # JSONDecodeError is a ValueError subclass
            body = r.text
        return r.status_code, body

    def get(self, url: str, headers: dict[str, str]) -> _Response:
        import requests  # type: ignore[import]

        r = requests.get(url, headers=headers, timeout=self._timeout)
        sc, body = self._parse(r)
        return _Response(sc, body)

    def put(self, url: str, headers: dict[str, str], json: dict) -> _Response:
        import requests  # type: ignore[import]

        r = requests.put(url, headers=headers, json=json, timeout=self._timeout)
        sc, body = self._parse(r)
        return _Response(sc, body)

    def post(self, url: str, headers: dict[str, str], json: dict) -> _Response:
        import requests  # type: ignore[import]

        r = requests.post(url, headers=headers, json=json, timeout=self._timeout)
        sc, body = self._parse(r)
        return _Response(sc, body)

    def delete(self, url: str, headers: dict[str, str]) -> _Response:
        import requests  # type: ignore[import]

        r = requests.delete(url, headers=headers, timeout=self._timeout)
        sc, body = self._parse(r)
        return _Response(sc, body)


# ---------------------------------------------------------------------------
# Release Manager
# ---------------------------------------------------------------------------


class ReleaseManager:
    """Typed Azure AI Search index release manager.

    Parameters
    ----------
    endpoint:
        Azure AI Search service endpoint.
    token_provider:
        Callable returning a bearer token string.  Not required when
        ``transport`` is a fake/mock.
    transport:
        Injectable HTTP transport.  Defaults to a real requests-based transport.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        token_provider: Optional[Callable[[], str]] = None,
        transport: Optional[SearchTransport] = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._token_provider = token_provider
        self._transport: SearchTransport = transport or _RequestsTransport()

    def _headers(self) -> dict[str, str]:
        """Return request headers.

        When token_provider is set the ``Authorization`` header is
        ``Bearer <token>``.  Tests can call ``_headers()`` directly to verify
        the header key and prefix without exposing token values.
        """
        if self._token_provider is not None:
            tok = self._token_provider()
            return {
                "Authorization": f"Bearer {tok}",
                "Content-Type": "application/json",
            }
        return {"Content-Type": "application/json"}

    def _v(self) -> str:
        return f"api-version={_API_VERSION}"

    def _alias_v(self) -> str:
        return f"api-version={_ALIAS_API_VERSION}"

    # ------------------------------------------------------------------
    # Index lifecycle
    # ------------------------------------------------------------------

    def get_or_create_index(
        self,
        index_name: str,
        schema_dict: dict[str, Any],
    ) -> ReleaseResult:
        """Idempotently create (or verify) an AI Search index.

        When the index already exists the stored schema is included in
        ``metadata["stored_schema"]`` so callers can validate it with
        :meth:`validate_stored_schema` before uploading documents.
        """
        headers = self._headers()
        get_url = f"{self._endpoint}/indexes/{index_name}?{self._v()}"
        resp = self._transport.get(get_url, headers)

        if resp.status_code == 200:
            logger.info("[release_manager] Index '%s' already exists.", index_name)
            return ReleaseResult(
                ok=True, index_name=index_name,
                metadata={"action": "reused", "stored_schema": resp.body or {}},
            )

        if resp.status_code != 404:
            err = f"GET index '{index_name}' returned unexpected {resp.status_code}: {resp.body}"
            logger.error("[release_manager] %s", err)
            return ReleaseResult(ok=False, index_name=index_name, errors=[err])

        from fabric_kg_builder.deploy.search_deployer import _strip_underscore_keys

        # Compiled schemas retain underscore-prefixed annotations for humans and
        # local validation, but Azure AI Search rejects those REST payload keys.
        schema = _strip_underscore_keys(schema_dict)
        schema["name"] = index_name
        put_url = f"{self._endpoint}/indexes/{index_name}?{self._v()}"
        put_resp = self._transport.put(put_url, headers, schema)

        if put_resp.status_code in (200, 201):
            logger.info("[release_manager] Created index '%s' (%s).", index_name, put_resp.status_code)
            return ReleaseResult(ok=True, index_name=index_name, metadata={"action": "created"})

        err = f"PUT index '{index_name}' failed with {put_resp.status_code}: {put_resp.body}"
        logger.error("[release_manager] %s", err)
        return ReleaseResult(ok=False, index_name=index_name, errors=[err])

    def validate_stored_schema(
        self,
        phys_name: str,
        expected_schema: dict[str, Any],
        expected_embedding_model: str,
        expected_dimensions: int,
        stored_schema: Optional[dict[str, Any]] = None,
    ) -> ReleaseResult:
        """Validate the stored index schema against expected configuration.

        This is a genuine validation — NOT circular with the fingerprint used to
        derive the physical name.  Compares vector field dimensions and schema
        fingerprint so that a physical index created under the same name but with
        a different schema is caught *before* any document upload.

        Parameters
        ----------
        stored_schema:
            Schema from ``get_or_create_index`` ``metadata["stored_schema"]``.
            When None the method GETs the index.
        """
        if stored_schema is None:
            headers = self._headers()
            resp = self._transport.get(f"{self._endpoint}/indexes/{phys_name}?{self._v()}", headers)
            if not resp.ok:
                err = f"Cannot validate stored schema for '{phys_name}': GET returned {resp.status_code}"
                return ReleaseResult(ok=False, index_name=phys_name, errors=[err])
            stored_schema = resp.body or {}

        for f in stored_schema.get("fields", []):
            if f.get("type") == "Collection(Edm.Single)":
                stored_dims = f.get("dimensions")
                if stored_dims is not None and stored_dims != expected_dimensions:
                    err = (
                        f"Stored index '{phys_name}' has dimensions={stored_dims}, "
                        f"expected {expected_dimensions}. "
                        "Embedding dimensions are immutable per versioned index."
                    )
                    return ReleaseResult(ok=False, index_name=phys_name, errors=[err])

        from fabric_kg_builder.serving.index_version import compute_index_fingerprint
        expected_fp = compute_index_fingerprint(expected_schema, expected_embedding_model, expected_dimensions)
        stored_fp = compute_index_fingerprint(
            _project_schema_to_expected_shape(stored_schema, expected_schema),
            expected_embedding_model,
            expected_dimensions,
        )
        if expected_fp != stored_fp:
            err = (
                f"Schema fingerprint mismatch for physical index '{phys_name}': "
                f"stored={stored_fp!r} expected={expected_fp!r}. "
                "The stored index was created with a different schema."
            )
            return ReleaseResult(ok=False, index_name=phys_name, errors=[err])

        return ReleaseResult(ok=True, index_name=phys_name, metadata={"fingerprint_match": expected_fp})


    # ------------------------------------------------------------------
    # Pre-cutover probes
    # ------------------------------------------------------------------

    def count_probe(self, index_name: str) -> ReleaseResult:
        """Query document count in *index_name*.

        A malformed (non-integer) count body is an explicit failure.
        Returns docs_found=-1 and ok=False on any error.
        """
        headers = self._headers()
        url = f"{self._endpoint}/indexes/{index_name}/docs/$count?{self._v()}"
        resp = self._transport.get(url, headers)

        if resp.status_code == 429:
            err = f"count_probe '{index_name}': rate limited (429)"
            return ReleaseResult(
                ok=False, index_name=index_name, docs_found=-1, errors=[err],
                partial_failures=[{"probe": "count", "error": "rate_limited",
                                   "retry_after": _parse_retry_after(resp)}],
            )

        if not resp.ok:
            err = f"count_probe '{index_name}' returned {resp.status_code}"
            logger.error("[release_manager] %s", err)
            return ReleaseResult(
                ok=False, index_name=index_name, docs_found=-1, errors=[err],
                partial_failures=[{"probe": "count", "status_code": resp.status_code}],
            )

        try:
            count = int(resp.body)
        except (TypeError, ValueError) as exc:
            err = f"count_probe '{index_name}': malformed count body {resp.body!r}: {exc}"
            logger.error("[release_manager] %s", err)
            return ReleaseResult(
                ok=False, index_name=index_name, docs_found=-1, errors=[err],
                partial_failures=[{"probe": "count", "error": "malformed_body",
                                   "body": str(resp.body)[:200]}],
            )

        logger.info("[release_manager] count_probe '%s': %d docs", index_name, count)
        return ReleaseResult(ok=True, index_name=index_name, docs_found=count)

    def text_query_probe(
        self,
        index_name: str,
        query_text: str = "test",
        text_field: str = "content",
    ) -> ReleaseResult:
        """BM25 text-only search probe.

        Does NOT send a vector — use :meth:`vector_query_probe` for vector/hybrid.
        Zero hits is a pass (index may be empty); an HTTP error is a failure.
        """
        headers = self._headers()
        url = f"{self._endpoint}/indexes/{index_name}/docs/search?{self._v()}"
        body: dict[str, Any] = {
            "search": query_text,
            "searchFields": text_field,
            "top": 3,
            "select": ",".join([text_field, "chunk_id", "document_element_id",
                                 "project_id", "asset_id", "run_id"]),
        }
        resp = self._transport.post(url, headers, body)

        if resp.status_code == 429:
            err = f"text_query_probe '{index_name}': rate limited (429)"
            return ReleaseResult(
                ok=False, index_name=index_name, errors=[err],
                partial_failures=[{"probe": "text_query", "error": "rate_limited",
                                   "retry_after": _parse_retry_after(resp)}],
            )

        if not resp.ok:
            err = f"text_query_probe '{index_name}' returned {resp.status_code}: {resp.body}"
            logger.error("[release_manager] %s", err)
            return ReleaseResult(
                ok=False, index_name=index_name, errors=[err],
                partial_failures=[{"probe": "text_query", "status_code": resp.status_code}],
            )

        hits = resp.body.get("value", []) if isinstance(resp.body, dict) else []
        logger.info("[release_manager] text_query_probe '%s': %d hits", index_name, len(hits))
        return ReleaseResult(
            ok=True, index_name=index_name, docs_found=len(hits),
            metadata={"mode": "text-only", "hits_sample": hits[:1]},
        )

    def hybrid_query_probe(
        self,
        index_name: str,
        query_text: str = "test",
        vector_field: Optional[str] = None,
        text_field: str = "content",
        query_vector: Optional[list] = None,
    ) -> ReleaseResult:
        """Text or hybrid search probe (backward-compatible entry point).

        When ``query_vector`` + ``vector_field`` are provided a true hybrid
        request is sent.  Otherwise falls back to :meth:`text_query_probe`.
        ``metadata["mode"]`` is ``"hybrid"``, ``"vector"``, or ``"text-only"``.
        """
        if query_vector is not None and vector_field:
            return self.vector_query_probe(
                index_name, query_vector=query_vector, vector_field=vector_field,
                query_text=query_text, text_field=text_field,
            )
        return self.text_query_probe(index_name, query_text=query_text, text_field=text_field)

    def vector_query_probe(
        self,
        index_name: str,
        query_vector: list,
        vector_field: str = "chunk_vector",
        query_text: Optional[str] = None,
        text_field: str = "content",
        top: int = 3,
    ) -> ReleaseResult:
        """True vector (or hybrid) search probe.

        Sends a vectorized query.  When *query_text* is also set, the request
        is hybrid (text + vector).  Uses exhaustive kNN so small test indexes work.
        """
        headers = self._headers()
        url = f"{self._endpoint}/indexes/{index_name}/docs/search?{self._v()}"
        body: dict[str, Any] = {
            "top": top,
            "select": ",".join(["chunk_id", "document_element_id", "project_id", "asset_id"]),
            "vectors": [{"value": query_vector, "fields": vector_field, "k": top, "exhaustive": True}],
        }
        if query_text:
            body["search"] = query_text
            body["searchFields"] = text_field

        mode = "hybrid" if query_text else "vector"
        resp = self._transport.post(url, headers, body)

        if resp.status_code == 429:
            err = f"vector_query_probe '{index_name}': rate limited (429)"
            return ReleaseResult(
                ok=False, index_name=index_name, errors=[err],
                partial_failures=[{"probe": "vector_query", "error": "rate_limited",
                                   "retry_after": _parse_retry_after(resp)}],
            )

        if not resp.ok:
            err = f"vector_query_probe '{index_name}' returned {resp.status_code}: {resp.body}"
            logger.error("[release_manager] %s", err)
            return ReleaseResult(
                ok=False, index_name=index_name, errors=[err],
                partial_failures=[{"probe": "vector_query", "status_code": resp.status_code}],
            )

        hits = resp.body.get("value", []) if isinstance(resp.body, dict) else []
        logger.info("[release_manager] vector_query_probe '%s' (%s): %d hits", index_name, mode, len(hits))
        return ReleaseResult(
            ok=True, index_name=index_name, docs_found=len(hits),
            metadata={"mode": mode, "hits_sample": hits[:1]},
        )

    def citation_sample_probe(
        self,
        index_name: str,
        query_text: str = "test",
        lineage_fields: Optional[list[str]] = None,
    ) -> ReleaseResult:
        """Sample citation/lineage fields from a search result.

        Requires at least one document returned AND all required lineage fields
        to be present (non-None).  An empty result set or any missing required
        field is an explicit failure — NOT ok=True with a partial/empty sample.
        """
        _required = lineage_fields or CANONICAL_LINEAGE_FIELDS
        select = ",".join(_required)
        headers = self._headers()
        url = f"{self._endpoint}/indexes/{index_name}/docs/search?{self._v()}"
        body = {"search": query_text, "top": 1, "select": select}
        resp = self._transport.post(url, headers, body)

        if resp.status_code == 429:
            err = f"citation_sample_probe '{index_name}': rate limited (429)"
            return ReleaseResult(
                ok=False, index_name=index_name, errors=[err],
                partial_failures=[{"probe": "citation_sample", "error": "rate_limited",
                                   "retry_after": _parse_retry_after(resp)}],
            )

        if not resp.ok:
            err = f"citation_sample_probe '{index_name}' returned {resp.status_code}"
            logger.error("[release_manager] %s", err)
            return ReleaseResult(
                ok=False, index_name=index_name, errors=[err],
                partial_failures=[{"probe": "citation_sample", "status_code": resp.status_code}],
            )

        hits = resp.body.get("value", []) if isinstance(resp.body, dict) else []
        if not hits:
            err = (
                f"citation_sample_probe '{index_name}': no documents returned. "
                "Index may be empty or lineage fields were not indexed."
            )
            logger.error("[release_manager] %s", err)
            return ReleaseResult(
                ok=False, index_name=index_name, docs_found=0, errors=[err],
                partial_failures=[{"probe": "citation_sample", "error": "no_hits"}],
            )

        sample = hits[0]
        missing = [f for f in _required if sample.get(f) is None]
        if missing:
            err = (
                f"citation_sample_probe '{index_name}': required lineage fields absent: {missing}. "
                "Ensure all canonical lineage fields are populated before upload."
            )
            logger.error("[release_manager] %s", err)
            return ReleaseResult(
                ok=False, index_name=index_name, docs_found=1, errors=[err],
                partial_failures=[{"probe": "citation_sample", "missing_fields": missing}],
            )

        logger.info(
            "[release_manager] citation_sample_probe '%s': all %d lineage fields present",
            index_name, len(_required),
        )
        return ReleaseResult(
            ok=True, index_name=index_name, docs_found=len(hits),
            metadata={"sample": sample, "lineage_fields_present": _required},
        )

    # ------------------------------------------------------------------
    # Alias lifecycle
    # ------------------------------------------------------------------

    def atomic_alias_cutover(
        self,
        alias: str,
        target_index_name: str,
        *,
        previous_index_name: Optional[str] = None,
    ) -> ReleaseResult:
        """Atomically point *alias* at *target_index_name*.

        Pre-conditions enforced:
          1. Target index must exist (GET returns 200).
          2. Existing alias GET must be 200 or 404 — any other status is a hard
             failure and the alias is NOT modified.

        The previous target is in ``metadata["previous_target"]`` for rollback.
        """
        headers = self._headers()

        # 1. Verify target index exists before touching the alias.
        idx_url = f"{self._endpoint}/indexes/{target_index_name}?{self._v()}"
        idx_resp = self._transport.get(idx_url, headers)
        if idx_resp.status_code != 200:
            err = (
                f"atomic_alias_cutover: target index '{target_index_name}' "
                f"not found (GET returned {idx_resp.status_code}). Alias not modified."
            )
            logger.error("[release_manager] %s", err)
            return ReleaseResult(ok=False, index_name=target_index_name, alias=alias, errors=[err])

        # 2. Discover existing alias — only 200 or 404 accepted.
        get_url = f"{self._endpoint}/aliases/{alias}?{self._alias_v()}"
        get_resp = self._transport.get(get_url, headers)
        if get_resp.status_code not in (200, 404):
            err = (
                f"atomic_alias_cutover: GET alias '{alias}' returned unexpected "
                f"{get_resp.status_code}. Alias not modified."
            )
            logger.error("[release_manager] %s", err)
            return ReleaseResult(ok=False, index_name=target_index_name, alias=alias, errors=[err])

        existing_target: Optional[str] = None
        if get_resp.status_code == 200 and isinstance(get_resp.body, dict):
            existing_target = (get_resp.body.get("indexes") or [None])[0]

        # 3. PUT the alias.
        put_url = f"{self._endpoint}/aliases/{alias}?{self._alias_v()}"
        body = {"name": alias, "indexes": [target_index_name]}
        put_resp = self._transport.put(put_url, headers, body)

        if put_resp.status_code in (200, 201, 204):
            logger.info(
                "[release_manager] alias '%s' -> '%s' (was '%s')",
                alias, target_index_name, existing_target,
            )
            return ReleaseResult(
                ok=True, index_name=target_index_name, alias=alias,
                metadata={
                    "action": "cutover",
                    "previous_target": existing_target or previous_index_name,
                },
            )

        err = f"alias cutover '{alias}' -> '{target_index_name}' failed: {put_resp.status_code}"
        logger.error("[release_manager] %s", err)
        return ReleaseResult(ok=False, index_name=target_index_name, alias=alias, errors=[err])

    def rollback(self, alias: str, previous_index_name: str) -> ReleaseResult:
        """Point *alias* back at *previous_index_name* (rollback after post-cutover failure)."""
        logger.info("[release_manager] ROLLBACK alias '%s' -> '%s'", alias, previous_index_name)
        result = self.atomic_alias_cutover(alias, previous_index_name)
        if result.ok:
            result.metadata["action"] = "rollback"
        return result


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _parse_retry_after(resp: _Response) -> int:
    """Extract Retry-After value from a 429 response body or default to 5."""
    if isinstance(resp.body, dict):
        try:
            return int(resp.body.get("Retry-After", 5))
        except (TypeError, ValueError):
            return 5
    return 5
