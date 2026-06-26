"""Azure Document Intelligence integration — OCR and polygon extraction.

SPEC-004 §8 split
-----------------
- **This module (Document Intelligence):** OCR text, bounding polygons, page
  geometry.  Populates :class:`~fabric_kg_builder.model.schemas.VisualRegionRow`
  ``polygon_json``, ``normalized_polygon_json``, ``text``, ``confidence``.
- **docintel.py does NOT** assign semantic labels or entity links — that is the
  vision LLM's job (via ``foundry_client``).
- **Vision LLM:** semantic ``label``, ``identified_entity_id``, ``region_type``
  classification beyond ``ocr_text``/``table_region``.

Mockability
-----------
Inject ``_di_client`` at construction time::

    from tests.conftest import make_document_intelligence_client
    client = DocIntelClient(config, _di_client=make_document_intelligence_client())

The mock must satisfy::

    _di_client.begin_analyze_document(...).result()
        -> obj with .pages, .paragraphs, .figures, .content attributes
           (or dict with those keys — both forms are handled)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..config.schema import DocumentIntelligenceConfig
from ..model.ids import make_visual_region_id
from ..model.schemas import VisualRegionRow


# ---------------------------------------------------------------------------
# Intermediate geometry types
# ---------------------------------------------------------------------------


@dataclass
class PageGeometry:
    """Page dimensions from a Document Intelligence response page entry."""

    page_number: int
    width: float
    height: float
    unit: str  # "pixel" | "inch"


@dataclass
class DocIntelResult:
    """Structured output from a Document Intelligence analysis pass."""

    visual_regions: list[VisualRegionRow]
    page_geometries: list[PageGeometry]
    raw_content: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get(obj: Any, key: str) -> Any:
    """Get attribute or dict key from *obj* (handles both dict and object forms)."""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _polygon_to_pairs(polygon: list[float]) -> list[list[float]]:
    """Convert flat ``[x0, y0, x1, y1, …]`` list to ``[[x, y], …]`` pairs."""
    return [[polygon[i], polygon[i + 1]] for i in range(0, len(polygon) - 1, 2)]


def _normalize_polygon(
    polygon: list[float],
    page_width: float,
    page_height: float,
) -> list[list[float]]:
    """Normalize pixel coordinates to ``[0.0, 1.0]`` relative to page dimensions."""
    if not polygon or page_width <= 0 or page_height <= 0:
        return []
    return [
        [
            round(polygon[i] / page_width, 6),
            round(polygon[i + 1] / page_height, 6),
        ]
        for i in range(0, len(polygon) - 1, 2)
    ]


def _build_page_geometry_map(pages: list[Any]) -> dict[int, PageGeometry]:
    """Build a ``page_number → PageGeometry`` lookup from a DI pages list."""
    result: dict[int, PageGeometry] = {}
    for page in pages:
        pn = int(_get(page, "page_number") or 0)
        width = float(_get(page, "width") or 0.0)
        height = float(_get(page, "height") or 0.0)
        unit = str(_get(page, "unit") or "pixel")
        if pn:
            result[pn] = PageGeometry(page_number=pn, width=width, height=height, unit=unit)
    return result


# ---------------------------------------------------------------------------
# Core mapping function (pure — no SDK dependency)
# ---------------------------------------------------------------------------


def map_di_result_to_visual_regions(
    di_result: Any,
    image_id: str,
    *,
    now: datetime | None = None,
) -> DocIntelResult:
    """Map a Document Intelligence result to :class:`VisualRegionRow` records.

    This is a **pure mapping function** — it takes the DI result object (real or
    mocked) and converts it to canonical row-model objects.  No SDK calls are made
    here.

    SPEC-002 §3.9.1 provenance:
    - ``polygon_json`` ← DI ``boundingPolygon`` pixel coords (JSON-encoded ``[[x,y],…]``).
    - ``normalized_polygon_json`` ← same coords divided by page ``width``/``height``.
    - ``text`` ← DI ``content`` field on paragraphs / words.
    - ``label``, ``identified_entity_id`` ← left ``None`` (vision LLM responsibility).

    Parameters
    ----------
    di_result:
        Result from ``begin_analyze_document(...).result()``.  May be a real Azure
        SDK ``AnalyzeResult`` object or a ``MagicMock`` satisfying the same
        attribute contract (see conftest ``make_document_intelligence_client``).
    image_id:
        Stable ``image_id`` for the parent visual asset (FK in visual_regions).
    now:
        UTC timestamp for ``created_at`` (injectable for tests).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    pages_raw: list[Any] = list(_get(di_result, "pages") or [])
    paragraphs_raw: list[Any] = list(_get(di_result, "paragraphs") or [])
    raw_content: str = str(_get(di_result, "content") or "")

    page_geos = _build_page_geometry_map(pages_raw)
    visual_regions: list[VisualRegionRow] = []
    sort_idx = 0

    for para in paragraphs_raw:
        content = str(_get(para, "content") or "")
        bounding_regions: list[Any] = list(_get(para, "bounding_regions") or [])
        confidence_raw = _get(para, "confidence")
        confidence: float | None = float(confidence_raw) if confidence_raw is not None else None

        for br in bounding_regions:
            pn = int(_get(br, "page_number") or 1)
            polygon: list[float] = list(_get(br, "polygon") or [])

            geo = page_geos.get(pn)
            polygon_pairs = _polygon_to_pairs(polygon) if polygon else []
            polygon_json = json.dumps(polygon_pairs) if polygon_pairs else None

            norm_pairs = (
                _normalize_polygon(polygon, geo.width, geo.height)
                if geo and polygon
                else []
            )
            normalized_polygon_json = json.dumps(norm_pairs) if norm_pairs else None

            region_id = make_visual_region_id(image_id, "ocr_text", None, sort_idx)
            visual_regions.append(
                VisualRegionRow(
                    visual_region_id=region_id,
                    image_id=image_id,
                    region_type="ocr_text",
                    text=content or None,
                    polygon_json=polygon_json,
                    normalized_polygon_json=normalized_polygon_json,
                    confidence=confidence,
                    created_at=now,
                )
            )
            sort_idx += 1

    return DocIntelResult(
        visual_regions=visual_regions,
        page_geometries=list(page_geos.values()),
        raw_content=raw_content,
    )


# ---------------------------------------------------------------------------
# DocIntelClient wrapper
# ---------------------------------------------------------------------------


class DocIntelClient:
    """Thin wrapper around Azure AI Document Intelligence SDK.

    Uses the Layout model by default to extract OCR text + bounding polygons.
    Produces :class:`VisualRegionRow` records per SPEC-002 §3.9.

    Vision LLM semantic enrichment is **not** performed here (SPEC-004 §8):
    ``label``, ``identified_entity_id``, and non-structural ``region_type``
    values are assigned by a separate vision LLM pass.

    Parameters
    ----------
    config:
        :class:`~fabric_kg_builder.config.schema.DocumentIntelligenceConfig`
        with ``endpoint`` (non-secret).
    _di_client:
        Optional pre-built ``DocumentIntelligenceClient`` for testing.
    """

    def __init__(
        self,
        config: DocumentIntelligenceConfig,
        *,
        _di_client: Any = None,
    ) -> None:
        self._config = config
        self._client = (
            _di_client if _di_client is not None else self._build_client(config)
        )

    @staticmethod
    def _build_client(config: DocumentIntelligenceConfig) -> Any:
        """Build the Document Intelligence client using DefaultAzureCredential."""
        try:
            from azure.ai.documentintelligence import (  # type: ignore[import]
                DocumentIntelligenceClient,
            )
        except ImportError as exc:
            raise ImportError(
                "azure-ai-documentintelligence is required for live DI calls. "
                "Install it with: pip install azure-ai-documentintelligence"
            ) from exc

        api_key = os.environ.get("AZURE_DOCINTEL_API_KEY")
        if api_key:
            from azure.core.credentials import AzureKeyCredential  # type: ignore[import]
            credential: Any = AzureKeyCredential(api_key)
        else:
            from azure.identity import DefaultAzureCredential  # type: ignore[import]
            credential = DefaultAzureCredential()

        return DocumentIntelligenceClient(
            endpoint=config.endpoint,
            credential=credential,
        )

    def layout_analyze_raw(
        self,
        data: bytes,
        *,
        model_id: str = "prebuilt-layout",
        output_content_format: str = "markdown",
    ) -> Any:
        """Run Layout analysis and return the raw DI AnalyzeResult (not processed).

        Unlike :meth:`analyze_document_bytes`, this method returns the raw SDK
        result object without mapping it to :class:`VisualRegionRow` records.
        Feed the result directly to :func:`~fabric_kg_builder.enrichment.docintel_tables.extract_tables`
        to obtain table document_elements and chunks.

        Parameters
        ----------
        data:
            Raw document bytes (e.g. PDF).
        model_id:
            Document Intelligence model (default: ``prebuilt-layout``).
        output_content_format:
            DI output format; ``"markdown"`` gives full-document Markdown with
            tables rendered as HTML ``<table>`` elements (recommended).
        """
        kwargs: dict[str, Any] = {
            "model_id": model_id,
            "body": data,
            "content_type": "application/octet-stream",
            "output_content_format": output_content_format,
        }
        poller = self._client.begin_analyze_document(**kwargs)
        return poller.result()

    def analyze_document_bytes(
        self,
        data: bytes,
        image_id: str,
        *,
        model_id: str = "prebuilt-layout",
        output_content_format: str | None = None,
        now: datetime | None = None,
    ) -> DocIntelResult:
        """Run Layout analysis on raw bytes and return :class:`DocIntelResult`.

        Parameters
        ----------
        data:
            Raw document or image bytes.
        image_id:
            Stable ``image_id`` of the parent visual asset (FK → visual_assets).
        model_id:
            Document Intelligence model to use (default: ``prebuilt-layout``).
        output_content_format:
            Optional content format for the DI result.  Pass ``"markdown"`` to
            request Markdown output (``analyze_result.content`` will be the full
            document in Markdown with tables as HTML ``<table>`` elements).
            Verify parameter name against the current azure-ai-documentintelligence
            SDK version — the kwarg is ``output_content_format`` in SDK ≥ 1.0.
        now:
            UTC timestamp (injectable for tests).
        """
        kwargs: dict[str, Any] = {
            "model_id": model_id,
            "body": data,
            "content_type": "application/octet-stream",
        }
        if output_content_format is not None:
            kwargs["output_content_format"] = output_content_format
        poller = self._client.begin_analyze_document(**kwargs)
        result = poller.result()
        return map_di_result_to_visual_regions(result, image_id, now=now)

    def analyze_document_url(
        self,
        url: str,
        image_id: str,
        *,
        model_id: str = "prebuilt-layout",
        output_content_format: str | None = None,
        now: datetime | None = None,
    ) -> DocIntelResult:
        """Run Layout analysis on a document URL and return :class:`DocIntelResult`.

        Parameters
        ----------
        url:
            Publicly accessible document URL (e.g., a Blob Storage SAS URL).
        image_id:
            Stable ``image_id`` of the parent visual asset.
        model_id:
            Document Intelligence model to use (default: ``prebuilt-layout``).
        output_content_format:
            Optional content format.  Pass ``"markdown"`` to request Markdown
            output from the Layout model.  Verify against the current
            azure-ai-documentintelligence SDK version.
        now:
            UTC timestamp (injectable for tests).
        """
        kwargs: dict[str, Any] = {
            "model_id": model_id,
            "body": {"urlSource": url},
        }
        if output_content_format is not None:
            kwargs["output_content_format"] = output_content_format
        poller = self._client.begin_analyze_document(**kwargs)
        result = poller.result()
        return map_di_result_to_visual_regions(result, image_id, now=now)
