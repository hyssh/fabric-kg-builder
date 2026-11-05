"""Original-PDF geometry from already authorized, hash-verified local bytes."""

from __future__ import annotations

from typing import Any

_RULE_TOLERANCE_POINTS = 0.01


def _native_rules(drawings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rules = []
    for drawing_index, drawing in enumerate(drawings):
        if drawing.get("type") not in {"s", "fs"}:
            continue
        for item_index, item in enumerate(drawing["items"]):
            if item[0] == "l":
                segments = [(item[1].x, item[1].y, item[2].x, item[2].y)]
            elif item[0] == "re":
                rect = item[1]
                segments = [
                    (rect.x0, rect.y0, rect.x1, rect.y0),
                    (rect.x1, rect.y0, rect.x1, rect.y1),
                    (rect.x1, rect.y1, rect.x0, rect.y1),
                    (rect.x0, rect.y1, rect.x0, rect.y0),
                ]
            else:
                continue
            for edge_index, (x0, y0, x1, y1) in enumerate(segments):
                if abs(y0 - y1) <= _RULE_TOLERANCE_POINTS and abs(x1 - x0) > _RULE_TOLERANCE_POINTS:
                    axis, position, start, end = "horizontal", y0, min(x0, x1), max(x0, x1)
                elif abs(x0 - x1) <= _RULE_TOLERANCE_POINTS and abs(y1 - y0) > _RULE_TOLERANCE_POINTS:
                    axis, position, start, end = "vertical", x0, min(y0, y1), max(y0, y1)
                else:
                    continue
                rules.append({
                    "axis": axis, "position": position, "start": start, "end": end,
                    "source_ref": {
                        "drawing_index": drawing_index, "item_index": item_index, "edge_index": edge_index,
                    },
                })
    return rules


def _rule_coverage(
    rules: list[dict[str, Any]], axis: str, position: float, start: float, end: float,
) -> list[dict[str, int]] | None:
    parts = sorted(
        (rule for rule in rules if rule["axis"] == axis
         and abs(rule["position"] - position) <= _RULE_TOLERANCE_POINTS),
        key=lambda rule: (rule["start"], rule["end"]),
    )
    reached, references = start, []
    for rule in parts:
        if rule["end"] < reached - _RULE_TOLERANCE_POINTS:
            continue
        if rule["start"] > reached + _RULE_TOLERANCE_POINTS:
            break
        reached = max(reached, rule["end"])
        references.append(rule["source_ref"])
        if reached >= end - _RULE_TOLERANCE_POINTS:
            return references
    return None


def _native_rule_enclosure(
    rules: list[dict[str, Any]], bbox: tuple[float, ...] | None,
) -> dict[str, Any]:
    """Prove a native rule enclosure, not a backend text-extent interpretation."""
    result: dict[str, Any] = {
        "native_rule_enclosure_bbox": None, "rule_boundary_status": "NOT_PROVEN",
        "rule_boundary_basis": "nearest_continuous_stroked_axis_aligned_enclosure",
        "rule_tolerance_points": _RULE_TOLERANCE_POINTS,
        "source_rule_refs": {},
        "semantic_cell_boundary_verified": False,
    }
    if bbox is None:
        return result
    x0, y0, x1, y1 = bbox
    if x1 - x0 <= _RULE_TOLERANCE_POINTS or y1 - y0 <= _RULE_TOLERANCE_POINTS:
        return result
    vertical = {
        rule["position"] for rule in rules if rule["axis"] == "vertical"
        and _rule_coverage(rules, "vertical", rule["position"], y0, y1) is not None
    }
    horizontal = {
        rule["position"] for rule in rules if rule["axis"] == "horizontal"
        and _rule_coverage(rules, "horizontal", rule["position"], x0, x1) is not None
    }
    sides = (
        [x for x in vertical if x <= x0 + _RULE_TOLERANCE_POINTS],
        [y for y in horizontal if y <= y0 + _RULE_TOLERANCE_POINTS],
        [x for x in vertical if x >= x1 - _RULE_TOLERANCE_POINTS],
        [y for y in horizontal if y >= y1 - _RULE_TOLERANCE_POINTS],
    )
    if not all(sides):
        return result
    left, top, right, bottom = max(sides[0]), max(sides[1]), min(sides[2]), min(sides[3])
    references = {
        "left": _rule_coverage(rules, "vertical", left, top, bottom),
        "top": _rule_coverage(rules, "horizontal", top, left, right),
        "right": _rule_coverage(rules, "vertical", right, top, bottom),
        "bottom": _rule_coverage(rules, "horizontal", bottom, left, right),
    }
    if all(value is not None for value in references.values()):
        result.update(
            native_rule_enclosure_bbox=[left, top, right, bottom],
            rule_boundary_status="NATIVE_RULE_ENCLOSURE",
            source_rule_refs=references,
        )
    return result


def pdf_source_location(
    raw: bytes, *, page_number: int | None, quote: str | None,
) -> dict[str, Any]:
    """Keep local text matches and page-context tables/images distinct from DI."""
    import pymupdf

    result: dict[str, Any] = {
        "backend": "pymupdf-local-original", "backend_version": pymupdf.VersionBind,
        "is_document_intelligence": False, "assertion_trust_promoted": False,
        "coordinate_system": "PDF points; top-left; [x0,y0,x1,y1]",
        "page_number_convention": "1-based original PDF page index",
        "text_match_method": "exact case-sensitive whitespace-token sequence",
        "table_bbox_semantics": "backend_detected_region; not guaranteed to be ruled cell boundaries",
        "text_matches": [], "context_tables": [], "context_figures": [],
    }
    if page_number is None:
        return result | {"status": "INCOMPLETE", "reason": "NO_ORIGINAL_PAGE_RECORDED"}
    with pymupdf.open(stream=raw, filetype="pdf") as document:
        if not 1 <= page_number <= len(document):
            return result | {"status": "INCOMPLETE", "reason": "PAGE_OUTSIDE_ORIGINAL"}
        page = document[page_number - 1]
        result.update(
            page_number=page_number,
            page_width=page.rect.width, page_height=page.rect.height,
        )
        words = page.get_text("words")
        tokens = [word[4] for word in words]
        phrase = quote.split() if quote else []
        if len(tokens) > 100_000 or len(phrase) > 4_000:
            return result | {"status": "INCOMPLETE", "reason": "LOCAL_TEXT_MATCH_LIMIT_EXCEEDED"}
        if phrase:
            for start in range(len(tokens) - len(phrase) + 1):
                if tokens[start:start + len(phrase)] != phrase:
                    continue
                lines: dict[tuple[int, int], Any] = {}
                for word in words[start:start + len(phrase)]:
                    key = (word[5], word[6])
                    rect = pymupdf.Rect(word[:4])
                    lines[key] = lines[key] | rect if key in lines else rect
                result["text_matches"].append({
                    "word_start": start, "word_end": start + len(phrase),
                    "line_bboxes": [list(rect) for rect in lines.values()],
                    "claim_to_region_verified": False,
                })
        count = len(result["text_matches"])
        result["status"] = (
            "PAGE_CONTEXT_ONLY" if not phrase else
            "UNIQUE_TEXT_MATCH" if count == 1 else "AMBIGUOUS_TEXT_MATCH" if count else "TEXT_NOT_FOUND"
        )
        rules = _native_rules(page.get_drawings())
        for index, table in enumerate(page.find_tables().tables):
            values = table.extract()
            result["context_tables"].append({
                "local_table_index": index, "bbox": list(table.bbox),
                "row_count": table.row_count, "column_count": table.col_count,
                "indexing": "zero-based; includes header",
                "cells": [{
                    "row_index": row_index, "column_index": column_index,
                    "bbox": list(bbox) if bbox is not None else None,
                    **_native_rule_enclosure(rules, bbox),
                    "extracted_value": values[row_index][column_index],
                    "merged_cell_extent": "not_inferred",
                } for row_index, row in enumerate(table.rows)
                    for column_index, bbox in enumerate(row.cells)],
                "claim_to_region_verified": False,
            })
        for index, image in enumerate(page.get_image_info(xrefs=True)):
            result["context_figures"].append({
                "local_image_index": index, "xref": image["xref"], "bbox": list(image["bbox"]),
                "meaning_scope": "embedded image occurrence only; excludes vector-only figures",
                "claim_to_region_verified": False,
            })
    return result
