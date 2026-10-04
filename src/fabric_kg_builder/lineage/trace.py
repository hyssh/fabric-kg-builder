"""Lineage v2 trace — forward/backward cycle-safe graph traversal.

Implements ``trace_record`` which walks the FK graph across all canonical
tables in either direction.  Results are fully typed; missing-ID diagnostics
are explicit — no silent fallback.

Edge catalogue
--------------
Backward edges encode: (source_table, fk_field, target_table, target_pk)
Forward edges are the inverse and are computed dynamically.

Terminal nodes (backward traversal terminus):
    assets, processing_runs

Common lineage envelope edges (all CommonLineageRow-derived tables):
    asset_id → assets.asset_id
    asset_version_id → asset_versions.asset_version_id
    run_id → processing_runs.run_id
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from fabric_kg_builder.lineage.common import TABLE_ID_FIELDS


# ---------------------------------------------------------------------------
# Edge catalogue
# ---------------------------------------------------------------------------

# (from_table, fk_field, to_table, to_pk_field)
# Backward edges encode the FK relationships in the canonical schema.
_BACKWARD_EDGES: list[tuple[str, str, str, str]] = [
    # ── Domain/content FK edges ──────────────────────────────────────────────
    ("document_elements", "source_file_id", "source_files", "source_file_id"),
    ("chunks", "source_file_id", "source_files", "source_file_id"),
    ("chunks", "document_element_id", "document_elements", "document_element_id"),
    ("evidence", "source_file_id", "source_files", "source_file_id"),
    ("evidence", "document_element_id", "document_elements", "document_element_id"),
    ("evidence", "chunk_id", "chunks", "chunk_id"),
    ("evidence", "visual_region_id", "visual_regions", "visual_region_id"),
    ("visual_assets", "source_file_id", "source_files", "source_file_id"),
    ("visual_assets", "document_element_id", "document_elements", "document_element_id"),
    ("visual_regions", "image_id", "visual_assets", "image_id"),
    ("relationships", "source_entity_id", "entities", "entity_id"),
    ("relationships", "target_entity_id", "entities", "entity_id"),
    ("relationships", "evidence_id", "evidence", "evidence_id"),
    ("claims", "subject_entity_id", "entities", "entity_id"),
    ("claims", "object_entity_id", "entities", "entity_id"),
    ("claim_evidence", "claim_id", "claims", "claim_id"),
    ("claim_evidence", "evidence_id", "evidence", "evidence_id"),
    ("cluster_memberships", "cluster_id", "clusters", "cluster_id"),
    ("cluster_memberships", "entity_id", "entities", "entity_id"),
    ("cluster_memberships", "relationship_id", "relationships", "relationship_id"),
    ("cluster_memberships", "claim_id", "claims", "claim_id"),
    ("clusters", "parent_cluster_id", "clusters", "cluster_id"),  # self-ref
    ("deployments", "run_id", "processing_runs", "run_id"),
    ("asset_versions", "asset_id", "assets", "asset_id"),
    ("processing_runs", "parent_run_id", "processing_runs", "run_id"),  # self-ref
    # ── v2 common lineage envelope edges (all CommonLineageRow tables) ────────
    ("source_files", "asset_id", "assets", "asset_id"),
    ("source_files", "asset_version_id", "asset_versions", "asset_version_id"),
    ("source_files", "run_id", "processing_runs", "run_id"),
    ("document_elements", "asset_id", "assets", "asset_id"),
    ("document_elements", "asset_version_id", "asset_versions", "asset_version_id"),
    ("document_elements", "run_id", "processing_runs", "run_id"),
    ("chunks", "asset_id", "assets", "asset_id"),
    ("chunks", "asset_version_id", "asset_versions", "asset_version_id"),
    ("chunks", "run_id", "processing_runs", "run_id"),
    ("entities", "asset_id", "assets", "asset_id"),
    ("entities", "asset_version_id", "asset_versions", "asset_version_id"),
    ("entities", "run_id", "processing_runs", "run_id"),
    ("relationships", "asset_id", "assets", "asset_id"),
    ("relationships", "asset_version_id", "asset_versions", "asset_version_id"),
    ("relationships", "run_id", "processing_runs", "run_id"),
    ("evidence", "asset_id", "assets", "asset_id"),
    ("evidence", "asset_version_id", "asset_versions", "asset_version_id"),
    ("evidence", "run_id", "processing_runs", "run_id"),
    ("visual_assets", "asset_id", "assets", "asset_id"),
    ("visual_assets", "asset_version_id", "asset_versions", "asset_version_id"),
    ("visual_assets", "run_id", "processing_runs", "run_id"),
    ("visual_regions", "asset_id", "assets", "asset_id"),
    ("visual_regions", "asset_version_id", "asset_versions", "asset_version_id"),
    ("visual_regions", "run_id", "processing_runs", "run_id"),
    ("claims", "asset_id", "assets", "asset_id"),
    ("claims", "asset_version_id", "asset_versions", "asset_version_id"),
    ("claims", "run_id", "processing_runs", "run_id"),
    ("clusters", "asset_id", "assets", "asset_id"),
    ("clusters", "asset_version_id", "asset_versions", "asset_version_id"),
    ("clusters", "run_id", "processing_runs", "run_id"),
]

# Tables that always terminate backward traversal. A processing run terminates
# only when it has no parent run.
_TERMINAL_TABLES: frozenset[str] = frozenset({"assets", "asset_versions"})

# Build forward edges (inverse of backward) grouped by target table.
# forward_edges[target_table] = list of (source_table, fk_field, target_pk)
_FORWARD_EDGES: dict[str, list[tuple[str, str, str]]] = {}
for _from_t, _fk, _to_t, _to_pk in _BACKWARD_EDGES:
    _FORWARD_EDGES.setdefault(_to_t, []).append((_from_t, _fk, _to_pk))

# Build backward edges grouped by source table.
# backward_edges[from_table] = list of (fk_field, to_table, to_pk)
_TABLE_BACKWARD_EDGES: dict[str, list[tuple[str, str, str]]] = {}
for _from_t, _fk, _to_t, _to_pk in _BACKWARD_EDGES:
    _TABLE_BACKWARD_EDGES.setdefault(_from_t, []).append((_fk, _to_t, _to_pk))


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TraceStep:
    """One hop in the lineage path."""

    table: str
    record_id: str
    row: dict[str, Any]
    via_field: str | None = None  # FK field used to arrive here


@dataclass(frozen=True)
class BrokenEdge:
    """Diagnostic: a FK field pointed to a non-existent record."""

    from_table: str
    from_record_id: str
    fk_field: str
    expected_table: str
    missing_id: str

    def __str__(self) -> str:
        return (
            f"{self.from_table}[{self.from_record_id}].{self.fk_field}"
            f" → {self.expected_table}[{self.missing_id}] NOT FOUND"
        )


@dataclass
class TraceResult:
    """Complete result of a lineage trace traversal."""

    record_id: str
    direction: str  # "backward" | "forward"
    path: list[tuple[str, str]] = field(default_factory=list)
    is_complete: bool = True
    broken_edge: tuple[str, str] | None = None
    table_name: str | None = None
    cycle_detected: bool = False
    _broken_edge_detail: BrokenEdge | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    @property
    def start_record_id(self) -> str:
        """Deprecated alias retained for callers using the original M2 draft."""
        return self.record_id

    @property
    def start_table(self) -> str | None:
        """Deprecated alias retained for callers using the original M2 draft."""
        return self.table_name

    @property
    def complete(self) -> bool:
        """Deprecated alias retained for callers using the original M2 draft."""
        return self.is_complete

    @property
    def broken_edges(self) -> list[BrokenEdge]:
        """Deprecated list view of the canonical first broken edge."""
        return [self._broken_edge_detail] if self._broken_edge_detail else []

    def as_dict(self) -> dict[str, Any]:
        """Serialisable summary without raw row content."""
        return {
            "record_id": self.record_id,
            "table_name": self.table_name,
            "direction": self.direction,
            "is_complete": self.is_complete,
            "cycle_detected": self.cycle_detected,
            "path_length": len(self.path),
            "path": [
                {
                    "table": table,
                    "record_id": record_id,
                }
                for table, record_id in self.path
            ],
            "broken_edge": (
                {
                    "table": self.broken_edge[0],
                    "record_id": self.broken_edge[1],
                    "detail": str(self._broken_edge_detail)
                    if self._broken_edge_detail
                    else None,
                }
                if self.broken_edge
                else None
            ),
        }

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Index helpers
# ---------------------------------------------------------------------------


def _build_pk_index(
    tables: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, dict[str, Any]]]:
    """Build a {table_name: {pk_value: row}} index for O(1) lookup."""
    index: dict[str, dict[str, dict[str, Any]]] = {}
    for table_name, rows in tables.items():
        pk_field = TABLE_ID_FIELDS.get(table_name)
        if pk_field is None:
            continue
        tbl_idx: dict[str, dict[str, Any]] = {}
        for row in rows:
            pk_value = row.get(pk_field)
            if pk_value is not None:
                tbl_idx[str(pk_value)] = row
        index[table_name] = tbl_idx
    return index


def _build_fk_inverted_index(
    tables: dict[str, list[dict[str, Any]]],
    target_table: str,
) -> dict[str, list[tuple[str, str, dict[str, Any]]]]:
    """Build an inverted index for forward traversal from *target_table*.

    Returns: {target_pk_value: [(source_table, fk_field, source_row), ...]}
    """
    inv: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
    for src_table, fk_field, _tgt_pk in _FORWARD_EDGES.get(target_table, []):
        for row in tables.get(src_table, []):
            fk_value = row.get(fk_field)
            if fk_value is not None:
                inv.setdefault(str(fk_value), []).append((src_table, fk_field, row))
    return inv


def _find_record_tables(
    record_id: str,
    pk_index: dict[str, dict[str, dict[str, Any]]],
) -> list[str]:
    """Return canonical tables containing *record_id* in deterministic order."""
    return sorted(
        table_name
        for table_name, rows_by_id in pk_index.items()
        if record_id in rows_by_id
    )


def _set_broken_edge(result: TraceResult, edge: BrokenEdge) -> None:
    """Record only the first broken edge, as required by the public contract."""
    if result.broken_edge is not None:
        return
    result.broken_edge = (edge.expected_table, edge.missing_id)
    result._broken_edge_detail = edge
    result.is_complete = False


def _is_backward_terminal(table_name: str, row: dict[str, Any]) -> bool:
    return table_name in _TERMINAL_TABLES or (
        table_name == "processing_runs" and not row.get("parent_run_id")
    )


_BACKWARD_FIELD_PRIORITY: dict[str, int] = {
    "evidence_id": 10,
    "chunk_id": 20,
    "document_element_id": 30,
    "source_file_id": 40,
    "visual_region_id": 50,
    "image_id": 60,
    "claim_id": 70,
    "entity_id": 80,
    "relationship_id": 90,
    "cluster_id": 100,
    "asset_version_id": 110,
    "asset_id": 120,
    "run_id": 130,
    "parent_run_id": 140,
    "parent_cluster_id": 150,
}


def _ordered_backward_edges(table_name: str) -> list[tuple[str, str, str]]:
    """Return deterministic lineage edges, preferring evidence over shortcuts."""
    return sorted(
        _TABLE_BACKWARD_EDGES.get(table_name, []),
        key=lambda edge: (
            _BACKWARD_FIELD_PRIORITY.get(edge[0], 1_000),
            edge[0],
            edge[1],
        ),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def trace_record(
    record_id: str,
    tables: dict[str, list[dict[str, Any]]],
    *,
    table_name: str | None = None,
    direction: str = "backward",
    max_depth: int = 20,
    table_hint: str | None = None,
) -> TraceResult:
    """Trace lineage from *record_id* in the specified direction.

    Parameters
    ----------
    record_id:
        Primary key value of the start record.
    tables:
        Dict mapping table name to list of row dicts. Missing tables are treated
        as empty.
    table_name:
        Optional canonical table hint. When omitted the record ID is resolved
        across all supplied tables. Ambiguous IDs require an explicit hint.
    direction:
        ``"backward"`` — follow FK references up toward source assets (default).
        ``"forward"`` — find all derived records that reference this record.
    max_depth:
        Maximum hops before stopping to prevent unbounded traversal on large
        data sets.  Raises ``ValueError`` if exceeded.

    Returns
    -------
    TraceResult
        Canonical result with an ordered ``(table, record_id)`` path and the
        first broken edge, if any.

    Raises
    ------
    KeyError
        If *table_name* is not a known canonical table.
    LookupError
        If the record ID occurs in more than one table and no hint is supplied.
    ValueError
        If *direction* is not ``"backward"`` or ``"forward"``.
    RuntimeError
        If traversal exceeds *max_depth*.
    """
    if direction not in ("backward", "forward"):
        raise ValueError(
            f"direction must be 'backward' or 'forward', got {direction!r}"
        )

    if table_name and table_hint and table_name != table_hint:
        raise ValueError(
            f"Conflicting table hints: table_name={table_name!r}, "
            f"table_hint={table_hint!r}"
        )
    resolved_table = table_name or table_hint
    if resolved_table is not None and resolved_table not in TABLE_ID_FIELDS:
        raise KeyError(
            f"Unknown table {resolved_table!r}. "
            f"Known tables: {sorted(TABLE_ID_FIELDS)}"
        )

    pk_index = _build_pk_index(tables)
    record_id = str(record_id)
    if resolved_table is None:
        matches = _find_record_tables(record_id, pk_index)
        if len(matches) > 1:
            raise LookupError(
                f"Record {record_id!r} is ambiguous across tables {matches}. "
                "Pass table_name to select the intended record."
            )
        resolved_table = matches[0] if matches else None

    start_row = (
        pk_index.get(resolved_table, {}).get(record_id)
        if resolved_table is not None
        else None
    )
    if start_row is None:
        expected_table = resolved_table or "record"
        detail = BrokenEdge(
            from_table=expected_table,
            from_record_id=record_id,
            fk_field=TABLE_ID_FIELDS.get(expected_table, "record_id"),
            expected_table=expected_table,
            missing_id=record_id,
        )
        return TraceResult(
            record_id=record_id,
            table_name=resolved_table,
            direction=direction,
            path=[],
            is_complete=False,
            broken_edge=(expected_table, record_id),
            _broken_edge_detail=detail,
        )

    result = TraceResult(
        record_id=record_id,
        table_name=resolved_table,
        direction=direction,
    )

    if direction == "backward":
        _trace_backward(
            pk_index=pk_index,
            start_table=resolved_table,
            start_id=record_id,
            start_row=start_row,
            result=result,
            max_depth=max_depth,
        )
    else:
        _trace_forward(
            tables=tables,
            pk_index=pk_index,
            start_table=resolved_table,
            start_id=record_id,
            start_row=start_row,
            result=result,
            max_depth=max_depth,
        )

    return result


def _trace_backward(
    pk_index: dict[str, dict[str, dict[str, Any]]],
    start_table: str,
    start_id: str,
    start_row: dict[str, Any],
    result: TraceResult,
    max_depth: int,
) -> None:
    """Follow the deterministic primary provenance chain toward the original."""
    current_table = start_table
    current_id = start_id
    current_row = start_row
    visited_ids: set[str] = set()

    for depth in range(max_depth + 1):
        if current_id in visited_ids:
            result.cycle_detected = True
            result.is_complete = False
            return

        visited_ids.add(current_id)
        result.path.append((current_table, current_id))

        if _is_backward_terminal(current_table, current_row):
            result.is_complete = True
            return

        if depth >= max_depth:
            raise RuntimeError(
                f"Lineage traversal exceeded max_depth={max_depth} "
                f"at {current_table}[{current_id}]. "
                "Consider increasing max_depth."
            )

        parent_id = current_row.get("parent_record_id")
        if parent_id:
            parent_id = str(parent_id)
            parent_tables = _find_record_tables(parent_id, pk_index)
            if len(parent_tables) > 1:
                raise LookupError(
                    f"Parent record {parent_id!r} is ambiguous across tables "
                    f"{parent_tables}."
                )
            if not parent_tables:
                _set_broken_edge(
                    result,
                    BrokenEdge(
                        from_table=current_table,
                        from_record_id=current_id,
                        fk_field="parent_record_id",
                        expected_table="parent_record",
                        missing_id=parent_id,
                    ),
                )
                return
            next_table = parent_tables[0]
            next_row = pk_index[next_table][parent_id]
            next_id = parent_id
        else:
            next_table = None
            next_id = None
            next_row = None
            for fk_field, to_table, _to_pk_field in _ordered_backward_edges(
                current_table
            ):
                fk_value = current_row.get(fk_field)
                if fk_value is None:
                    continue
                fk_id = str(fk_value)
                target_row = pk_index.get(to_table, {}).get(fk_id)
                if target_row is None:
                    _set_broken_edge(
                        result,
                        BrokenEdge(
                            from_table=current_table,
                            from_record_id=current_id,
                            fk_field=fk_field,
                            expected_table=to_table,
                            missing_id=fk_id,
                        ),
                    )
                    return
                next_table = to_table
                next_id = fk_id
                next_row = target_row
                break

            if next_table is None or next_id is None or next_row is None:
                result.is_complete = False
                return

        if next_id in visited_ids:
            result.cycle_detected = True
            result.is_complete = False
            return

        current_table = next_table
        current_id = next_id
        current_row = next_row


def _trace_forward(
    tables: dict[str, list[dict[str, Any]]],
    pk_index: dict[str, dict[str, dict[str, Any]]],
    start_table: str,
    start_id: str,
    start_row: dict[str, Any],
    result: TraceResult,
    max_depth: int,
) -> None:
    """BFS forward traversal: find all derived records referencing this record."""
    queue: deque[tuple[str, str, dict[str, Any], int]] = deque(
        [(start_table, start_id, start_row, 0)]
    )
    visited_ids: set[str] = {start_id}
    result.path.append((start_table, start_id))
    result.is_complete = True

    inv_cache: dict[str, dict[str, list[tuple[str, str, dict[str, Any]]]]] = {}

    while queue:
        current_table, current_id, _current_row, depth = queue.popleft()
        if current_table not in inv_cache:
            inv_cache[current_table] = _build_fk_inverted_index(
                tables,
                current_table,
            )

        children = list(inv_cache[current_table].get(current_id, []))
        for child_table, child_rows in tables.items():
            for child_row in child_rows:
                if str(child_row.get("parent_record_id") or "") == current_id:
                    children.append(
                        (child_table, "parent_record_id", child_row)
                    )

        ordered_children: list[tuple[str, str, dict[str, Any], str]] = []
        for child_table, fk_field, child_row in children:
            child_pk = TABLE_ID_FIELDS.get(child_table)
            if child_pk is None or child_row.get(child_pk) is None:
                continue
            child_id = str(child_row[child_pk])
            ordered_children.append(
                (child_table, fk_field, child_row, child_id)
            )

        for child_table, _fk_field, child_row, child_id in sorted(
            ordered_children,
            key=lambda item: (item[0], item[3], item[1]),
        ):
            if child_id in visited_ids:
                result.cycle_detected = True
                continue
            if depth >= max_depth:
                raise RuntimeError(
                    f"Lineage traversal exceeded max_depth={max_depth} "
                    f"at {current_table}[{current_id}]. "
                    "Consider increasing max_depth."
                )
            visited_ids.add(child_id)
            result.path.append((child_table, child_id))
            queue.append((child_table, child_id, child_row, depth + 1))


# ---------------------------------------------------------------------------
# Convenience: multi-record trace for a whole run or asset
# ---------------------------------------------------------------------------


def trace_asset(
    tables: dict[str, list[dict[str, Any]]],
    asset_id: str,
    *,
    direction: str = "forward",
    max_depth: int = 20,
) -> TraceResult:
    """Trace forward from a known asset_id to all derived records.

    Convenience wrapper around ``trace_record`` for the common case of
    discovering everything produced from a given asset.
    """
    return trace_record(
        asset_id,
        tables,
        table_name="assets",
        direction=direction,
        max_depth=max_depth,
    )


def trace_run(
    tables: dict[str, list[dict[str, Any]]],
    run_id: str,
    *,
    direction: str = "forward",
    max_depth: int = 20,
) -> TraceResult:
    """Trace forward from a processing run to all records produced in that run."""
    return trace_record(
        run_id,
        tables,
        table_name="processing_runs",
        direction=direction,
        max_depth=max_depth,
    )
