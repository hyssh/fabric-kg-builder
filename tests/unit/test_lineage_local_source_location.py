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
    assert cell["bbox"] == [50, 190, 150, 210]
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
