"""Read-only, bounded DI cache location bridge; never validates visual claims."""

from __future__ import annotations

import json
from typing import Any
import unicodedata

from fabric_kg_builder.contracts.evidence import EvidenceSpanV1_1, SourceUnit
from fabric_kg_builder.sources.docintel_cache import CachedLayout


def raw_layout_location(
    unit: SourceUnit, span: EvidenceSpanV1_1 | None,
    caches: list[CachedLayout], original_hash: str,
) -> dict[str, Any]:
    """Map unchanged Unicode page text back to raw spans and modality objects.

    Only the cache key already recorded by the reader binds a raw response.
    Page-context figures are not evidence that a claim refers to that figure.
    UTF-16/text-element indices and text-changing normalization stay unsupported.
    """
    result: dict[str, Any] = {
        "status": "UNAVAILABLE", "assertion_trust_promoted": False,
        "source_unit_locator": unit.locator.model_dump(mode="json"),
        "exact_regions": [], "page_context_regions": [],
    }
    key = unit.locator.native_object_id
    page_number = unit.locator.page
    if not key or page_number is None:
        result["reason"] = "NO_RECORDED_DI_CACHE_PAGE_BINDING"
        return result
    matches = [
        cached for cached in caches
        if cached.input_sha256 == original_hash
        and key == f"{cached.cache_key}/pages/{page_number}"
        and unit.locator.native_layer_id == "docintel-pages/1.0.0"
    ]
    if len(matches) != 1:
        result["reason"] = "CACHE_NOT_SUPPLIED_OR_BINDING_MISMATCH"
        return result
    cached = matches[0]
    raw = json.loads(cached.analyze_result_json)
    result.update(cache_key=cached.cache_key, response_hash=cached.response_hash)
    if raw.get("stringIndexType") != "unicodeCodePoint":
        result.update(status="UNSUPPORTED", reason="RAW_INDEX_CONVENTION_NOT_SUPPORTED")
        return result
    pages = [
        page for page in raw.get("pages", ())
        if isinstance(page, dict) and page.get("pageNumber") == page_number
    ]
    if len(pages) != 1:
        result.update(status="INCOMPLETE", reason="RAW_PAGE_NOT_UNIQUE")
        return result
    page = pages[0]
    content = raw["content"]

    def bounds(item: Any) -> tuple[int, int] | None:
        if not isinstance(item, dict):
            return None
        offset, length = item.get("offset"), item.get("length")
        if (
            type(offset) is not int or type(length) is not int
            or offset < 0 or length <= 0 or offset + length > len(content)
        ):
            return None
        return offset, offset + length

    page_spans = page.get("spans", ())
    page_bounds = bounds(page_spans[0]) if len(page_spans) == 1 else None
    if page_bounds is None:
        result.update(status="INCOMPLETE", reason="NONCONTIGUOUS_OR_INVALID_RAW_PAGE_SPAN")
        return result
    start, end = page_bounds
    text = content[start:end]
    if text.strip() != unit.text or unicodedata.normalize("NFC", text) != text:
        result.update(status="INCOMPLETE", reason="NORMALIZATION_OFFSET_MAP_NOT_RECORDED")
        return result
    offset = start + len(text) - len(text.lstrip())
    query = (
        (offset + span.span_start, offset + span.span_end)
        if span is not None else (offset, offset + len(unit.text))
    )
    result.update(
        status="EXACT_RAW_SPAN", raw_span={"offset": query[0], "length": query[1] - query[0]},
        indexing_convention="unicodeCodePoint",
        page_geometry={key: page.get(key) for key in ("pageNumber", "width", "height", "unit")},
        geometry_status="RECORDED" if all(page.get(key) is not None for key in (
            "width", "height", "unit",
        )) else "INCOMPLETE",
    )
    for collection, kind in (("tables", "table"), ("figures", "figure")):
        for index, obj in enumerate(raw.get(collection, ())):
            if not isinstance(obj, dict):
                continue
            objects = list(enumerate(obj.get("cells", ()))) if kind == "table" else [(None, obj)]
            for cell_index, region in objects:
                if not isinstance(region, dict):
                    continue
                regions = [
                    bounding for bounding in region.get("boundingRegions", ())
                    if isinstance(bounding, dict) and bounding.get("pageNumber") == page_number
                ]
                if not regions:
                    continue
                locator = {
                    "kind": "table_cell" if kind == "table" else "figure",
                    "raw_object_pointer": (
                        f"/{collection}/{index}/cells/{cell_index}" if kind == "table"
                        else f"/{collection}/{index}"
                    ),
                    "object_id": obj.get("id"), "bounding_regions": regions,
                    "spans": region.get("spans", []),
                    **{key: region.get(key) for key in (
                        "rowIndex", "columnIndex", "rowSpan", "columnSpan",
                    ) if key in region},
                    "claim_to_region_verified": False,
                }
                overlaps = any(
                    query[0] < item_end and item_start < query[1]
                    for item in region.get("spans", ())
                    for pair in (bounds(item),) if pair is not None
                    for item_start, item_end in (pair,)
                )
                # With no assertion span, these are only page-context locations.
                target = "exact_regions" if span is not None and overlaps else "page_context_regions"
                result[target].append(locator)
    return result
