"""Injectable direct Graph, Search/KB, and Data Agent MCP executors."""

from __future__ import annotations

import json
import random
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from fabric_kg_builder.knowledge.retrieve import (
    KnowledgeBaseRetriever,
    PartialRetrievalError,
)
from fabric_kg_builder.knowledge.transport import (
    HttpError,
    HttpRequest,
    HttpTransport,
    RequestsTransport,
)
from fabric_kg_builder.serving.graph_model import GraphModelGQLClient

from .contract import CompetencyCase, SearchProbe
from .semantic_reliability import (
    QueryExecutionStatus,
    RetryPolicy,
    SourceExecutionOutcome,
    TurnRetryCoordinator,
    classify_execution_status,
)
from fabric_kg_builder.semantic.query_validation import (
    compute_physical_query_hash,
    resolve_query_plan,
    validate_physical_query as _validate_physical_query,
)
from fabric_kg_builder.semantic.schemas import (
    PersistedQuerySchema,
    compute_query_plan_hash,
)

_TECHNICAL_ERROR_SIGNALS = (
    "technical error",
    "technical execution error",
    "technical failure",
    "technical issue",
    "something went wrong",
    "encountered an issue",
    "there was an error",
    "request failed",
    "try again later",
    "could not complete",
    "couldn't complete",
    "could not be completed",
    "could not be processed",
    "could not access",
    "unable to process your request",
    "unable to provide verified",
    "unable to provide confirmed",
    "an unexpected error occurred",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request_ids(
    headers: dict[str, str] | None,
    body: Any = None,
) -> list[str]:
    headers = headers or {}
    values: list[str] = []
    for key, value in headers.items():
        if key.casefold() in {
            "request-id",
            "x-ms-request-id",
            "x-ms-client-request-id",
            "x-ms-correlation-request-id",
            "apim-request-id",
        } and value:
            values.append(str(value))
    if isinstance(body, dict):
        for key in (
            "requestId",
            "request_id",
            "correlationId",
            "correlation_id",
        ):
            if body.get(key):
                values.append(str(body[key]))
    return list(dict.fromkeys(values))


def _failure(
    *,
    error: Exception | str,
    remediation: str,
    elapsed_ms: float,
    request_ids: list[str] | None = None,
    http_status: int | None = None,
    result_category: QueryExecutionStatus | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if result_category is None:
        if isinstance(error, Exception):
            result_category = classify_execution_status(exception=error)
        elif http_status is not None and http_status >= 400:
            result_category = classify_execution_status(
                http_status=http_status
            )
        else:
            result_category = QueryExecutionStatus.PLATFORM_FAILURE
    return {
        "status": "failed",
        "result_category": result_category.value,
        "final_semantic_status": result_category.value,
        "http_status": http_status,
        "request_ids": request_ids or [],
        "timestamp_utc": _utc_now(),
        "latency_ms": round(elapsed_ms, 3),
        "error_type": type(error).__name__
        if isinstance(error, Exception)
        else "RuntimeError",
        "error_message": str(error),
        "remediation": remediation,
        **(diagnostics or {}),
    }


def _remote_error_message(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        error = body
    code = str(error.get("code") or "").strip()
    message = str(error.get("message") or "").strip()
    if code and message:
        return f"{code}: {message}"
    return code or message or None


def _source_locator(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return stripped
        value = parsed
    if isinstance(value, dict):
        for key in (
            "blob_uri",
            "blobUri",
            "blob_url",
            "blobUrl",
            "landing_uri",
            "landingUri",
            "landing_path",
            "landingPath",
            "source_uri",
            "sourceUri",
        ):
            if value.get(key):
                return str(value[key])
    return None


def _identifier_filter(
    identifiers: set[str],
    probe: SearchProbe,
) -> str:
    grouped: dict[tuple[str, bool], list[str]] = {}
    for identifier in sorted(identifiers):
        prefix, separator, _value = identifier.partition(":")
        if not separator:
            continue
        normalized_prefix = prefix.casefold()
        if normalized_prefix == "entity":
            fields = list(probe.canonical_id_fields)
        elif normalized_prefix in {"evid", "evidence"}:
            fields = [probe.evidence_id_field]
        elif normalized_prefix == "chunk":
            fields = [probe.citation_id_field]
        elif normalized_prefix == "src":
            fields = [probe.source_file_id_field]
        elif normalized_prefix == "asset-version":
            fields = [probe.asset_version_id_field]
        else:
            continue
        for field_name in fields:
            grouped.setdefault(
                (field_name, field_name.casefold().endswith("_ids")),
                [],
            ).append(identifier)

    clauses: list[str] = []
    for (field_name, collection), values in sorted(grouped.items()):
        delimiter = "|"
        joined = delimiter.join(
            value.replace("'", "''") for value in values
        )
        if collection:
            clauses.append(
                f"{field_name}/any(value: "
                f"search.in(value, '{joined}', '{delimiter}'))"
            )
        else:
            clauses.append(
                f"search.in({field_name}, '{joined}', '{delimiter}')"
            )
    return " or ".join(clauses)


def _citation_identifiers(citation: dict[str, Any]) -> set[str]:
    identifiers = {
        str(value)
        for key in (
            "citation_id",
            "evidence_id",
            "source_file_id",
            "asset_version_id",
        )
        if (value := citation.get(key))
    }
    identifiers.update(
        str(value)
        for value in citation.get("evidence_ids", [])
        if value
    )
    identifiers.update(
        str(value)
        for value in citation.get("canonical_ids", [])
        if value
    )
    return identifiers


def _merge_identifier_resolutions(
    *,
    requested_identifiers: set[str],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    if not results:
        return {
            "status": "success",
            "result_category": QueryExecutionStatus.NO_MATCH.value,
            "final_semantic_status": QueryExecutionStatus.NO_MATCH.value,
            "requested_identifiers": sorted(requested_identifiers),
            "resolved_identifiers": [],
            "request_ids": [],
            "citations": [],
            "accepted_facts": [],
            "canonical_ids": [],
            "result_count": 0,
            "lookup_mode": "exact_identifier",
        }
    successful = [
        result
        for result in results
        if result.get("status") == "success"
    ]
    if not successful:
        return results[-1]

    citations: list[dict[str, Any]] = []
    seen_citations: set[str] = set()
    facts: list[dict[str, Any]] = []
    seen_facts: set[str] = set()
    for result in successful:
        for citation in result.get("citations", []):
            if not isinstance(citation, dict):
                continue
            signature = json.dumps(
                citation,
                sort_keys=True,
                separators=(",", ":"),
            )
            if signature not in seen_citations:
                citations.append(citation)
                seen_citations.add(signature)
        for fact in result.get("accepted_facts", []):
            if not isinstance(fact, dict):
                continue
            signature = json.dumps(
                fact,
                sort_keys=True,
                separators=(",", ":"),
            )
            if signature not in seen_facts:
                facts.append(fact)
                seen_facts.add(signature)

    resolved_identifiers = {
        identifier
        for citation in citations
        for identifier in (
            _citation_identifiers(citation) & requested_identifiers
        )
    }
    result_category = (
        QueryExecutionStatus.SUCCESS
        if citations
        else QueryExecutionStatus.NO_MATCH
    )
    base = successful[-1]
    return {
        **base,
        "status": "success",
        "result_category": result_category.value,
        "final_semantic_status": result_category.value,
        "request_ids": list(
            dict.fromkeys(
                request_id
                for result in successful
                for request_id in result.get("request_ids", [])
            )
        ),
        "latency_ms": round(
            sum(float(result.get("latency_ms") or 0) for result in results),
            3,
        ),
        "partial_source": any(
            bool(result.get("partial_source"))
            for result in successful
        ),
        "result_count": len(citations),
        "canonical_ids": sorted(
            {
                str(identifier)
                for citation in citations
                for identifier in citation.get("canonical_ids", [])
                if identifier
            }
        ),
        "citations": citations,
        "accepted_facts": facts,
        "lookup_mode": "exact_identifier",
        "requested_identifiers": sorted(requested_identifiers),
        "resolved_identifiers": sorted(resolved_identifiers),
    }


def _is_technical_error_answer(answer: str) -> bool:
    normalized = " ".join(answer.casefold().split())
    return bool(normalized) and any(
        signal in normalized for signal in _TECHNICAL_ERROR_SIGNALS
    )


class RouteExecutionError(RuntimeError):
    """Runtime route error retaining transport diagnostics."""

    def __init__(
        self,
        message: str,
        *,
        request_ids: list[str] | None = None,
        http_status: int | None = None,
        result_category: QueryExecutionStatus | None = None,
    ) -> None:
        self.request_ids = request_ids or []
        self.http_status = http_status
        self.result_category = (
            result_category
            or (
                classify_execution_status(http_status=http_status)
                if http_status is not None and http_status >= 400
                else QueryExecutionStatus.PLATFORM_FAILURE
            )
        )
        super().__init__(message)


class FabricGraphExecutor:
    """Execute case-owned GQL and map rows to authoritative semantic IDs."""

    def __init__(
        self,
        *,
        workspace_id: str,
        graph_model_id: str,
        client: GraphModelGQLClient,
        query_schema: PersistedQuerySchema | None = None,
    ) -> None:
        self._workspace_id = workspace_id
        self._graph_model_id = graph_model_id
        self._client = client
        self._query_schema = query_schema

    def execute(self, case: CompetencyCase) -> dict[str, Any]:
        probe = case.probes.direct_graph
        if probe is None:
            return {"status": "not_expected", "timestamp_utc": _utc_now()}
        started = time.monotonic()
        semantic_plan = case.semantic_plan or probe.semantic_plan
        query_diagnostics: dict[str, Any] = {
            "physical_query_hash": compute_physical_query_hash(probe.query),
            "semantic_plan": (
                semantic_plan.model_dump(mode="json")
                if semantic_plan is not None
                else None
            ),
            "semantic_plan_hash": (
                compute_query_plan_hash(semantic_plan)
                if semantic_plan is not None
                else None
            ),
            "query_schema_hash": (
                self._query_schema.schema_hash
                if self._query_schema is not None
                else None
            ),
            "static_validation_passed": False,
        }

        if self._query_schema is not None:
            if semantic_plan is None:
                elapsed = (time.monotonic() - started) * 1000
                return _failure(
                    error=(
                        "Direct Graph execution requires a semantic plan "
                        "when a persisted query schema is configured."
                    ),
                    remediation=(
                        "Compile the competency case against the current "
                        "semantic manifest and persisted query schema."
                    ),
                    elapsed_ms=elapsed,
                    result_category=(
                        QueryExecutionStatus.INVALID_SEMANTIC_PLAN
                    ),
                    diagnostics=query_diagnostics,
                )
            plan_findings = resolve_query_plan(
                semantic_plan,
                self._query_schema,
            )
            if plan_findings:
                elapsed = (time.monotonic() - started) * 1000
                return _failure(
                    error=(
                        "Semantic query plan failed persisted-schema "
                        "resolution: "
                        + "; ".join(
                            f"{finding.code}: {finding.message}"
                            for finding in plan_findings
                        )
                    ),
                    remediation=(
                        "Regenerate the semantic plan from the current "
                        "manifest and projection crosswalk."
                    ),
                    elapsed_ms=elapsed,
                    result_category=(
                        QueryExecutionStatus.INVALID_SEMANTIC_PLAN
                    ),
                    diagnostics=query_diagnostics,
                )

        # §9.2: Validate physical query before submission — fenced/projection-less
        # queries must be blocked before they reach the GQL execution layer.
        _qfindings = _validate_physical_query(
            probe.query,
            semantic_plan,
            relationship_labels=probe.relationship_labels,
            type_labels=probe.type_labels,
            schema=self._query_schema,
            raise_on_findings=False,
        )
        _blocking = (
            _qfindings
            if self._query_schema is not None
            else [
                finding
                for finding in _qfindings
                if finding.code
                in {
                    "QUERY_FENCED",
                    "QUERY_NO_TERMINAL_PROJECTION",
                    "QUERY_EMPTY",
                    "QUERY_OPTIONAL_PATH_LOSS",
                }
            ]
        )
        if _blocking:
            elapsed = (time.monotonic() - started) * 1000
            return _failure(
                error=(
                    "Physical query failed pre-execution validation: "
                    + "; ".join(f"{f.code}: {f.message}" for f in _blocking)
                ),
                remediation=(
                    "Fix the physical query per SPEC-008A §9.2: remove markdown "
                    "fences, ensure a terminal RETURN clause is present, and "
                    "preserve every planned optional path."
                ),
                elapsed_ms=elapsed,
                request_ids=None,
                http_status=None,
                result_category=(
                    QueryExecutionStatus.INVALID_PHYSICAL_QUERY
                ),
                diagnostics=query_diagnostics,
            )
        query_diagnostics["static_validation_passed"] = True

        try:
            response = self._client.execute_query_all_pages(
                self._workspace_id,
                self._graph_model_id,
                probe.query,
            )
            code = str(response.get("status", {}).get("code") or "")
            if not code or code[:2] not in {"00", "01", "02", "03"}:
                elapsed = (time.monotonic() - started) * 1000
                return _failure(
                    error=(
                        "Fabric GQL application status failed: "
                        f"{code or 'missing'} "
                        f"{response.get('status', {}).get('description', '')}"
                    ),
                    remediation=(
                        "Verify Graph propagation, labels, GQL syntax, "
                        "permissions, and the configured Graph Model."
                    ),
                    elapsed_ms=elapsed,
                    request_ids=_request_ids(None, response),
                    http_status=200,
                    result_category=(
                        QueryExecutionStatus.INVALID_PHYSICAL_QUERY
                        if "query" in str(
                            response.get("status", {}).get(
                                "description", ""
                            )
                        ).casefold()
                        else QueryExecutionStatus.PLATFORM_FAILURE
                    ),
                    diagnostics=query_diagnostics,
                )
            result = response.get("result")
            rows = (
                result.get("data", [])
                if isinstance(result, dict)
                and result.get("kind") == "TABLE"
                and isinstance(result.get("data"), list)
                else []
            )
            entity_types: set[str] = set()
            canonical_ids: set[str] = set()
            relationships: dict[str, dict[str, Any]] = {}
            accepted_relationships: dict[str, dict[str, Any]] = {}
            lineage_records = 0
            for row in rows:
                if not isinstance(row, dict):
                    continue
                for binding in probe.entity_bindings:
                    if row.get(binding.column) not in {None, ""}:
                        entity_types.add(binding.semantic_id)
                for column in probe.canonical_id_columns:
                    value = row.get(column)
                    if isinstance(value, list):
                        canonical_ids.update(str(item) for item in value if item)
                    elif value not in {None, ""}:
                        canonical_ids.add(str(value))
                if probe.lineage_columns and all(
                    row.get(column) not in {None, ""}
                    for column in probe.lineage_columns
                ):
                    lineage_records += 1
                for binding in probe.relationship_bindings:
                    source = row.get(binding.source_column)
                    target = row.get(binding.target_column)
                    if source in {None, ""} or target in {None, ""}:
                        continue
                    relationship = {
                        "semantic_id": binding.semantic_id,
                        "direction": binding.direction,
                    }
                    relationships[binding.semantic_id] = relationship
                    evidence = (
                        row.get(binding.evidence_column)
                        if binding.evidence_column
                        else None
                    )
                    accepted_relationships[
                        f"{binding.semantic_id}:{source}:{target}"
                    ] = {
                        "id": f"{binding.semantic_id}:{source}:{target}",
                        "evidence_ids": [str(evidence)] if evidence else [],
                    }
            elapsed = (time.monotonic() - started) * 1000
            optional_relationships = (
                set(semantic_plan.optional_relationships)
                if semantic_plan is not None
                else set()
            )
            optional_data_absent = bool(
                rows
                and optional_relationships
                and not optional_relationships & set(relationships)
            )
            result_category = (
                QueryExecutionStatus.OPTIONAL_DATA_ABSENT
                if optional_data_absent
                else classify_execution_status(
                    http_status=200,
                    row_count=len(rows),
                )
            )
            return {
                "status": "success",
                "result_category": result_category.value,
                "final_semantic_status": result_category.value,
                "http_status": 200,
                "request_ids": _request_ids(None, response),
                "timestamp_utc": _utc_now(),
                "latency_ms": round(elapsed, 3),
                "row_count": len(rows),
                "entity_types": sorted(entity_types),
                "relationships": [
                    relationships[key] for key in sorted(relationships)
                ],
                "canonical_ids": sorted(canonical_ids),
                "lineage_record_count": lineage_records,
                "accepted_relationships": [
                    accepted_relationships[key]
                    for key in sorted(accepted_relationships)
                ],
                **query_diagnostics,
            }
        except Exception as exc:
            elapsed = (time.monotonic() - started) * 1000
            return _failure(
                error=exc,
                remediation=(
                    "Verify Graph propagation, labels, GQL syntax, permissions, "
                    "and the configured workspace and Graph Model IDs."
                ),
                elapsed_ms=elapsed,
                result_category=classify_execution_status(exception=exc),
                diagnostics=query_diagnostics,
            )


class SearchKnowledgeExecutor:
    """Execute either direct hybrid Search or knowledge-base retrieval."""

    def __init__(
        self,
        *,
        endpoint: str,
        mode: str,
        token_provider: Callable[[], str],
        index_name: str | None = None,
        knowledge_base_name: str | None = None,
        api_version: str = "2024-07-01",
        transport: HttpTransport | None = None,
        obo_token_provider: Callable[[], str] | None = None,
        query_vectorizer: Callable[[str], list[float]] | None = None,
    ) -> None:
        if mode not in {"direct_search", "knowledge_base"}:
            raise ValueError(
                "Search runtime mode must be direct_search or knowledge_base."
            )
        if mode == "direct_search" and not index_name:
            raise ValueError("direct_search mode requires index_name.")
        if mode == "knowledge_base" and not knowledge_base_name:
            raise ValueError(
                "knowledge_base mode requires knowledge_base_name."
            )
        self._endpoint = endpoint.rstrip("/")
        self._mode = mode
        self._token_provider = token_provider
        self._index_name = index_name
        self._knowledge_base_name = knowledge_base_name
        self._api_version = api_version
        self._transport = transport or RequestsTransport()
        self._obo_token_provider = obo_token_provider
        self._query_vectorizer = query_vectorizer

    def execute(self, case: CompetencyCase) -> dict[str, Any]:
        probe = case.probes.search
        if probe is None:
            return {"status": "not_expected", "timestamp_utc": _utc_now()}
        if self._mode == "knowledge_base":
            return self._execute_knowledge_base(case, probe.query or case.question)
        return self._execute_search(case, probe.query or case.question)

    def resolve_identifiers(
        self,
        case: CompetencyCase,
        identifiers: set[str],
    ) -> dict[str, Any]:
        """Resolve canonical and lineage IDs without vector similarity."""
        if self._mode != "direct_search":
            return {
                "status": "capability_unavailable",
                "result_category": (
                    QueryExecutionStatus.OPTIONAL_DATA_ABSENT.value
                ),
                "final_semantic_status": (
                    QueryExecutionStatus.OPTIONAL_DATA_ABSENT.value
                ),
                "requested_identifiers": sorted(identifiers),
                "resolved_identifiers": [],
            }
        preferred_identifiers = {
            identifier
            for identifier in identifiers
            if identifier.partition(":")[0].casefold()
            in {"entity", "evid", "evidence"}
        }
        results: list[dict[str, Any]] = []
        if preferred_identifiers:
            results.append(
                self._execute_search(
                    case,
                    "*",
                    exact_identifiers=preferred_identifiers,
                    additional_filter=(
                        "content_type eq 'relationship_evidence'"
                    ),
                )
            )
        resolved = {
            identifier
            for result in results
            for citation in result.get("citations", [])
            if isinstance(citation, dict)
            for identifier in (
                _citation_identifiers(citation) & identifiers
            )
        }
        unresolved = identifiers - resolved
        if unresolved:
            results.append(
                self._execute_search(
                    case,
                    "*",
                    exact_identifiers=unresolved,
                )
            )
        return _merge_identifier_resolutions(
            requested_identifiers=identifiers,
            results=results,
        )

    def _execute_search(
        self,
        case: CompetencyCase,
        query: str,
        *,
        exact_identifiers: set[str] | None = None,
        additional_filter: str | None = None,
    ) -> dict[str, Any]:
        probe = case.probes.search
        assert probe is not None
        started = time.monotonic()
        requested_identifiers = set(exact_identifiers or ())
        exact_filter = _identifier_filter(
            requested_identifiers,
            probe,
        )
        if requested_identifiers and not exact_filter:
            return {
                "status": "success",
                "result_category": QueryExecutionStatus.NO_MATCH.value,
                "final_semantic_status": QueryExecutionStatus.NO_MATCH.value,
                "http_status": None,
                "request_ids": [],
                "timestamp_utc": _utc_now(),
                "latency_ms": 0.0,
                "partial_source": False,
                "result_count": 0,
                "canonical_ids": [],
                "citations": [],
                "accepted_facts": [],
                "hybrid_query": False,
                "semantic_query": False,
                "lookup_mode": "exact_identifier",
                "requested_identifiers": sorted(requested_identifiers),
                "resolved_identifiers": [],
            }
        body: dict[str, Any] = {
            "search": query,
            "top": (
                min(1000, max(probe.top, len(requested_identifiers) * 20))
                if requested_identifiers
                else probe.top
            ),
        }
        if probe.select_fields:
            body["select"] = ",".join(probe.select_fields)
        if requested_identifiers:
            body["filter"] = (
                f"({exact_filter}) and ({additional_filter})"
                if additional_filter
                else exact_filter
            )
        elif probe.semantic_configuration:
            body["queryType"] = "semantic"
            body["semanticConfiguration"] = probe.semantic_configuration
        if probe.vector_fields and not requested_identifiers:
            vector_query: dict[str, Any] = {
                "fields": ",".join(probe.vector_fields),
                "k": probe.top,
            }
            if self._query_vectorizer is None:
                vector_query.update({"kind": "text", "text": query})
            else:
                try:
                    vector_query.update({
                        "kind": "vector",
                        "vector": self._query_vectorizer(query),
                    })
                except Exception as exc:
                    elapsed = (time.monotonic() - started) * 1000
                    return _failure(
                        error=exc,
                        remediation=(
                            "Verify the query embedding endpoint, deployment, "
                            "dimensions, and Azure OpenAI authorization."
                        ),
                        elapsed_ms=elapsed,
                        result_category=(
                            QueryExecutionStatus.PLATFORM_FAILURE
                        ),
                    )
            body["vectorQueries"] = [vector_query]
        try:
            token = self._token_provider()
        except Exception as exc:
            elapsed = (time.monotonic() - started) * 1000
            return _failure(
                error=exc,
                remediation=(
                    "Verify Search token acquisition and the configured "
                    "credential or managed identity."
                ),
                elapsed_ms=elapsed,
                result_category=(
                    QueryExecutionStatus.AUTHORIZATION_FAILURE
                ),
            )
        request = HttpRequest(
            method="POST",
            url=(
                f"{self._endpoint}/indexes/{self._index_name}/docs/search"
                f"?api-version={self._api_version}"
            ),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            body=body,
        )
        try:
            response = self._transport.send(request)
            ids = _request_ids(response.headers, response.body)
            if response.status_code >= 400:
                elapsed = (time.monotonic() - started) * 1000
                remote_error = _remote_error_message(response.body)
                return _failure(
                    error=(
                        f"Search query failed with HTTP "
                        f"{response.status_code}"
                        + (f": {remote_error}" if remote_error else ".")
                    ),
                    remediation=(
                        "Verify Search authorization, index readiness, "
                        "semantic configuration, vectorizer, and lineage fields."
                    ),
                    elapsed_ms=elapsed,
                    request_ids=ids,
                    http_status=response.status_code,
                    result_category=classify_execution_status(
                        http_status=response.status_code
                    ),
                )
            payload = response.body
            if not isinstance(payload, dict):
                elapsed = (time.monotonic() - started) * 1000
                return _failure(
                    error=(
                        "Search query returned a non-object JSON response."
                    ),
                    remediation=(
                        "Capture the Search request ID, verify the endpoint "
                        "and API version, and retry the probe."
                    ),
                    elapsed_ms=elapsed,
                    request_ids=ids,
                    http_status=response.status_code,
                    result_category=(
                        QueryExecutionStatus.PLATFORM_FAILURE
                    ),
                )
            documents = (
                payload.get("value", [])
                if isinstance(payload.get("value", []), list)
                else []
            )
            canonical_ids: set[str] = set()
            citations: list[dict[str, Any]] = []
            accepted_facts: list[dict[str, Any]] = []
            resolved_identifiers: set[str] = set()
            for document in documents:
                if not isinstance(document, dict):
                    continue
                document_canonical_ids: set[str] = set()
                for field in probe.canonical_id_fields:
                    value = document.get(field)
                    if isinstance(value, list):
                        document_canonical_ids.update(
                            str(item) for item in value if item
                        )
                    elif value not in {None, ""}:
                        document_canonical_ids.add(str(value))
                canonical_ids.update(document_canonical_ids)
                citation_id = document.get(probe.citation_id_field)
                source_locator = _source_locator(
                    document.get(probe.source_locator_field)
                )
                blob_url = (
                    document.get(probe.blob_url_field) or source_locator
                )
                asset_version_id = document.get(
                    probe.asset_version_id_field
                )
                evidence_value = document.get(probe.evidence_id_field)
                evidence_ids = (
                    [str(value) for value in evidence_value if value]
                    if isinstance(evidence_value, list)
                    else (
                        [str(evidence_value)]
                        if evidence_value not in {None, ""}
                        else []
                    )
                )
                if not evidence_ids and citation_id:
                    evidence_ids = [str(citation_id)]
                citation = {
                    "citation_id": str(citation_id or ""),
                    "evidence_id": evidence_ids[0] if evidence_ids else None,
                    "evidence_ids": evidence_ids,
                    "resolved": bool(
                        asset_version_id and blob_url
                    ),
                    "asset_version_id": asset_version_id,
                    "source_file_id": document.get(
                        probe.source_file_id_field
                    ),
                    "canonical_ids": sorted(document_canonical_ids),
                    "blob_url": blob_url,
                    "source_locator": source_locator,
                }
                citations.append(citation)
                resolved_identifiers.update(
                    _citation_identifiers(citation)
                    & requested_identifiers
                )
                accepted_facts.append(
                    {
                        "id": str(
                            citation_id
                            or (evidence_ids[0] if evidence_ids else "")
                        ),
                        "evidence_ids": (
                            evidence_ids
                            or (
                                [str(citation_id)] if citation_id else []
                            )
                        ),
                    }
                )
            partial_source = response.status_code == 206
            result_category = (
                QueryExecutionStatus.PARTIAL_RESULT
                if partial_source
                else classify_execution_status(
                    http_status=response.status_code,
                    row_count=len(documents),
                )
            )
            return {
                "status": "success",
                "result_category": result_category.value,
                "final_semantic_status": result_category.value,
                "http_status": response.status_code,
                "request_ids": ids,
                "timestamp_utc": _utc_now(),
                "latency_ms": round(response.elapsed_ms, 3),
                "partial_source": partial_source,
                "result_count": len(documents),
                "canonical_ids": sorted(canonical_ids),
                "citations": citations,
                "accepted_facts": accepted_facts,
                "hybrid_query": bool(
                    probe.vector_fields and not requested_identifiers
                ),
                "semantic_query": bool(
                    probe.semantic_configuration
                    and not requested_identifiers
                ),
                "lookup_mode": (
                    "exact_identifier"
                    if requested_identifiers
                    else "hybrid"
                ),
                "requested_identifiers": sorted(requested_identifiers),
                "resolved_identifiers": sorted(resolved_identifiers),
            }
        except Exception as exc:
            elapsed = (time.monotonic() - started) * 1000
            return _failure(
                error=exc,
                remediation=(
                    "Verify Search authorization, index readiness, semantic "
                    "configuration, vectorizer, and selected lineage fields."
                ),
                elapsed_ms=elapsed,
                result_category=classify_execution_status(exception=exc),
            )

    def _execute_knowledge_base(
        self,
        case: CompetencyCase,
        query: str,
    ) -> dict[str, Any]:
        started = time.monotonic()
        try:
            retriever = KnowledgeBaseRetriever(
                endpoint=self._endpoint,
                kb_name=str(self._knowledge_base_name),
                api_version=self._api_version,
                transport=self._transport,
                token_provider=self._token_provider,
                obo_token=(
                    self._obo_token_provider()
                    if self._obo_token_provider
                    else None
                ),
            )
            result = retriever.retrieve_full(query)
            elapsed = (time.monotonic() - started) * 1000
            citations = [
                {
                    "citation_id": citation.citation_id,
                    "evidence_id": (
                        citation.metadata.get("evidence_id")
                        or citation.citation_id
                    ),
                    "resolved": bool(
                        citation.metadata.get("asset_version_id")
                        and (
                            citation.metadata.get("blob_url")
                            or _source_locator(
                                citation.metadata.get(
                                    "source_locator_json"
                                )
                            )
                        )
                    ),
                    "asset_version_id": citation.metadata.get(
                        "asset_version_id"
                    ),
                    "source_file_id": citation.metadata.get("source_file_id"),
                    "blob_url": (
                        citation.metadata.get("blob_url")
                        or _source_locator(
                            citation.metadata.get("source_locator_json")
                        )
                    ),
                    "source_locator": _source_locator(
                        citation.metadata.get("source_locator_json")
                    ),
                }
                for citation in result.citations
            ]
            canonical_ids: set[str] = set()
            for citation in result.citations:
                for field in ("canonical_id", "linked_entity_ids"):
                    value = citation.metadata.get(field)
                    if isinstance(value, list):
                        canonical_ids.update(
                            str(item) for item in value if item
                        )
                    elif value:
                        canonical_ids.add(str(value))
            result_category = (
                QueryExecutionStatus.PARTIAL_RESULT
                if result.is_partial
                else classify_execution_status(
                    http_status=200,
                    row_count=len(citations),
                )
            )
            return {
                "status": "success",
                "result_category": result_category.value,
                "final_semantic_status": result_category.value,
                "http_status": 200,
                "request_ids": _request_ids(result.response_headers),
                "timestamp_utc": _utc_now(),
                "latency_ms": round(elapsed, 3),
                "partial_source": result.is_partial,
                "canonical_ids": sorted(canonical_ids),
                "citations": citations,
                "accepted_facts": [
                    {
                        "id": citation["citation_id"],
                        "evidence_ids": [
                            str(
                                citation.get("evidence_id")
                                or citation["citation_id"]
                            )
                        ],
                    }
                    for citation in citations
                ],
                "answer": result.answer_text,
                "activity_count": len(result.activity),
            }
        except PartialRetrievalError as exc:
            elapsed = (time.monotonic() - started) * 1000
            return {
                "status": "partial",
                "result_category": (
                    QueryExecutionStatus.PARTIAL_RESULT.value
                ),
                "final_semantic_status": (
                    QueryExecutionStatus.PARTIAL_RESULT.value
                ),
                "http_status": 206,
                "request_ids": _request_ids(
                    exc.response_headers,
                    exc.raw_body,
                ),
                "timestamp_utc": _utc_now(),
                "latency_ms": round(elapsed, 3),
                "partial_source": True,
                "citation_count": len(exc.citations),
                "activity_count": len(exc.activity),
                "remediation": (
                    "Repair every failed knowledge-source authorization or "
                    "runtime before accepting the knowledge-base route."
                ),
            }
        except HttpError as exc:
            elapsed = (time.monotonic() - started) * 1000
            return _failure(
                error=exc,
                remediation=(
                    "Verify knowledge-base readiness, Search authentication, "
                    "and query-source authorization for every source."
                ),
                elapsed_ms=elapsed,
                request_ids=_request_ids(
                    exc.response_headers,
                    exc.body,
                ),
                http_status=exc.status_code,
                result_category=classify_execution_status(
                    http_status=exc.status_code
                ),
            )
        except Exception as exc:
            elapsed = (time.monotonic() - started) * 1000
            return _failure(
                error=exc,
                remediation=(
                    "Verify knowledge-base readiness, Search authentication, "
                    "and query-source authorization for every source."
                ),
                elapsed_ms=elapsed,
                result_category=classify_execution_status(exception=exc),
            )


class DataAgentMcpExecutor:
    """Initialize, discover, and invoke a Streamable HTTP MCP endpoint."""

    def __init__(
        self,
        *,
        endpoint: str,
        token_provider: Callable[[], str],
        transport: HttpTransport | None = None,
        protocol_version: str = "2025-03-26",
        max_attempts: int = 1,
        retry_base_delay_seconds: float = 0.25,
        retry_jitter_seconds: float = 0.25,
        request_timeout_seconds: int = 120,
        sleep_fn: Callable[[float], None] = time.sleep,
        turn_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if retry_jitter_seconds < 0:
            raise ValueError("retry_jitter_seconds must be nonnegative.")
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive.")
        self._endpoint = endpoint
        self._token_provider = token_provider
        self._transport = transport or RequestsTransport()
        self._protocol_version = protocol_version
        self._request_timeout_seconds = request_timeout_seconds
        self._session_id: str | None = None
        self._tool_names: list[str] | None = None
        self._next_id = 1
        self._session_lock = threading.RLock()
        self._turn_id_factory = (
            turn_id_factory or (lambda: uuid.uuid4().hex)
        )
        self._retry_coordinator = TurnRetryCoordinator(
            retry_policy=RetryPolicy(
                max_attempts=max_attempts,
                base_delay_seconds=retry_base_delay_seconds,
            ),
            sleep=sleep_fn,
            jitter=lambda: random.uniform(
                0.0,
                retry_jitter_seconds,
            ),
        )

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._token_provider()}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self._protocol_version,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _rpc(
        self,
        method: str,
        params: dict[str, Any] | None,
        *,
        notification: bool = False,
        client_request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[dict[str, Any], int, dict[str, str], float]:
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notification:
            body["id"] = self._next_id
            self._next_id += 1
        if params is not None:
            body["params"] = params
        expected_id = body.get("id")
        request_headers = self._headers()
        client_request_id = client_request_id or str(uuid.uuid4())
        request_headers["x-ms-client-request-id"] = client_request_id
        if idempotency_key:
            request_headers["Idempotency-Key"] = idempotency_key
        response = self._transport.send(
            HttpRequest(
                method="POST",
                url=self._endpoint,
                headers=request_headers,
                body=body,
                timeout=self._request_timeout_seconds,
            )
        )
        response_headers = dict(response.headers)
        if response.status_code >= 400:
            remote_error = _remote_error_message(response.body)
            failure_request_ids = _request_ids(
                response_headers,
                response.body,
            ) or [client_request_id]
            raise RouteExecutionError(
                f"MCP {method} failed with HTTP {response.status_code}"
                + (f": {remote_error}" if remote_error else "."),
                request_ids=failure_request_ids,
                http_status=response.status_code,
                result_category=classify_execution_status(
                    http_status=response.status_code
                ),
            )
        request_ids = _request_ids(response_headers, response.body)
        if notification and response.status_code in {202, 204}:
            return (
                {},
                response.status_code,
                response_headers,
                response.elapsed_ms,
            )
        try:
            payload = self._parse_payload(response.body)
        except (
            json.JSONDecodeError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            raise RouteExecutionError(
                f"MCP {method} returned malformed JSON: {exc}",
                request_ids=request_ids or [client_request_id],
                http_status=response.status_code,
                result_category=(
                    QueryExecutionStatus.PLATFORM_FAILURE
                ),
            ) from exc
        if not notification and (
            payload.get("jsonrpc") != "2.0"
            or payload.get("id") != expected_id
            or not ({"result", "error"} & set(payload))
        ):
            raise RouteExecutionError(
                f"MCP {method} returned an invalid JSON-RPC envelope.",
                request_ids=request_ids or [client_request_id],
                http_status=response.status_code,
                result_category=(
                    QueryExecutionStatus.PLATFORM_FAILURE
                ),
            )
        if isinstance(payload.get("error"), dict):
            error = payload["error"]
            raise RouteExecutionError(
                (
                    f"MCP {method} error {error.get('code')}: "
                    f"{error.get('message')}"
                ),
                request_ids=(
                    _request_ids(response_headers, payload)
                    or [client_request_id]
                ),
                http_status=response.status_code,
                result_category=(
                    QueryExecutionStatus.INVALID_PHYSICAL_QUERY
                    if error.get("code") in {-32600, -32601, -32602}
                    else QueryExecutionStatus.PLATFORM_FAILURE
                ),
            )
        if method == "tools/call" and not _request_ids(
            response_headers,
            payload,
        ):
            response_headers["x-ms-client-request-id"] = client_request_id
        session = next(
            (
                value
                for key, value in response_headers.items()
                if key.casefold() == "mcp-session-id"
            ),
            None,
        )
        if session:
            self._session_id = str(session)
        return (
            payload,
            response.status_code,
            response_headers,
            response.elapsed_ms,
        )

    @staticmethod
    def _parse_payload(body: Any) -> dict[str, Any]:
        if isinstance(body, dict):
            return body
        if body in {None, ""}:
            return {}
        if isinstance(body, (bytes, bytearray)):
            if not body.strip():
                return {}
            body = body.decode("utf-8")
        text = str(body)
        if not text.strip():
            return {}
        data_lines = [
            line[5:].strip()
            for line in text.splitlines()
            if line.startswith("data:")
        ]
        candidate = data_lines[-1] if data_lines else text
        parsed = json.loads(candidate)
        if not isinstance(parsed, dict):
            raise RuntimeError("MCP response must be a JSON object.")
        return parsed

    def _ensure_initialized(self) -> tuple[list[str], list[str]]:
        if self._tool_names is not None:
            return self._tool_names, []
        initialize, _, headers, _ = self._rpc(
            "initialize",
            {
                "protocolVersion": self._protocol_version,
                "capabilities": {},
                "clientInfo": {
                    "name": "fabric-kg-builder",
                    "version": "0.2.0",
                },
            },
        )
        self._rpc("notifications/initialized", None, notification=True)
        tools_payload, _, tool_headers, _ = self._rpc("tools/list", {})
        tools = tools_payload.get("result", {}).get("tools", [])
        self._tool_names = [
            str(tool["name"])
            for tool in tools
            if isinstance(tool, dict) and tool.get("name")
        ]
        ids = _request_ids(headers, initialize)
        ids.extend(_request_ids(tool_headers, tools_payload))
        return self._tool_names, list(dict.fromkeys(ids))

    def execute(self, case: CompetencyCase) -> dict[str, Any]:
        probe = case.probes.data_agent_mcp
        if probe is None:
            return {"status": "not_expected", "timestamp_utc": _utc_now()}
        started = time.monotonic()
        try:
            with self._session_lock:
                tools, discovery_ids = self._ensure_initialized()
                tool_name = probe.tool_name or (tools[0] if tools else None)
                if not tool_name or tool_name not in tools:
                    raise RouteExecutionError(
                        f"Requested MCP tool {tool_name!r} was not discovered.",
                        request_ids=discovery_ids,
                    )
                arguments = dict(probe.static_arguments)
                arguments[probe.question_argument] = case.question
                attempt_results: dict[int, dict[str, Any]] = {}

                def invoke_attempt(
                    attempt: int,
                    idempotency_key: str,
                    request_id: str,
                ) -> SourceExecutionOutcome:
                    try:
                        payload, status, headers, elapsed = self._rpc(
                            "tools/call",
                            {
                                "name": tool_name,
                                "arguments": arguments,
                            },
                            client_request_id=request_id,
                            idempotency_key=idempotency_key,
                        )
                    except RouteExecutionError as exc:
                        actual_request_id = (
                            exc.request_ids[0]
                            if exc.request_ids
                            else request_id
                        )
                        actual_correlation_id = (
                            exc.request_ids[1]
                            if len(exc.request_ids) > 1
                            else actual_request_id
                        )
                        attempt_results[attempt] = {
                            "status": exc.http_status,
                            "request_ids": exc.request_ids,
                            "client_request_id": request_id,
                            "error": exc,
                        }
                        return SourceExecutionOutcome(
                            source_id="data_agent_mcp",
                            status=exc.result_category,
                            unsupported_portion=str(exc),
                            request_id=actual_request_id,
                            correlation_id=actual_correlation_id,
                        )
                    result = payload.get("result", {})
                    answer = self._extract_answer(result)
                    citations = self._extract_citations(result)
                    request_ids = _request_ids(headers, payload)
                    is_error = bool(
                        result.get("isError")
                        if isinstance(result, dict)
                        else False
                    )
                    technical_error = _is_technical_error_answer(answer)
                    normalized_answer = answer.casefold()
                    if is_error:
                        if any(
                            signal in normalized_answer
                            for signal in (
                                "concurrency",
                                "conflict",
                                "too many requests",
                                "throttl",
                            )
                        ):
                            result_category = (
                                QueryExecutionStatus.CONCURRENCY_CONFLICT
                            )
                        elif "timeout" in normalized_answer:
                            result_category = QueryExecutionStatus.TIMEOUT
                        elif any(
                            signal in normalized_answer
                            for signal in (
                                "unauthorized",
                                "forbidden",
                                "permission",
                            )
                        ):
                            result_category = (
                                QueryExecutionStatus.AUTHORIZATION_FAILURE
                            )
                        else:
                            result_category = (
                                QueryExecutionStatus.PLATFORM_FAILURE
                            )
                    elif technical_error:
                        result_category = (
                            QueryExecutionStatus.PLATFORM_FAILURE
                        )
                    else:
                        result_category = classify_execution_status(
                            http_status=status,
                            row_count=1 if answer.strip() else 0,
                        )
                    attempt_results[attempt] = {
                        "payload": payload,
                        "status": status,
                        "headers": headers,
                        "elapsed": elapsed,
                        "answer": answer,
                        "citations": citations,
                        "request_ids": request_ids,
                        "client_request_id": request_id,
                        "is_error": is_error,
                        "technical_error": technical_error,
                        "result_category": result_category,
                    }
                    actual_request_id = (
                        request_ids[0] if request_ids else request_id
                    )
                    actual_correlation_id = (
                        request_ids[1]
                        if len(request_ids) > 1
                        else actual_request_id
                    )
                    return SourceExecutionOutcome(
                        source_id="data_agent_mcp",
                        status=result_category,
                        unsupported_portion=(
                            answer
                            or "Data Agent MCP execution did not succeed."
                        )
                        if result_category
                        != QueryExecutionStatus.SUCCESS
                        else None,
                        request_id=actual_request_id,
                        correlation_id=actual_correlation_id,
                    )

                turn_id = (
                    f"{case.id}:{self._turn_id_factory()}"
                )
                receipt = self._retry_coordinator.execute_turn(
                    turn_id,
                    invoke_attempt,
                )
                final_attempt = receipt.attempts[-1].attempt
                details = attempt_results[final_attempt]
                result_category = receipt.final_status
                request_ids = list(dict.fromkeys([
                    *discovery_ids,
                    *[
                        request_id
                        for attempt_details in attempt_results.values()
                        for request_id in attempt_details.get(
                            "request_ids", []
                        )
                    ],
                ]))
                retry_request_ids = [
                    record.request_id for record in receipt.attempts
                ]
                retry_correlation_ids = [
                    record.correlation_id for record in receipt.attempts
                ]
                client_request_ids = [
                    str(attempt_details["client_request_id"])
                    for attempt_details in attempt_results.values()
                    if attempt_details.get("client_request_id")
                ]
                first_failure = (
                    receipt.first_failure.model_dump(mode="json")
                    if receipt.first_failure is not None
                    else None
                )
                common = {
                    "result_category": result_category.value,
                    "final_semantic_status": result_category.value,
                    "http_status": details.get("status"),
                    "request_ids": request_ids,
                    "retry_request_ids": retry_request_ids,
                    "retry_correlation_ids": retry_correlation_ids,
                    "client_request_ids": client_request_ids,
                    "retry_count": len(receipt.attempts) - 1,
                    "first_failure": first_failure,
                    "idempotency_key": receipt.idempotency_key,
                    "timestamp_utc": _utc_now(),
                    "latency_ms": round(
                        (time.monotonic() - started) * 1000,
                        3,
                    ),
                    "tool_name": tool_name,
                    "initialized": True,
                    "discovered_tools": tools,
                }
                if result_category == QueryExecutionStatus.SUCCESS:
                    return {
                        **common,
                        "status": "success",
                        "answer": details["answer"],
                        "citations": details["citations"],
                        "is_error": False,
                    }
                if result_category in {
                    QueryExecutionStatus.NO_MATCH,
                    QueryExecutionStatus.OPTIONAL_DATA_ABSENT,
                }:
                    return {
                        **common,
                        "status": "success",
                        "answer": details.get("answer", ""),
                        "citations": details.get("citations", []),
                        "is_error": False,
                        "technical_error": False,
                    }
                error = details.get("error")
                answer = str(details.get("answer") or "")
                return {
                    **common,
                    "status": "failed",
                    "answer": answer,
                    "citations": details.get("citations", []),
                    "is_error": bool(details.get("is_error")),
                    "technical_error": bool(
                        details.get("technical_error")
                    ),
                    "error_type": (
                        type(error).__name__
                        if isinstance(error, Exception)
                        else "RuntimeError"
                    ),
                    "error_message": (
                        str(error)
                        if error is not None
                        else (
                            answer
                            or "Data Agent MCP execution returned no match."
                        )
                    ),
                    "remediation": (
                        "Inspect the published Data Agent and Fabric request "
                        "diagnostics for the failed MCP tool result."
                    ),
                }
        except RouteExecutionError as exc:
            elapsed = (time.monotonic() - started) * 1000
            return _failure(
                error=exc,
                remediation=(
                    "Verify the published Data Agent, MCP endpoint, token "
                    "audience, discovered tool schema, and Fabric diagnostics."
                ),
                elapsed_ms=elapsed,
                request_ids=exc.request_ids,
                http_status=exc.http_status,
                result_category=exc.result_category,
            )
        except Exception as exc:
            elapsed = (time.monotonic() - started) * 1000
            return _failure(
                error=exc,
                remediation=(
                    "Verify the published Data Agent, MCP endpoint, token "
                    "audience, discovered tool schema, and Fabric diagnostics."
                ),
                elapsed_ms=elapsed,
                result_category=classify_execution_status(exception=exc),
            )

    @staticmethod
    def _extract_answer(result: Any) -> str:
        if not isinstance(result, dict):
            return str(result or "")
        content = result.get("content")
        if isinstance(content, list):
            texts = [
                str(item.get("text"))
                for item in content
                if isinstance(item, dict) and item.get("text")
            ]
            if texts:
                return "\n".join(texts)
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            for key in ("answer", "text", "content"):
                if structured.get(key):
                    return str(structured[key])
            return json.dumps(structured, sort_keys=True)
        return str(result.get("answer") or result.get("text") or "")

    @classmethod
    def _extract_citations(cls, result: Any) -> list[dict[str, Any]]:
        """Retain only immutable citation and lineage fields from MCP output."""
        if not isinstance(result, dict):
            return []
        candidates: list[dict[str, Any]] = []
        for key in ("citations", "references", "sources"):
            cls._collect_citation_candidates(result.get(key), candidates)
        cls._collect_citation_candidates(result.get("content"), candidates)
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            for key in ("citations", "references", "sources"):
                cls._collect_citation_candidates(
                    structured.get(key),
                    candidates,
                )

        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in candidates:
            citation = {
                "citation_id": cls._first_value(
                    candidate,
                    "citation_id",
                    "citationId",
                    "id",
                ),
                "evidence_id": cls._first_value(
                    candidate,
                    "evidence_id",
                    "evidenceId",
                ),
                "chunk_id": cls._first_value(
                    candidate,
                    "chunk_id",
                    "chunkId",
                ),
                "canonical_id": cls._first_value(
                    candidate,
                    "canonical_id",
                    "canonicalId",
                ),
                "asset_version_id": cls._first_value(
                    candidate,
                    "asset_version_id",
                    "assetVersionId",
                ),
                "blob_url": cls._first_value(
                    candidate,
                    "blob_url",
                    "blobUrl",
                ),
                "source_locator": cls._first_value(
                    candidate,
                    "source_locator",
                    "sourceLocator",
                    "source_url",
                    "sourceUrl",
                    "url",
                    "uri",
                ),
            }
            citation = {
                key: str(value)
                for key, value in citation.items()
                if value not in {None, ""}
            }
            citation["resolved"] = bool(
                citation.get("asset_version_id")
                and (
                    citation.get("blob_url")
                    or citation.get("source_locator")
                )
            )
            fingerprint = json.dumps(citation, sort_keys=True)
            if fingerprint not in seen:
                seen.add(fingerprint)
                normalized.append(citation)
        return normalized

    @classmethod
    def _collect_citation_candidates(
        cls,
        value: Any,
        candidates: list[dict[str, Any]],
    ) -> None:
        if isinstance(value, dict):
            citation_keys = {
                "citation_id",
                "citationId",
                "evidence_id",
                "evidenceId",
                "chunk_id",
                "chunkId",
                "canonical_id",
                "canonicalId",
                "asset_version_id",
                "assetVersionId",
                "blob_url",
                "blobUrl",
                "source_locator",
                "sourceLocator",
                "source_url",
                "sourceUrl",
                "url",
                "uri",
            }
            if citation_keys.intersection(value):
                candidates.append(value)
            for nested in value.values():
                if isinstance(nested, (dict, list)):
                    cls._collect_citation_candidates(
                        nested,
                        candidates,
                    )
        elif isinstance(value, list):
            for item in value:
                cls._collect_citation_candidates(item, candidates)

    @staticmethod
    def _first_value(
        value: dict[str, Any],
        *keys: str,
    ) -> Any:
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, (str, int, float)) and candidate != "":
                return candidate
        return None
