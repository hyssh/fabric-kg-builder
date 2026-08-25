"""knowledge.data_agent — Fabric Data Agent lifecycle management.

AGK-005: Manages the full lifecycle of a Fabric Data Agent item:

  * **Definition builder** — assembles InlineBase64 definition parts from a
    :class:`DataAgentSpec`, including:

      - ``Files/Config/data_agent.json``       — agent manifest
      - ``Files/Config/draft/stage_config.json`` — draft stage config
      - ``Files/Config/published/stage_config.json`` — published stage config
      - ``Files/Config/draft/{dsType}-{name}/datasource.json`` — per-source config

  * **Idempotent create-or-update** — LIST the workspace to check existence,
    then POST to create (201 sync | 202 LRO) or POST to ``updateDefinition``
    to update an existing item.

  * **LRO polling** — respects the ``Retry-After`` header; raises
    :class:`LROTimeoutError` after *lro_timeout_seconds*.

  * **Source cap** — enforced at build time (max 5 sources); raises
    :class:`~fabric_kg_builder.knowledge.validation.SourceCapError` if exceeded.

  * **Capability discovery** — ontology/search sources are treated as
    capability-discovered preview rather than assumed; the builder will include
    them only when the spec flags them as preview-confirmed.

Security
--------
  * Definition parts are JSON-encoded and base64-encoded before transmission.
    Connection-string values inside ``DataSourceSpec.connection_properties``
    are **not** logged — only the ``type`` and ``name`` fields are surfaced.
  * Token strings are never written to logs.

Permissions required (caller's identity or SPN)
------------------------------------------------
  * **Contributor** on the Fabric workspace (or ``Item.ReadWrite.All`` on the
    DataAgent item).

API
---
  ``POST https://api.fabric.microsoft.com/v1/workspaces/{workspaceId}/dataAgents``

Usage (test with FakeTransport)::

    from fabric_kg_builder.knowledge.transport import FakeTransport, HttpResponse
    from fabric_kg_builder.knowledge.data_agent import (
        DataAgentSpec, DataSourceSpec, FabricDataAgentClient
    )

    t = FakeTransport()
    # No existing agent
    t.register("GET", "/workspaces/ws-1/items", HttpResponse(200, body={"value": []}))
    # Create returns 201 sync
    t.register("POST", "/workspaces/ws-1/dataAgents",
        HttpResponse(201, body={"id": "agent-id-1", "displayName": "my-agent"}))

    spec = DataAgentSpec(
        display_name="my-agent",
        instruction="Answer questions about the knowledge graph.",
        sources=[DataSourceSpec(source_type="lakehouse", name="my-lakehouse")],
    )
    client = FabricDataAgentClient(workspace_id="ws-1", transport=t, token="fake")
    result = client.upsert(spec)
    assert result.item_id == "agent-id-1"
    assert result.created
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping
from urllib.parse import quote, urljoin

from .transport import HttpError, HttpRequest, HttpTransport
from .validation import MAX_SOURCES, SourceCapError
from . import lineage_adapter as _lin

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FABRIC_API_BASE = "https://api.fabric.microsoft.com/v1"
_FABRIC_API_VERSION = "v1"  # Fabric REST API base version for lineage recording
_FABRIC_TOKEN_SCOPE = "https://api.fabric.microsoft.com/.default"

# Datasource type enum — snake_case values from official Fabric docs
# https://learn.microsoft.com/rest/api/fabric/articles/item-management/definitions/data-agent-definition
_DOCUMENTED_DATASOURCE_TYPES: frozenset[str] = frozenset(
    {
        "unknown",
        "lakehouse_tables",
        "lakehouse",
        "data_warehouse",
        "kusto",
        "semantic_model",
        "graph",
        "mirrored_database",
        "mirrored_azure_databricks",
    }
)

# Public JSON schema identifiers for definition parts
# Source: https://learn.microsoft.com/en-us/rest/api/fabric/articles/item-management/definitions/data-agent-definition
_DATA_AGENT_SCHEMA_BASE = (
    "https://developer.microsoft.com/json-schemas/fabric/item/dataAgent/"
    "definition"
)
_STAGE_CONFIG_SCHEMA = (
    f"{_DATA_AGENT_SCHEMA_BASE}/stageConfiguration/1.0.0/schema.json"
)
_DATASOURCE_SCHEMA = (
    f"{_DATA_AGENT_SCHEMA_BASE}/dataSource/1.0.0/schema.json"
)
_FEWSHOTS_SCHEMA = (
    f"{_DATA_AGENT_SCHEMA_BASE}/fewShots/1.0.0/schema.json"
)
_DATA_AGENT_SCHEMA = (
    f"{_DATA_AGENT_SCHEMA_BASE}/dataAgent/2.1.0/schema.json"
)

# Graph datasource element type literals
ELEMENT_TYPE_NODE = "graph.nodeType"
ELEMENT_TYPE_EDGE = "graph.edgeType"
ELEMENT_TYPE_PROPERTY = "graph.property"

# Preview-only datasource types (capability-discovered)
_PREVIEW_DATASOURCE_TYPES: frozenset[str] = frozenset({"ontology", "search"})

_DEFAULT_LRO_POLL_INTERVAL = 5  # seconds
_DEFAULT_LRO_TIMEOUT = 300  # seconds
_GQL_SUCCESS_PREFIXES = ("00", "01", "02", "03")

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LROTimeoutError(Exception):
    """Raised when a long-running operation does not complete within the timeout.

    Attributes
    ----------
    operation_url : str
        The LRO status URL being polled.
    elapsed_seconds : float
        Elapsed wall-clock seconds before giving up.
    """

    def __init__(self, operation_url: str, elapsed_seconds: float) -> None:
        self.operation_url = operation_url
        self.elapsed_seconds = elapsed_seconds
        super().__init__(
            f"LRO did not complete after {elapsed_seconds:.1f}s: {operation_url}"
        )


class DataAgentLroFailedError(RuntimeError):
    """Raised with complete diagnostics when a Fabric Data Agent LRO fails."""

    def __init__(
        self,
        *,
        operation_url: str,
        status_code: int,
        body: dict[str, Any],
        response_headers: dict[str, str],
        elapsed_seconds: float,
    ) -> None:
        self.operation_url = operation_url
        self.status_code = status_code
        self.body = body
        self.response_headers = response_headers
        self.elapsed_seconds = elapsed_seconds
        self.request_id = (
            response_headers.get("x-ms-request-id")
            or response_headers.get("request-id")
            or response_headers.get("x-ms-correlation-request-id")
            or ""
        )
        error = body.get("error") if isinstance(body.get("error"), dict) else body
        code = error.get("errorCode") or error.get("code") or "UnknownError"
        message = (
            error.get("message")
            or error.get("errorMessage")
            or "Fabric reported a failed Data Agent operation."
        )
        request_text = f", request_id={self.request_id}" if self.request_id else ""
        super().__init__(
            f"Data Agent LRO failed: {code} — {message} "
            f"(operation={operation_url}{request_text}, "
            f"elapsed={elapsed_seconds:.1f}s, response={body!r})"
        )


class UnsupportedDataSourceType(Exception):
    """Raised when a datasource type is unrecognised and not flagged as preview.

    Attributes
    ----------
    source_type : str
        The unrecognised type.
    """

    def __init__(self, source_type: str) -> None:
        self.source_type = source_type
        super().__init__(
            f"Datasource type {source_type!r} is not in the documented enum "
            f"{sorted(_DOCUMENTED_DATASOURCE_TYPES)} and is not flagged as "
            "capability-discovered preview. Set preview=True on the DataSourceSpec "
            "to allow it."
        )


class DataAgentDefinitionError(ValueError):
    """Raised when the deployed public definition cannot be verified."""


class DataAgentTargetError(ValueError):
    """Raised when an explicit Data Agent target mode cannot be honored."""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class DataSourceElement:
    """A single selectable element within a datasource (e.g. a Graph node/edge type).

    Fields match the documented ``DataSourceElement`` schema for the Fabric Data
    Agent definition.  Only explicitly selected elements (``is_selected=True``)
    are registered; do not silently select all available types.

    Attributes
    ----------
    id : str
        Stable UUID for this element (must be consistent across updates).
    display_name : str
        Human-readable name (matches the node/edge alias in the Graph Model).
    type : str
        Element category.  Use :data:`ELEMENT_TYPE_NODE` (``"graph.nodeType"``)
        or :data:`ELEMENT_TYPE_EDGE` (``"graph.edgeType"``).
    is_selected : bool
        Whether this element is active for the agent.  Must be set explicitly;
        defaults to ``False`` to prevent silently exposing all schema types.
    data_type : str | None
        Optional underlying data type hint.
    description : str | None
        Optional description surfaced in agent context.
    children : list | None
        Optional nested child elements.
    index_state : str | None
        Optional indexing state value.
    """

    id: str
    display_name: str
    type: str  # ELEMENT_TYPE_NODE or ELEMENT_TYPE_EDGE
    is_selected: bool = False
    data_type: str | None = None
    description: str | None = None
    children: list[Any] | None = None
    index_state: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the official element dict shape."""
        d: dict[str, Any] = {
            "id": self.id,
            "display_name": self.display_name,
            "type": self.type,
            "is_selected": self.is_selected,
        }
        if self.data_type is not None:
            d["data_type"] = self.data_type
        if self.description is not None:
            d["description"] = self.description
        if self.children is not None:
            d["children"] = self.children
        if self.index_state is not None:
            d["index_state"] = self.index_state
        return d


@dataclass
class FewShotExample:
    """One few-shot question/GQL pair derived from domain competency questions.

    Attributes
    ----------
    id : str
        Stable UUID for this example (must be consistent across updates).
    question : str
        Natural-language question from an approved competency question.
    query : str
        Bounded GQL query targeting actual node/edge aliases in the Graph Model.
        Must be non-empty; placeholder or raw query strings are rejected.
    """

    id: str
    question: str
    query: str

    def __post_init__(self) -> None:
        if not self.question.strip():
            raise ValueError("FewShotExample.question must be a non-empty string")
        if not self.query.strip():
            raise ValueError("FewShotExample.query must be a non-empty string")

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the official few-shot dict shape."""
        return {"id": self.id, "question": self.question, "query": self.query}


def graph_few_shots_from_competency_contract(
    contract: dict[str, Any],
    *,
    limit: int = 7,
    availability: "dict[str, Any] | None" = None,
) -> list[FewShotExample]:
    """Return validated Graph question/query examples from a compiled contract.

    When *availability* is provided (a mapping of semantic_id →
    :class:`~fabric_kg_builder.semantic.schemas.DataAvailability`), each
    case's required relationship IDs are checked against observed row counts
    via :func:`~fabric_kg_builder.knowledge.validation.gate_competency_examples`.
    Optional-absent cases are silently dropped; required-absent cases raise
    :class:`~fabric_kg_builder.knowledge.validation.DataAgentRequiredExampleEmpty`.

    When *availability* is ``None`` the function falls back to the original
    static-validation-only path for backward compatibility.
    """
    if limit < 1 or not isinstance(contract, dict):
        return []
    # When availability is provided, gate first — raises on required-absent.
    # Retain receipts to derive which case IDs are published; optional-absent
    # cases return published=False and must be excluded from examples.
    published_case_ids: set[str] | None = None
    if availability is not None:
        from fabric_kg_builder.knowledge.validation import (  # noqa: PLC0415
            gate_competency_examples,
        )
        receipts = gate_competency_examples(contract, availability)
        published_case_ids = {r.competency_id for r in receipts if r.published}
    examples: list[FewShotExample] = []
    cases = contract.get("cases")
    if not isinstance(cases, list):
        return examples
    for case in cases:
        if not isinstance(case, dict):
            continue
        case_id = str(case.get("id") or "").strip()
        # Skip cases that gate_competency_examples marked as not published.
        if published_case_ids is not None and case_id not in published_case_ids:
            continue
        question = str(case.get("question") or "").strip()
        probes = case.get("probes")
        graph = probes.get("direct_graph") if isinstance(probes, dict) else None
        if (
            not question
            or not isinstance(graph, dict)
            or graph.get("static_validation_passed") is not True
        ):
            continue
        query = normalize_graph_query_for_fabric(
            str(graph.get("query") or "")
        )
        if not query:
            continue
        stable_key = "\n".join(
            [str(case.get("id") or ""), question, query]
        )
        examples.append(
            FewShotExample(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, stable_key)),
                question=question,
                query=query,
            )
        )
        if len(examples) >= limit:
            break
    return examples


@dataclass(frozen=True)
class GraphFewShotValidationSummary:
    """Outcome of validating competency Graph examples for publication."""

    examples: list[FewShotExample]
    receipts: list[Any]
    direct_results: dict[str, dict[str, Any]]
    candidate_count: int


_FENCE_BLOCK_RE = re.compile(
    r"^\s*```(?:[A-Za-z0-9_-]+)?\s*\n(?P<body>.*)\n```\s*$",
    re.DOTALL,
)


def _strip_query_fence(query: str) -> str:
    match = _FENCE_BLOCK_RE.match(query)
    if match:
        return str(match.group("body") or "").strip()
    return query.strip()


def _normalize_single_quoted_literals(query: str) -> str:
    """Convert single-quoted string literals to Fabric-compatible double quotes."""
    out: list[str] = []
    in_single = False
    in_double = False
    in_backtick = False
    index = 0
    while index < len(query):
        char = query[index]
        if in_single:
            if char == "\\" and index + 1 < len(query):
                out.extend((char, query[index + 1]))
                index += 2
                continue
            if char == "'" and index + 1 < len(query) and query[index + 1] == "'":
                out.append("'")
                index += 2
                continue
            if char == "'":
                out.append('"')
                in_single = False
                index += 1
                continue
            if char == '"':
                out.append('\\"')
                index += 1
                continue
            out.append(char)
            index += 1
            continue
        if in_double:
            out.append(char)
            if char == "\\" and index + 1 < len(query):
                out.append(query[index + 1])
                index += 2
                continue
            if char == '"':
                in_double = False
            index += 1
            continue
        if in_backtick:
            out.append(char)
            if char == "`":
                in_backtick = False
            index += 1
            continue
        if char == "`":
            in_backtick = True
            out.append(char)
        elif char == '"':
            in_double = True
            out.append(char)
        elif char == "'":
            in_single = True
            out.append('"')
        else:
            out.append(char)
        index += 1
    return "".join(out)


def normalize_graph_query_for_fabric(query: str) -> str:
    """Normalize generated GQL into Fabric-compatible syntax."""
    return _normalize_single_quoted_literals(
        _strip_query_fence(query)
    ).strip()


def _case_required(case: dict[str, Any]) -> bool:
    routes = case.get("routes")
    if isinstance(routes, dict):
        return str(routes.get("direct_graph", "required")) != "optional"
    return True


def _required_relationship_ids(case: dict[str, Any]) -> list[str]:
    from fabric_kg_builder.runtime.acceptance import _required_relationships  # noqa: PLC0415

    probes = case.get("probes")
    direct_graph = probes.get("direct_graph") if isinstance(probes, dict) else {}
    from_probe = (
        direct_graph.get("required_relationship_ids")
        if isinstance(direct_graph, dict)
        else None
    )
    probe_ids = (
        [str(item) for item in from_probe if item]
        if isinstance(from_probe, list)
        else []
    )
    expected = case.get("expected") if isinstance(case.get("expected"), dict) else {}
    relationship_specs = _required_relationships(expected.get("relationship_types"))
    expected_ids = list(relationship_specs.keys())
    ordered: list[str] = []
    seen: set[str] = set()
    for rel_id in [*probe_ids, *expected_ids]:
        if rel_id not in seen:
            ordered.append(rel_id)
            seen.add(rel_id)
    return ordered


def _response_request_ids(payload: dict[str, Any]) -> list[str]:
    identifiers: list[str] = []
    for key in (
        "requestId",
        "request_id",
        "correlationId",
        "correlation_id",
        "operationId",
        "operation_id",
    ):
        value = payload.get(key)
        if value:
            identifiers.append(str(value))
    status = payload.get("status")
    if isinstance(status, dict):
        for key in ("requestId", "request_id", "correlationId", "correlation_id"):
            value = status.get(key)
            if value:
                identifiers.append(str(value))
    result = payload.get("result")
    if isinstance(result, dict):
        for key in ("requestId", "request_id", "correlationId", "correlation_id"):
            value = result.get(key)
            if value:
                identifiers.append(str(value))
    return list(dict.fromkeys(identifiers))


def _graph_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = payload.get("result")
    if not isinstance(result, dict) or result.get("kind") != "TABLE":
        return []
    rows = result.get("data")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _collect_canonical_ids(
    rows: list[dict[str, Any]],
    columns: list[str],
) -> set[str]:
    identifiers: set[str] = set()
    for row in rows:
        for column in columns:
            value = row.get(column)
            if isinstance(value, list):
                identifiers.update(str(item) for item in value if item not in {None, ""})
            elif value not in {None, ""}:
                identifiers.add(str(value))
    return identifiers


def _evidence_coverage(
    rows: list[dict[str, Any]],
    evidence_columns: list[str],
) -> float:
    if not rows:
        return 0.0
    if not evidence_columns:
        return 0.0
    covered = 0
    for row in rows:
        has_evidence = False
        for column in evidence_columns:
            value = row.get(column)
            if isinstance(value, list):
                if any(item not in {None, ""} for item in value):
                    has_evidence = True
                    break
            elif value not in {None, ""}:
                has_evidence = True
                break
        if has_evidence:
            covered += 1
    return covered / len(rows)


def validate_graph_few_shot_examples(
    contract: dict[str, Any],
    *,
    availability: "dict[str, Any] | None" = None,
    limit: int = 7,
    dry_run: bool = False,
    execute_graph_query: "Callable[[str], dict[str, Any]] | None" = None,
    query_schema: Any | None = None,
    require_schema: bool = False,
) -> GraphFewShotValidationSummary:
    """Validate, execute, and gate competency Graph examples for publication."""
    from fabric_kg_builder.knowledge.validation import (  # noqa: PLC0415
        DataAgentExampleValidationFailed,
        DataAgentRequiredExampleEmpty,
        gate_competency_examples,
    )
    from fabric_kg_builder.semantic.query_validation import (  # noqa: PLC0415
        compute_physical_query_hash,
        validate_physical_query,
    )
    from fabric_kg_builder.semantic.query_rendering import (  # noqa: PLC0415
        render_bounded_gql,
        validate_bounded_query_plan,
    )
    from fabric_kg_builder.semantic.schemas import (  # noqa: PLC0415
        CompetencyExampleReceipt,
        PersistedQuerySchema,
        SemanticQueryPlan,
        compute_query_plan_hash,
    )

    if not isinstance(contract, dict) or limit < 1:
        return GraphFewShotValidationSummary(
            examples=[],
            receipts=[],
            direct_results={},
            candidate_count=0,
        )
    cases = contract.get("cases")
    if not isinstance(cases, list):
        return GraphFewShotValidationSummary(
            examples=[],
            receipts=[],
            direct_results={},
            candidate_count=0,
        )
    resolved_schema: PersistedQuerySchema | None = None
    schema_payload = query_schema
    if schema_payload is None and isinstance(contract.get("query_schema"), dict):
        schema_payload = contract.get("query_schema")
    if isinstance(schema_payload, PersistedQuerySchema):
        resolved_schema = schema_payload
    elif isinstance(schema_payload, dict):
        resolved_schema = PersistedQuerySchema.model_validate(schema_payload)

    receipt_map: dict[str, Any] = {}
    if availability is not None:
        gate_receipts = gate_competency_examples(contract, availability)
        receipt_map = {
            receipt.competency_id: receipt for receipt in gate_receipts
        }
    else:
        for case in cases:
            if not isinstance(case, dict):
                continue
            case_id = str(case.get("id") or "").strip()
            if not case_id:
                continue
            receipt_map[case_id] = CompetencyExampleReceipt(
                competency_id=case_id,
                required=_case_required(case),
                required_relationship_ids=_required_relationship_ids(case),
                observed_rows={},
                min_required_rows=1,
                status="published",
                remediation="",
                published=True,
            )

    candidate_count = 0
    examples: list[FewShotExample] = []
    receipts: list[Any] = []
    direct_results: dict[str, dict[str, Any]] = {}
    for case in cases:
        if not isinstance(case, dict):
            continue
        case_id = str(case.get("id") or "").strip()
        if not case_id:
            continue
        question = str(case.get("question") or "").strip()
        probes = case.get("probes")
        graph_probe = probes.get("direct_graph") if isinstance(probes, dict) else None
        if not question or not isinstance(graph_probe, dict):
            continue
        query = str(graph_probe.get("query") or "").strip()
        if not query:
            continue
        candidate_count += 1
        base_receipt = receipt_map.get(case_id)
        if base_receipt is None:
            base_receipt = CompetencyExampleReceipt(
                competency_id=case_id,
                required=_case_required(case),
                required_relationship_ids=_required_relationship_ids(case),
                observed_rows={},
                min_required_rows=1,
                status="published",
                remediation="",
                published=True,
            )
        if base_receipt.published is not True:
            receipts.append(base_receipt)
            continue

        normalized_query = normalize_graph_query_for_fabric(query)
        original_query_hash = compute_physical_query_hash(query)
        normalized_query_hash = compute_physical_query_hash(normalized_query)

        if require_schema and resolved_schema is None:
            reason = (
                "query_schema is missing; label/relationship/property/"
                "direction/projection validation cannot run."
            )
            remediation = (
                "Rebuild the agent package with compile-agent so "
                "competency-contract.json embeds query_schema."
            )
            if base_receipt.required:
                raise DataAgentExampleValidationFailed(
                    competency_id=case_id,
                    stage="static-validation",
                    reason=reason,
                    remediation=remediation,
                    required=True,
                )
            receipts.append(base_receipt.model_copy(update={
                "status": "omitted",
                "published": False,
                "remediation": remediation,
                "original_query_hash": original_query_hash,
                "normalized_query_hash": normalized_query_hash,
            }))
            continue

        plan_payload = (
            graph_probe.get("semantic_plan")
            if isinstance(graph_probe.get("semantic_plan"), dict)
            else case.get("semantic_plan")
        )
        try:
            plan = (
                SemanticQueryPlan.model_validate(plan_payload)
                if isinstance(plan_payload, dict)
                else None
            )
        except Exception as exc:
            remediation = (
                "Recompile competency-contract.json against the current "
                "persisted query schema."
            )
            if base_receipt.required:
                raise DataAgentExampleValidationFailed(
                    competency_id=case_id,
                    stage="static-validation",
                    reason=f"Invalid semantic_plan payload: {exc}",
                    remediation=remediation,
                    required=True,
                    result_category="invalid_semantic_plan",
                ) from exc
            receipts.append(base_receipt.model_copy(update={
                "status": "omitted",
                "published": False,
                "remediation": remediation,
                "original_query_hash": original_query_hash,
                "normalized_query_hash": normalized_query_hash,
                "direct_result_category": "invalid_semantic_plan",
            }))
            continue
        if (
            resolved_schema is not None
            and resolved_schema.schema_mode == "schema2_bounded"
        ):
            if plan is None:
                raise DataAgentExampleValidationFailed(
                    competency_id=case_id,
                    stage="static-validation",
                    reason="Schema-2 example has no structured semantic plan.",
                    remediation=(
                        "Recompile the competency contract from the approved "
                        "DomainContractV2 question plan."
                    ),
                    required=base_receipt.required,
                    result_category="invalid_semantic_plan",
                )
            bounded_findings = validate_bounded_query_plan(
                plan,
                resolved_schema,
            )
            if bounded_findings:
                raise DataAgentExampleValidationFailed(
                    competency_id=case_id,
                    stage="static-validation",
                    reason="; ".join(
                        f"{finding.code}: {finding.message}"
                        for finding in bounded_findings
                    ),
                    remediation=(
                        "Recompile the competency contract from the current "
                        "bounded query authority."
                    ),
                    required=base_receipt.required,
                    result_category="invalid_semantic_plan",
                )
            expected_query = render_bounded_gql(plan, resolved_schema)
            if normalized_query != expected_query:
                raise DataAgentExampleValidationFailed(
                    competency_id=case_id,
                    stage="static-validation",
                    reason=(
                        "Schema-2 example query differs from deterministic "
                        "structured-plan rendering."
                    ),
                    remediation=(
                        "Remove authored GQL and rebuild the agent examples."
                    ),
                    required=base_receipt.required,
                    result_category="invalid_physical_query",
                )
            base_receipt = base_receipt.model_copy(update={
                "semantic_plan_hash": compute_query_plan_hash(plan),
                "query_authority_hash": plan.query_authority_hash,
                "actual_hop_count": len(plan.path_steps),
            })
        findings = validate_physical_query(
            normalized_query,
            plan,
            schema=resolved_schema,
            raise_on_findings=False,
        )
        if findings:
            finding_text = "; ".join(
                f"{finding.code}: {finding.message}"
                for finding in findings
            )
            remediation = (
                "Fix the competency Graph probe so it uses Fabric-compatible "
                "GQL with valid labels, relationships, properties, "
                "direction, projection, and bounded LIMIT."
            )
            if base_receipt.required:
                raise DataAgentExampleValidationFailed(
                    competency_id=case_id,
                    stage="static-validation",
                    reason=finding_text,
                    remediation=remediation,
                    required=True,
                    result_category="invalid_physical_query",
                )
            receipts.append(base_receipt.model_copy(update={
                "status": "omitted",
                "published": False,
                "remediation": remediation,
                "original_query_hash": original_query_hash,
                "normalized_query_hash": normalized_query_hash,
                "direct_result_category": "invalid_physical_query",
            }))
            continue

        if dry_run:
            if len(examples) >= limit:
                reason = (
                    f"Graph example limit exceeded (maximum {limit})."
                )
                remediation = (
                    f"Reduce published Graph competency examples to ≤{limit}."
                )
                if base_receipt.required:
                    raise DataAgentExampleValidationFailed(
                        competency_id=case_id,
                        stage="limit",
                        reason=reason,
                        remediation=remediation,
                        required=True,
                    )
                receipts.append(base_receipt.model_copy(update={
                    "status": "omitted",
                    "published": False,
                    "remediation": remediation,
                    "original_query_hash": original_query_hash,
                    "normalized_query_hash": normalized_query_hash,
                    "direct_result_category": "limit_exceeded",
                }))
                continue
            examples.append(FewShotExample(
                id=str(uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    "\n".join([case_id, question, normalized_query]),
                )),
                question=question,
                query=normalized_query,
            ))
            receipts.append(base_receipt.model_copy(update={
                "status": "published",
                "published": True,
                "original_query_hash": original_query_hash,
                "normalized_query_hash": normalized_query_hash,
                "direct_result_category": "dry_run",
            }))
            continue

        if execute_graph_query is None:
            raise ValueError(
                "execute_graph_query is required when dry_run is False."
            )
        response = execute_graph_query(normalized_query)
        if not isinstance(response, dict):
            raise DataAgentExampleValidationFailed(
                competency_id=case_id,
                stage="direct-graph",
                reason="Graph query response is not an object.",
                remediation=(
                    "Capture the request ID and Graph response payload, then "
                    "verify workspace, Graph Model ID, and query compatibility."
                ),
                required=base_receipt.required,
                result_category="platform_failure",
            )
        status = response.get("status")
        status_code = str(status.get("code") if isinstance(status, dict) else "")
        status_description = str(
            status.get("description") if isinstance(status, dict) else ""
        )
        status_ok = (
            len(status_code) >= 2 and status_code[:2] in _GQL_SUCCESS_PREFIXES
        )
        request_ids = _response_request_ids(response)
        rows = _graph_rows(response)
        row_count = len(rows)
        if not status_ok:
            result_category = (
                "invalid_physical_query"
                if "query" in status_description.casefold()
                or "syntax" in status_description.casefold()
                else "platform_failure"
            )
            remediation = (
                "Fix the Graph probe GQL and verify Fabric Graph readiness. "
                "If this is a syntax failure, regenerate the query using "
                "Fabric-compatible literals and projections."
            )
            if base_receipt.required:
                raise DataAgentExampleValidationFailed(
                    competency_id=case_id,
                    stage="direct-graph",
                    reason=(
                        f"Fabric application status failed "
                        f"(code={status_code or 'missing'}): {status_description}"
                    ),
                    remediation=remediation,
                    required=True,
                    result_category=result_category,
                )
            receipts.append(base_receipt.model_copy(update={
                "status": "omitted",
                "published": False,
                "remediation": remediation,
                "original_query_hash": original_query_hash,
                "normalized_query_hash": normalized_query_hash,
                "direct_graph_row_count": row_count,
                "direct_result_category": result_category,
                "direct_request_ids": request_ids,
            }))
            continue

        if row_count <= 0 and base_receipt.required:
            relationship_id = (
                base_receipt.required_relationship_ids[0]
                if base_receipt.required_relationship_ids
                else "unknown"
            )
            raise DataAgentRequiredExampleEmpty(
                competency_id=case_id,
                relationship_id=relationship_id,
                observed_rows=0,
                expected_minimum=1,
                stage="pre-publication-live",
                remediation=(
                    "Direct Graph execution returned no rows for a required "
                    f"example ('{case_id}'). Validate deployed data and "
                    "relationship materialization before publishing."
                ),
            )
        if row_count <= 0 and not base_receipt.required:
            receipts.append(base_receipt.model_copy(update={
                "status": "omitted",
                "published": False,
                "original_query_hash": original_query_hash,
                "normalized_query_hash": normalized_query_hash,
                "direct_graph_row_count": row_count,
                "direct_result_category": "optional_data_absent",
                "direct_request_ids": request_ids,
            }))
            continue

        evidence_columns = ["evidence_id", "evidenceId"]
        relationship_bindings = (
            graph_probe.get("relationship_bindings")
            if isinstance(graph_probe.get("relationship_bindings"), list)
            else []
        )
        for binding in relationship_bindings:
            if isinstance(binding, dict):
                column = str(binding.get("evidence_column") or "").strip()
                if column:
                    evidence_columns.append(column)
        coverage = _evidence_coverage(rows, list(dict.fromkeys(evidence_columns)))
        expected = case.get("expected")
        evidence_required = True
        if isinstance(expected, dict) and "evidence_required" in expected:
            evidence_required = bool(expected.get("evidence_required"))
        if evidence_required and coverage < 1.0:
            remediation = (
                "Required evidence IDs are missing in one or more result rows. "
                "Ensure the direct Graph probe returns evidence_id for every "
                "row before publishing this example."
            )
            if base_receipt.required:
                raise DataAgentExampleValidationFailed(
                    competency_id=case_id,
                    stage="direct-graph",
                    reason=(
                        f"Evidence coverage is {coverage:.2%}; "
                        "100% is required."
                    ),
                    remediation=remediation,
                    required=True,
                    result_category="invalid_physical_query",
                )
            receipts.append(base_receipt.model_copy(update={
                "status": "omitted",
                "published": False,
                "remediation": remediation,
                "original_query_hash": original_query_hash,
                "normalized_query_hash": normalized_query_hash,
                "direct_graph_row_count": row_count,
                "evidence_coverage": coverage,
                "direct_result_category": "invalid_physical_query",
                "direct_request_ids": request_ids,
            }))
            continue

        canonical_columns = [
            str(column)
            for column in (
                graph_probe.get("canonical_id_columns")
                if isinstance(graph_probe.get("canonical_id_columns"), list)
                else []
            )
            if column
        ]
        if len(examples) >= limit:
            reason = (
                f"Graph example limit exceeded (maximum {limit})."
            )
            remediation = (
                f"Reduce published Graph competency examples to ≤{limit}."
            )
            if base_receipt.required:
                raise DataAgentExampleValidationFailed(
                    competency_id=case_id,
                    stage="limit",
                    reason=reason,
                    remediation=remediation,
                    required=True,
                )
            receipts.append(base_receipt.model_copy(update={
                "status": "omitted",
                "published": False,
                "remediation": remediation,
                "original_query_hash": original_query_hash,
                "normalized_query_hash": normalized_query_hash,
                "direct_graph_row_count": row_count,
                "evidence_coverage": coverage if evidence_required else 1.0,
                "direct_result_category": "limit_exceeded",
                "direct_request_ids": request_ids,
            }))
            continue
        direct_results[case_id] = {
            "canonical_ids": sorted(
                _collect_canonical_ids(rows, canonical_columns)
            ),
            "row_count": row_count,
            "result_category": "success",
        }
        examples.append(FewShotExample(
            id=str(uuid.uuid5(
                uuid.NAMESPACE_URL,
                "\n".join([case_id, question, normalized_query]),
            )),
            question=question,
            query=normalized_query,
        ))
        receipts.append(base_receipt.model_copy(update={
            "status": "published",
            "published": True,
            "original_query_hash": original_query_hash,
            "normalized_query_hash": normalized_query_hash,
            "direct_graph_row_count": row_count,
            "evidence_coverage": coverage if evidence_required else 1.0,
            "direct_result_category": "success",
            "direct_request_ids": request_ids,
        }))

    return GraphFewShotValidationSummary(
        examples=examples,
        receipts=receipts,
        direct_results=direct_results,
        candidate_count=candidate_count,
    )


def compare_graph_few_shot_semantics(
    contract: dict[str, Any],
    receipts: list[Any],
    *,
    direct_results: Mapping[str, Mapping[str, Any]],
    execute_data_agent_case: "Callable[[dict[str, Any]], dict[str, Any]]",
) -> list[Any]:
    """Run published examples through Data Agent and compare semantic outcomes."""
    from fabric_kg_builder.knowledge.validation import (  # noqa: PLC0415
        DataAgentExampleValidationFailed,
    )

    if not isinstance(contract, dict):
        return list(receipts)
    cases = contract.get("cases")
    if not isinstance(cases, list):
        return list(receipts)
    case_map = {
        str(case.get("id") or ""): case
        for case in cases
        if isinstance(case, dict) and case.get("id")
    }
    updated: list[Any] = []
    successful_categories = {"success", "optional_data_absent"}
    for receipt in receipts:
        if getattr(receipt, "published", False) is not True:
            updated.append(receipt)
            continue
        competency_id = str(getattr(receipt, "competency_id", "") or "")
        case = case_map.get(competency_id)
        if case is None:
            raise DataAgentExampleValidationFailed(
                competency_id=competency_id or "<unknown>",
                stage="semantic-compare",
                reason="Published example is missing from competency contract.",
                remediation=(
                    "Rebuild the agent package and ensure competency-contract "
                    "contains every published example case."
                ),
                required=bool(getattr(receipt, "required", True)),
                result_category="invalid_semantic_plan",
            )
        response = execute_data_agent_case(case)
        if not isinstance(response, dict):
            raise DataAgentExampleValidationFailed(
                competency_id=competency_id,
                stage="semantic-compare",
                reason="Data Agent response is not an object.",
                remediation=(
                    "Capture Data Agent request IDs and verify the MCP tool "
                    "response envelope."
                ),
                required=bool(getattr(receipt, "required", True)),
                result_category="platform_failure",
            )
        result_category = str(
            response.get("result_category")
            or response.get("final_semantic_status")
            or ""
        ).strip()
        if not result_category and str(response.get("status") or "") == "success":
            result_category = "success"
        request_ids = response.get("request_ids")
        request_ids = (
            [str(item) for item in request_ids if item]
            if isinstance(request_ids, list)
            else []
        )
        citations = (
            response.get("citations")
            if isinstance(response.get("citations"), list)
            else []
        )
        data_agent_ids: set[str] = set()
        for citation in citations:
            if not isinstance(citation, dict):
                continue
            if citation.get("canonical_id") not in {None, ""}:
                data_agent_ids.add(str(citation.get("canonical_id")))
            if isinstance(citation.get("canonical_ids"), list):
                data_agent_ids.update(
                    str(value)
                    for value in citation.get("canonical_ids")
                    if value not in {None, ""}
                )
        data_agent_row_count = len(citations)
        if data_agent_row_count == 0 and str(response.get("answer") or "").strip():
            data_agent_row_count = 1
        direct = direct_results.get(competency_id, {})
        direct_ids = {
            str(value)
            for value in (
                direct.get("canonical_ids")
                if isinstance(direct.get("canonical_ids"), list)
                else []
            )
            if value not in {None, ""}
        }
        semantic_match = (
            bool(direct_ids & data_agent_ids)
            if direct_ids
            else (
                result_category in successful_categories
                and data_agent_row_count > 0
            )
        )
        if (
            result_category not in successful_categories
            or not semantic_match
        ):
            remediation = (
                "Review the published Data Agent answer/citations and align "
                "its semantic output with the direct Graph query results."
            )
            if bool(getattr(receipt, "required", True)):
                raise DataAgentExampleValidationFailed(
                    competency_id=competency_id,
                    stage="semantic-compare",
                    reason=(
                        "Data Agent semantic output does not match the direct "
                        "Graph execution result."
                    ),
                    remediation=remediation,
                    required=True,
                    result_category=result_category or "platform_failure",
                )
            updated.append(receipt.model_copy(update={
                "status": "omitted",
                "published": False,
                "remediation": remediation,
                "data_agent_result_category": (
                    result_category or "platform_failure"
                ),
                "data_agent_row_count": data_agent_row_count,
                "data_agent_request_ids": request_ids,
                "semantic_match": False,
            }))
            continue
        updated.append(receipt.model_copy(update={
            "status": "published",
            "published": True,
            "data_agent_result_category": result_category,
            "data_agent_row_count": data_agent_row_count,
            "data_agent_request_ids": request_ids,
            "semantic_match": True,
        }))
    return updated


@dataclass
class DataSourceSpec:
    """A single data source configuration for a Fabric Data Agent.

    Attributes
    ----------
    source_type : str
        Datasource type using the official snake_case enum:
        ``graph``, ``lakehouse``, ``lakehouse_tables``, ``data_warehouse``,
        ``kusto``, ``semantic_model``, ``mirrored_database``,
        ``mirrored_azure_databricks``.  Use ``preview=True`` for
        capability-discovered types outside the documented enum.
    name : str
        Unique path-safe name used in the definition part path
        ``{type}-{name}/datasource.json`` (e.g. the safe display name of the
        Graph Model item).
    artifact_id : str
        Fabric item GUID of the source artifact (required in datasource.json).
    workspace_id : str
        Fabric workspace GUID owning the source artifact.
    display_name : str
        Human-readable display name for the datasource entry.
    instructions : str
        ``dataSourceInstructions`` injected into the datasource.json.
    description : str
        ``userDescription`` field in datasource.json.
    metadata : dict
        Additional metadata dict in datasource.json.
    elements : list[DataSourceElement]
        Explicitly selected elements (node/edge types for graph sources).
        Only elements with ``is_selected=True`` should be included.
        Do not auto-populate with all available types.
    few_shots : list[FewShotExample] | None
        Optional few-shot examples derived from domain competency questions.
        When provided, a ``fewshots.json`` part is emitted alongside
        ``datasource.json``.  Only include validated GQL against actual aliases.
    preview : bool
        Set to ``True`` for types outside the documented enum.  Prevents the
        builder from raising :class:`UnsupportedDataSourceType`.
    """

    source_type: str
    name: str
    artifact_id: str = ""
    workspace_id: str = ""
    display_name: str = ""
    instructions: str = ""
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    elements: list[DataSourceElement] = field(default_factory=list)
    few_shots: list[FewShotExample] | None = None
    preview: bool = False

    def datasource_path(self) -> str:
        """Return the definition part path for this source's datasource.json."""
        return f"Files/Config/draft/{self.source_type}-{self.name}/datasource.json"

    def fewshots_path(self) -> str:
        """Return the definition part path for this source's fewshots.json."""
        return f"Files/Config/draft/{self.source_type}-{self.name}/fewshots.json"

    def datasource_payload(self) -> dict[str, Any]:
        """Return the official datasource.json content dict.

        Conforms to the Fabric Data Agent datasource definition schema at:
        https://learn.microsoft.com/rest/api/fabric/articles/item-management/definitions/data-agent-definition
        """
        return {
            "$schema": _DATASOURCE_SCHEMA,
            "artifactId": self.artifact_id,
            "workspaceId": self.workspace_id,
            "displayName": self.display_name or self.name,
            "type": self.source_type,
            "dataSourceInstructions": self.instructions,
            "userDescription": self.description,
            "metadata": self.metadata,
            "elements": [e.to_dict() for e in self.elements],
        }

    def fewshots_payload(self) -> dict[str, Any]:
        """Return the official fewshots.json content dict.

        Only call this when :attr:`few_shots` is not ``None`` and non-empty.
        """
        examples = self.few_shots or []
        return {
            "$schema": _FEWSHOTS_SCHEMA,
            "fewShots": [fs.to_dict() for fs in examples],
        }


@dataclass
class DataAgentSpec:
    """Complete specification for a Fabric Data Agent item.

    Attributes
    ----------
    display_name : str
        The ``displayName`` of the Fabric item.
    instruction : str
        System instruction injected into the agent manifest.
    sources : list[DataSourceSpec]
        Data sources to include (max :data:`MAX_SOURCES` = 5).
    schema_version : str
        Manifest schema version string.
    """

    display_name: str
    instruction: str = ""
    sources: list[DataSourceSpec] = field(default_factory=list)
    schema_version: str = _DATA_AGENT_SCHEMA


# ---------------------------------------------------------------------------
# Definition builder (AGK-005 part builder)
# ---------------------------------------------------------------------------


def build_definition_parts(spec: DataAgentSpec) -> list[dict[str, str]]:
    """Build the ``InlineBase64`` definition parts for a Fabric Data Agent item.

    Validates the source count cap and source type legality before building.

    Parameters
    ----------
    spec : DataAgentSpec
        The agent specification to encode.

    Returns
    -------
    list[dict[str, str]]
        List of ``{"path": ..., "payload": ..., "payloadType": "InlineBase64"}``
        dicts ready to embed in the Fabric Items API ``definition.parts`` array.

    Raises
    ------
    SourceCapError
        More than :data:`MAX_SOURCES` sources.
    UnsupportedDataSourceType
        A source type is not in the documented enum and not marked
        ``preview=True``.
    """
    if len(spec.sources) > MAX_SOURCES:
        raise SourceCapError(len(spec.sources))

    for src in spec.sources:
        normalised = src.source_type.lower().replace(" ", "")
        if normalised not in _DOCUMENTED_DATASOURCE_TYPES and not src.preview:
            raise UnsupportedDataSourceType(src.source_type)

    parts: list[dict[str, str]] = []

    # 1. data_agent.json — contains only the schema version per official spec
    # https://learn.microsoft.com/rest/api/fabric/articles/item-management/definitions/data-agent-definition
    agent_manifest: dict[str, Any] = {"$schema": spec.schema_version}
    parts.append(_encode_part("Files/Config/data_agent.json", agent_manifest))

    # 2. stage_config.json (draft) — AI instructions for the draft stage
    stage_config: dict[str, Any] = {
        "$schema": _STAGE_CONFIG_SCHEMA,
        "aiInstructions": spec.instruction,
    }
    parts.append(
        _encode_part("Files/Config/draft/stage_config.json", stage_config)
    )

    # 3. Per-source datasource.json + optional fewshots.json
    for src in spec.sources:
        parts.append(_encode_part(src.datasource_path(), src.datasource_payload()))
        if src.few_shots:
            parts.append(_encode_part(src.fewshots_path(), src.fewshots_payload()))

    return parts


def _encode_part(path: str, payload: dict[str, Any]) -> dict[str, str]:
    """Base64-encode *payload* as JSON and return a definition part dict."""
    raw = json.dumps(payload, ensure_ascii=False, indent=2)
    b64 = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    return {
        "path": path,
        "payload": b64,
        "payloadType": "InlineBase64",
    }


# ---------------------------------------------------------------------------
# Upsert result
# ---------------------------------------------------------------------------


@dataclass
class DataAgentUpsertResult:
    """Outcome of a Fabric Data Agent upsert operation.

    Attributes
    ----------
    item_id : str
        Fabric item GUID (or ``"lro:<location>"`` if still provisioning).
    created : bool
        ``True`` if the item was newly created.
    status : str
        ``"created-201"``, ``"created-lro"``, ``"updated"``, or ``"mock"``.
    display_name : str
        The agent display name.
    note : str
        Human-readable status message.
    """

    item_id: str
    created: bool
    status: str
    display_name: str
    note: str = ""


@dataclass
class DataAgentPublishResult:
    """Outcome of promoting a Data Agent staging definition to production."""

    item_id: str
    published_description: str
    status: str = "published"


@dataclass(frozen=True)
class DataAgentStageSnapshot:
    """Decoded draft or published instruction and source selection."""

    stage: Literal["draft", "published"]
    instruction: str
    sources: tuple[dict[str, Any], ...]

    @property
    def instruction_hash(self) -> str:
        return _text_hash(self.instruction)

    @property
    def source_selection_hash(self) -> str:
        return _canonical_hash({
            "sources": [
                _normalized_source_selection(source)
                for source in self.sources
            ]
        })

    @property
    def selected_element_hash(self) -> str:
        return _canonical_hash({
            "elements": [
                {
                    "source_type": str(source.get("type") or ""),
                    "element": element,
                }
                for source in self.sources
                for element in _selected_elements(source)
            ]
        })

    @property
    def selected_element_count(self) -> int:
        return sum(
            len(_selected_elements(source)) for source in self.sources
        )

    @property
    def property_child_count(self) -> int:
        return sum(
            len(_selected_children(element))
            for source in self.sources
            for element in _selected_elements(source)
        )

    @property
    def selected_property_ids(self) -> list[str]:
        """Sorted canonical property child IDs across all selected elements.

        Used to compute content-based property selection hashes that distinguish
        equal-size but different selections (fix for #14).
        """
        return sorted(
            str(child.get("id") or "")
            for source in self.sources
            for element in _selected_elements(source)
            for child in _selected_children(element)
        )

    @property
    def agent_schema_sidecar(self) -> dict[str, Any] | None:
        sidecars = [
            metadata.get("fabricKgAgentSchema")
            for source in self.sources
            if isinstance((metadata := source.get("metadata")), dict)
            and isinstance(metadata.get("fabricKgAgentSchema"), dict)
        ]
        if not sidecars:
            return None
        first = sidecars[0]
        if any(sidecar != first for sidecar in sidecars[1:]):
            raise DataAgentDefinitionError(
                "Data Agent sources contain inconsistent semantic sidecars."
            )
        return first

    @property
    def agent_schema_reference(self) -> dict[str, str] | None:
        """Return the compact public-definition semantic metadata reference."""
        keys = (
            "fabricKgAgentSchemaHash",
            "fabricKgSemanticModelManifestHash",
            "fabricKgPersistedProjectionReceiptHash",
            "fabricKgOntologyItemId",
            "fabricKgGraphModelId",
            "fabricKgPropertyChildCoverage",
            "fabricKgExpectedPropertyCount",
            "fabricKgDomainContractHash",
            "fabricKgQueryAuthorityHash",
            "fabricKgPersistedQuerySchemaHash",
            "fabricKgApprovedMaxHops",
        )
        references = [
            {
                key: str(metadata.get(key) or "")
                for key in keys
            }
            for source in self.sources
            if isinstance((metadata := source.get("metadata")), dict)
            and metadata.get("fabricKgAgentSchemaHash")
        ]
        if not references:
            return None
        first = references[0]
        if any(reference != first for reference in references[1:]):
            raise DataAgentDefinitionError(
                "Data Agent sources contain inconsistent semantic metadata "
                "references."
            )
        return first

    @property
    def agent_schema_sidecar_hash(self) -> str | None:
        sidecar = self.agent_schema_sidecar
        if sidecar is not None:
            return _canonical_hash(sidecar)
        reference = self.agent_schema_reference
        if reference is None:
            return None
        return reference["fabricKgAgentSchemaHash"] or None

    def source_receipts(self) -> list[dict[str, Any]]:
        """Return source identities and independently observed selection counts."""
        return [
            {
                "source_type": str(source.get("type") or ""),
                "source_name": str(source.get("_source_name") or ""),
                "workspace_id": str(source.get("workspaceId") or ""),
                "artifact_id": str(source.get("artifactId") or ""),
                "selected_element_count": len(
                    _selected_elements(source)
                ),
                "property_child_count": sum(
                    len(_selected_children(element))
                    for element in _selected_elements(source)
                ),
            }
            for source in self.sources
        ]


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _text_hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _selected_children(element: dict[str, Any]) -> list[dict[str, Any]]:
    children = element.get("children")
    if not isinstance(children, list):
        return []
    return sorted(
        (
            _normalized_data_source_element(child)
            for child in children
            if isinstance(child, dict)
            and child.get("is_selected") is True
        ),
        key=lambda child: str(child.get("id") or ""),
    )


def _normalized_data_source_element(
    element: dict[str, Any],
) -> dict[str, Any]:
    normalized = {
        "id": str(element.get("id") or ""),
        "display_name": str(element.get("display_name") or ""),
        "type": str(element.get("type") or ""),
        "is_selected": element.get("is_selected") is True,
    }
    for key in ("data_type", "description", "index_state"):
        value = element.get(key)
        if value is not None:
            normalized[key] = value
    children = _selected_children(element)
    if children:
        normalized["children"] = children
    return normalized


def _selected_elements(source: dict[str, Any]) -> list[dict[str, Any]]:
    elements = source.get("elements")
    if not isinstance(elements, list):
        return []
    return sorted(
        (
            _normalized_data_source_element(element)
            for element in elements
            if isinstance(element, dict)
            and element.get("is_selected") is True
        ),
        key=lambda element: str(element.get("id") or ""),
    )


def _normalized_source_selection(
    source: dict[str, Any],
) -> dict[str, Any]:
    return {
        "source_type": str(source.get("type") or ""),
        "workspace_id": str(source.get("workspaceId") or ""),
        "artifact_id": str(source.get("artifactId") or ""),
        "display_name": str(source.get("displayName") or ""),
        "metadata": source.get("metadata") or {},
        "elements": _selected_elements(source),
    }


def _decode_part_payload(part: dict[str, Any]) -> dict[str, Any]:
    if part.get("payloadType") != "InlineBase64":
        raise DataAgentDefinitionError(
            f"Definition part {part.get('path')!r} must use InlineBase64."
        )
    try:
        decoded = base64.b64decode(
            str(part.get("payload") or ""),
            validate=True,
        ).decode("utf-8")
        payload = json.loads(decoded)
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise DataAgentDefinitionError(
            f"Could not decode definition part {part.get('path')!r}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise DataAgentDefinitionError(
            f"Definition part {part.get('path')!r} must decode to an object."
        )
    return payload


def decode_stage_snapshot(
    definition: dict[str, Any],
    stage: Literal["draft", "published"],
) -> DataAgentStageSnapshot:
    """Decode one stage from an independently read-back definition."""
    parts = definition.get("parts")
    if not isinstance(parts, list):
        raise DataAgentDefinitionError(
            "Data Agent definition has no parts list."
        )
    stage_path = f"Files/Config/{stage}/stage_config.json"
    instruction: str | None = None
    sources: list[dict[str, Any]] = []
    prefix = f"Files/Config/{stage}/"
    suffix = "/datasource.json"
    for part in parts:
        if not isinstance(part, dict):
            continue
        path = str(part.get("path") or "")
        if path == stage_path:
            payload = _decode_part_payload(part)
            value = payload.get("aiInstructions")
            if not isinstance(value, str) or not value.strip():
                raise DataAgentDefinitionError(
                    f"{stage_path} has no non-empty aiInstructions string."
                )
            instruction = value
            continue
        if not path.startswith(prefix) or not path.endswith(suffix):
            continue
        payload = _decode_part_payload(part)
        source_type = str(payload.get("type") or "")
        directory = path[len(prefix):-len(suffix)]
        name_prefix = f"{source_type}-"
        source_name = (
            directory[len(name_prefix):]
            if source_type and directory.startswith(name_prefix)
            else directory
        )
        payload["_source_name"] = source_name
        sources.append(payload)
    if instruction is None:
        raise DataAgentDefinitionError(
            f"Data Agent definition is missing {stage_path}."
        )
    return DataAgentStageSnapshot(
        stage=stage,
        instruction=instruction,
        sources=tuple(sorted(
            sources,
            key=lambda source: (
                str(source.get("type") or ""),
                str(source.get("_source_name") or ""),
            ),
        )),
    )


def stage_snapshot_from_spec(spec: DataAgentSpec) -> DataAgentStageSnapshot:
    """Build the expected draft snapshot from the compiled Data Agent spec."""
    return decode_stage_snapshot(
        {"parts": build_definition_parts(spec)},
        "draft",
    )


# ---------------------------------------------------------------------------
# FabricDataAgentClient
# ---------------------------------------------------------------------------


class FabricDataAgentClient:
    """Client for idempotent create/update of Fabric Data Agent items.

    Parameters
    ----------
    workspace_id : str
        Fabric workspace GUID.
    transport : HttpTransport
        Injectable transport (use ``FakeTransport`` in tests).
    token : str | None
        Pre-obtained bearer token.
    token_provider : Callable[[], str] | None
        Token factory.  Defaults to ``DefaultAzureCredential`` when both
        *token* and *token_provider* are ``None``.
    lro_timeout_seconds : int
        Maximum seconds to wait for an LRO to complete (default 300).
    lro_poll_interval : int
        Seconds between LRO poll attempts (default 5; overridden by
        ``Retry-After`` header when present).
    """

    def __init__(
        self,
        workspace_id: str,
        transport: HttpTransport,
        token: str | None = None,
        token_provider: Callable[[], str] | None = None,
        lro_timeout_seconds: int = _DEFAULT_LRO_TIMEOUT,
        lro_poll_interval: int = _DEFAULT_LRO_POLL_INTERVAL,
    ) -> None:
        self._ws = workspace_id
        self._transport = transport
        self._token = token
        self._token_provider = token_provider
        self._lro_timeout = lro_timeout_seconds
        self._lro_poll = lro_poll_interval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_token(self) -> str:
        if self._token:
            return self._token
        if self._token_provider:
            self._token = self._token_provider()
            return self._token
        from azure.identity import DefaultAzureCredential  # noqa: PLC0415

        from fabric_kg_builder.azure_identity import default_azure_credential

        cred = default_azure_credential()
        self._token = cred.get_token(_FABRIC_TOKEN_SCOPE).token
        return self._token

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
        }

    def _base_url(self) -> str:
        return f"{_FABRIC_API_BASE}/workspaces/{self._ws}"

    # ------------------------------------------------------------------
    # List / get
    # ------------------------------------------------------------------

    def _list_items(self, *, max_pages: int = 100) -> list[dict[str, Any]]:
        """Return all items in the workspace (GET /workspaces/{ws}/items)."""
        url = f"{self._base_url()}/items"
        items: list[dict[str, Any]] = []
        for _page in range(max_pages):
            resp = self._transport.send(
                HttpRequest(
                    method="GET",
                    url=url,
                    headers=self._auth_headers(),
                )
            )
            if resp.status_code >= 400:
                raise HttpError(resp.status_code, resp.body)
            body = resp.body if isinstance(resp.body, dict) else {}
            values = body.get("value", [])
            if not isinstance(values, list):
                raise DataAgentTargetError(
                    "Fabric item listing returned a non-list value."
                )
            items.extend(
                value for value in values if isinstance(value, dict)
            )

            continuation_uri = body.get("continuationUri")
            continuation_token = body.get("continuationToken")
            if not continuation_uri and not continuation_token:
                return items
            if continuation_uri:
                candidate = urljoin(
                    f"{_FABRIC_API_BASE}/",
                    str(continuation_uri),
                )
                if not candidate.startswith(f"{_FABRIC_API_BASE}/"):
                    raise DataAgentTargetError(
                        "Fabric item listing returned an untrusted "
                        "continuation URI."
                    )
                url = candidate
            else:
                url = (
                    f"{self._base_url()}/items?continuationToken="
                    f"{quote(str(continuation_token), safe='')}"
                )

        raise DataAgentTargetError(
            f"Fabric item listing exceeded {max_pages} pages."
        )

    def get_data_agent(self, display_name: str) -> dict[str, Any] | None:
        """Return the existing DataAgent item dict matching *display_name*, or ``None``."""
        items = self._list_items()
        return next(
            (
                it
                for it in items
                if it.get("displayName") == display_name
                and it.get("type") == "DataAgent"
            ),
            None,
        )

    def get_data_agent_by_id(self, item_id: str) -> dict[str, Any] | None:
        """Return the exact configured DataAgent item, never a name fallback."""
        if not item_id:
            raise ValueError("Data Agent item ID must not be empty.")
        return next(
            (
                item
                for item in self._list_items()
                if item.get("id") == item_id
                and item.get("type") == "DataAgent"
            ),
            None,
        )

    def get_definition(self, item_id: str) -> dict[str, Any]:
        """Return the deployed public definition for a Data Agent item."""
        url = (
            f"{self._base_url()}/dataAgents/{item_id}/getDefinition"
        )
        resp = self._transport.send(
            HttpRequest(
                method="POST",
                url=url,
                headers=self._auth_headers(),
                body={},
            )
        )
        if resp.status_code >= 400:
            raise HttpError(resp.status_code, resp.body)
        body = resp.body if isinstance(resp.body, dict) else {}
        if resp.status_code == 202:
            location = (
                resp.headers.get("Location")
                or resp.headers.get("location")
                or ""
            )
            if not location:
                raise DataAgentDefinitionError(
                    "Data Agent getDefinition returned 202 without Location."
                )
            retry_after_text = (
                resp.headers.get("Retry-After")
                or resp.headers.get("retry-after")
                or str(self._lro_poll)
            )
            try:
                retry_after = int(retry_after_text)
            except ValueError:
                retry_after = self._lro_poll
            body = self._poll_lro(location, retry_after)
            definition = self._definition_from_body(body)
            if definition is None:
                result_resp = self._transport.send(
                    HttpRequest(
                        method="GET",
                        url=f"{location.rstrip('/')}/result",
                        headers=self._auth_headers(),
                    )
                )
                if result_resp.status_code >= 400:
                    raise HttpError(
                        result_resp.status_code,
                        result_resp.body,
                    )
                body = (
                    result_resp.body
                    if isinstance(result_resp.body, dict)
                    else {}
                )
        definition = self._definition_from_body(body)
        if definition is None:
            raise DataAgentDefinitionError(
                "Data Agent getDefinition response has no definition."
            )
        return definition

    def get_deployed_instruction(self, item_id: str) -> str:
        """Read the independently observed published instruction."""
        return self.get_stage_snapshot(item_id, "published").instruction

    def get_stage_snapshot(
        self,
        item_id: str,
        stage: Literal["draft", "published"],
    ) -> DataAgentStageSnapshot:
        """Read and decode one persisted Data Agent stage."""
        return decode_stage_snapshot(self.get_definition(item_id), stage)

    def get_stage_snapshots(
        self,
        item_id: str,
    ) -> tuple[DataAgentStageSnapshot, DataAgentStageSnapshot]:
        """Read draft and published stages from the same persisted definition."""
        definition = self.get_definition(item_id)
        return (
            decode_stage_snapshot(definition, "draft"),
            decode_stage_snapshot(definition, "published"),
        )

    @staticmethod
    def _definition_from_body(
        body: dict[str, Any],
    ) -> dict[str, Any] | None:
        definition = body.get("definition")
        if isinstance(definition, dict):
            return definition
        result = body.get("result")
        if isinstance(result, dict):
            definition = result.get("definition")
            if isinstance(definition, dict):
                return definition
        return None

    # ------------------------------------------------------------------
    # LRO polling
    # ------------------------------------------------------------------

    def _poll_lro(
        self,
        operation_url: str,
        retry_after: int,
    ) -> dict[str, Any]:
        """Poll *operation_url* until the LRO completes or times out.

        Parameters
        ----------
        operation_url : str
            The operation status URL from the ``Location`` / ``x-ms-operation-id``
            header.
        retry_after : int
            Initial poll interval in seconds (from ``Retry-After`` header;
            falls back to :attr:`_lro_poll`).

        Returns
        -------
        dict
            The completed operation result body.

        Raises
        ------
        LROTimeoutError
            If the LRO does not complete within :attr:`_lro_timeout` seconds.
        HttpError
            If a poll request returns an error status.
        """
        start = time.monotonic()
        interval = max(1, retry_after)

        while True:
            elapsed = time.monotonic() - start
            if elapsed > self._lro_timeout:
                raise LROTimeoutError(operation_url, elapsed)

            logger.debug(
                "[data_agent] LRO poll %s (elapsed %.1fs)", operation_url, elapsed
            )
            time.sleep(interval)

            resp = self._transport.send(
                HttpRequest(
                    method="GET",
                    url=operation_url,
                    headers=self._auth_headers(),
                )
            )
            if resp.status_code >= 400:
                raise HttpError(resp.status_code, resp.body)

            body = resp.body if isinstance(resp.body, dict) else {}
            status_str = body.get("status", "").lower()

            # Retry-After overrides the default poll interval
            ra = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
            if ra:
                try:
                    interval = int(ra)
                except ValueError:
                    pass

            if status_str in ("succeeded", "completed", ""):
                # Empty status or explicit success → done
                if status_str in ("succeeded", "completed") or resp.status_code == 200:
                    logger.info("[data_agent] LRO completed: %s", operation_url)
                    return body
            elif status_str in ("failed", "canceled", "cancelled"):
                raise DataAgentLroFailedError(
                    operation_url=operation_url,
                    status_code=resp.status_code,
                    body=body,
                    response_headers=dict(resp.headers),
                    elapsed_seconds=elapsed,
                )
            # else: still running — loop

    # ------------------------------------------------------------------
    # Create / update
    # ------------------------------------------------------------------

    def _create(self, spec: DataAgentSpec) -> DataAgentUpsertResult:
        """POST /workspaces/{ws}/dataAgents to create a new item."""
        parts = build_definition_parts(spec)
        payload: dict[str, Any] = {
            "displayName": spec.display_name,
            "definition": {"parts": parts},
        }
        url = f"{self._base_url()}/dataAgents"
        max_retries = 3
        for attempt in range(max_retries):
            resp = self._transport.send(
                HttpRequest(method="POST", url=url, headers=self._auth_headers(), body=payload)
            )
            if resp.status_code == 429:
                ra_str = resp.headers.get("Retry-After") or resp.headers.get("retry-after", "")
                wait = int(ra_str) if ra_str else 30
                logger.warning(
                    "[data_agent] 429 rate-limit on create (attempt %d/%d), "
                    "retrying after %ds",
                    attempt + 1,
                    max_retries,
                    wait,
                )
                if attempt < max_retries - 1:
                    time.sleep(wait)
                    continue
                raise HttpError(429, resp.body)
            break

        if resp.status_code == 201:
            body = resp.body if isinstance(resp.body, dict) else {}
            item_id = str(body.get("id") or "")
            if not item_id:
                created_item = self.get_data_agent(spec.display_name)
                item_id = str(
                    created_item.get("id", "")
                    if isinstance(created_item, dict)
                    else ""
                )
            if not item_id:
                raise DataAgentDefinitionError(
                    "Data Agent create returned HTTP 201 without an item ID, "
                    f"and '{spec.display_name}' could not be resolved."
                )
            logger.info(
                "[data_agent] created '%s' (id=%s, 201 sync)", spec.display_name, item_id
            )
            return DataAgentUpsertResult(
                item_id=item_id,
                created=True,
                status="created-201",
                display_name=spec.display_name,
                note=f"Created DataAgent '{spec.display_name}' (201 sync).",
            )

        if resp.status_code == 202:
            location = resp.headers.get("Location") or resp.headers.get("location", "")
            if not location:
                raise DataAgentDefinitionError(
                    "Data Agent create returned 202 without a Location header."
                )
            ra_str = resp.headers.get("Retry-After") or resp.headers.get("retry-after", "")
            retry_after = int(ra_str) if ra_str else self._lro_poll
            logger.info(
                "[data_agent] creating '%s' (202 LRO, location=%s)",
                spec.display_name,
                location,
            )
            try:
                lro_result = self._poll_lro(location, retry_after)
            except DataAgentLroFailedError as exc:
                try:
                    shell = self.get_data_agent(spec.display_name)
                    shell_id = str((shell or {}).get("id") or "")
                    if shell_id:
                        self._delete(shell_id)
                except (
                    DataAgentDefinitionError,
                    DataAgentLroFailedError,
                    DataAgentTargetError,
                    HttpError,
                    LROTimeoutError,
                ) as cleanup_exc:
                    raise DataAgentTargetError(
                        f"{exc} Cleanup of the failed create target also failed: "
                        f"{cleanup_exc}"
                    ) from exc
                raise
            nested_result = lro_result.get("result")
            item_id = str(
                lro_result.get("id")
                or lro_result.get("itemId")
                or (
                    nested_result.get("id")
                    if isinstance(nested_result, dict)
                    else ""
                )
                or (
                    nested_result.get("itemId")
                    if isinstance(nested_result, dict)
                    else ""
                )
                or ""
            )
            if not item_id:
                for attempt in range(3):
                    created_item = self.get_data_agent(spec.display_name)
                    item_id = str(
                        created_item.get("id", "")
                        if isinstance(created_item, dict)
                        else ""
                    )
                    if item_id:
                        break
                    if attempt < 2:
                        time.sleep(max(0, self._lro_poll))
            if not item_id:
                raise DataAgentDefinitionError(
                    "Data Agent create LRO succeeded but returned no item ID, "
                    f"and '{spec.display_name}' could not be resolved in the workspace."
                )
            return DataAgentUpsertResult(
                item_id=item_id,
                created=True,
                status="created-lro",
                display_name=spec.display_name,
                note=f"Created DataAgent '{spec.display_name}' via LRO.",
            )

        raise HttpError(resp.status_code, resp.body)

    def _update(
        self, spec: DataAgentSpec, item_id: str
    ) -> DataAgentUpsertResult:
        """POST the Data Agent-specific updateDefinition endpoint."""
        parts = build_definition_parts(spec)
        payload: dict[str, Any] = {"definition": {"parts": parts}}
        url = (
            f"{self._base_url()}/dataAgents/{item_id}/updateDefinition"
        )
        max_retries = 3
        for attempt in range(max_retries):
            resp = self._transport.send(
                HttpRequest(method="POST", url=url, headers=self._auth_headers(), body=payload)
            )
            if resp.status_code == 429:
                ra_str = resp.headers.get("Retry-After") or resp.headers.get("retry-after", "")
                wait = int(ra_str) if ra_str else 30
                logger.warning(
                    "[data_agent] 429 rate-limit on updateDefinition (attempt %d/%d), "
                    "retrying after %ds",
                    attempt + 1,
                    max_retries,
                    wait,
                )
                if attempt < max_retries - 1:
                    time.sleep(wait)
                    continue
                raise HttpError(429, resp.body)
            break

        if resp.status_code >= 400:
            raise HttpError(resp.status_code, resp.body)

        if resp.status_code == 202:
            location = resp.headers.get("Location") or resp.headers.get("location", "")
            if not location:
                raise DataAgentDefinitionError(
                    "Data Agent updateDefinition returned 202 without a Location header."
                )
            ra_str = resp.headers.get("Retry-After") or resp.headers.get("retry-after", "")
            retry_after = int(ra_str) if ra_str else self._lro_poll
            self._poll_lro(location, retry_after)

        logger.info(
            "[data_agent] updated '%s' (id=%s)", spec.display_name, item_id
        )
        return DataAgentUpsertResult(
            item_id=item_id,
            created=False,
            status="updated",
            display_name=spec.display_name,
            note=f"Updated DataAgent '{spec.display_name}' (id={item_id}).",
        )

    def _delete(self, item_id: str) -> None:
        """Delete one exact Data Agent target before an approved replacement."""
        url = f"{self._base_url()}/dataAgents/{item_id}"
        resp = self._transport.send(
            HttpRequest(
                method="DELETE",
                url=url,
                headers=self._auth_headers(),
            )
        )
        if resp.status_code == 404:
            return
        if resp.status_code in {200, 204}:
            return
        if resp.status_code == 202:
            location = (
                resp.headers.get("Location")
                or resp.headers.get("location")
                or ""
            )
            if not location:
                raise DataAgentTargetError(
                    "Data Agent delete returned 202 without Location."
                )
            retry_after_text = (
                resp.headers.get("Retry-After")
                or resp.headers.get("retry-after")
                or str(self._lro_poll)
            )
            try:
                retry_after = int(retry_after_text)
            except ValueError:
                retry_after = self._lro_poll
            self._poll_lro(location, retry_after)
            return
        raise HttpError(
            resp.status_code,
            resp.body,
            response_headers=resp.headers,
        )

    def delete_data_agent(self, item_id: str) -> None:
        """Delete an exact Data Agent item, including any delete LRO."""
        self._delete(item_id)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def deploy_target(
        self,
        spec: DataAgentSpec,
        *,
        target_mode: Literal["update", "create", "replace"],
        configured_item_id: str | None = None,
        replace_approved: bool = False,
    ) -> DataAgentUpsertResult:
        """Honor one explicit Data Agent target mode without name fallback."""
        item_id = str(configured_item_id or "").strip()
        if target_mode == "create":
            if item_id:
                raise DataAgentTargetError(
                    "Create mode cannot be used with a configured item ID."
                )
            existing = self.get_data_agent(spec.display_name)
            if existing is not None:
                raise DataAgentTargetError(
                    f"Data Agent '{spec.display_name}' already exists; choose "
                    "update with its exact item ID or approved replace."
                )
            result = self._create(spec)
        elif target_mode in {"update", "replace"}:
            if not item_id:
                raise DataAgentTargetError(
                    f"{target_mode} mode requires an exact configured item ID."
                )
            existing = self.get_data_agent_by_id(item_id)
            if existing is None:
                raise DataAgentTargetError(
                    f"Configured Data Agent item '{item_id}' does not exist "
                    "or is not a DataAgent."
                )
            existing_name = str(existing.get("displayName") or "")
            if existing_name != spec.display_name:
                raise DataAgentTargetError(
                    f"Configured Data Agent '{item_id}' is named "
                    f"'{existing_name}', not '{spec.display_name}'."
                )
            if target_mode == "update":
                result = self._update(spec, item_id)
            else:
                if not replace_approved:
                    raise DataAgentTargetError(
                        "Replace mode requires explicit replacement approval."
                    )
                self._delete(item_id)
                created = self._create(spec)
                if created.item_id == item_id:
                    raise DataAgentTargetError(
                        "Approved replacement reused the deleted item ID; "
                        "Fabric did not create a distinct target."
                    )
                result = DataAgentUpsertResult(
                    item_id=created.item_id,
                    created=True,
                    status="replaced",
                    display_name=created.display_name,
                    note=(
                        f"Replaced DataAgent '{spec.display_name}' "
                        f"({item_id} -> {created.item_id})."
                    ),
                )
        else:
            raise DataAgentTargetError(
                f"Unsupported Data Agent target mode: {target_mode!r}."
            )
        _lin.record(
            operation="fabric_data_agent",
            action=target_mode,
            api_version=_FABRIC_API_VERSION,
            capability_mode="ga",
            resource_name=spec.display_name,
            status=result.status,
            endpoint=self._ws,
            remote_id=result.item_id,
        )
        return result

    def upsert(self, spec: DataAgentSpec) -> DataAgentUpsertResult:
        """Idempotently create or update a Fabric Data Agent item.

        1. Lists workspace items to detect an existing DataAgent with the same
           ``displayName``.
        2. If absent: POST to create (handles 201 sync and 202 LRO).
        3. If present: POST to ``updateDefinition`` (handles 200 and 202 LRO).

        Parameters
        ----------
        spec : DataAgentSpec
            Desired state for the agent.

        Returns
        -------
        DataAgentUpsertResult
            Contains the item GUID and whether the item was newly created.

        Raises
        ------
        SourceCapError
            If ``spec.sources`` exceeds five items.
        UnsupportedDataSourceType
            If a source type is unrecognised and not flagged preview.
        HttpError
            On non-success HTTP responses.
        LROTimeoutError
            If an LRO does not complete within *lro_timeout_seconds*.
        """
        existing = self.get_data_agent(spec.display_name)
        if existing is None:
            result = self._create(spec)
        else:
            result = self._update(spec, existing.get("id", ""))
        _lin.record(
            operation="fabric_data_agent",
            action="upsert",
            api_version=_FABRIC_API_VERSION,
            capability_mode="ga",
            resource_name=spec.display_name,
            status=result.status,
            endpoint=self._ws,
            remote_id=result.item_id or None,
        )
        return result

    def publish(
        self,
        item_id: str,
        *,
        description: str,
    ) -> DataAgentPublishResult:
        """Promote the current staging configuration to the live MCP agent."""
        if not item_id:
            raise ValueError("Data Agent publish requires an item ID.")
        published_description = description.strip()
        if not published_description:
            raise ValueError(
                "Data Agent publish requires a non-empty description."
            )
        url = (
            f"{self._base_url()}/dataAgents/{item_id}/staging/publish"
        )
        max_retries = 3
        for attempt in range(max_retries):
            resp = self._transport.send(
                HttpRequest(
                    method="POST",
                    url=url,
                    headers=self._auth_headers(),
                    body={
                        "publishedDescription": published_description,
                    },
                )
            )
            if resp.status_code != 429:
                break
            retry_after_text = (
                resp.headers.get("Retry-After")
                or resp.headers.get("retry-after")
                or "30"
            )
            try:
                retry_after = max(0, int(retry_after_text))
            except ValueError:
                retry_after = 30
            if attempt == max_retries - 1:
                raise HttpError(
                    resp.status_code,
                    resp.body,
                    response_headers=resp.headers,
                )
            time.sleep(retry_after)
        if resp.status_code >= 400:
            raise HttpError(
                resp.status_code,
                resp.body,
                response_headers=resp.headers,
            )
        if resp.status_code != 200:
            raise HttpError(
                resp.status_code,
                (
                    "Data Agent publish expected HTTP 200 but received "
                    f"{resp.status_code}."
                ),
                response_headers=resp.headers,
            )
        body = resp.body if isinstance(resp.body, dict) else {}
        observed_description = str(
            body.get("publishedDescription")
            or published_description
        )
        _lin.record(
            operation="fabric_data_agent",
            action="publish",
            api_version=_FABRIC_API_VERSION,
            capability_mode="preview",
            resource_name=item_id,
            status="published",
            endpoint=self._ws,
            remote_id=item_id,
        )
        return DataAgentPublishResult(
            item_id=item_id,
            published_description=observed_description,
        )
