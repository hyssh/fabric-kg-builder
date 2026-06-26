"""Image/figure extractor for PDF sources.

Two extraction modes
--------------------
1. **DI-based figure extraction** (``extract_figures_from_di``): given a PDF path and
   a Document Intelligence Layout result (with ``.figures`` carrying ``bounding_regions``
   + optional ``caption``), renders precise crops via PyMuPDF.  This is the primary path
   for structured PDFs where DI has already run.
2. **pdfplumber inline-image extraction** (``extract_visual_assets``): falls back to
   extracting embedded image streams when DI is not available.

Document Intelligence split (SPEC-004 §8)
------------------------------------------
- **This module**: physical extraction — bytes, hashes, page position, bounding box.
- **docintel.py**: OCR text, bounding polygons, page geometry (Document Intelligence).
- **Vision LLM** (foundry_client): semantic labels and entity linking.

Mockability
-----------
``extract_visual_assets`` internally calls ``pdfplumber.open``.  For tests, patch
``pdfplumber.open`` with a MagicMock that returns pages with a ``.images`` attribute::

    with patch("fabric_kg_builder.enrichment.image_extractor.pdfplumber.open", return_value=mock_pdf):
        candidates = extract_visual_assets("fake.pdf", "src:abc")

``extract_figures_from_di`` accepts a ``_fitz_open`` keyword argument for test injection::

    candidates = extract_figures_from_di("fake.pdf", di_result, "src:abc", _fitz_open=mock_open)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pdfplumber

from ..model.ids import make_image_id, make_visual_region_id
from ..model.schemas import VisualAssetRow, VisualRegionRow

# Zoom factor matching the reference implementation: ~200 DPI for legible figure crops.
_FIGURE_ZOOM = 200 / 72


# ---------------------------------------------------------------------------
# Intermediate result type
# ---------------------------------------------------------------------------


@dataclass
class VisualAssetCandidate:
    """Intermediate result from image extraction before Blob upload.

    Produced by :func:`extract_visual_assets`.  Passed to
    :func:`make_visual_asset_row` after the Blob upload returns a URL.
    """

    image_bytes: bytes
    image_hash: str  # SHA-256 hex digest of image_bytes
    page_number: int
    asset_type: str  # figure | inline_image | diagram | photo | chart | table_image
    caption: str | None = None
    alt_text: str | None = None
    width: int | None = None
    height: int | None = None
    document_element_id: str | None = None
    bbox: tuple[float, float, float, float] | None = None  # x0, top, x1, bottom
    source_index: int = 0  # sequential index on the page (for disambiguation)
    polygon: list[float] = field(default_factory=list)  # raw DI bounding polygon (inches)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compute_image_hash(data: bytes) -> str:
    """Return SHA-256 hex digest of *data*."""
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# PyMuPDF (fitz) figure-crop helpers — DI-based extraction
# ---------------------------------------------------------------------------


def _polygon_to_rect(polygon: list[float]) -> Any:
    """Convert a DI flat polygon (inches) to a ``fitz.Rect`` (points = inches × 72).

    DI Layout returns bounding polygons as a flat ``[x0,y0, x1,y1, …]`` list in
    **inches**.  PyMuPDF uses **points** (1 pt = 1/72 inch), so each coordinate
    is multiplied by 72.
    """
    import fitz  # lazy import — fitz is an optional dependency

    xs = polygon[0::2]
    ys = polygon[1::2]
    return fitz.Rect(min(xs) * 72, min(ys) * 72, max(xs) * 72, max(ys) * 72)


def _render_figure_crop(
    fitz_doc: Any,
    page_number: int,
    polygon: list[float],
) -> tuple[bytes, int, int] | None:
    """Render a figure crop from a PyMuPDF document page.

    Parameters
    ----------
    fitz_doc:
        An open ``fitz.Document`` (or a mock satisfying the same interface).
    page_number:
        1-based page number from the DI bounding region.
    polygon:
        Flat DI polygon ``[x0,y0,…]`` in inches.

    Returns
    -------
    tuple[bytes, int, int] | None
        ``(png_bytes, width_px, height_px)`` or ``None`` if the page is out of range.
    """
    import fitz  # lazy import

    page_idx = page_number - 1
    if page_idx < 0 or page_idx >= fitz_doc.page_count:
        return None
    page = fitz_doc.load_page(page_idx)
    rect = _polygon_to_rect(polygon)
    pix = page.get_pixmap(
        matrix=fitz.Matrix(_FIGURE_ZOOM, _FIGURE_ZOOM),
        clip=rect,
        alpha=False,
    )
    return pix.tobytes("png"), pix.width, pix.height


def _extract_caption(fig: Any) -> str | None:
    """Pull caption text from a DI figure dict or object."""
    from .docintel import _get  # local import to avoid circular dependency at module init

    caption_obj = _get(fig, "caption")
    if caption_obj is None:
        return None
    content = _get(caption_obj, "content")
    return str(content).strip() if content else None


def extract_figures_from_di(
    pdf_path: str | Path,
    di_analyze_result: Any,
    source_file_id: str,
    *,
    _fitz_open: Callable | None = None,
) -> list[VisualAssetCandidate]:
    """Extract figure crops from a PDF using DI Layout bounding regions.

    For each figure in ``di_analyze_result.figures``:
    - Reads ``bounding_regions[0].page_number`` and ``.polygon`` (inches).
    - Renders a crop via PyMuPDF at ~200 DPI using ``_render_figure_crop``.
    - Deduplicates by SHA-256 of the PNG bytes.

    Parameters
    ----------
    pdf_path:
        Path to the source PDF file (opened by PyMuPDF).
    di_analyze_result:
        Raw DI ``AnalyzeResult`` (real or mock) exposing ``.figures``, ``.pages``.
    source_file_id:
        Stable source-file identifier — used in downstream ID generation.
    _fitz_open:
        Optional injectable replacement for ``fitz.open`` — used in unit tests
        to avoid actual PDF rendering.  Signature: ``(path_str) -> context manager``.

    Returns
    -------
    list[VisualAssetCandidate]
        One candidate per unique figure crop (deduplicated by image hash).
    """
    from .docintel import _get  # local import

    if _fitz_open is None:
        import fitz as _fitz_mod
        _open_fn: Callable = _fitz_mod.open
    else:
        _open_fn = _fitz_open

    path = Path(pdf_path)
    candidates: list[VisualAssetCandidate] = []
    seen_hashes: set[str] = set()

    figures_raw = list(_get(di_analyze_result, "figures") or [])
    if not figures_raw:
        return candidates

    with _open_fn(str(path)) as fdoc:
        for idx, fig in enumerate(figures_raw):
            caption = _extract_caption(fig)
            bounding_regions = list(_get(fig, "bounding_regions") or [])
            if not bounding_regions:
                continue
            region = bounding_regions[0]
            page_number = int(_get(region, "page_number") or 1)
            polygon = list(_get(region, "polygon") or [])
            if len(polygon) < 4:
                continue

            result = _render_figure_crop(fdoc, page_number, polygon)
            if result is None:
                continue
            crop_bytes, width, height = result

            img_hash = _compute_image_hash(crop_bytes)
            if img_hash in seen_hashes:
                continue
            seen_hashes.add(img_hash)

            candidates.append(
                VisualAssetCandidate(
                    image_bytes=crop_bytes,
                    image_hash=img_hash,
                    page_number=page_number,
                    asset_type="figure",
                    caption=caption,
                    width=width,
                    height=height,
                    source_index=idx,
                    polygon=polygon,
                )
            )

    return candidates


def make_visual_regions_for_figure(
    image_id: str,
    candidate: VisualAssetCandidate,
    di_analyze_result: Any,
    *,
    blob_url: str | None = None,
    now: datetime | None = None,
) -> list[VisualRegionRow]:
    """Build a :class:`VisualRegionRow` for a figure's bounding polygon.

    Produces exactly one region per figure with ``region_type="figure_region"``,
    ensuring the ``visual_regions`` table is non-empty and FKs resolve against
    the parent ``visual_assets`` row.

    Parameters
    ----------
    image_id:
        Stable ``image_id`` of the parent visual asset (FK).
    candidate:
        The :class:`VisualAssetCandidate` produced by :func:`extract_figures_from_di`.
        Its ``polygon`` field holds the raw DI bounding polygon (inches).
    di_analyze_result:
        DI result used to look up page geometry for polygon normalisation.
    blob_url:
        The blob URL of the parent visual asset (inherited for provenance).
    now:
        UTC timestamp (injectable for tests).
    """
    from .docintel import _get, _polygon_to_pairs, _normalize_polygon, _build_page_geometry_map

    if now is None:
        now = datetime.now(timezone.utc)

    polygon = candidate.polygon

    # Build page-geometry map for normalisation.
    pages_raw = list(_get(di_analyze_result, "pages") or [])
    page_geos = _build_page_geometry_map(pages_raw)
    geo = page_geos.get(candidate.page_number)

    polygon_pairs = _polygon_to_pairs(polygon) if polygon else []
    polygon_json = json.dumps(polygon_pairs) if polygon_pairs else None

    norm_pairs = (
        _normalize_polygon(polygon, geo.width, geo.height)
        if geo and polygon
        else []
    )
    normalized_polygon_json = json.dumps(norm_pairs) if norm_pairs else None

    region_id = make_visual_region_id(image_id, "figure_region", None, candidate.source_index)

    return [
        VisualRegionRow(
            visual_region_id=region_id,
            image_id=image_id,
            region_type="figure_region",
            text=candidate.caption,
            polygon_json=polygon_json,
            normalized_polygon_json=normalized_polygon_json,
            blob_url=blob_url,
            confidence=1.0,
            created_at=now,
        )
    ]


def _extract_raw_bytes(img_entry: Any) -> bytes | None:
    """Attempt to extract raw image bytes from a pdfplumber image dict entry."""
    stream = img_entry.get("stream") if isinstance(img_entry, dict) else getattr(img_entry, "stream", None)
    if stream is None:
        return None
    if hasattr(stream, "rawdata"):
        data = stream.rawdata
    elif hasattr(stream, "read"):
        data = stream.read()
    elif isinstance(stream, (bytes, bytearray)):
        data = bytes(stream)
    else:
        try:
            data = bytes(stream)
        except Exception:
            return None
    return data if data else None


# ---------------------------------------------------------------------------
# Core extraction function
# ---------------------------------------------------------------------------


def extract_visual_assets(
    path: str | Path,
    source_file_id: str,
) -> list[VisualAssetCandidate]:
    """Extract embedded images from a PDF using pdfplumber.

    Replaces the placeholder ``extract_images`` stub in ``pdf_extractor.py`` with
    real extraction logic.  Images are deduplicated by content hash across the entire
    document.

    Parameters
    ----------
    path:
        Path to the PDF file.
    source_file_id:
        Stable source file ID (used for image_id generation downstream).

    Returns
    -------
    list[VisualAssetCandidate]
        One candidate per unique embedded image (deduplicated by SHA-256 hash of
        image bytes).  Empty if the PDF has no embedded images or if pdfplumber
        cannot extract them.
    """
    path = Path(path)
    candidates: list[VisualAssetCandidate] = []
    seen_hashes: set[str] = set()

    with pdfplumber.open(path) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            images = page.images or []
            for idx, img in enumerate(images):
                data = _extract_raw_bytes(img if isinstance(img, dict) else {"stream": img})
                if not data:
                    continue

                img_hash = _compute_image_hash(data)
                if img_hash in seen_hashes:
                    continue
                seen_hashes.add(img_hash)

                # Dimensions — prefer display size (width/height in points)
                if isinstance(img, dict):
                    width = img.get("width") or (img.get("srcsize", [None])[0] if img.get("srcsize") else None)
                    height_raw = img.get("height") or (
                        img.get("srcsize", [None, None])[1] if img.get("srcsize") else None
                    )
                    x0 = img.get("x0")
                    top = img.get("top")
                    x1 = img.get("x1")
                    bottom = img.get("bottom")
                else:
                    width = getattr(img, "width", None)
                    height_raw = getattr(img, "height", None)
                    x0 = getattr(img, "x0", None)
                    top = getattr(img, "top", None)
                    x1 = getattr(img, "x1", None)
                    bottom = getattr(img, "bottom", None)

                bbox = (
                    (float(x0), float(top), float(x1), float(bottom))
                    if all(v is not None for v in (x0, top, x1, bottom))
                    else None
                )

                candidates.append(
                    VisualAssetCandidate(
                        image_bytes=data,
                        image_hash=img_hash,
                        page_number=page_number,
                        asset_type="inline_image",
                        width=int(width) if width is not None else None,
                        height=int(height_raw) if height_raw is not None else None,
                        bbox=bbox,
                        source_index=idx,
                    )
                )

    return candidates


# ---------------------------------------------------------------------------
# Row assembler — called after Blob upload
# ---------------------------------------------------------------------------


def make_visual_asset_row(
    candidate: VisualAssetCandidate,
    source_file_id: str,
    blob_url: str | None = None,
    *,
    now: datetime | None = None,
) -> VisualAssetRow:
    """Assemble a :class:`VisualAssetRow` from a candidate and optional *blob_url*.

    Called **after** :meth:`BlobUploader.upload` has returned the URL.  If
    ``blob_url`` is None (e.g. upload deferred), ``is_placeholder=True`` is set.

    Parameters
    ----------
    candidate:
        ``VisualAssetCandidate`` from :func:`extract_visual_assets`.
    source_file_id:
        Stable source file ID.
    blob_url:
        URL returned by :class:`BlobUploader` after successful upload.
    now:
        UTC timestamp (injectable for tests).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    image_id = make_image_id(source_file_id, candidate.image_hash)

    return VisualAssetRow(
        image_id=image_id,
        source_file_id=source_file_id,
        document_element_id=candidate.document_element_id,
        asset_type=candidate.asset_type,
        page_number=candidate.page_number,
        caption=candidate.caption,
        alt_text=candidate.alt_text,
        blob_url=blob_url,
        image_hash=candidate.image_hash,
        width=candidate.width,
        height=candidate.height,
        is_placeholder=(blob_url is None),
        created_at=now,
    )
