"""serving.competency — competency verification for bound counts and path queries.

M6 SRV-009: Verify that the Fabric Lakehouse/Ontology/Graph bindings are
functional by:

  1. Counting bound entity and relationship records (Lakehouse pre-flight).
  2. Running a path query through entities/relationships (Lakehouse pre-flight).
  3. (Optional) Running injectable Ontology/Graph domain competency questions.

Pre-flight (Lakehouse) checks verify that the data is present and query-able.
Ontology/Graph checks verify the higher-level bindings via injectable clients.
Both are reported separately; failures in either are propagated — not silently
logged.

All queries are executed through injectable client interfaces so tests run
without any real cloud calls.
"""

from __future__ import annotations

import re as _re

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lakehouse client protocol — injectable for tests
# ---------------------------------------------------------------------------


class LakehouseClient(Protocol):
    """Minimal client for verifying Lakehouse bindings."""

    def count_table(
        self,
        workspace_id: str,
        lakehouse_item_id: str,
        schema: str,
        table: str,
    ) -> int:
        """Return the row count for a given table."""
        ...

    def path_query(
        self,
        workspace_id: str,
        lakehouse_item_id: str,
        schema: str,
        entity_table: str,
        relationship_table: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Return a sample of entity→relationship→entity paths."""
        ...


# ---------------------------------------------------------------------------
# Production OneLake client
# ---------------------------------------------------------------------------


def _default_onelake_token_provider() -> str:
    """Return a storage-scoped token for OneLake."""
    from azure.identity import DefaultAzureCredential  # type: ignore[import]

    return DefaultAzureCredential().get_token(
        "https://storage.azure.com/.default"
    ).token


class OneLakeDeltaClient:
    """Read schema-enabled Lakehouse Delta tables through OneLake.

    This client verifies the rows that were actually written to the target
    Lakehouse.  It does not use a local Parquet approximation.
    """

    def __init__(
        self,
        *,
        token_provider: Optional[Callable[[], str]] = None,
        delta_table_factory: Optional[Callable[..., Any]] = None,
        path_lister: Optional[
            Callable[[str, str, str], list[str]]
        ] = None,
    ) -> None:
        self._token_provider = token_provider or _default_onelake_token_provider
        self._delta_table_factory = delta_table_factory
        self._path_lister = path_lister

    @staticmethod
    def _table_uri(
        workspace_id: str,
        lakehouse_item_id: str,
        schema: str,
        table: str,
    ) -> str:
        if not workspace_id or not lakehouse_item_id:
            raise ValueError("workspace_id and lakehouse_item_id are required")
        if not schema or not table:
            raise ValueError("schema and table are required")
        return (
            f"abfss://{workspace_id}@onelake.dfs.fabric.microsoft.com/"
            f"{lakehouse_item_id}/Tables/{schema}/{table}"
        )

    def _open_table(
        self,
        workspace_id: str,
        lakehouse_item_id: str,
        schema: str,
        table: str,
    ) -> Any:
        factory = self._delta_table_factory
        if factory is None:
            from deltalake import DeltaTable  # type: ignore[import]

            factory = DeltaTable
        return factory(
            self._table_uri(workspace_id, lakehouse_item_id, schema, table),
            storage_options={
                "bearer_token": self._token_provider(),
                "use_fabric_endpoint": "true",
            },
        )

    def count_table(
        self,
        workspace_id: str,
        lakehouse_item_id: str,
        schema: str,
        table: str,
    ) -> int:
        delta_table = self._open_table(
            workspace_id, lakehouse_item_id, schema, table
        )
        return int(delta_table.to_pyarrow_table().num_rows)

    def read_table(
        self,
        workspace_id: str,
        lakehouse_item_id: str,
        schema: str,
        table: str,
        columns: Optional[list[str]] = None,
    ) -> Any:
        """Return persisted Arrow data for schema, key, and count validation."""
        delta_table = self._open_table(
            workspace_id,
            lakehouse_item_id,
            schema,
            table,
        )
        return delta_table.to_pyarrow_table(columns=columns)

    def list_tables(
        self,
        workspace_id: str,
        lakehouse_item_id: str,
        schema: str,
    ) -> list[str]:
        """List immediate table directories through the OneLake DFS API."""
        if self._path_lister is not None:
            return sorted(set(
                self._path_lister(
                    workspace_id,
                    lakehouse_item_id,
                    schema,
                )
            ))
        import requests  # type: ignore[import]

        url = (
            "https://onelake.dfs.fabric.microsoft.com/"
            f"{workspace_id}"
        )
        directory = f"{lakehouse_item_id}/Tables/{schema}"
        headers = {
            "Authorization": f"Bearer {self._token_provider()}",
            "x-ms-version": "2023-11-03",
        }
        headers["Authorization"] = "Bearer " + self._token_provider()
        continuation = ""
        table_names: set[str] = set()
        while True:
            params = {
                "resource": "filesystem",
                "directory": directory,
                "recursive": "false",
                "maxResults": "5000",
            }
            if continuation:
                params["continuation"] = continuation
            response = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=30,
            )
            if not response.ok:
                raise RuntimeError(
                    "OneLake table enumeration failed: "
                    f"HTTP {response.status_code}: {response.text[:500]}"
                )
            payload = response.json()
            paths = payload.get("paths", [])
            if not isinstance(paths, list):
                raise RuntimeError(
                    "OneLake table enumeration returned malformed paths."
                )
            prefix = directory.rstrip("/") + "/"
            for item in paths:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "")
                if (
                    str(item.get("isDirectory")).casefold() != "true"
                    or not name.startswith(prefix)
                ):
                    continue
                relative = name[len(prefix):]
                if relative and "/" not in relative:
                    table_names.add(relative)
            continuation = str(
                response.headers.get("x-ms-continuation") or ""
            )
            if not continuation:
                break
        return sorted(table_names)

    def path_query(
        self,
        workspace_id: str,
        lakehouse_item_id: str,
        schema: str,
        entity_table: str,
        relationship_table: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        entities = self._open_table(
            workspace_id, lakehouse_item_id, schema, entity_table
        ).to_pyarrow_table(
            columns=["entity_id", "display_name", "entity_type"]
        ).to_pylist()
        relationships = self._open_table(
            workspace_id, lakehouse_item_id, schema, relationship_table
        ).to_pyarrow_table(
            columns=[
                "relationship_id",
                "relationship_type",
                "source_entity_id",
                "target_entity_id",
            ]
        ).to_pylist()

        entities_by_id = {
            str(row.get("entity_id", "")): row
            for row in entities
            if row.get("entity_id")
        }
        paths: list[dict[str, Any]] = []
        for relationship in relationships:
            source = entities_by_id.get(
                str(relationship.get("source_entity_id", ""))
            )
            target = entities_by_id.get(
                str(relationship.get("target_entity_id", ""))
            )
            if source is None or target is None:
                continue
            paths.append({
                "source_entity_id": source["entity_id"],
                "source_display_name": source.get("display_name"),
                "source_entity_type": source.get("entity_type"),
                "relationship_id": relationship.get("relationship_id"),
                "relationship_type": relationship.get("relationship_type"),
                "target_entity_id": target["entity_id"],
                "target_display_name": target.get("display_name"),
                "target_entity_type": target.get("entity_type"),
            })
            if len(paths) >= limit:
                break
        return paths


# ---------------------------------------------------------------------------
# Ontology/Graph query client protocol — injectable for tests
# ---------------------------------------------------------------------------


class OntologyQueryClient(Protocol):
    """Injectable client for Ontology/Graph domain competency questions.

    Implementations should execute fixture GQL/ontology path queries and
    return structured results for validation.
    """

    def run_competency_query(
        self,
        workspace_id: str,
        ontology_item_id: str,
        query: str,
    ) -> list[dict[str, Any]]:
        """Execute a competency query and return result rows."""
        ...

    def count_instances(
        self,
        workspace_id: str,
        ontology_item_id: str,
        type_name: str,
    ) -> int:
        """Return the number of instances for an ontology type."""
        ...



# ---------------------------------------------------------------------------
# GQL query client protocol — injectable for tests
# ---------------------------------------------------------------------------

# Source: https://learn.microsoft.com/en-us/rest/api/fabric/graphmodel/items/execute-query(beta)
# Source: https://learn.microsoft.com/en-us/fabric/graph/gql-query-api


class GQLQueryClient(Protocol):
    """Injectable client for Fabric GQL queries against a GraphModel.

    This is a BETA API (``executeQuery?beta=true``).  Callers must set
    ``gql_beta_acknowledged=True`` on the CompetencyVerifier to enable
    GQL gates.

    HTTP 200 is NOT sufficient.  Implementations must return the raw
    response body dict so that the caller can inspect ``status.code``.
    Status code prefixes: 00/01/02/03 = success variant; 04+ = error.
    """

    def execute_query(
        self,
        workspace_id: str,
        graph_model_id: str,
        query: str,
    ) -> dict[str, Any]:
        """Execute a GQL query; return raw response body dict.

        Caller is responsible for parsing ``status.code`` — this method
        must NOT mask application errors inside HTTP 200.
        """
        ...

    def execute_query_all_pages(
        self,
        workspace_id: str,
        graph_model_id: str,
        query: str,
    ) -> dict[str, Any]:
        """Execute a GQL query collecting all pagination pages.

        Returns response dict with all data rows combined.
        """
        ...


# ---------------------------------------------------------------------------
# GQL status / result helpers
# ---------------------------------------------------------------------------

# Status code prefixes per GQL API docs:
#   00xxxx - Complete success
#   01xxxx - Success with warnings
#   02xxxx - Success with no data returned
#   03xxxx - Success with information
#   04xxxx and higher - Errors / exception conditions
_GQL_SUCCESS_PREFIXES = ("00", "01", "02", "03")


def _gql_status_code(response: dict[str, Any]) -> str:
    """Extract status code from GQL response. Returns '99999' on missing."""
    return str(response.get("status", {}).get("code", "99999"))


def _gql_status_ok(code: str) -> bool:
    """True iff code prefix is 00/01/02/03 (success variants per GQL spec)."""
    return len(code) >= 2 and code[:2] in _GQL_SUCCESS_PREFIXES


def _gql_has_data(response: dict[str, Any]) -> bool:
    """True iff result is a non-empty TABLE with declared columns."""
    result = response.get("result", {})
    return (
        result.get("kind") == "TABLE"
        and bool(result.get("columns"))
        and bool(result.get("data"))
    )


def _gql_description(response: dict[str, Any]) -> str:
    return str(response.get("status", {}).get("description", "(no description)"))


def _gql_extract_count(
    response: dict[str, Any], column: str = "count"
) -> Optional[int]:
    """Extract integer count from the first data row.  None if malformed."""
    data = response.get("result", {}).get("data", [])
    if not data:
        return 0
    row = data[0]
    val = row.get(column)
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        try:
            return int(val)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# GQL query builder — domain-bound queries only, no user input
# ---------------------------------------------------------------------------


class GQLQueryBuilder:
    """Build domain-derived GQL queries from graph-model labels/aliases.

    Labels and edge aliases come from the compiled graphType.json —
    NEVER from raw user input.  All values are validated before being
    interpolated into backtick-quoted GQL identifiers.

    Source: https://learn.microsoft.com/en-us/fabric/graph/gql-language-guide
    """

    _SAFE_PROPERTY_RE = _re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
    _SAFE_LABEL_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _./:-]*$")

    @classmethod
    def _safe(cls, label: str) -> str:
        """Validate a domain-derived label for GQL backtick quoting."""
        if (
            not label
            or len(label) > 128
            or not cls._SAFE_LABEL_RE.fullmatch(label)
        ):
            raise ValueError(
                f"Invalid GQL label {label!r}: labels must be non-empty printable "
                "values without backticks and must come from the compiled graph model."
            )
        return label

    @classmethod
    def _safe_property(cls, property_name: str) -> str:
        if not cls._SAFE_PROPERTY_RE.fullmatch(property_name):
            raise ValueError(
                f"Invalid GQL property {property_name!r}: expected a compiled "
                "alphanumeric/underscore property name."
            )
        return property_name

    @classmethod
    def node_count(cls, label: str) -> str:
        """MATCH (n:`{label}`) RETURN count(n) AS `count`"""
        l = cls._safe(label)
        return f"MATCH (n:`{l}`) RETURN count(n) AS `count`"

    @classmethod
    def edge_count(cls, src_label: str, edge_alias: str, dst_label: str) -> str:
        """Return a count query with the reserved alias quoted for Fabric GQL."""
        s = cls._safe(src_label)
        e = cls._safe(edge_alias)
        d = cls._safe(dst_label)
        return (
            f"MATCH (s:`{s}`)-[r:`{e}`]->(d:`{d}`) "
            "RETURN count(r) AS `count`"
        )

    @classmethod
    def typed_path_sample(
        cls,
        src_label: str,
        edge_alias: str,
        dst_label: str,
        limit: int = 1,
    ) -> str:
        """Return a bounded typed source-edge-target path query."""
        s = cls._safe(src_label)
        e = cls._safe(edge_alias)
        d = cls._safe(dst_label)
        return (
            f"MATCH (s:`{s}`)-[r:`{e}`]->(d:`{d}`) "
            f"RETURN s, r, d LIMIT {int(limit)}"
        )

    @classmethod
    def path_traversal(
        cls, start_label: str, edge_alias: str, limit: int = 5
    ) -> str:
        """Sample traversal: MATCH (a:`{start}`)-[r:`{edge}`]->(b) RETURN a, r, b LIMIT {n}"""
        s = cls._safe(start_label)
        e = cls._safe(edge_alias)
        return f"MATCH (a:`{s}`)-[r:`{e}`]->(b) RETURN a, r, b LIMIT {int(limit)}"

    @classmethod
    def lineage_property_sample(
        cls, label: str, property_names: list[str], limit: int = 5
    ) -> str:
        """Return declared lineage properties for a bounded node sample."""
        l = cls._safe(label)
        safe_props = [cls._safe_property(p) for p in property_names]
        return_clause = ", ".join(f"n.{p} AS {p}" for p in safe_props)
        return f"MATCH (n:`{l}`) RETURN {return_clause} LIMIT {int(limit)}"

# ---------------------------------------------------------------------------
# Fake clients for tests
# ---------------------------------------------------------------------------


class FakeLakehouseClient:
    """In-memory fake Lakehouse client for testing — no cloud calls."""

    def __init__(
        self,
        entity_count: int = 0,
        relationship_count: int = 0,
        path_sample: Optional[list[dict[str, Any]]] = None,
        table_counts: Optional[dict[str, int]] = None,
        should_fail: bool = False,
        fail_message: str = "Fake client error",
    ) -> None:
        self._entity_count = entity_count
        self._relationship_count = relationship_count
        self._path_sample = path_sample or []
        self._table_counts = table_counts or {}
        self._should_fail = should_fail
        self._fail_message = fail_message
        self.call_log: list[tuple[str, ...]] = []

    def count_table(
        self,
        workspace_id: str,
        lakehouse_item_id: str,
        schema: str,
        table: str,
    ) -> int:
        self.call_log.append(("count_table", workspace_id, table))
        if self._should_fail:
            raise RuntimeError(self._fail_message)
        if table in self._table_counts:
            return self._table_counts[table]
        if table == "entities":
            return self._entity_count
        if table == "relationships":
            return self._relationship_count
        return 0

    def path_query(
        self,
        workspace_id: str,
        lakehouse_item_id: str,
        schema: str,
        entity_table: str,
        relationship_table: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        self.call_log.append(("path_query", workspace_id, entity_table, relationship_table))
        if self._should_fail:
            raise RuntimeError(self._fail_message)
        return self._path_sample[:limit]


class FakeOntologyQueryClient:
    """In-memory fake Ontology/Graph query client — no cloud calls."""

    def __init__(
        self,
        query_results: Optional[list[dict[str, Any]]] = None,
        instance_counts: Optional[dict[str, int]] = None,
        should_fail: bool = False,
        fail_message: str = "Fake ontology error",
    ) -> None:
        self._query_results = query_results or []
        self._instance_counts = instance_counts or {}
        self._should_fail = should_fail
        self._fail_message = fail_message
        self.call_log: list[tuple[str, ...]] = []

    def run_competency_query(
        self,
        workspace_id: str,
        ontology_item_id: str,
        query: str,
    ) -> list[dict[str, Any]]:
        self.call_log.append(("run_competency_query", workspace_id, ontology_item_id, query[:40]))
        if self._should_fail:
            raise RuntimeError(self._fail_message)
        return list(self._query_results)

    def count_instances(
        self,
        workspace_id: str,
        ontology_item_id: str,
        type_name: str,
    ) -> int:
        self.call_log.append(("count_instances", workspace_id, type_name))
        if self._should_fail:
            raise RuntimeError(self._fail_message)
        return self._instance_counts.get(type_name, 0)



class FakeGQLQueryClient:
    """In-memory fake GQL query client for tests — no cloud calls.

    Inject per-query-fragment responses via the ``responses`` dict
    (key = substring of query, value = response body dict).
    When no fragment matches, return a success response with ``default_count`` rows.
    """

    _DEFAULT_SUCCESS = staticmethod(lambda count: {
        "status": {"code": "00000", "description": "note: successful completion",
                   "diagnostics": {"OPERATION": "query"}},
        "result": {
            "kind": "TABLE",
            "columns": [{"name": "count", "gqlType": "INT64", "jsonType": "number"}],
            "data": [{"count": count}],
        },
    })

    def __init__(
        self,
        responses: Optional[dict[str, dict[str, Any]]] = None,
        default_count: int = 5,
        should_fail: bool = False,
        fail_message: str = "Fake GQL error",
    ) -> None:
        self._responses = responses or {}
        self._default_count = default_count
        self._should_fail = should_fail
        self._fail_message = fail_message
        self.call_log: list[tuple[str, str, str]] = []

    def _resolve(self, query: str) -> dict[str, Any]:
        for fragment, resp in self._responses.items():
            if fragment in query:
                return resp
        return self._DEFAULT_SUCCESS(self._default_count)

    def execute_query(
        self,
        workspace_id: str,
        graph_model_id: str,
        query: str,
    ) -> dict[str, Any]:
        self.call_log.append(("execute_query", graph_model_id, query[:80]))
        if self._should_fail:
            raise RuntimeError(self._fail_message)
        return self._resolve(query)

    def execute_query_all_pages(
        self,
        workspace_id: str,
        graph_model_id: str,
        query: str,
    ) -> dict[str, Any]:
        self.call_log.append(("execute_query_all_pages", graph_model_id, query[:80]))
        if self._should_fail:
            raise RuntimeError(self._fail_message)
        return self._resolve(query)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class CompetencyResult:
    """Typed result of a competency verification run.

    Attributes
    ----------
    ok:
        True iff all verifications passed (Lakehouse AND Ontology when present).
    entity_count:
        Number of entity records found in Lakehouse.
    relationship_count:
        Number of relationship records found in Lakehouse.
    path_sample:
        Sample paths from Lakehouse path query.
    ontology_instance_counts:
        Dict of type_name → instance count from Ontology query (when client present).
    ontology_query_results:
        Sample rows from competency question queries (when client present).
    errors:
        All error messages (non-empty → failure).
    partial_failures:
        Structured per-check failures (Lakehouse and/or Ontology).
    metadata:
        Arbitrary per-check metadata.
    """

    ok: bool
    entity_count: int = 0
    relationship_count: int = 0
    path_sample: list[dict[str, Any]] = field(default_factory=list)
    ontology_instance_counts: dict[str, int] = field(default_factory=dict)
    ontology_query_results: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    partial_failures: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    gql_node_counts: dict[str, int] = field(default_factory=dict)
    gql_edge_counts: dict[str, int] = field(default_factory=dict)
    gql_lineage_sample: list[dict[str, Any]] = field(default_factory=list)
    gql_status_codes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------


class CompetencyVerifier:
    """Run competency checks against Fabric Lakehouse/Ontology/Graph bindings.

    Parameters
    ----------
    client:
        Injectable Lakehouse client.  Use ``FakeLakehouseClient`` in tests.
    ontology_client:
        Optional injectable Ontology/Graph query client.  When provided,
        domain competency questions are executed and validated.
    entity_table:
        Name of the entity table to count.
    relationship_table:
        Name of the relationship table to count and path-query.
    min_entity_count:
        Minimum number of entities required to pass (default: 1).
    """

    def __init__(
        self,
        client: LakehouseClient,
        ontology_client: Optional[Any] = None,
        gql_client: Optional[Any] = None,
        entity_table: str = "entities",
        relationship_table: str = "relationships",
        min_entity_count: int = 1,
        gql_beta_acknowledged: bool = False,
    ) -> None:
        self._client = client
        self._ontology_client = ontology_client
        self._gql_client = gql_client
        self._entity_table = entity_table
        self._relationship_table = relationship_table
        self._min_entity_count = min_entity_count
        self._gql_beta_acknowledged = gql_beta_acknowledged

    def verify_entity_count(
        self,
        workspace_id: str,
        lakehouse_item_id: str,
        schema: str,
    ) -> CompetencyResult:
        """Verify that the entity table has at least min_entity_count rows."""
        try:
            count = self._client.count_table(
                workspace_id, lakehouse_item_id, schema, self._entity_table
            )
        except Exception as exc:
            err = f"count_table({self._entity_table}) failed: {exc}"
            logger.error("[competency] %s", err)
            return CompetencyResult(
                ok=False, errors=[err],
                partial_failures=[{"check": "entity_count", "table": self._entity_table, "error": str(exc)}],
            )

        ok = count >= self._min_entity_count
        if not ok:
            err = (
                f"Entity count {count} < required minimum {self._min_entity_count} "
                f"in {schema}.{self._entity_table}"
            )
            logger.warning("[competency] %s", err)
            return CompetencyResult(
                ok=False, entity_count=count, errors=[err],
                partial_failures=[{"check": "entity_count", "count": count, "min": self._min_entity_count}],
            )
        logger.info("[competency] entity_count OK: %d >= %d", count, self._min_entity_count)
        return CompetencyResult(ok=True, entity_count=count)

    def verify_relationship_count(
        self,
        workspace_id: str,
        lakehouse_item_id: str,
        schema: str,
    ) -> CompetencyResult:
        """Verify that the relationship table can be counted.

        A count of 0 is allowed (the graph may have entities but no relationships
        yet).  Exceptions are failures — they are NOT silently omitted.
        """
        try:
            count = self._client.count_table(
                workspace_id, lakehouse_item_id, schema, self._relationship_table
            )
        except Exception as exc:
            err = f"count_table({self._relationship_table}) failed: {exc}"
            logger.error("[competency] %s", err)
            return CompetencyResult(
                ok=False, errors=[err],
                partial_failures=[{"check": "relationship_count", "table": self._relationship_table,
                                   "error": str(exc)}],
            )
        logger.info("[competency] relationship_count: %d", count)
        return CompetencyResult(ok=True, relationship_count=count)

    def verify_path_query(
        self,
        workspace_id: str,
        lakehouse_item_id: str,
        schema: str,
        limit: int = 5,
    ) -> CompetencyResult:
        """Verify that a path query returns at least one result."""
        try:
            paths = self._client.path_query(
                workspace_id, lakehouse_item_id, schema,
                self._entity_table, self._relationship_table, limit,
            )
        except Exception as exc:
            err = f"path_query({self._entity_table}->{self._relationship_table}) failed: {exc}"
            logger.error("[competency] %s", err)
            return CompetencyResult(
                ok=False, errors=[err],
                partial_failures=[{"check": "path_query", "error": str(exc)}],
            )

        if not paths:
            err = (
                f"path_query returned 0 results for "
                f"{schema}.{self._entity_table} -> {schema}.{self._relationship_table}"
            )
            logger.warning("[competency] %s", err)
            return CompetencyResult(
                ok=False, path_sample=[],
                errors=[err], partial_failures=[{"check": "path_query", "count": 0}],
            )

        logger.info("[competency] path_query OK: %d paths", len(paths))
        return CompetencyResult(ok=True, path_sample=paths)

    def verify_ontology(
        self,
        workspace_id: str,
        ontology_item_id: str,
        type_names: Optional[list[str]] = None,
        competency_query: Optional[str] = None,
    ) -> CompetencyResult:
        """Run Ontology/Graph domain competency checks.

        Executes instance count and optional competency question queries
        through the injectable ontology client.

        Parameters
        ----------
        type_names:
            Ontology type names to count instances for.
        competency_query:
            Optional fixture GQL/SPARQL/path query string to execute.
        """
        if self._ontology_client is None:
            return CompetencyResult(
                ok=True,
                metadata={"ontology_check": "skipped", "reason": "no ontology_client provided"},
            )

        errors: list[str] = []
        partial_failures: list[dict[str, Any]] = []
        instance_counts: dict[str, int] = {}
        query_results: list[dict[str, Any]] = []

        for type_name in (type_names or []):
            try:
                count = self._ontology_client.count_instances(workspace_id, ontology_item_id, type_name)
                instance_counts[type_name] = count
                logger.info("[competency] ontology instance count %s: %d", type_name, count)
            except Exception as exc:
                err = f"count_instances({type_name}) failed: {exc}"
                errors.append(err)
                partial_failures.append({"check": "ontology_instance_count", "type": type_name, "error": str(exc)})

        if competency_query:
            try:
                query_results = self._ontology_client.run_competency_query(
                    workspace_id, ontology_item_id, competency_query
                )
                logger.info("[competency] ontology query returned %d rows", len(query_results))
            except Exception as exc:
                err = f"run_competency_query failed: {exc}"
                errors.append(err)
                partial_failures.append({"check": "ontology_query", "error": str(exc)})

        return CompetencyResult(
            ok=not errors,
            ontology_instance_counts=instance_counts,
            ontology_query_results=query_results,
            errors=errors,
            partial_failures=partial_failures,
            metadata={"ontology_item_id": ontology_item_id},
        )


    def verify_gql_node_counts(
        self,
        workspace_id: str,
        graph_model_id: str,
        node_labels: list[str],
        *,
        min_count: int = 1,
    ) -> "CompetencyResult":
        """Verify node counts by label via Fabric GQL executeQuery.

        Requires ``gql_beta_acknowledged=True`` on this CompetencyVerifier.
        HTTP 200 is NOT sufficient — ``status.code`` is checked.
        Status code ``02xxxx`` (no data) maps to count=0 (which fails if min_count>0).
        Application errors inside HTTP 200 are NOT masked.

        Source: https://learn.microsoft.com/en-us/rest/api/fabric/graphmodel/items/execute-query(beta)
        """
        if not self._gql_beta_acknowledged:
            raise RuntimeError(
                "GQL competency gates require explicit beta acknowledgement. "
                "Set gql_beta_acknowledged=True on CompetencyVerifier "
                "(this API is beta and may change)."
            )
        if self._gql_client is None:
            return CompetencyResult(
                ok=False,
                errors=["gql_client is required for verify_gql_node_counts but was not provided"],
            )

        errors: list[str] = []
        partial_failures: list[dict[str, Any]] = []
        node_counts: dict[str, int] = {}
        status_codes: list[str] = []

        for label in node_labels:
            try:
                query = GQLQueryBuilder.node_count(label)
                resp = self._gql_client.execute_query(workspace_id, graph_model_id, query)
                code = _gql_status_code(resp)
                status_codes.append(code)

                if not _gql_status_ok(code):
                    err = (
                        f"GQL node count for '{label}' failed: "
                        f"status {code}: {_gql_description(resp)}"
                    )
                    errors.append(err)
                    partial_failures.append(
                        {"check": "gql_node_count", "label": label, "code": code}
                    )
                    continue

                # 02xxxx = success with no data -> count = 0
                if code[:2] == "02":
                    count = 0
                else:
                    count = _gql_extract_count(resp)
                    if count is None:
                        err = (
                            f"GQL node count for '{label}': malformed result "
                            f"(missing 'count' column in first data row)"
                        )
                        errors.append(err)
                        partial_failures.append(
                            {"check": "gql_node_count", "label": label, "error": "malformed"}
                        )
                        continue

                node_counts[label] = count
                if count < min_count:
                    err = (
                        f"GQL node count for '{label}': {count} < required minimum {min_count}"
                    )
                    errors.append(err)
                    partial_failures.append(
                        {"check": "gql_node_count", "label": label,
                         "count": count, "min": min_count}
                    )
            except Exception as exc:
                err = f"GQL node count for '{label}' raised: {exc}"
                errors.append(err)
                partial_failures.append(
                    {"check": "gql_node_count", "label": label, "error": str(exc)}
                )
                logger.error("[competency] %s", err)

        return CompetencyResult(
            ok=not errors,
            gql_node_counts=node_counts,
            gql_status_codes=status_codes,
            errors=errors,
            partial_failures=partial_failures,
        )

    def verify_gql_edge_counts(
        self,
        workspace_id: str,
        graph_model_id: str,
        relationship_pairs: list[dict[str, Any]],
        *,
        min_count: int = 0,
    ) -> "CompetencyResult":
        """Verify relationship edge counts via Fabric GQL executeQuery.

        ``relationship_pairs`` elements must have keys:
          ``name``          — relationship/edge alias (bound from graph model)
          ``source_type``   — source node label
          ``target_type``   — destination node label

        Requires ``gql_beta_acknowledged=True``.
        Status code ``02xxxx`` maps to count=0 (acceptable when min_count=0).
        """
        if not self._gql_beta_acknowledged:
            raise RuntimeError(
                "GQL competency gates require explicit beta acknowledgement. "
                "Set gql_beta_acknowledged=True on CompetencyVerifier."
            )
        if self._gql_client is None:
            return CompetencyResult(
                ok=False,
                errors=["gql_client is required for verify_gql_edge_counts but was not provided"],
            )

        errors: list[str] = []
        partial_failures: list[dict[str, Any]] = []
        edge_counts: dict[str, int] = {}
        status_codes: list[str] = []

        for rp in relationship_pairs:
            name = rp.get("name", "")
            src = rp.get("source_type", "")
            dst = rp.get("target_type", "")
            try:
                query = GQLQueryBuilder.edge_count(src, name, dst)
                resp = self._gql_client.execute_query(workspace_id, graph_model_id, query)
                code = _gql_status_code(resp)
                status_codes.append(code)

                if not _gql_status_ok(code):
                    err = (
                        f"GQL edge count for '{name}' failed: "
                        f"status {code}: {_gql_description(resp)}"
                    )
                    errors.append(err)
                    partial_failures.append(
                        {"check": "gql_edge_count", "name": name, "code": code}
                    )
                    continue

                count = 0 if code[:2] == "02" else (_gql_extract_count(resp) or 0)
                edge_counts[name] = count
                if count < min_count:
                    err = (
                        f"GQL edge count for '{name}': {count} < required minimum {min_count}"
                    )
                    errors.append(err)
                    partial_failures.append(
                        {"check": "gql_edge_count", "name": name,
                         "count": count, "min": min_count}
                    )
            except Exception as exc:
                err = f"GQL edge count for '{name}' raised: {exc}"
                errors.append(err)
                partial_failures.append(
                    {"check": "gql_edge_count", "name": name, "error": str(exc)}
                )
                logger.error("[competency] %s", err)

        return CompetencyResult(
            ok=not errors,
            gql_edge_counts=edge_counts,
            gql_status_codes=status_codes,
            errors=errors,
            partial_failures=partial_failures,
        )

    def verify_gql_lineage_fields(
        self,
        workspace_id: str,
        graph_model_id: str,
        label: str,
        lineage_fields: Optional[list[str]] = None,
        *,
        limit: int = 5,
    ) -> "CompetencyResult":
        """Sample lineage property fields on graph nodes via GQL.

        Verifies that nodes have lineage fields (source_file_id, run_id, etc.)
        populated.  A non-empty sample is required for passing.

        Requires ``gql_beta_acknowledged=True``.
        """
        if not self._gql_beta_acknowledged:
            raise RuntimeError(
                "GQL competency gates require explicit beta acknowledgement. "
                "Set gql_beta_acknowledged=True on CompetencyVerifier."
            )
        if self._gql_client is None:
            return CompetencyResult(
                ok=False,
                errors=["gql_client is required for verify_gql_lineage_fields but was not provided"],
            )

        fields = lineage_fields or ["source_file_id", "run_id"]
        errors: list[str] = []
        partial_failures: list[dict[str, Any]] = []
        status_codes: list[str] = []

        try:
            query = GQLQueryBuilder.lineage_property_sample(label, fields, limit=limit)
            resp = self._gql_client.execute_query_all_pages(workspace_id, graph_model_id, query)
            code = _gql_status_code(resp)
            status_codes.append(code)

            if not _gql_status_ok(code):
                err = (
                    f"GQL lineage field sample for '{label}' failed: "
                    f"status {code}: {_gql_description(resp)}"
                )
                errors.append(err)
                partial_failures.append(
                    {"check": "gql_lineage_fields", "label": label, "code": code}
                )
                return CompetencyResult(
                    ok=False, errors=errors, partial_failures=partial_failures,
                    gql_status_codes=status_codes,
                )

            data = resp.get("result", {}).get("data", [])
            if not data:
                err = (
                    f"GQL lineage field sample for '{label}': no rows returned "
                    f"(fields: {fields}). Nodes may be missing lineage properties."
                )
                errors.append(err)
                partial_failures.append(
                    {"check": "gql_lineage_fields", "label": label, "count": 0}
                )

        except Exception as exc:
            err = f"GQL lineage field sample for '{label}' raised: {exc}"
            errors.append(err)
            partial_failures.append(
                {"check": "gql_lineage_fields", "label": label, "error": str(exc)}
            )
            logger.error("[competency] %s", err)
            data = []

        return CompetencyResult(
            ok=not errors,
            gql_lineage_sample=data,
            gql_status_codes=status_codes,
            errors=errors,
            partial_failures=partial_failures,
        )

    def verify_all(
        self,
        workspace_id: str,
        lakehouse_item_id: str,
        schema: str = "dbo",
        ontology_item_id: str = "",
        ontology_type_names: Optional[list[str]] = None,
        ontology_query: Optional[str] = None,
        graph_model_id: str = "",
        gql_node_labels: Optional[list[str]] = None,
        gql_node_min_count: int = 1,
        gql_relationship_pairs: Optional[list[dict[str, Any]]] = None,
        gql_lineage_label: str = "",
        gql_lineage_fields: Optional[list[str]] = None,
    ) -> CompetencyResult:
        """Run all competency checks and return a combined result.

        Partial failures are accumulated — a single check failure does not
        abort other checks.  Relationship count failures are propagated to
        ``errors`` and ``partial_failures``, NOT silently omitted.

        Lakehouse checks (entity count, relationship count, path query) are
        named "preflight" checks; GQL checks (node counts, edge counts,
        lineage fields) are the actual serving-layer competency gates.

        GQL gates only run when ``gql_beta_acknowledged=True`` (set on the
        CompetencyVerifier constructor), ``gql_client`` is provided, and
        ``graph_model_id`` is non-empty.
        """
        count_result = self.verify_entity_count(workspace_id, lakehouse_item_id, schema)
        rel_result = self.verify_relationship_count(workspace_id, lakehouse_item_id, schema)
        path_result = self.verify_path_query(workspace_id, lakehouse_item_id, schema)

        ontology_result: CompetencyResult = CompetencyResult(ok=True)
        if self._ontology_client and ontology_item_id:
            ontology_result = self.verify_ontology(
                workspace_id, ontology_item_id,
                type_names=ontology_type_names,
                competency_query=ontology_query,
            )

        # GQL serving-layer competency gates
        gql_node_result = CompetencyResult(ok=True)
        gql_edge_result = CompetencyResult(ok=True)
        gql_lineage_result = CompetencyResult(ok=True)

        if self._gql_client and self._gql_beta_acknowledged and graph_model_id:
            if gql_node_labels:
                gql_node_result = self.verify_gql_node_counts(
                    workspace_id,
                    graph_model_id,
                    gql_node_labels,
                    min_count=gql_node_min_count,
                )
            if gql_relationship_pairs:
                gql_edge_result = self.verify_gql_edge_counts(
                    workspace_id, graph_model_id, gql_relationship_pairs
                )
            if gql_lineage_label:
                gql_lineage_result = self.verify_gql_lineage_fields(
                    workspace_id, graph_model_id, gql_lineage_label,
                    lineage_fields=gql_lineage_fields,
                )

        all_errors = (
            count_result.errors + rel_result.errors + path_result.errors
            + ontology_result.errors + gql_node_result.errors
            + gql_edge_result.errors + gql_lineage_result.errors
        )
        all_failures = (
            count_result.partial_failures + rel_result.partial_failures
            + path_result.partial_failures + ontology_result.partial_failures
            + gql_node_result.partial_failures + gql_edge_result.partial_failures
            + gql_lineage_result.partial_failures
        )
        ok = (
            count_result.ok and rel_result.ok and path_result.ok
            and ontology_result.ok and gql_node_result.ok
            and gql_edge_result.ok and gql_lineage_result.ok
        )
        gql_node_counts = {**gql_node_result.gql_node_counts}
        gql_edge_counts = {**gql_edge_result.gql_edge_counts}
        gql_lineage_sample = list(gql_lineage_result.gql_lineage_sample)
        gql_status_codes = (
            gql_node_result.gql_status_codes
            + gql_edge_result.gql_status_codes
            + gql_lineage_result.gql_status_codes
        )

        return CompetencyResult(
            ok=ok,
            entity_count=count_result.entity_count,
            relationship_count=rel_result.relationship_count,
            path_sample=path_result.path_sample,
            ontology_instance_counts=ontology_result.ontology_instance_counts,
            ontology_query_results=ontology_result.ontology_query_results,
            gql_node_counts=gql_node_counts,
            gql_edge_counts=gql_edge_counts,
            gql_lineage_sample=gql_lineage_sample,
            gql_status_codes=gql_status_codes,
            errors=all_errors,
            partial_failures=all_failures,
            metadata={
                "entity_table": self._entity_table,
                "relationship_table": self._relationship_table,
                "schema": schema,
                "ontology_item_id": ontology_item_id or None,
                "graph_model_id": graph_model_id or None,
                "gql_gates_active": bool(self._gql_client and self._gql_beta_acknowledged and graph_model_id),
            },
        )
