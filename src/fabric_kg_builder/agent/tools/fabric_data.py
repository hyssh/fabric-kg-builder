"""agent/tools/fabric_data.py — Fabric Data Agent adapter.

Wraps the Fabric Graph / Ontology API so the grounded agent can answer
ontology-route and mixed-route queries.

Domain-neutrality contract
--------------------------
This module contains NO hardcoded domain entity types, relationship names, or
product-specific terminology.  Entity types, relationship names, and query
routing MUST come from either:
  a) explicit caller parameters (entity_type, relationship, child_type), OR
  b) domain metadata loaded from ontology/model.yaml at runtime.

Missing routing config returns status="unsupported" — never silently falls back
to a hardcoded domain type.

Status values
-------------
  FabricDataResult.status = "ok"          → results returned
  FabricDataResult.status = "unsupported" → query type or config not supported
  FabricDataResult.status = "error"       → transient or permanent API error
  FabricDataResult.status = "no_data"    → query executed, 0 rows returned

No secrets are stored in instances; auth is via injected credentials.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from fabric_kg_builder.semantic.query_rendering import (
    compile_approved_query_plan,
    render_bounded_gql,
)
from fabric_kg_builder.semantic.schemas import PersistedQuerySchema


class FabricDataError(Exception):
    """Raised when the Fabric Data Agent API returns an unrecoverable error."""


@dataclass(frozen=True)
class FabricDataResult:
    """Result from a Fabric Data Agent / Graph query.

    Attributes
    ----------
    status:
        "ok" | "unsupported" | "error" | "no_data"
    rows:
        Returned rows (empty for non-ok statuses).
    gql:
        The GQL query that was executed (for debugging; never includes secrets).
    error_message:
        Human-readable error detail for non-ok statuses.
    entity_count:
        Convenience: len(rows) for ok results.
    """

    status: str  # "ok" | "unsupported" | "error" | "no_data"
    rows: list[dict[str, Any]] = field(default_factory=list)
    gql: str = ""
    error_message: str = ""

    @property
    def entity_count(self) -> int:
        return len(self.rows)

    @property
    def is_ok(self) -> bool:
        return self.status == "ok"

    @property
    def is_unsupported(self) -> bool:
        return self.status == "unsupported"

    def to_citation_dicts(self) -> list[dict[str, Any]]:
        """Convert rows to citation dicts for the normalized citation model."""
        citations = []
        for row in self.rows:
            entity_id = row.get("entity_id") or row.get("id") or ""
            entity_type = row.get("entity_type") or row.get("type") or ""
            display_text = row.get("display_name") or str(entity_id)[:200]
            citations.append({
                "source_type": "ontology",
                "source_id": "fabric-graph",
                "entity_id": str(entity_id),
                "entity_type": str(entity_type),
                "display_text": str(display_text)[:500],
            })
        return citations


@runtime_checkable
class GraphClientProtocol(Protocol):
    """Minimal protocol for a Fabric graph/GQL client."""

    def execute_gql(self, gql: str) -> dict[str, Any]:
        ...


class FabricGraphModelGqlClient:
    """Managed-identity client for the Fabric GraphModel executeQuery API."""

    def __init__(
        self,
        *,
        workspace_id: str,
        graph_model_id: str,
        credential: Any,
        scope: str = "https://api.fabric.microsoft.com/.default",
        api_endpoint: str = "https://api.fabric.microsoft.com",
        timeout_s: float = 30.0,
        _session: Any | None = None,
    ) -> None:
        self.workspace_id = workspace_id
        self.graph_model_id = graph_model_id
        self.credential = credential
        self.scope = scope
        self.api_endpoint = api_endpoint.rstrip("/")
        self.timeout_s = timeout_s
        self._session = _session

    @property
    def execute_url(self) -> str:
        return (
            f"{self.api_endpoint}/v1/workspaces/{self.workspace_id}"
            f"/graphModels/{self.graph_model_id}/executeQuery?beta=true"
        )

    def _get_session(self) -> Any:
        if self._session is None:
            try:
                import requests
            except ImportError as exc:
                raise FabricDataError(
                    "requests is required for Fabric GraphModel queries."
                ) from exc
            self._session = requests.Session()
        return self._session

    def execute_gql(self, gql: str) -> dict[str, Any]:
        token = self.credential.get_token(self.scope).token
        try:
            response = self._get_session().post(
                self.execute_url,
                json={"query": gql},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout_s,
            )
        except Exception as exc:
            raise FabricDataError("Fabric GraphModel request failed.") from exc

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "unknown")
            raise FabricDataError(
                f"Fabric GraphModel rate limit exceeded; retry after {retry_after} seconds."
            )
        if not 200 <= response.status_code < 300:
            raise FabricDataError(
                f"Fabric GraphModel returned HTTP {response.status_code}."
            )
        try:
            payload = response.json()
        except Exception as exc:
            raise FabricDataError(
                "Fabric GraphModel returned invalid JSON."
            ) from exc

        status = payload.get("status") or {}
        status_code = str(status.get("code", ""))
        if not status_code.startswith(("00", "01", "02", "03")):
            description = str(status.get("description") or "query execution failed")
            raise FabricDataError(f"Fabric GraphModel GQL error: {description[:300]}")

        result = payload.get("result") or {}
        rows = result.get("data") if result.get("kind") == "TABLE" else []
        return {"rows": rows if isinstance(rows, list) else []}


# GQL query templates — no domain-specific entity types or relationship names.
# All type and relationship placeholders MUST be supplied by the caller.

_GQL_ENTITY_SEARCH = """
MATCH (n:`{entity_type}`)
WHERE LOWER(n.`display_name`) CONTAINS LOWER("{keyword}")
RETURN n.`entity_id`, n.`display_name`, n.`entity_type`
LIMIT {limit}
""".strip()

_GQL_RELATED_ENTITIES = """
MATCH (p:`{parent_type}`)-[:`{relationship}`]->(c:`{child_type}`)
WHERE LOWER(p.`display_name`) CONTAINS LOWER("{keyword}")
RETURN DISTINCT c.`entity_id`, c.`display_name`, c.`entity_type`
LIMIT {limit}
""".strip()

_GQL_GENERIC_SEARCH = """
MATCH (n)
WHERE LOWER(TO_JSON_STRING(n)) CONTAINS LOWER("{keyword}")
RETURN TO_JSON_STRING(n) AS entity_json
LIMIT {limit}
""".strip()

# Unsupported query types — definitively not answerable by any graph.
_UNSUPPORTED_TYPES = frozenset({
    "price",
    "cost",
    "availability",
    "purchase",
    "forecast",
    "sentiment",
})


def _sanitize_keyword(kw: str) -> str:
    """Strip GQL injection characters from a keyword.

    Removes quote characters and semicolons that could escape the GQL string
    template.  The keyword is placed inside a quoted string in GQL, so only
    the string-closing characters matter for injection prevention.
    """
    bad_chars = set('"\'`;')
    cleaned = "".join(c for c in kw if c not in bad_chars).strip()
    return cleaned[:100]


class FabricDataAgentAdapter:
    """Domain-neutral adapter between the grounded agent and the Fabric Graph API.

    Domain entity types and relationship names must be supplied explicitly by
    the caller — either from domain metadata (ontology/model.yaml) or from
    the user query context.  No domain-specific defaults are provided.

    Parameters
    ----------
    _client:
        Injected graph client.  If None the adapter operates in no-op mode
        (returns unsupported status) — used during offline tests.
    max_rows:
        Limit returned rows to avoid oversized responses.
    """

    def __init__(
        self,
        *,
        _client: GraphClientProtocol | None = None,
        max_rows: int = 20,
        schema_mode: str,
        query_schema: PersistedQuerySchema | None = None,
    ) -> None:
        if schema_mode not in {"schema1_compatibility", "schema2_bounded"}:
            raise ValueError(
                "schema_mode must be schema1_compatibility or schema2_bounded."
            )
        if schema_mode == "schema2_bounded" and (
            query_schema is None
            or query_schema.schema_mode != "schema2_bounded"
            or query_schema.authority is None
        ):
            raise ValueError(
                "Schema-2 Fabric adapter requires sealed bounded query schema."
            )
        self._client = _client
        self.max_rows = max(1, min(max_rows, 100))
        self.schema_mode = schema_mode
        self.query_schema = query_schema

    @property
    def is_available(self) -> bool:
        return self._client is not None

    def query_entity(
        self,
        keyword: str,
        entity_type: str | None = None,
    ) -> FabricDataResult:
        """Search for entities matching *keyword* in the graph.

        ``entity_type`` is **required** — it must come from the active domain
        metadata (e.g. ontology/model.yaml entityTypeNames).  When it is None
        or empty the method returns status="unsupported" with a diagnostic
        message so callers know to supply domain configuration rather than
        silently using a wrong type.

        Returns FabricDataResult with status "ok", "no_data", "error",
        or "unsupported".
        """
        if self.schema_mode == "schema2_bounded":
            return self._bounded_only_result()
        if self._client is None:
            return FabricDataResult(
                status="unsupported",
                error_message="Fabric graph client not configured (offline mode).",
            )
        if not entity_type:
            return FabricDataResult(
                status="unsupported",
                error_message=(
                    "entity_type is required and must come from domain metadata "
                    "(ontology/model.yaml entityTypeNames). "
                    "No domain-specific default is applied."
                ),
            )
        safe_kw = _sanitize_keyword(keyword)
        if not safe_kw:
            return FabricDataResult(
                status="error",
                error_message="Empty or invalid keyword after sanitization.",
            )
        gql = _GQL_ENTITY_SEARCH.format(
            entity_type=entity_type,
            keyword=safe_kw,
            limit=self.max_rows,
        )
        return self._execute(gql)

    def query_related_entities(
        self,
        parent_keyword: str,
        *,
        parent_type: str,
        relationship: str,
        child_type: str,
    ) -> FabricDataResult:
        """Return entities related to a parent entity via a named relationship.

        All three structural parameters (parent_type, relationship, child_type)
        must come from the active domain metadata.  This replaces the old
        domain-specific predecessor method which assumed a fixed device/component
        relationship.

        Example (supply chain domain):
            query_related_entities(
                "Assembly-A001",
                parent_type="Assembly",
                relationship="contains",
                child_type="Part",
            )

        Example (knowledge base domain):
            query_related_entities(
                "Chapter 3",
                parent_type="Section",
                relationship="has_subsection",
                child_type="SubSection",
            )
        """
        if self.schema_mode == "schema2_bounded":
            return self._bounded_only_result()
        if self._client is None:
            return FabricDataResult(
                status="unsupported",
                error_message="Fabric graph client not configured (offline mode).",
            )
        if not parent_type or not relationship or not child_type:
            return FabricDataResult(
                status="unsupported",
                error_message=(
                    "parent_type, relationship, and child_type are required. "
                    "Supply them from domain metadata (ontology/model.yaml)."
                ),
            )
        safe_kw = _sanitize_keyword(parent_keyword)
        if not safe_kw:
            return FabricDataResult(
                status="error",
                error_message="Empty or invalid parent_keyword after sanitization.",
            )
        gql = _GQL_RELATED_ENTITIES.format(
            parent_type=parent_type,
            relationship=relationship,
            child_type=child_type,
            keyword=safe_kw,
            limit=self.max_rows,
        )
        return self._execute(gql)

    def query_keyword(self, keyword: str) -> FabricDataResult:
        """Search all node types without assuming a domain schema."""
        if self.schema_mode == "schema2_bounded":
            return self._bounded_only_result()
        if self._client is None:
            return FabricDataResult(
                status="unsupported",
                error_message="Fabric graph client not configured (offline mode).",
            )
        safe_kw = _sanitize_keyword(keyword)
        if not safe_kw:
            return FabricDataResult(
                status="error",
                error_message="Empty or invalid graph keyword after sanitization.",
            )
        gql = _GQL_GENERIC_SEARCH.format(keyword=safe_kw, limit=self.max_rows)
        return self._execute(gql)

    def query_raw_gql(self, gql: str) -> FabricDataResult:
        """Execute raw GQL only in explicit schema-1 compatibility mode."""
        if self.schema_mode != "schema1_compatibility":
            return self._bounded_only_result()
        if self._client is None:
            return FabricDataResult(
                status="unsupported",
                error_message="Fabric graph client not configured (offline mode).",
            )
        return self._execute(gql)

    def execute_approved_plan(
        self,
        question_id: str,
        *,
        intent: str,
    ) -> FabricDataResult:
        """Execute one sealed schema-2 plan after deterministic local rendering."""
        if self.schema_mode != "schema2_bounded" or self.query_schema is None:
            return FabricDataResult(
                status="unsupported",
                error_message=(
                    "Approved plan execution requires schema2_bounded mode."
                ),
            )
        if self._client is None:
            return FabricDataResult(
                status="unsupported",
                error_message="Fabric graph client not configured (offline mode).",
            )
        try:
            plan = compile_approved_query_plan(
                schema=self.query_schema,
                question_id=question_id,
                intent=intent,
                result_limit=self.max_rows,
            )
            gql = render_bounded_gql(plan, self.query_schema)
        except ValueError as exc:
            return FabricDataResult(
                status="unsupported",
                error_message=str(exc),
            )
        return self._execute(gql, expose_query=False)

    def is_unsupported_query_type(self, query_lower: str) -> bool:
        """Return True if this query type is definitively unsupported by any graph."""
        return any(t in query_lower for t in _UNSUPPORTED_TYPES)

    def _execute(
        self,
        gql: str,
        *,
        expose_query: bool = True,
    ) -> FabricDataResult:
        diagnostic_query = gql if expose_query else ""
        try:
            response = self._client.execute_gql(gql)
        except FabricDataError as exc:
            return FabricDataResult(
                status="error",
                gql=diagnostic_query,
                error_message=str(exc),
            )
        except Exception as exc:
            return FabricDataResult(
                status="error",
                gql=diagnostic_query,
                error_message=f"Unexpected error: {type(exc).__name__}",
            )

        rows = response.get("rows") or response.get("data") or []
        if not isinstance(rows, list):
            rows = []
        rows = [self._normalize_row(row) for row in rows if isinstance(row, dict)]
        if not rows:
            return FabricDataResult(
                status="no_data",
                gql=diagnostic_query,
                rows=[],
            )
        return FabricDataResult(
            status="ok",
            gql=diagnostic_query,
            rows=rows,
        )

    def check_ready(self) -> bool:
        """Verify that the configured GraphModel can execute a bounded query."""
        if self._client is None:
            return False
        if self.schema_mode == "schema2_bounded":
            assert self.query_schema is not None
            authority = self.query_schema.authority
            assert authority is not None
            first_plan = next(
                (
                    path.question_id
                    for path in authority.question_paths
                    if path.covered
                ),
                None,
            )
            if first_plan is None:
                return False
            result = self.execute_approved_plan(
                first_plan,
                intent="Graph readiness",
            )
            if result.status == "error":
                raise FabricDataError(
                    result.error_message
                    or "Fabric GraphModel readiness check failed."
                )
            return result.status in {"ok", "no_data"}
        result = self.query_raw_gql(
            "MATCH (n) RETURN TO_JSON_STRING(n) AS entity_json LIMIT 1"
        )
        if result.status == "error":
            raise FabricDataError(
                result.error_message or "Fabric GraphModel readiness check failed."
            )
        return result.status in {"ok", "no_data"}

    @staticmethod
    def _bounded_only_result() -> FabricDataResult:
        return FabricDataResult(
            status="unsupported",
            error_message=(
                "Schema-2 Graph access accepts approved bounded plan IDs only; "
                "raw or model-authored GQL is disabled."
            ),
        )

    @staticmethod
    def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
        if len(row) != 1:
            return row
        raw = next(iter(row.values()))
        if not isinstance(raw, str):
            return row
        try:
            entity = json.loads(raw)
        except json.JSONDecodeError:
            return row
        if not isinstance(entity, dict):
            return row
        properties = entity.get("properties") or {}
        if not isinstance(properties, dict):
            properties = {}
        labels = entity.get("labels") or []
        entity_type = str(labels[0]) if isinstance(labels, list) and labels else ""
        entity_id = (
            properties.get("entity_id")
            or properties.get("id")
            or entity.get("oid")
            or ""
        )
        display_name = (
            properties.get("display_name")
            or properties.get("name")
            or entity_id
        )
        return {
            **properties,
            "entity_id": entity_id,
            "entity_type": entity_type,
            "display_name": display_name,
        }