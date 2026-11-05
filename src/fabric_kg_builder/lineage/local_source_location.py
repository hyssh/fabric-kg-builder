"""Original-PDF geometry from already authorized, hash-verified local bytes."""

from __future__ import annotations

from typing import Any


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
        for index, table in enumerate(page.find_tables().tables):
            values = table.extract()
            result["context_tables"].append({
                "local_table_index": index, "bbox": list(table.bbox),
                "row_count": table.row_count, "column_count": table.col_count,
                "indexing": "zero-based; includes header",
                "cells": [{
                    "row_index": row_index, "column_index": column_index,
                    "bbox": list(bbox) if bbox is not None else None,
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
