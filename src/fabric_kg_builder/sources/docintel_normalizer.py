"""Document Intelligence v4 Layout output normalizer (EXT-007).

Normalises the DI v4 Layout ``AnalyzeResult`` into a structured manifest
containing pages, spans, polygons, tables, and figures — while reusing the
existing ``docintel`` utilities for coordinate normalisation.

This module is a *pure mapping layer* (no SDK calls).  Inject the raw DI
result from ``DocIntelClient.layout_analyze_raw()`` or a mock.

The returned ``DiNormalizedLayout`` is independent of ``VisualRegionRow`` so
callers can use it for downstream element construction without the existing
enrichment pipeline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Reuse polygon helpers from the existing docintel utilities
from fabric_kg_builder.enrichment.docintel import (
    _get,  # attribute-or-dict getter
    _polygon_to_pairs,
    _normalize_polygon,
    _build_page_geometry_map,
    PageGeometry,
)
from fabric_kg_builder.model.ids import content_hash as compute_content_hash


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class DiSpan:
    """A content span: character offset range within the raw content string."""

    offset: int
    length: int
    text: str


@dataclass
class DiPolygon:
    """A bounding polygon for a region on one page."""

    page_number: int
    polygon: list[list[float]]              # [[x,y], …] pixel coords
    normalized: list[list[float]]           # [[x,y], …] in [0,1] relative coords
    polygon_json: str
    normalized_polygon_json: str


@dataclass
class DiPage:
    """Per-page summary from a DI Layout result."""

    page_number: int
    width: float
    height: float
    unit: str
    angle: float | None
    word_count: int
    line_count: int
    spans: list[DiSpan] = field(default_factory=list)


@dataclass
class DiCell:
    """A single cell in a DI table."""

    row_index: int
    col_index: int
    row_span: int
    col_span: int
    kind: str  # "columnHeader" | "rowHeader" | "content" | "stubHead"
    content: str
    polygons: list[DiPolygon] = field(default_factory=list)


@dataclass
class DiTable:
    """A table from a DI Layout result."""

    table_index: int
    row_count: int
    col_count: int
    cells: list[DiCell]
    page_numbers: list[int]
    caption: str | None = None
    html: str = ""


@dataclass
class DiFigure:
    """A figure region from a DI Layout result."""

    figure_index: int
    page_numbers: list[int]
    polygons: list[DiPolygon]
    caption: str | None = None


@dataclass
class DiNormalizedLayout:
    """Normalised output of a DI v4 Layout analysis pass."""

    pages: list[DiPage]
    tables: list[DiTable]
    figures: list[DiFigure]
    raw_content: str
    content_hash: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_spans(obj: Any) -> list[DiSpan]:
    spans_raw: list[Any] = list(_get(obj, "spans") or [])
    result: list[DiSpan] = []
    for sp in spans_raw:
        offset = int(_get(sp, "offset") or 0)
        length = int(_get(sp, "length") or 0)
        result.append(DiSpan(offset=offset, length=length, text=""))
    return result


def _resolve_span_text(spans: list[DiSpan], raw_content: str) -> list[DiSpan]:
    """Fill in span text from the raw content string."""
    resolved: list[DiSpan] = []
    for sp in spans:
        text = raw_content[sp.offset : sp.offset + sp.length]
        resolved.append(DiSpan(offset=sp.offset, length=sp.length, text=text))
    return resolved


def _parse_bounding_regions(
    obj: Any,
    page_geos: dict[int, PageGeometry],
) -> list[DiPolygon]:
    brs: list[Any] = list(_get(obj, "bounding_regions") or [])
    result: list[DiPolygon] = []
    for br in brs:
        pn = int(_get(br, "page_number") or 1)
        poly_flat: list[float] = list(_get(br, "polygon") or [])
        if len(poly_flat) < 2:
            continue
        pairs = _polygon_to_pairs(poly_flat)
        geo = page_geos.get(pn)
        norm = (
            _normalize_polygon(poly_flat, geo.width, geo.height)
            if geo and geo.width and geo.height
            else []
        )
        result.append(
            DiPolygon(
                page_number=pn,
                polygon=pairs,
                normalized=norm,
                polygon_json=json.dumps(pairs),
                normalized_polygon_json=json.dumps(norm) if norm else "[]",
            )
        )
    return result


def _table_to_html(table: Any, cells: list[DiCell]) -> str:
    """Render a DI table as an HTML string."""
    header_cells = [c for c in cells if c.kind in ("columnHeader", "rowHeader", "stubHead")]
    header_row_indices = sorted({c.row_index for c in header_cells})

    rows: dict[int, list[str]] = {}
    for cell in sorted(cells, key=lambda c: (c.row_index, c.col_index)):
        tag = "th" if cell.row_index in header_row_indices else "td"
        span_attrs = ""
        if cell.row_span > 1:
            span_attrs += f' rowspan="{cell.row_span}"'
        if cell.col_span > 1:
            span_attrs += f' colspan="{cell.col_span}"'
        from html import escape  # noqa: PLC0415
        cell_html = f"<{tag}{span_attrs}>{escape(cell.content)}</{tag}>"
        rows.setdefault(cell.row_index, []).append(cell_html)

    rows_html: list[str] = []
    for row_idx in sorted(rows.keys()):
        row_tag = "thead" if row_idx in header_row_indices else "tbody"
        rows_html.append(f"<tr>{''.join(rows[row_idx])}</tr>")

    return f"<table>{''.join(rows_html)}</table>"


# ---------------------------------------------------------------------------
# Main normaliser
# ---------------------------------------------------------------------------


def normalize_di_layout(di_result: Any) -> DiNormalizedLayout:
    """Normalise a DI v4 Layout ``AnalyzeResult`` into structured typed objects.

    Parameters
    ----------
    di_result:
        The result from ``begin_analyze_document(...).result()`` — may be a real
        Azure SDK object or a compatible MagicMock / dict.

    Returns
    -------
    DiNormalizedLayout
        Fully typed pages, tables, figures, and raw content string.
    """
    raw_content: str = str(_get(di_result, "content") or "")

    # ---- pages ----
    pages_raw: list[Any] = list(_get(di_result, "pages") or [])
    page_geos = _build_page_geometry_map(pages_raw)
    pages: list[DiPage] = []
    for pg in pages_raw:
        pn = int(_get(pg, "page_number") or 0)
        words: list[Any] = list(_get(pg, "words") or [])
        lines: list[Any] = list(_get(pg, "lines") or [])
        spans = _resolve_span_text(_parse_spans(pg), raw_content)
        angle_raw = _get(pg, "angle")
        pages.append(
            DiPage(
                page_number=pn,
                width=float(_get(pg, "width") or 0.0),
                height=float(_get(pg, "height") or 0.0),
                unit=str(_get(pg, "unit") or "pixel"),
                angle=float(angle_raw) if angle_raw is not None else None,
                word_count=len(words),
                line_count=len(lines),
                spans=spans,
            )
        )

    # ---- tables ----
    tables_raw: list[Any] = list(_get(di_result, "tables") or [])
    tables: list[DiTable] = []
    for t_idx, tbl in enumerate(tables_raw):
        row_count = int(_get(tbl, "row_count") or 0)
        col_count = int(_get(tbl, "column_count") or 0)
        cells_raw: list[Any] = list(_get(tbl, "cells") or [])
        cells: list[DiCell] = []
        for cell in cells_raw:
            polygons = _parse_bounding_regions(cell, page_geos)
            cells.append(
                DiCell(
                    row_index=int(_get(cell, "row_index") or 0),
                    col_index=int(_get(cell, "column_index") or 0),
                    row_span=int(_get(cell, "row_span") or 1),
                    col_span=int(_get(cell, "column_span") or 1),
                    kind=str(_get(cell, "kind") or "content"),
                    content=str(_get(cell, "content") or ""),
                    polygons=polygons,
                )
            )

        brs: list[Any] = list(_get(tbl, "bounding_regions") or [])
        page_nums: list[int] = sorted(
            {int(_get(br, "page_number") or 0) for br in brs} - {0}
        )

        caption_obj = _get(tbl, "caption")
        caption = str(_get(caption_obj, "content") or "") if caption_obj else None

        table_html = _table_to_html(tbl, cells)
        tables.append(
            DiTable(
                table_index=t_idx,
                row_count=row_count,
                col_count=col_count,
                cells=cells,
                page_numbers=page_nums,
                caption=caption or None,
                html=table_html,
            )
        )

    # ---- figures ----
    figures_raw: list[Any] = list(_get(di_result, "figures") or [])
    figures: list[DiFigure] = []
    for f_idx, fig in enumerate(figures_raw):
        polygons = _parse_bounding_regions(fig, page_geos)
        brs_f: list[Any] = list(_get(fig, "bounding_regions") or [])
        page_nums_f: list[int] = sorted(
            {int(_get(br, "page_number") or 0) for br in brs_f} - {0}
        )
        caption_obj_f = _get(fig, "caption")
        caption_f = str(_get(caption_obj_f, "content") or "") if caption_obj_f else None
        figures.append(
            DiFigure(
                figure_index=f_idx,
                page_numbers=page_nums_f,
                polygons=polygons,
                caption=caption_f or None,
            )
        )

    return DiNormalizedLayout(
        pages=pages,
        tables=tables,
        figures=figures,
        raw_content=raw_content,
        content_hash=compute_content_hash(raw_content),
    )
