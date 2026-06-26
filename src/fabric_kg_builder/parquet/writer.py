"""Parquet writer and placeholder generator for the 8 canonical tables.

SPEC-002 §7 (writer) and §8 (placeholders).

Public API
----------
write_table(table_name, rows, out_dir)
    Write validated canonical records for one table to ``out_dir/<table>.parquet``.

write_all_tables(table_rows, out_dir)
    Write multiple tables in one call.

write_placeholder(table_name, out_dir)
    Write an empty-but-typed placeholder Parquet for one table.

write_all_placeholders(out_dir)
    Write placeholder files for all 8 tables.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from fabric_kg_builder.model.arrow_schemas import TABLE_SCHEMAS

# ---------------------------------------------------------------------------
# Writer config — SPEC-002 §7.2
# ---------------------------------------------------------------------------

WRITER_CONFIG: dict[str, Any] = {
    "compression": "snappy",
    "use_dictionary": True,
    "write_statistics": True,
    "data_page_size": 1024 * 1024,
    "version": "2.6",
}

# ---------------------------------------------------------------------------
# JSON helpers — SPEC-002 §7.3
# ---------------------------------------------------------------------------

_PLACEHOLDER_HASH = hashlib.sha256(b"__placeholder__").hexdigest()
_PLACEHOLDER_SENTINEL = "__placeholder__"


def safe_json_str(value: dict | list | None) -> str | None:
    """Serialize a dict/list to a JSON string, or return None for None."""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------------------
# Core writer
# ---------------------------------------------------------------------------


def write_table(
    table_name: str,
    rows: list[dict],
    out_dir: str | Path,
    *,
    schema: pa.Schema | None = None,
) -> Path:
    """Write *rows* for *table_name* to ``out_dir/<table_name>.parquet``.

    Parameters
    ----------
    table_name:
        One of the 8 canonical table names.
    rows:
        List of dicts; keys must match the declared pyarrow schema.
    out_dir:
        Directory where the Parquet file is written.  Created if absent.
    schema:
        Optional override schema.  Defaults to the registered schema for
        *table_name*.

    Returns
    -------
    Path
        The path of the written Parquet file.

    Raises
    ------
    KeyError
        If *table_name* is not in the canonical registry.
    ValueError
        If a NOT NULL column is null in any row, or on schema mismatch.
    """
    if schema is None:
        if table_name not in TABLE_SCHEMAS:
            raise KeyError(
                f"Unknown table '{table_name}'. "
                f"Known tables: {sorted(TABLE_SCHEMAS)}"
            )
        schema = TABLE_SCHEMAS[table_name]

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{table_name}.parquet"

    _validate_not_null(rows, schema, table_name)

    try:
        table = pa.Table.from_pylist(rows, schema=schema)
    except (pa.ArrowInvalid, pa.ArrowTypeError) as exc:
        raise ValueError(
            f"Schema mismatch writing table '{table_name}': {exc}"
        ) from exc

    pq.write_table(table, out_path, **WRITER_CONFIG)
    return out_path


def write_all_tables(
    table_rows: dict[str, list[dict]],
    out_dir: str | Path,
) -> dict[str, Path]:
    """Write multiple tables to *out_dir*.

    Parameters
    ----------
    table_rows:
        Dict mapping table_name → list of row dicts.
    out_dir:
        Output directory.

    Returns
    -------
    dict
        Mapping table_name → written file Path.
    """
    results: dict[str, Path] = {}
    for name, rows in table_rows.items():
        results[name] = write_table(name, rows, out_dir)
    return results


# ---------------------------------------------------------------------------
# NOT NULL enforcement
# ---------------------------------------------------------------------------


def _validate_not_null(
    rows: list[dict], schema: pa.Schema, table_name: str
) -> None:
    """Raise ValueError if any NOT NULL column contains a None value."""
    not_null_cols = [
        schema.field(i).name
        for i in range(len(schema))
        if not schema.field(i).nullable
    ]
    for row_idx, row in enumerate(rows):
        for col in not_null_cols:
            if row.get(col) is None:
                raise ValueError(
                    f"NOT NULL violation in table '{table_name}', "
                    f"row {row_idx}, column '{col}': value is None."
                )


# ---------------------------------------------------------------------------
# Placeholder writer — SPEC-002 §8
# ---------------------------------------------------------------------------

_NOW_SENTINEL = datetime(2000, 1, 1, tzinfo=timezone.utc)

# Placeholder rows per table; all NOT NULL columns get sentinel values.
_PLACEHOLDER_ROWS: dict[str, dict] = {
    "source_files": {
        "source_file_id": f"src:{_PLACEHOLDER_SENTINEL}",
        "path": _PLACEHOLDER_SENTINEL,
        "filename": _PLACEHOLDER_SENTINEL,
        "source_type": _PLACEHOLDER_SENTINEL,
        "content_hash": _PLACEHOLDER_HASH,
        "byte_size": None,
        "ingested_at": _NOW_SENTINEL,
        "schema_profile_path": None,
        "row_count": None,
        "notes": None,
    },
    "document_elements": {
        "document_element_id": f"elem:{_PLACEHOLDER_SENTINEL}",
        "source_file_id": f"src:{_PLACEHOLDER_SENTINEL}",
        "element_type": _PLACEHOLDER_SENTINEL,
        "parent_element_id": None,
        "title": None,
        "content": None,
        "content_html": None,
        "blob_url": None,
        "page_number": None,
        "section_path": None,
        "sort_order": None,
        "row_index": None,
        "col_index": None,
        "content_hash": _PLACEHOLDER_HASH,
        "extracted_at": _NOW_SENTINEL,
    },
    "chunks": {
        "chunk_id": f"chunk:{_PLACEHOLDER_SENTINEL}",
        "source_file_id": f"src:{_PLACEHOLDER_SENTINEL}",
        "document_element_id": None,
        "chunk_type": _PLACEHOLDER_SENTINEL,
        "content": _PLACEHOLDER_SENTINEL,
        "content_html": None,
        "embedding_text": None,
        "blob_url": None,
        "page_number": None,
        "section_path": None,
        "table_id": None,
        "figure_id": None,
        "image_id": None,
        "related_entity_ids": None,
        "entity_search_keys": None,
        "content_hash": _PLACEHOLDER_HASH,
        "created_at": _NOW_SENTINEL,
    },
    "entities": {
        "entity_id": f"entity:{_PLACEHOLDER_SENTINEL}",
        "entity_type": _PLACEHOLDER_SENTINEL,
        "display_name": _PLACEHOLDER_SENTINEL,
        "canonical_key": f"{_PLACEHOLDER_SENTINEL}:{_PLACEHOLDER_SENTINEL}",
        "aliases": None,
        "search_aliases": None,
        "description": None,
        "properties_json": None,
        "source_file_id": None,
        "confidence": None,
        "is_placeholder": True,
        "content_hash": _PLACEHOLDER_HASH,
        "created_at": _NOW_SENTINEL,
        "updated_at": _NOW_SENTINEL,
    },
    "relationships": {
        "relationship_id": f"rel:{_PLACEHOLDER_SENTINEL}",
        "relationship_type": _PLACEHOLDER_SENTINEL,
        "source_entity_id": f"entity:{_PLACEHOLDER_SENTINEL}",
        "target_entity_id": f"entity:{_PLACEHOLDER_SENTINEL}",
        "evidence_id": None,
        "properties_json": None,
        "confidence": None,
        "is_placeholder": True,
        "content_hash": _PLACEHOLDER_HASH,
        "created_at": _NOW_SENTINEL,
    },
    "evidence": {
        "evidence_id": f"evid:{_PLACEHOLDER_SENTINEL}",
        "source_file_id": f"src:{_PLACEHOLDER_SENTINEL}",
        "source_type": _PLACEHOLDER_SENTINEL,
        "document_element_id": None,
        "chunk_id": None,
        "page_number": None,
        "section_path": None,
        "table_id": None,
        "row_index": None,
        "col_index": None,
        "figure_id": None,
        "image_id": None,
        "callout_id": None,
        "visual_region_id": None,
        "blob_url": None,
        "text": None,
        "content_hash": _PLACEHOLDER_HASH,
        "created_at": _NOW_SENTINEL,
    },
    "visual_assets": {
        "image_id": f"img:{_PLACEHOLDER_SENTINEL}",
        "source_file_id": f"src:{_PLACEHOLDER_SENTINEL}",
        "document_element_id": None,
        "asset_type": _PLACEHOLDER_SENTINEL,
        "page_number": None,
        "section_path": None,
        "caption": None,
        "alt_text": None,
        "blob_url": None,
        "image_path": None,
        "image_hash": _PLACEHOLDER_HASH,
        "width": None,
        "height": None,
        "description": None,
        "confidence": None,
        "is_placeholder": True,
        "created_at": _NOW_SENTINEL,
    },
    "visual_regions": {
        "visual_region_id": f"vr:{_PLACEHOLDER_SENTINEL}",
        "image_id": f"img:{_PLACEHOLDER_SENTINEL}",
        "region_type": _PLACEHOLDER_SENTINEL,
        "label": None,
        "text": None,
        "polygon_json": None,
        "normalized_polygon_json": None,
        "identified_entity_id": None,
        "blob_url": None,
        "confidence": None,
        "created_at": _NOW_SENTINEL,
    },
}


def write_placeholder(
    table_name: str,
    out_dir: str | Path,
) -> Path:
    """Write a single-row placeholder Parquet for *table_name*.

    The file is written to ``out_dir/<table_name>/_placeholder.parquet``
    as specified in SPEC-002 §8.2.

    Returns
    -------
    Path
        Path of the written placeholder file.
    """
    if table_name not in TABLE_SCHEMAS:
        raise KeyError(
            f"Unknown table '{table_name}'. Known tables: {sorted(TABLE_SCHEMAS)}"
        )

    schema = TABLE_SCHEMAS[table_name]
    placeholder_row = _PLACEHOLDER_ROWS[table_name]

    table_dir = Path(out_dir) / table_name
    table_dir.mkdir(parents=True, exist_ok=True)
    out_path = table_dir / "_placeholder.parquet"

    table = pa.Table.from_pylist([placeholder_row], schema=schema)
    pq.write_table(table, out_path, **WRITER_CONFIG)
    return out_path


def write_all_placeholders(out_dir: str | Path) -> dict[str, Path]:
    """Write placeholder Parquet files for all 8 canonical tables.

    Returns
    -------
    dict
        Mapping table_name → written placeholder Path.
    """
    return {name: write_placeholder(name, out_dir) for name in TABLE_SCHEMAS}
