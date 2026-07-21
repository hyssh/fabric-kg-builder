"""Parquet source adapter (EXT-004).

Reads a Parquet file via PyArrow and produces:
- One ``SourceFileRow`` entry.
- One ``DocumentElementRow(element_type="table")`` per row-group, carrying the
  schema profile and the raw row-group locator in ``extra_meta``.
- One ``DocumentElementRow(element_type="table_row")`` per data row with a
  row-group/row-index locator in the element's ``notes`` (via content).

Schema preservation
-------------------
- Arrow schema (column names, types, metadata) is captured in ``extra_meta["schema"]``.
- Nested columns (structs, lists, maps) are JSON-serialised per-cell.
- The ``AdapterResult.extra_meta["schema_json"]`` key holds the Arrow schema
  serialised via ``schema.to_string()``.

Limits (EXT-009)
----------------
- File size: ``adapter.MAX_FILE_BYTES`` (200 MB).
- Row count:  ``adapter.MAX_ROWS`` (5 000 000).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from fabric_kg_builder.model.ids import (
    content_hash as compute_content_hash,
    make_document_element_id,
    make_source_file_id,
)
from fabric_kg_builder.model.schemas import DocumentElementRow, SourceFileRow

from .adapter import (
    ADAPTER_CONTRACT_VERSION,
    MAX_FILE_BYTES,
    MAX_ROWS,
    AdapterError,
    AdapterResult,
    FailureType,
    check_file_size,
)
from .media_type import validate_extension_vs_signature

_ADAPTER_NAME = "parquet_adapter"
_ADAPTER_VERSION = "1.0.0"

_SAMPLE_SIZE = 5  # values per column in schema profile


def _file_hash(path: Path) -> str:
    """SHA-256 of file bytes via streaming to avoid reading the whole file at once."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _canonical_path(path: Path, project_root: Path | None) -> str:
    root = project_root or Path.cwd()
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _scalar_to_python(val: Any) -> Any:
    """Convert a PyArrow scalar to a JSON-serialisable Python value."""
    if val is None:
        return None
    if isinstance(val, pa.Scalar):
        py_val = val.as_py()
        if isinstance(py_val, (dict, list)):
            return py_val
        return py_val
    return val


def _row_to_content(schema: pa.Schema, row_dict: dict[str, Any]) -> str:
    """Serialise a row dict as a compact ``key=value`` string."""
    parts = []
    for name in schema.names:
        v = row_dict.get(name)
        if isinstance(v, (dict, list)):
            v = json.dumps(v, default=str)
        parts.append(f"{name}={v}")
    return "; ".join(parts)


def _infer_pa_type_label(dtype: pa.DataType) -> str:
    if pa.types.is_integer(dtype):
        return "integer"
    if pa.types.is_floating(dtype):
        return "float"
    if pa.types.is_boolean(dtype):
        return "boolean"
    if pa.types.is_string(dtype) or pa.types.is_large_string(dtype):
        return "string"
    if pa.types.is_timestamp(dtype):
        return "timestamp"
    if pa.types.is_list(dtype) or pa.types.is_large_list(dtype):
        return "list"
    if pa.types.is_struct(dtype):
        return "struct"
    if pa.types.is_map(dtype):
        return "map"
    return "other"


def _build_schema_profile(
    schema: pa.Schema,
    table: pa.Table,
    source_file_id: str,
    can_path: str,
    now: datetime,
) -> dict[str, Any]:
    columns: list[dict[str, Any]] = []
    for idx, name in enumerate(schema.names):
        col = table.column(name)
        non_null = col.drop_null()
        samples: list[Any] = []
        for i in range(min(_SAMPLE_SIZE, len(non_null))):
            v = _scalar_to_python(non_null[i])
            if isinstance(v, (dict, list)):
                v = json.dumps(v, default=str)
            elif not isinstance(v, (str, int, float, bool, type(None))):
                v = str(v)
            samples.append(v)

        columns.append(
            {
                "index": idx,
                "name": name,
                "arrow_type": str(schema.field(name).type),
                "inferred_type": _infer_pa_type_label(schema.field(name).type),
                "null_count": col.null_count,
                "unique_count": None,  # expensive for large files
                "sample_values": samples,
                "metadata": schema.field(name).metadata,
            }
        )

    return {
        "schema_profile_version": "1",
        "source_file_id": source_file_id,
        "source_path": can_path,
        "source_type": "parquet",
        "inspected_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "row_count": len(table),
        "column_count": len(schema.names),
        "columns": columns,
        "schema_string": schema.to_string(),
        "warnings": [],
    }


class ParquetAdapter:
    """Source adapter for Apache Parquet files (EXT-004)."""

    @staticmethod
    def extract(
        path: str | Path,
        *,
        project_root: str | Path | None = None,
        validate_mime: bool = True,
        max_rows: int = MAX_ROWS,
        max_bytes: int = MAX_FILE_BYTES,
        lineage: dict | None = None,
    ) -> AdapterResult:
        """Extract rows and schema from a Parquet file.

        Parameters
        ----------
        path:
            Path to the ``.parquet`` file.
        project_root:
            Optional root for canonical relative-path computation.
        validate_mime:
            When True (default), validate magic bytes match ``.parquet`` extension.
        max_rows:
            Reject files exceeding this row count (EXT-009).
        max_bytes:
            Reject files exceeding this byte size (EXT-009).
        lineage:
            Optional dict with lineage envelope keys (project_id, asset_id,
            asset_version_id, run_id, domain_hash) forwarded to all emitted rows.

        Returns
        -------
        AdapterResult
            ``adapter_name="parquet_adapter"``, ``detected_media_type``,
            ``source_file``, ``document_elements`` (one table + N table_row),
            ``extra_meta["schema_profile"]`` + ``extra_meta["row_groups"]``.
        """
        lineage = lineage or {}
        _LINEAGE_KEYS = frozenset(
            {"project_id", "asset_id", "asset_version_id", "run_id", "domain_hash"}
        )
        lineage_kwargs = {k: v for k, v in lineage.items() if k in _LINEAGE_KEYS}
        path = Path(path)
        if not path.exists():
            raise AdapterError(
                FailureType.NOT_FOUND,
                f"Source file not found: {path}",
                source_locator=str(path),
            )

        check_file_size(path, max_bytes)

        detected_mime = "application/x-parquet"
        if validate_mime:
            detected_mime = validate_extension_vs_signature(path)

        now = datetime.now(timezone.utc)
        file_hash = _file_hash(path)
        can_path = _canonical_path(path, Path(project_root) if project_root else None)
        source_file_id = make_source_file_id(can_path, file_hash)

        try:
            pf = pq.ParquetFile(path)
            meta = pf.metadata
            num_row_groups = meta.num_row_groups
            total_rows = meta.num_rows
        except Exception as exc:
            raise AdapterError(
                FailureType.CORRUPT,
                f"Cannot read Parquet metadata from '{path.name}': {exc}",
                source_locator=str(path),
            ) from exc

        if total_rows > max_rows:
            raise AdapterError(
                FailureType.TOO_MANY_ROWS,
                f"Parquet file '{path.name}' contains {total_rows:,} rows, "
                f"which exceeds the {max_rows:,}-row limit.",
                source_locator=str(path),
            )

        try:
            table = pq.read_table(path)
        except Exception as exc:
            raise AdapterError(
                FailureType.CORRUPT,
                f"Failed to read Parquet data from '{path.name}': {exc}",
                source_locator=str(path),
            ) from exc

        schema = table.schema
        schema_profile = _build_schema_profile(schema, table, source_file_id, can_path, now)

        source_file = SourceFileRow(
            source_file_id=source_file_id,
            path=can_path,
            filename=path.name,
            source_type="parquet",
            content_hash=file_hash,
            byte_size=path.stat().st_size,
            ingested_at=now,
            row_count=total_rows,
            notes=f"row_groups={num_row_groups}",
            **lineage_kwargs,
        )

        document_elements: list[DocumentElementRow] = []
        sort_order = 0
        row_groups_meta: list[dict[str, Any]] = []
        global_row_index = 0

        for rg_idx in range(num_row_groups):
            rg_meta = meta.row_group(rg_idx)
            rg_num_rows = rg_meta.num_rows
            rg_total_bytes = rg_meta.total_byte_size

            # Read just this row group
            rg_table = pf.read_row_group(rg_idx)

            rg_content = (
                f"row_group={rg_idx}; rows={rg_num_rows}; "
                f"schema={schema.to_string()}"
            )
            rg_hash = compute_content_hash(rg_content)
            rg_elem_id = make_document_element_id(
                source_file_id, "table", None, sort_order, rg_hash
            )

            document_elements.append(
                DocumentElementRow(
                    document_element_id=rg_elem_id,
                    source_file_id=source_file_id,
                    element_type="table",
                    title=f"{path.stem} (row_group {rg_idx})",
                    content=rg_content,
                    sort_order=sort_order,
                    content_hash=rg_hash,
                    extracted_at=now,
                    **lineage_kwargs,
                )
            )
            sort_order += 1

            row_groups_meta.append(
                {
                    "row_group_index": rg_idx,
                    "num_rows": rg_num_rows,
                    "total_byte_size": rg_total_bytes,
                    "element_id": rg_elem_id,
                }
            )

            # Emit each row as a table_row element
            for row_idx in range(rg_num_rows):
                row_dict = {
                    name: _scalar_to_python(rg_table.column(name)[row_idx])
                    for name in schema.names
                }
                row_content = _row_to_content(schema, row_dict)
                row_hash = compute_content_hash(row_content)
                locator_json = json.dumps({
                    "row_group": rg_idx,
                    "row_group_row_index": row_idx,
                    "global_row_index": global_row_index,
                })
                document_elements.append(
                    DocumentElementRow(
                        document_element_id=make_document_element_id(
                            source_file_id,
                            "table_row",
                            None,
                            sort_order,
                            row_hash,
                        ),
                        source_file_id=source_file_id,
                        element_type="table_row",
                        parent_element_id=rg_elem_id,
                        content=row_content,
                        sort_order=sort_order,
                        row_index=row_idx,
                        content_hash=row_hash,
                        extracted_at=now,
                        source_locator_json=locator_json,
                        **lineage_kwargs,
                    )
                )
                sort_order += 1
                global_row_index += 1

        return AdapterResult(
            adapter_name=_ADAPTER_NAME,
            adapter_version=_ADAPTER_VERSION,
            detected_media_type=detected_mime,
            source_locator=f"file://{path.resolve().as_posix()}",
            source_file=source_file,
            document_elements=document_elements,
            extra_meta={
                "schema_profile": schema_profile,
                "row_groups": row_groups_meta,
                "schema_string": schema.to_string(),
                "arrow_metadata": dict(schema.metadata or {}),
            },
        )
