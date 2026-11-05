from __future__ import annotations

import pymupdf
import pytest

from fabric_kg_builder.lineage.local_source_location import pdf_source_location


@pytest.fixture
def pdf_bytes():
    with pymupdf.open() as document:
        page = document.new_page(width=612, height=792)
        page.insert_text((50, 50), "Hardware demo", fontsize=12)
        page.insert_text((50, 90), "Update example", fontsize=12)
        for y in (150, 170, 190, 210):
            page.draw_line((50, y), (300, y))
        for x in (50, 150, 300):
            page.draw_line((x, 150), (x, 210))
        for y, left, right in ((163, "Version", "Date"), (183, "A", "First"), (203, "B", "Second")):
            page.insert_text((55, y), left, fontsize=9)
            page.insert_text((155, y), right, fontsize=9)
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 4, 4), False)
        pixmap.clear_with(128)
        page.insert_image(pymupdf.Rect(50, 250, 90, 290), stream=pixmap.tobytes("png"))
        yield document.tobytes()


def test_original_text_match_and_context_geometry(pdf_bytes):
    result = pdf_source_location(pdf_bytes, page_number=1, quote="Update example")
    assert result["status"] == "UNIQUE_TEXT_MATCH"
    assert result["is_document_intelligence"] is False
    assert result["assertion_trust_promoted"] is False
    assert result["page_width"] == 612 and result["page_height"] == 792
    assert len(result["text_matches"]) == 1
    cell = next(
        cell for table in result["context_tables"] for cell in table["cells"]
        if cell["row_index"] == 2 and cell["column_index"] == 0
    )
    assert cell["extracted_value"] == "B"
    assert cell["native_rule_enclosure_bbox"] == [50, 190, 150, 210]
    assert cell["rule_boundary_status"] == "NATIVE_RULE_ENCLOSURE"
    assert all(cell["source_rule_refs"][side] for side in ("left", "top", "right", "bottom"))
    assert result["context_figures"][0]["bbox"] == [50, 250, 90, 290]
    assert result["context_figures"][0]["claim_to_region_verified"] is False


def test_phrase_matching_is_exact_and_case_sensitive(pdf_bytes):
    result = pdf_source_location(pdf_bytes, page_number=1, quote="update example")
    assert result["status"] == "TEXT_NOT_FOUND"
    result = pdf_source_location(pdf_bytes, page_number=1, quote="Update\nexample")
    assert result["status"] == "UNIQUE_TEXT_MATCH"


@pytest.mark.parametrize("page_number", [None, 0, 2])
def test_missing_or_outside_page_remains_incomplete(pdf_bytes, page_number):
    result = pdf_source_location(pdf_bytes, page_number=page_number, quote="Hardware demo")
    assert result["status"] == "INCOMPLETE"


def test_page_context_without_a_claim(pdf_bytes):
    result = pdf_source_location(pdf_bytes, page_number=1, quote=None)
    assert result["status"] == "PAGE_CONTEXT_ONLY"
    assert not result["text_matches"]


def test_text_extent_recovers_original_grid_without_changing_backend_bbox(pdf_bytes):
    from types import SimpleNamespace

    # PyMuPDF 2.0 can return this tighter text region for the same drawn cell.
    detected = (55.0, 191.0, 149.0, 203.207)
    original_find = pymupdf.Page.find_tables

    def text_region_backend(page, **kwargs):
        found = original_find(page, **kwargs)
        tables = []
        for table in found.tables:
            rows = [SimpleNamespace(cells=list(row.cells)) for row in table.rows]
            rows[2].cells[0] = detected
            tables.append(SimpleNamespace(
                rows=rows, bbox=table.bbox, row_count=table.row_count, col_count=table.col_count,
                extract=lambda values=table.extract(): values,
            ))
        return SimpleNamespace(tables=tables)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(pymupdf.Page, "find_tables", text_region_backend)
        result = pdf_source_location(pdf_bytes, page_number=1, quote=None)
    cell = result["context_tables"][0]["cells"][4]
    assert cell["bbox"] == list(detected)
    assert cell["native_rule_enclosure_bbox"] == [50, 190, 150, 210]
    assert cell["semantic_cell_boundary_verified"] is False


@pytest.mark.parametrize("removed_side", ["left", "top", "right", "bottom"])
def test_open_or_partial_frame_does_not_promote_text_extent(removed_side):
    from fabric_kg_builder.lineage.local_source_location import _native_rules, _native_rule_enclosure

    edges = {
        "left": ((50, 190), (50, 210)), "top": ((50, 190), (150, 190)),
        "right": ((150, 190), (150, 210)), "bottom": ((50, 210), (150, 210)),
    }
    with pymupdf.open() as document:
        page = document.new_page()
        for side, (start, end) in edges.items():
            if side != removed_side:
                page.draw_line(start, end)
        location = _native_rule_enclosure(_native_rules(page.get_drawings()), (55, 191, 149, 203))
    assert location["native_rule_enclosure_bbox"] is None
    assert location["rule_boundary_status"] == "NOT_PROVEN"


def test_rule_segments_require_continuous_full_boundary():
    from fabric_kg_builder.lineage.local_source_location import _native_rules, _native_rule_enclosure

    with pymupdf.open() as document:
        page = document.new_page()
        page.draw_rect((50, 190, 150, 210))
        page.draw_line((50, 190), (100, 190))  # Duplicate partial strokes are harmless.
        location = _native_rule_enclosure(_native_rules(page.get_drawings()), (55, 191, 149, 203))
    assert location["native_rule_enclosure_bbox"] == [50, 190, 150, 210]


def test_gap_in_rule_is_not_closed_by_detected_text_bounds():
    from fabric_kg_builder.lineage.local_source_location import _native_rules, _native_rule_enclosure

    with pymupdf.open() as document:
        page = document.new_page()
        for start, end in (
            ((50, 190), (95, 190)), ((105, 190), (150, 190)),
            ((50, 190), (50, 210)), ((150, 190), (150, 210)), ((50, 210), (150, 210)),
        ):
            page.draw_line(start, end)
        location = _native_rule_enclosure(_native_rules(page.get_drawings()), (55, 191, 149, 203))
    assert location["native_rule_enclosure_bbox"] is None
    assert location["rule_boundary_status"] == "NOT_PROVEN"
