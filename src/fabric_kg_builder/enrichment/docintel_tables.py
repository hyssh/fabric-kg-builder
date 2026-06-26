"""Table extraction from Azure Document Intelligence Layout results.

Extends SPEC-004 §8: DI Layout becomes the source of truth for tables.
Each table is extracted as an independent HTML artifact, indexable via AI Search
and graph-linkable via document_element_id.

Design decision (coordinator-tables-via-docintel.md, 2026-06-24):
  - DI Layout = geometry / OCR / structure (this module)
  - LLM = semantics only (table summary, entity linking over the HTML)
  - Tables become independent document_elements + chunks with chunk_type="table_html"

SDK note: output_content_format="markdown" requests Markdown output from DI Layout.
Verify parameter name against current azure-ai-documentintelligence SDK version.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..model.ids import content_hash, make_chunk_id, make_document_element_id
from ..model.schemas import ChunkRow, DocumentElementRow
from .docintel import _get


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


@dataclass
class DocIntelTableResult:
    """Structured output from :func:`extract_tables`.

    Attributes
    ----------
    document_elements:
        One :class:`~fabric_kg_builder.model.schemas.DocumentElementRow` per
        table (element_type="table", content_html set).
    chunks:
        One :class:`~fabric_kg_builder.model.schemas.ChunkRow` per table
        (chunk_type="table_html", content_html set).
    html_artifacts:
        Mapping of artifact filename → HTML string (e.g. "table_0.html" → "<table>…").
        Caller may write these to disk or upload to Blob storage.
    markdown:
        Whole-document Markdown string from analyze_result.content.
        Tables render as HTML ``<table>`` elements within the Markdown when
        ``output_content_format="markdown"`` was used.  Feed this to the
        semantic chunker for non-table content.
    """

    document_elements: list[DocumentElementRow] = field(default_factory=list)
    chunks: list[ChunkRow] = field(default_factory=list)
    html_artifacts: dict[str, str] = field(default_factory=dict)
    markdown: str = ""


# ---------------------------------------------------------------------------
# HTML builder
# ---------------------------------------------------------------------------


def table_to_html(table: Any) -> str:
    """Convert a DI Layout table object/dict to a clean HTML ``<table>`` string.

    Cells whose ``kind`` equals ``"columnHeader"`` are placed in ``<thead>``
    using ``<th>`` tags.  All other cells go into ``<tbody>`` as ``<td>`` tags.
    Row and column indices (``row_index``, ``column_index``) determine grid
    position; missing cells are rendered as empty strings.

    Parameters
    ----------
    table:
        A single DI table entry — either a real Azure SDK object or a dict/
        MagicMock with the same attribute contract.

    Returns
    -------
    str
        A well-formed HTML ``<table>`` string.
    """
    cells = list(_get(table, "cells") or [])
    col_count = int(_get(table, "column_count") or 0)
    # Fallback: derive column count from max column_index seen in cells
    if col_count == 0 and cells:
        col_count = max((int(_get(c, "column_index") or 0) for c in cells), default=0) + 1

    header_rows: dict[int, dict[int, str]] = {}
    body_rows: dict[int, dict[int, str]] = {}

    for cell in cells:
        r = int(_get(cell, "row_index") or 0)
        c = int(_get(cell, "column_index") or 0)
        text = str(_get(cell, "content") or "")
        kind = str(_get(cell, "kind") or "content")

        if kind == "columnHeader":
            header_rows.setdefault(r, {})[c] = text
        else:
            body_rows.setdefault(r, {})[c] = text

    parts: list[str] = ["<table>"]

    if header_rows:
        parts.append("<thead>")
        for r in sorted(header_rows):
            parts.append("<tr>")
            for c in range(col_count):
                parts.append(f"<th>{header_rows[r].get(c, '')}</th>")
            parts.append("</tr>")
        parts.append("</thead>")

    if body_rows:
        parts.append("<tbody>")
        for r in sorted(body_rows):
            parts.append("<tr>")
            for c in range(col_count):
                parts.append(f"<td>{body_rows[r].get(c, '')}</td>")
            parts.append("</tr>")
        parts.append("</tbody>")

    parts.append("</table>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Plain-text + embedding helpers
# ---------------------------------------------------------------------------


def _table_to_plain_text(table: Any) -> str:
    """Convert a DI table to a tab-delimited plain-text string (all rows)."""
    cells = list(_get(table, "cells") or [])
    col_count = int(_get(table, "column_count") or 0)
    if col_count == 0 and cells:
        col_count = max((int(_get(c, "column_index") or 0) for c in cells), default=0) + 1

    rows: dict[int, dict[int, str]] = {}
    for cell in cells:
        r = int(_get(cell, "row_index") or 0)
        c = int(_get(cell, "column_index") or 0)
        rows.setdefault(r, {})[c] = str(_get(cell, "content") or "")

    lines = [
        "\t".join(rows[r].get(c, "") for c in range(col_count))
        for r in sorted(rows)
    ]
    return "\n".join(lines)


def _table_to_embedding_text(table: Any) -> str:
    """Build a compact pipe-delimited text for embedding.

    Header row is prefixed with ``"Table: "``; body rows follow as
    ``"col1 | col2 | …"`` lines.  This format is compact, human-readable,
    and works well as embedding input for retrieval.
    """
    cells = list(_get(table, "cells") or [])
    col_count = int(_get(table, "column_count") or 0)
    if col_count == 0 and cells:
        col_count = max((int(_get(c, "column_index") or 0) for c in cells), default=0) + 1

    header_row: dict[int, str] = {}
    body_rows: dict[int, dict[int, str]] = {}

    for cell in cells:
        r = int(_get(cell, "row_index") or 0)
        c = int(_get(cell, "column_index") or 0)
        text = str(_get(cell, "content") or "")
        kind = str(_get(cell, "kind") or "content")
        if kind == "columnHeader":
            header_row[c] = text
        else:
            body_rows.setdefault(r, {})[c] = text

    parts: list[str] = []
    if header_row:
        header_text = " | ".join(header_row.get(c, "") for c in range(col_count))
        parts.append(f"Table: {header_text}")
    for r in sorted(body_rows):
        row_text = " | ".join(body_rows[r].get(c, "") for c in range(col_count))
        parts.append(row_text)

    return "\n".join(parts)


def _get_table_page_number(table: Any) -> int | None:
    """Extract the first page number from a DI table's cell bounding_regions."""
    for cell in list(_get(table, "cells") or []):
        for region in list(_get(cell, "bounding_regions") or []):
            pn = _get(region, "page_number")
            if pn is not None:
                return int(pn)
    return None


# ---------------------------------------------------------------------------
# Top-level extraction function
# ---------------------------------------------------------------------------


def extract_tables(
    analyze_result: Any,
    source_file_id: str,
    *,
    section_path: str | None = None,
    now: datetime | None = None,
    sort_order_start: int = 0,
) -> DocIntelTableResult:
    """Extract all tables from a DI Layout analyze_result.

    For each table in ``analyze_result.tables`` this function produces:

    - One :class:`~fabric_kg_builder.model.schemas.DocumentElementRow`
      (``element_type="table"``, ``content_html`` set to the rendered HTML,
      ``blob_url=None`` — the uploader/Verbal sets this after upload).
    - One :class:`~fabric_kg_builder.model.schemas.ChunkRow`
      (``chunk_type="table_html"``, FK ``document_element_id`` set).
    - One HTML artifact entry in ``DocIntelTableResult.html_artifacts``
      keyed as ``"table_{n}.html"``.

    Parameters
    ----------
    analyze_result:
        Raw DI result (real SDK ``AnalyzeResult`` or mock).  Must expose
        ``.tables`` (list), ``.content`` (str), ``.pages`` (list).
    source_file_id:
        Stable ``source_file_id`` FK — used to build deterministic IDs.
    section_path:
        Nearest heading context for provenance (optional).
    now:
        UTC timestamp — injectable for deterministic tests.
    sort_order_start:
        Starting ``sort_order`` index (allows callers to interleave table
        elements with paragraph elements).

    Returns
    -------
    DocIntelTableResult
    """
    if now is None:
        now = datetime.now(timezone.utc)

    tables_raw = list(_get(analyze_result, "tables") or [])
    markdown = str(_get(analyze_result, "content") or "")

    document_elements: list[DocumentElementRow] = []
    chunks: list[ChunkRow] = []
    html_artifacts: dict[str, str] = {}

    for idx, table in enumerate(tables_raw):
        html = table_to_html(table)
        plain = _table_to_plain_text(table)
        embedding_text = _table_to_embedding_text(table)
        page_num = _get_table_page_number(table)
        sort_order = sort_order_start + idx

        html_hash = content_hash(html)

        elem_id = make_document_element_id(
            source_file_id,
            "table",
            page_num,
            sort_order,
            html_hash,
        )

        document_elements.append(
            DocumentElementRow(
                document_element_id=elem_id,
                source_file_id=source_file_id,
                element_type="table",
                content=plain,
                content_html=html,
                blob_url=None,  # blob_url set by uploader after artifact upload
                page_number=page_num,
                section_path=section_path,
                sort_order=sort_order,
                content_hash=html_hash,
                extracted_at=now,
            )
        )

        chunk_id = make_chunk_id(source_file_id, "table_html", html_hash)

        chunks.append(
            ChunkRow(
                chunk_id=chunk_id,
                source_file_id=source_file_id,
                document_element_id=elem_id,
                chunk_type="table_html",
                content=plain,
                content_html=html,
                embedding_text=embedding_text,
                blob_url=None,
                page_number=page_num,
                section_path=section_path,
                content_hash=html_hash,
                created_at=now,
            )
        )

        html_artifacts[f"table_{idx}.html"] = html

    return DocIntelTableResult(
        document_elements=document_elements,
        chunks=chunks,
        html_artifacts=html_artifacts,
        markdown=markdown,
    )


# ---------------------------------------------------------------------------
# Artifact writer
# ---------------------------------------------------------------------------


def write_table_artifacts(
    html_artifacts: dict[str, str],
    out_dir: Path,
    source_file_id: str,
) -> list[Path]:
    """Write table HTML (and plain-text Markdown) artifacts to disk.

    Creates ``{out_dir}/tables/{source_file_id}/table_{n}.html`` and a
    companion ``table_{n}.md`` (same HTML content — Markdown renderers
    display it as a table).

    Parameters
    ----------
    html_artifacts:
        ``DocIntelTableResult.html_artifacts`` mapping.
    out_dir:
        Base build output directory (e.g. ``build/``).
    source_file_id:
        Used as the subdirectory name beneath ``tables/``.

    Returns
    -------
    list[Path]
        Paths of all files written (both ``.html`` and ``.md``).
    """
    # Sanitize source_file_id for use as a directory name (colons are invalid on Windows).
    safe_dir_name = source_file_id.replace(":", "_")
    table_dir = out_dir / "tables" / safe_dir_name
    table_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for name, html in html_artifacts.items():
        html_path = table_dir / name
        html_path.write_text(html, encoding="utf-8")
        written.append(html_path)

        md_name = name.replace(".html", ".md")
        md_path = table_dir / md_name
        md_path.write_text(html, encoding="utf-8")
        written.append(md_path)

    return written


# ---------------------------------------------------------------------------
# Whole-document Markdown accessor
# ---------------------------------------------------------------------------


def get_document_markdown(analyze_result: Any) -> str:
    """Return the whole-document Markdown from a DI Layout result.

    When ``output_content_format="markdown"`` is passed to
    ``begin_analyze_document``, ``analyze_result.content`` contains the full
    document as Markdown with tables rendered as HTML ``<table>`` elements.

    This is the recommended input for semantic chunking in RAG pipelines
    (coordinator-tables-via-docintel.md, SPEC-004 §8).

    The chunker (Verbal) consumes this string; table chunks are handled
    separately by :func:`extract_tables` and should be excluded from the
    semantic chunking pass.
    """
    return str(_get(analyze_result, "content") or "")
