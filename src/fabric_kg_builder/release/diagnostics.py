"""Local-only, privacy-safe Fabric Data Agent diagnostic export inspector.

Implements SPEC-008A §10.4 and §11.2 (S8A-DIA-001, S8A-DIA-002, S8A-DIA-003,
VAL-087).

Scope
-----
This module never uploads, transmits, or persists raw diagnostic content.
It reads local JSON (or NDJSON) diagnostic export files, tolerates unknown
or varying shapes ("generic Fabric Data Agent diagnostic JSON export"
rather than one fixed structure), and produces a redacted aggregate
report:

- every raw identifier (workspace, target item, request, correlation,
  thread, run, and operation IDs, plus evidence IDs) is replaced by a
  deterministic one-way fingerprint before it can appear in any output;
- every unclassified or free-form string value (a potential question,
  answer, prompt, entity value, document name, URL, path, or token) is
  fingerprinted or dropped -- it is never copied verbatim;
- already-hashed envelope fields (manifest/plan/query/projection hashes)
  pass through unchanged because they are one-way digests of compiled
  artifacts, not customer content;
- the default report is aggregate-only; per-record detail is opt-in and
  remains fully redacted even then;
- a final redaction canary (:func:`assert_report_is_redacted`) scans the
  assembled report and refuses to return it if any value is not a
  recognised safe shape (fingerprint, hash, timestamp, or known token).

See :mod:`fabric_kg_builder.cli.diagnostics_cmd` for the ``inspect-diagnostics``
CLI command built on this module.

This module reuses (read-only) the existing runtime diagnostic contracts in
:mod:`fabric_kg_builder.semantic` -- ``PartialDiagnosticExport``,
``QueryExecutionStatus``, and ``validate_diagnostic_record`` -- so that a
sealed/normalized per-record shape stays compatible with the persisted
diagnostic schema where practical, without duplicating its invariants.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence, get_args

from pydantic import ValidationError

from fabric_kg_builder.semantic import (
    SEMANTIC_SCHEMAS_VERSION,
    PartialDiagnosticExport,
    QueryExecutionStatus,
    validate_diagnostic_record,
)

REPORT_SCHEMA_VERSION = "1.0"


class DiagnosticsInspectionError(Exception):
    """Raised for invalid local input: missing file, unparseable JSON/NDJSON,
    or a file that yields zero diagnostic records."""


class DiagnosticsPrivacyViolation(Exception):
    """Fail-closed safety net: raised when the redaction canary finds a value
    that is not a fingerprint, hash, timestamp, or known safe token.

    This should never fire in normal operation -- every value is redacted
    before it reaches the report.  If it does fire the report is not
    returned or written.
    """


# ---------------------------------------------------------------------------
# Fingerprinting -- deterministic, one-way, never reversible.
# ---------------------------------------------------------------------------

_FP_PREFIX = "fp:"
_FP_LENGTH = 16
_FP_RE = re.compile(rf"^{_FP_PREFIX}[0-9a-f]{{{_FP_LENGTH}}}$")


def fingerprint(value: Any, *, length: int = _FP_LENGTH) -> str:
    """Return a deterministic one-way fingerprint for *value*.

    Never reversible.  Equal inputs always produce equal fingerprints
    (needed to deterministically detect exact duplicates and overlaps);
    the original value cannot be recovered from the fingerprint.
    """
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{_FP_PREFIX}{digest[:length]}"


def _is_fingerprint(value: Any) -> bool:
    return isinstance(value, str) and bool(_FP_RE.fullmatch(value))


_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_DOMAIN_HASH_RE = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and bool(_HASH_RE.fullmatch(value))


def _normalize_timestamp(value: Any) -> str | None:
    """Return a canonical UTC timestamp only when the entire value is valid."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _looks_like_timestamp(value: Any) -> bool:
    return _normalize_timestamp(value) is not None


# ---------------------------------------------------------------------------
# Generic key normalization and alias tables (shape-agnostic parsing)
# ---------------------------------------------------------------------------

_SEP_RE = re.compile(r"[^a-z0-9]")


def _normalize_key(key: str) -> str:
    """Fold camelCase / PascalCase / snake_case / kebab-case to one form."""
    return _SEP_RE.sub("", key.lower())


def _aliases(*names: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_normalize_key(n) for n in names))


# Canonical field name -> accepted raw-key spellings (normalized at lookup
# time), covering common Fabric/Azure telemetry vocabulary variants. This
# table intentionally does not assume one exact export shape.
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "schema_mode": _aliases("schema_mode", "schemaMode"),
    "export_freshness_watermark": _aliases(
        "export_freshness_watermark", "exportedAt", "exportTimestamp",
        "snapshotTimestamp", "generatedAt", "asOf", "watermark",
    ),
    "partial_snapshot": _aliases("partial_snapshot", "isPartial", "partial"),
    "overlapping_snapshot": _aliases(
        "overlapping_snapshot", "isOverlapping", "overlapping"
    ),
    "workspace_id": _aliases("workspace_id", "workspaceId", "workspaceGuid"),
    "target_item_id": _aliases(
        "target_item_id", "targetId", "itemId", "agentId", "dataAgentId"
    ),
    "semantic_contract_hash": _aliases("semantic_contract_hash", "contractHash"),
    "domain_contract_hash": _aliases(
        "domain_contract_hash", "domainContractHash"
    ),
    "query_authority_hash": _aliases(
        "query_authority_hash", "queryAuthorityHash"
    ),
    "manifest_hash": _aliases(
        "manifest_hash", "modelManifestHash", "semanticModelHash"
    ),
    "ontology_projection_hash": _aliases("ontology_projection_hash", "ontologyHash"),
    "graph_projection_hash": _aliases("graph_projection_hash", "graphHash"),
    "search_projection_hash": _aliases("search_projection_hash", "searchHash"),
    "instruction_hash": _aliases("instruction_hash", "agentInstructionHash"),
    "source_selection_hash": _aliases("source_selection_hash", "sourceHash"),
    "query_schema_hash": _aliases(
        "query_schema_hash", "persistedQuerySchemaHash", "querySchemaHash"
    ),
    "route": _aliases("route", "queryRoute"),
    "actual_hop_count": _aliases(
        "actual_hop_count", "actualHopCount", "hopCount"
    ),
    "selected_source": _aliases("selected_source", "source", "dataSource"),
    "semantic_plan_hash": _aliases("semantic_plan_hash", "queryPlanHash", "planHash"),
    "physical_query_hash": _aliases(
        "physical_query_hash", "queryTextHash", "generatedQueryHash"
    ),
    "static_validation_passed": _aliases(
        "static_validation_passed", "staticValidation", "queryStaticValid"
    ),
    "query_row_count": _aliases("query_row_count", "rowCount", "resultRowCount"),
    "result_category": _aliases("result_category", "executionStatus", "resultStatus"),
    "error_category": _aliases("error_category", "errorType", "failureCategory"),
    "request_id": _aliases("request_id", "clientRequestId"),
    "correlation_id": _aliases("correlation_id", "operationCorrelationId"),
    "thread_id": _aliases("thread_id", "conversationId", "sessionId"),
    "run_id": _aliases("run_id", "executionId"),
    "operation_id": _aliases("operation_id", "activityId"),
    "latency_ms": _aliases("latency_ms", "durationMs", "elapsedMs", "latency"),
    "retry_count": _aliases("retry_count", "attemptCount", "retries"),
    "evidence_ids": _aliases("evidence_ids", "citationIds", "sourceLocatorIds"),
    "final_semantic_status": _aliases(
        "final_semantic_status", "finalStatus", "semanticStatus", "status"
    ),
}

_PLAN_ALIASES = _aliases(
    "semantic_plan", "queryPlan", "businessIntentPlan", "semanticQueryPlan"
)

_WRAPPER_ALIASES = frozenset(
    _normalize_key(n)
    for n in (
        "runs", "records", "diagnostics", "executions", "entries", "items",
        "data", "results", "exports", "snapshots", "diagnosticRuns",
    )
)

_HASH_FIELDS: frozenset[str] = frozenset({
    "semantic_contract_hash", "manifest_hash", "ontology_projection_hash",
    "graph_projection_hash", "search_projection_hash", "instruction_hash",
    "source_selection_hash", "query_schema_hash", "semantic_plan_hash",
    "physical_query_hash", "query_authority_hash",
})
_ID_FIELDS: frozenset[str] = frozenset({
    "workspace_id", "target_item_id", "request_id", "correlation_id",
    "thread_id", "run_id", "operation_id",
})
_IDENTITY_PRIORITY: tuple[str, ...] = (
    "run_id", "thread_id", "request_id", "operation_id", "correlation_id",
)

_STATUS_VALUES: frozenset[str] = frozenset(get_args(QueryExecutionStatus))
_FAILURE_STATUS_VALUES: frozenset[str] = frozenset({
    "invalid_semantic_plan", "invalid_physical_query", "authorization_failure",
    "platform_failure", "timeout", "concurrency_conflict",
})
_SOURCE_CATEGORY_KEYWORDS: tuple[str, ...] = (
    "graph", "search", "lakehouse", "warehouse", "ontology", "agent",
    "composed", "sql", "onelake",
)

# Required §10.4 envelope fields, grouped by SPEC-008A §11.2 gap category.
_CATEGORY_FIELDS: dict[str, tuple[str, ...]] = {
    "schema": (
        "semantic_contract_hash", "manifest_hash", "query_schema_hash",
    ),
    "planning": (
        "ontology_projection_hash", "graph_projection_hash",
        "search_projection_hash", "instruction_hash", "source_selection_hash",
        "selected_source", "semantic_plan",
    ),
    "query": (
        "physical_query_hash", "static_validation_passed", "query_row_count",
        "result_category", "error_category",
    ),
    "runtime": (
        "workspace_id", "target_item_id", "request_id", "correlation_id",
        "thread_id", "run_id", "operation_id", "retry_count",
        "final_semantic_status", "partial_snapshot", "overlapping_snapshot",
        "export_freshness_watermark",
    ),
    "evidence": ("evidence_ids",),
    "latency": ("latency_ms",),
}
_ALL_REQUIRED_FIELDS: tuple[str, ...] = tuple(
    f for fields in _CATEGORY_FIELDS.values() for f in fields
)

# QueryFinding.code (from semantic.validate_diagnostic_record) -> gap category.
# DIAGNOSTIC_FIELD_MISSING is intentionally excluded: this module computes its
# own per-category, per-field completeness instead of that aggregate code.
_FINDING_CATEGORY: dict[str, str] = {
    "DIAGNOSTIC_STATUS_MASKED": "runtime",
    "DIAGNOSTIC_CONFLICT_MASKED": "runtime",
    "DIAGNOSTIC_VALIDATION_MISSING": "query",
    "DIAGNOSTIC_SUCCESS_WITHOUT_ROWS": "query",
    "DIAGNOSTIC_PARTIAL_SUCCESS": "runtime",
    "DIAGNOSTIC_WATERMARK_MISSING": "runtime",
    "DIAGNOSTIC_WATERMARK_STALE": "runtime",
    "DIAGNOSTIC_WATERMARK_INVALID": "runtime",
    "DIAGNOSTIC_WATERMARK_FUTURE": "runtime",
    "DIAGNOSTIC_EVIDENCE_MISSING": "evidence",
    "DIAGNOSTIC_NEGATIVE_LATENCY": "latency",
    "DIAGNOSTIC_NEGATIVE_ROW_COUNT": "query",
}


# ---------------------------------------------------------------------------
# Local-only parsing (JSON or NDJSON, arbitrary wrapper shape)
# ---------------------------------------------------------------------------


def _parse_json_or_ndjson(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        raise DiagnosticsInspectionError("File is empty.")
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    records: list[Any] = []
    for line_no, line in enumerate(stripped.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise DiagnosticsInspectionError(
                f"Could not parse as JSON or NDJSON (line {line_no}): {exc}"
            ) from exc
    if not records:
        raise DiagnosticsInspectionError("No JSON records found in file.")
    return records


def _extract_raw_records(parsed: Any) -> list[dict]:
    """Return the list of per-run record dicts from an arbitrary parsed shape."""
    if isinstance(parsed, list):
        return [r for r in parsed if isinstance(r, dict)]
    if isinstance(parsed, dict):
        for key, value in parsed.items():
            if _normalize_key(str(key)) in _WRAPPER_ALIASES and isinstance(value, list):
                records = [r for r in value if isinstance(r, dict)]
                if records:
                    return records
        return [parsed]
    return []


def _find_leaf(
    record: dict, aliases: tuple[str, ...], *, max_depth: int = 4, max_nodes: int = 2000
) -> Any:
    """Breadth-first search for the first scalar leaf matching *aliases*."""
    alias_set = set(aliases)
    queue: list[tuple[Any, int]] = [(record, 0)]
    visited = 0
    while queue:
        node, depth = queue.pop(0)
        visited += 1
        if visited > max_nodes:
            break
        if isinstance(node, dict):
            for k, v in node.items():
                if _normalize_key(str(k)) in alias_set and not isinstance(v, (dict, list)):
                    return v
            if depth < max_depth:
                for v in node.values():
                    if isinstance(v, dict):
                        queue.append((v, depth + 1))
                    elif isinstance(v, list):
                        queue.extend((item, depth + 1) for item in v if isinstance(item, dict))
    return None


def _find_nested_object(
    record: dict, aliases: tuple[str, ...], *, max_depth: int = 4, max_nodes: int = 2000
) -> Any:
    """Breadth-first search for a non-empty dict/list matching *aliases* (presence only)."""
    alias_set = set(aliases)
    queue: list[tuple[Any, int]] = [(record, 0)]
    visited = 0
    while queue:
        node, depth = queue.pop(0)
        visited += 1
        if visited > max_nodes:
            break
        if isinstance(node, dict):
            for k, v in node.items():
                if _normalize_key(str(k)) in alias_set and isinstance(v, (dict, list)) and v:
                    return v
            if depth < max_depth:
                for v in node.values():
                    if isinstance(v, dict):
                        queue.append((v, depth + 1))
                    elif isinstance(v, list):
                        queue.extend((item, depth + 1) for item in v if isinstance(item, dict))
    return None


def _contains_alias(
    record: dict,
    aliases: tuple[str, ...],
    *,
    max_depth: int = 4,
    max_nodes: int = 2000,
) -> bool:
    """Return whether any matching key exists, including an empty value."""
    alias_set = set(aliases)
    queue: list[tuple[Any, int]] = [(record, 0)]
    visited = 0
    while queue:
        node, depth = queue.pop(0)
        visited += 1
        if visited > max_nodes:
            break
        if not isinstance(node, dict):
            continue
        if any(_normalize_key(str(key)) in alias_set for key in node):
            return True
        if depth < max_depth:
            for value in node.values():
                if isinstance(value, dict):
                    queue.append((value, depth + 1))
                elif isinstance(value, list):
                    queue.extend(
                        (item, depth + 1)
                        for item in value
                        if isinstance(item, dict)
                    )
    return False


def _extract_canonical(raw_record: dict) -> dict[str, Any]:
    """Map an arbitrary raw record onto canonical §10.4 field names (unredacted)."""
    out: dict[str, Any] = {}
    for canonical, aliases in _FIELD_ALIASES.items():
        if canonical == "evidence_ids":
            # evidence_ids is list-typed; _find_leaf only matches scalar
            # leaves, so use the list/dict-aware search and keep the list.
            value = _find_nested_object(raw_record, aliases)
        else:
            value = _find_leaf(raw_record, aliases)
        if value is not None:
            out[canonical] = value
    out["_evidence_ids_present"] = _contains_alias(
        raw_record,
        _FIELD_ALIASES["evidence_ids"],
    )
    out["semantic_plan"] = _find_nested_object(raw_record, _PLAN_ALIASES)
    return out


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _match_enum(value: Any, allowed: frozenset[str]) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = re.sub(r"[\s\-]+", "_", value.strip().lower())
    return normalized if normalized in allowed else None


def _match_source_category(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    lowered = value.lower()
    for keyword in _SOURCE_CATEGORY_KEYWORDS:
        if keyword in lowered:
            return keyword
    return None


# ---------------------------------------------------------------------------
# Redaction: canonical raw values -> safe, typed, never-raw representation
# ---------------------------------------------------------------------------


def redact_record(canonical: dict[str, Any]) -> dict[str, Any]:
    """Return a fully redacted, typed representation of one canonical record.

    Every entry is one of: a boolean, a number, an already one-way hash
    (``sha256:...``), a deterministic fingerprint (``fp:...``), a narrow
    ISO-8601-shaped timestamp, or a value matched against a small known-safe
    enum/category vocabulary.  No free-form string ever passes through
    unredacted.
    """
    redacted: dict[str, Any] = {}

    watermark = canonical.get("export_freshness_watermark")
    redacted["export_freshness_watermark"] = _normalize_timestamp(watermark)

    redacted["partial_snapshot"] = _coerce_bool(canonical.get("partial_snapshot"))
    redacted["overlapping_snapshot"] = _coerce_bool(canonical.get("overlapping_snapshot"))
    redacted["static_validation_passed"] = _coerce_bool(
        canonical.get("static_validation_passed")
    )

    for field_name in _HASH_FIELDS:
        value = canonical.get(field_name)
        redacted[field_name] = value if _is_hash(value) else None
    domain_hash = canonical.get("domain_contract_hash")
    redacted["domain_contract_hash"] = (
        domain_hash
        if isinstance(domain_hash, str)
        and _DOMAIN_HASH_RE.fullmatch(domain_hash)
        else None
    )
    redacted["schema_mode"] = _match_enum(
        canonical.get("schema_mode"),
        frozenset({"schema1_compatibility", "schema2_bounded"}),
    )
    route = canonical.get("route")
    redacted["route"] = (
        str(route)
        if isinstance(route, str)
        and route in {"direct_graph", "data_agent_mcp", "composed"}
        else None
    )

    for field_name in _ID_FIELDS:
        value = canonical.get(field_name)
        redacted[field_name] = (
            fingerprint(value) if isinstance(value, str) and value.strip() else None
        )

    evidence = canonical.get("evidence_ids")
    if isinstance(evidence, list) and evidence:
        redacted["evidence_ids"] = [
            fingerprint(e) for e in evidence if isinstance(e, (str, int, float))
        ]
    else:
        redacted["evidence_ids"] = []
    redacted["evidence_ids_present"] = bool(
        canonical.get("_evidence_ids_present", "evidence_ids" in canonical)
    )

    redacted["query_row_count"] = _coerce_int(canonical.get("query_row_count"))
    redacted["latency_ms"] = _coerce_float(canonical.get("latency_ms"))
    redacted["retry_count"] = _coerce_int(canonical.get("retry_count"))
    redacted["actual_hop_count"] = _coerce_int(
        canonical.get("actual_hop_count")
    )

    redacted["result_category"] = _match_enum(canonical.get("result_category"), _STATUS_VALUES)
    redacted["final_semantic_status"] = _match_enum(
        canonical.get("final_semantic_status"), _STATUS_VALUES
    )
    redacted["error_category"] = _match_enum(canonical.get("error_category"), _STATUS_VALUES)

    source = canonical.get("selected_source")
    redacted["selected_source_category"] = _match_source_category(source)
    redacted["selected_source_fingerprint"] = (
        fingerprint(source) if isinstance(source, str) and source.strip() else None
    )

    redacted["semantic_plan_present"] = bool(canonical.get("semantic_plan"))

    return redacted


def _field_present(field_name: str, redacted: dict[str, Any]) -> bool:
    """Return True when *field_name* counts as present for §10.4 completeness."""
    if (
        field_name == "instruction_hash"
        and redacted.get("schema_mode") == "schema2_bounded"
    ):
        return True
    if field_name == "error_category":
        result_cat = redacted.get("result_category")
        if result_cat is None or result_cat not in _FAILURE_STATUS_VALUES:
            return True  # not applicable when there is no source failure
        return redacted.get("error_category") is not None
    if field_name == "selected_source":
        return (
            redacted.get("selected_source_category") is not None
            or redacted.get("selected_source_fingerprint") is not None
        )
    if field_name == "semantic_plan":
        return (
            redacted.get("schema_mode") == "schema2_bounded"
            or bool(redacted.get("semantic_plan_present"))
        )
    if field_name == "evidence_ids":
        return redacted.get("evidence_ids_present") is True
    value = redacted.get(field_name)
    if isinstance(value, bool):
        return True
    return value is not None


def _content_fingerprint(redacted: dict[str, Any]) -> str:
    """Fingerprint of one record's redacted content (excludes computed flags)."""
    payload = {
        k: v for k, v in redacted.items()
        if k not in ("partial_snapshot", "overlapping_snapshot")
    }
    return fingerprint(payload)


def _identity_key(redacted: dict[str, Any]) -> str | None:
    for field_name in _IDENTITY_PRIORITY:
        value = redacted.get(field_name)
        if value:
            return value
    return None


# ---------------------------------------------------------------------------
# Per-record entry
# ---------------------------------------------------------------------------


@dataclass
class _RecordEntry:
    file_index: int
    record_index: int
    redacted: dict[str, Any]
    content_fingerprint: str
    raw_partial_flag: bool | None
    raw_overlap_flag: bool | None
    is_exact_duplicate: bool = False
    is_stale: bool | None = None
    findings: list[dict[str, str]] = field(default_factory=list)


def _build_partial_export(redacted: dict[str, Any]) -> PartialDiagnosticExport | None:
    """Best-effort construction of a PartialDiagnosticExport from *redacted*.

    All values in *redacted* are already safe (fingerprints, hashes, enums,
    numbers, booleans) so this construction never needs raw content.
    Returns None if construction unexpectedly fails (defensive only).
    """
    payload: dict[str, Any] = {
        "schema_mode": (
            redacted.get("schema_mode") or "schema1_compatibility"
        ),
        "export_freshness_watermark": redacted.get("export_freshness_watermark") or "",
        "partial_snapshot": bool(redacted.get("partial_snapshot")),
        "overlapping_snapshot": bool(redacted.get("overlapping_snapshot")),
        "workspace_id": redacted.get("workspace_id") or "",
        "target_item_id": redacted.get("target_item_id") or "",
        "manifest_hash": redacted.get("manifest_hash") or "",
        "semantic_contract_hash": redacted.get("semantic_contract_hash") or "",
        "domain_contract_hash": redacted.get("domain_contract_hash") or "",
        "query_authority_hash": redacted.get("query_authority_hash") or "",
        "ontology_projection_hash": redacted.get("ontology_projection_hash") or "",
        "graph_projection_hash": redacted.get("graph_projection_hash") or "",
        "search_projection_hash": redacted.get("search_projection_hash") or "",
        "instruction_hash": redacted.get("instruction_hash") or "",
        "source_selection_hash": redacted.get("source_selection_hash") or "",
        "query_schema_hash": redacted.get("query_schema_hash") or "",
        "route": redacted.get("route"),
        "semantic_plan_hash": redacted.get("semantic_plan_hash") or "",
        "actual_hop_count": redacted.get("actual_hop_count") or 0,
        "physical_query_hash": redacted.get("physical_query_hash") or "",
        "static_validation_passed": redacted.get("static_validation_passed"),
        "query_row_count": redacted.get("query_row_count"),
        "result_category": redacted.get("result_category"),
        "error_category": redacted.get("error_category"),
        "request_id": redacted.get("request_id"),
        "correlation_id": redacted.get("correlation_id"),
        "thread_id": redacted.get("thread_id"),
        "run_id": redacted.get("run_id"),
        "operation_id": redacted.get("operation_id"),
        "latency_ms": redacted.get("latency_ms"),
        "retry_count": redacted.get("retry_count") or 0,
        "evidence_ids": redacted.get("evidence_ids") or [],
        "final_semantic_status": redacted.get("final_semantic_status"),
    }
    try:
        return PartialDiagnosticExport(**payload)
    except ValidationError:
        return None


def _watermark_dt(redacted: dict[str, Any]) -> datetime | None:
    value = redacted.get("export_freshness_watermark")
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Batch analysis: duplicate / overlapping / partial / stale detection
# ---------------------------------------------------------------------------


def _analyze_snapshots(
    entries: list[_RecordEntry], *, max_age_hours: float, reference_time: datetime | None,
) -> dict[str, Any]:
    groups: dict[str, list[int]] = {}
    for idx, entry in enumerate(entries):
        key = _identity_key(entry.redacted)
        if key:
            groups.setdefault(key, []).append(idx)

    exact_duplicate_groups = 0
    overlapping_groups = 0
    for _key, idxs in groups.items():
        if len(idxs) < 2:
            continue
        fingerprints = {entries[i].content_fingerprint for i in idxs}
        if len(fingerprints) == 1:
            exact_duplicate_groups += 1
            for i in idxs:
                entries[i].is_exact_duplicate = True
        else:
            overlapping_groups += 1
            for i in idxs:
                entries[i].raw_overlap_flag = True

    watermarks = [w for w in (_watermark_dt(e.redacted) for e in entries) if w is not None]
    if reference_time is not None:
        reference = reference_time
    elif watermarks:
        reference = max(watermarks)
    else:
        reference = datetime.now(timezone.utc)

    stale_count = 0
    for entry in entries:
        wm = _watermark_dt(entry.redacted)
        if wm is None:
            entry.is_stale = None
            continue
        age_hours = (reference - wm).total_seconds() / 3600.0
        entry.is_stale = age_hours > max_age_hours
        if entry.is_stale:
            stale_count += 1

    partial_count = 0
    for entry in entries:
        other_fields = [
            f for f in _ALL_REQUIRED_FIELDS
            if f not in ("partial_snapshot", "overlapping_snapshot")
        ]
        present = sum(1 for f in other_fields if _field_present(f, entry.redacted))
        other_coverage = present / len(other_fields) if other_fields else 1.0
        computed_partial = entry.raw_partial_flag or (other_coverage < 1.0)
        entry.redacted["partial_snapshot"] = bool(computed_partial)
        # raw_overlap_flag already folds in the original raw-provided value
        # (captured at entry creation) and any group-detected overlap.
        entry.redacted["overlapping_snapshot"] = bool(entry.raw_overlap_flag)
        if entry.redacted["partial_snapshot"]:
            partial_count += 1

    return {
        "exact_duplicate_groups": exact_duplicate_groups,
        "overlapping_snapshot_groups": overlapping_groups,
        "partial_export_count": partial_count,
        "stale_export_count": stale_count,
        "unknown_freshness_count": sum(1 for e in entries if e.is_stale is None),
        "max_age_hours_threshold": max_age_hours,
        "reference_time": reference.isoformat(),
    }


# ---------------------------------------------------------------------------
# Completeness and gap aggregation
# ---------------------------------------------------------------------------


def _completeness_report(entries: list[_RecordEntry]) -> dict[str, Any]:
    by_category: dict[str, Any] = {}
    total_required = 0
    total_present = 0
    for category, fields_in_category in _CATEGORY_FIELDS.items():
        required = len(fields_in_category) * len(entries) if entries else 0
        present = 0
        missing_fields: set[str] = set()
        for entry in entries:
            for f in fields_in_category:
                if _field_present(f, entry.redacted):
                    present += 1
                else:
                    missing_fields.add(f)
        coverage = (present / required) if required else 1.0
        by_category[category] = {
            "required": required,
            "present": present,
            "coverage": coverage,
            "missing_fields": sorted(missing_fields),
        }
        total_required += required
        total_present += present

    overall_coverage = (total_present / total_required) if total_required else 1.0
    return {
        "overall_coverage": overall_coverage,
        "required_field_count": len(_ALL_REQUIRED_FIELDS),
        "by_category": by_category,
    }


def _gap_findings(entries: list[_RecordEntry]) -> list[dict[str, Any]]:
    counts: dict[tuple[str, str], int] = {}
    for entry in entries:
        for finding in entry.findings:
            code = finding["code"]
            category = _FINDING_CATEGORY.get(code, "runtime")
            counts[(category, code)] = counts.get((category, code), 0) + 1
    return [
        {"category": category, "code": code, "count": count}
        for (category, code), count in sorted(counts.items())
    ]


# ---------------------------------------------------------------------------
# Redaction canary -- final defense before returning/writing a report.
# ---------------------------------------------------------------------------

_SAFE_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_]{0,48}$")
_SAFE_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,48}$")
_SAFE_VERSION_RE = re.compile(r"^\d+(\.\d+){0,3}$")
_SAFE_REF_RE = re.compile(r"^file\d+_rec\d+$")


def _is_safe_report_string(value: str) -> bool:
    if value == "":
        return True
    if _is_fingerprint(value) or _is_hash(value):
        return True
    if _looks_like_timestamp(value):
        return True
    if _SAFE_TOKEN_RE.match(value) or _SAFE_CODE_RE.match(value):
        return True
    if _SAFE_VERSION_RE.match(value) or _SAFE_REF_RE.match(value):
        return True
    return False


def assert_report_is_redacted(report: Any, *, _path: str = "$") -> None:
    """Recursively assert every string in *report* is a known-safe shape.

    Raises :class:`DiagnosticsPrivacyViolation` on the first value that is
    not a fingerprint, hash, timestamp, or short identifier-like token.
    """
    if isinstance(report, str):
        if not _is_safe_report_string(report):
            raise DiagnosticsPrivacyViolation(
                f"Unredacted value detected at {_path}: {report!r}"
            )
        return
    if isinstance(report, dict):
        for key, value in report.items():
            assert_report_is_redacted(value, _path=f"{_path}.{key}")
        return
    if isinstance(report, list):
        for i, item in enumerate(report):
            assert_report_is_redacted(item, _path=f"{_path}[{i}]")
        return
    # bool / int / float / None are always safe.


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def inspect_files(
    paths: Sequence[Path],
    *,
    max_age_hours: float = 24.0,
    reference_time: str | datetime | None = None,
    detail: bool = False,
) -> dict[str, Any]:
    """Inspect local diagnostic export files and return a redacted report.

    Raises ``DiagnosticsInspectionError`` for missing/unreadable/unparseable
    files or when zero diagnostic records are found across all inputs.
    Raises ``DiagnosticsPrivacyViolation`` if the final redaction canary
    detects an unredacted value (fail-closed; the report is not returned).
    """
    if not paths:
        raise DiagnosticsInspectionError("No input files provided.")

    if isinstance(reference_time, str):
        try:
            reference_dt = datetime.fromisoformat(reference_time.replace("Z", "+00:00"))
        except ValueError as exc:
            raise DiagnosticsInspectionError(
                f"--reference-time is not valid ISO 8601: {exc}"
            ) from exc
        if reference_dt.tzinfo is None:
            reference_dt = reference_dt.replace(tzinfo=timezone.utc)
        reference_dt = reference_dt.astimezone(timezone.utc)
    else:
        reference_dt = reference_time
        if reference_dt is not None:
            if reference_dt.tzinfo is None:
                reference_dt = reference_dt.replace(tzinfo=timezone.utc)
            reference_dt = reference_dt.astimezone(timezone.utc)

    file_summaries: list[dict[str, Any]] = []
    entries: list[_RecordEntry] = []

    for file_index, path in enumerate(paths):
        if not path.is_file():
            raise DiagnosticsInspectionError(f"File not found: {path}")
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except OSError as exc:
            raise DiagnosticsInspectionError(f"Cannot read file {path}: {exc}") from exc

        parsed = _parse_json_or_ndjson(text)
        raw_records = _extract_raw_records(parsed)
        if not raw_records:
            raise DiagnosticsInspectionError(
                f"No diagnostic records found in file: {path}"
            )

        for record_index, raw_record in enumerate(raw_records):
            canonical = _extract_canonical(raw_record)
            redacted = redact_record(canonical)
            entries.append(_RecordEntry(
                file_index=file_index,
                record_index=record_index,
                redacted=redacted,
                content_fingerprint=_content_fingerprint(redacted),
                raw_partial_flag=redacted.get("partial_snapshot"),
                raw_overlap_flag=redacted.get("overlapping_snapshot"),
            ))

        file_summaries.append({
            "file_index": file_index,
            "file_fingerprint": fingerprint(text),
            "record_count": len(raw_records),
        })

    if not entries:
        raise DiagnosticsInspectionError("No diagnostic records found in any input file.")

    snapshot_analysis = _analyze_snapshots(
        entries, max_age_hours=max_age_hours, reference_time=reference_dt
    )

    # Best-effort masking/staleness findings via the existing runtime validator.
    # Reuse the same reference timestamp already resolved for snapshot
    # analysis so both layers agree on "now" for freshness checks.
    reference_watermark = snapshot_analysis["reference_time"]
    for entry in entries:
        partial_export = _build_partial_export(entry.redacted)
        if partial_export is None:
            continue
        findings = validate_diagnostic_record(
            partial_export,
            reference_watermark=reference_watermark,
            max_age_hours=max_age_hours,
        )
        entry.findings = [
            {"code": f.code, "message": f.message}
            for f in findings
            if f.code != "DIAGNOSTIC_FIELD_MISSING"
        ]

    completeness = _completeness_report(entries)
    gaps = _gap_findings(entries)

    has_gaps = bool(gaps)
    has_snapshot_issues = (
        snapshot_analysis["exact_duplicate_groups"] > 0
        or snapshot_analysis["overlapping_snapshot_groups"] > 0
        or snapshot_analysis["partial_export_count"] > 0
        or snapshot_analysis["stale_export_count"] > 0
    )
    status = "incomplete" if (
        completeness["overall_coverage"] < 1.0 or has_gaps or has_snapshot_issues
    ) else "complete"

    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "semantic_schemas_version": SEMANTIC_SCHEMAS_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "file_count": len(file_summaries),
            "record_count": len(entries),
            "files": file_summaries,
        },
        "completeness": completeness,
        "gaps": gaps,
        "snapshot_analysis": snapshot_analysis,
        "status": status,
    }

    if detail:
        # Only structured finding codes are surfaced, never free-text
        # messages: codes are the redacted, classifiable unit of a gap.
        report["records"] = [
            {
                "ref": f"file{e.file_index}_rec{e.record_index}",
                "is_exact_duplicate": e.is_exact_duplicate,
                "is_stale": e.is_stale,
                "finding_codes": sorted({f["code"] for f in e.findings}),
                **e.redacted,
            }
            for e in entries
        ]

    assert_report_is_redacted(report)
    return report
