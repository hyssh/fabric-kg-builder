"""Unit tests for visual_assets + visual_regions assembly (Sprint 2).

Tests:
- Full pipeline: extract_visual_assets -> make_visual_asset_row with blob_url.
- VisualAssetRow.image_id is a stable FK that VisualRegionRow.image_id references.
- DocIntelResult.visual_regions FK matches image_id from make_visual_asset_row.
- VisualRegionRow created_at is populated.
- Multiple visual_regions from one image all share the same image_id.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from fabric_kg_builder.enrichment.docintel import map_di_result_to_visual_regions
from fabric_kg_builder.enrichment.image_extractor import (
    VisualAssetCandidate,
    extract_visual_assets,
    make_visual_asset_row,
)
from fabric_kg_builder.model.ids import make_image_id
from fabric_kg_builder.model.schemas import VisualAssetRow, VisualRegionRow

from tests.conftest import make_blob_uploader, make_document_intelligence_client

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SOURCE_FILE_ID = "src:visual_assets_test_abc"
_NOW = datetime(2026, 6, 24, 14, 0, 0, tzinfo=timezone.utc)
_IMG_BYTES = b"dummy_image_bytes_for_visual_assets_test"
_IMG_HASH = hashlib.sha256(_IMG_BYTES).hexdigest()


# ---------------------------------------------------------------------------
# Tests: visual_assets assembly
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_visual_asset_row_image_id_is_stable():
    """image_id must be deterministic for same source_file_id + image_hash."""
    candidate = VisualAssetCandidate(
        image_bytes=_IMG_BYTES,
        image_hash=_IMG_HASH,
        page_number=1,
        asset_type="figure",
    )
    row1 = make_visual_asset_row(candidate, _SOURCE_FILE_ID, now=_NOW)
    row2 = make_visual_asset_row(candidate, _SOURCE_FILE_ID, now=_NOW)
    assert row1.image_id == row2.image_id
    assert row1.image_id == make_image_id(_SOURCE_FILE_ID, _IMG_HASH)


@pytest.mark.unit
def test_visual_asset_row_blob_url_populated_after_upload():
    """Full pipeline: candidate → upload mock → row with blob_url."""
    candidate = VisualAssetCandidate(
        image_bytes=_IMG_BYTES,
        image_hash=_IMG_HASH,
        page_number=1,
        asset_type="figure",
    )
    mock_uploader = make_blob_uploader()
    image_id = make_image_id(_SOURCE_FILE_ID, _IMG_HASH)

    blob_url = mock_uploader.upload(image_id, _IMG_BYTES, "png")
    row = make_visual_asset_row(candidate, _SOURCE_FILE_ID, blob_url=blob_url, now=_NOW)

    assert row.blob_url == blob_url
    assert row.is_placeholder is False
    assert row.image_id == image_id


# ---------------------------------------------------------------------------
# Tests: visual_regions FK to image_id
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_visual_regions_fk_matches_visual_asset_image_id():
    """visual_regions.image_id must match the image_id from visual_assets."""
    candidate = VisualAssetCandidate(
        image_bytes=_IMG_BYTES,
        image_hash=_IMG_HASH,
        page_number=1,
        asset_type="figure",
    )
    image_id = make_image_id(_SOURCE_FILE_ID, _IMG_HASH)
    asset_row = make_visual_asset_row(candidate, _SOURCE_FILE_ID, now=_NOW)

    # Build a DI result with the same image_id
    result_mock = MagicMock()
    result_mock.content = "Battery replacement"
    result_mock.pages = [{"page_number": 1, "width": 612.0, "height": 792.0, "unit": "pixel"}]
    result_mock.paragraphs = [
        {
            "content": "Battery replacement procedure.",
            "bounding_regions": [
                {"page_number": 1, "polygon": [72.0, 72.0, 400.0, 72.0, 400.0, 92.0, 72.0, 92.0]}
            ],
        }
    ]

    di_result = map_di_result_to_visual_regions(result_mock, image_id, now=_NOW)

    assert len(di_result.visual_regions) >= 1
    for region in di_result.visual_regions:
        assert region.image_id == asset_row.image_id


@pytest.mark.unit
def test_multiple_visual_regions_same_image_id():
    """All regions from one image must share the same image_id FK."""
    image_id = "img:shared_id_test"

    result_mock = MagicMock()
    result_mock.content = "Page content"
    result_mock.pages = [{"page_number": 1, "width": 612.0, "height": 792.0, "unit": "pixel"}]
    result_mock.paragraphs = [
        {
            "content": "Paragraph 1",
            "bounding_regions": [{"page_number": 1, "polygon": [0.0, 0.0, 10.0, 0.0, 10.0, 10.0, 0.0, 10.0]}],
        },
        {
            "content": "Paragraph 2",
            "bounding_regions": [{"page_number": 1, "polygon": [20.0, 20.0, 30.0, 20.0, 30.0, 30.0, 20.0, 30.0]}],
        },
    ]

    di_result = map_di_result_to_visual_regions(result_mock, image_id, now=_NOW)

    assert len(di_result.visual_regions) == 2
    image_ids = {r.image_id for r in di_result.visual_regions}
    assert image_ids == {image_id}


@pytest.mark.unit
def test_visual_region_rows_have_created_at():
    result_mock = MagicMock()
    result_mock.content = ""
    result_mock.pages = [{"page_number": 1, "width": 612.0, "height": 792.0, "unit": "pixel"}]
    result_mock.paragraphs = [
        {
            "content": "Text",
            "bounding_regions": [{"page_number": 1, "polygon": [0.0, 0.0, 5.0, 0.0, 5.0, 5.0, 0.0, 5.0]}],
        }
    ]

    di_result = map_di_result_to_visual_regions(result_mock, "img:xyz", now=_NOW)
    assert di_result.visual_regions[0].created_at == _NOW


@pytest.mark.unit
def test_visual_regions_via_conftest_di_fixture():
    """End-to-end: conftest DI fixture → map → FK check."""
    mock_di = make_document_intelligence_client()
    raw_result = mock_di.begin_analyze_document().result()

    image_id = "img:conftest_fixture_test"
    di_result = map_di_result_to_visual_regions(raw_result, image_id, now=_NOW)

    assert len(di_result.visual_regions) >= 1
    for region in di_result.visual_regions:
        assert region.image_id == image_id
        assert isinstance(region, VisualRegionRow)


# ---------------------------------------------------------------------------
# Tests: extract + assemble pipeline with mock pdfplumber
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_extract_and_assemble_visual_asset_row():
    """extract_visual_assets -> make_visual_asset_row -> correct image_id."""
    img_entry = {
        "stream": _IMG_BYTES,
        "x0": 100.0, "top": 200.0, "x1": 300.0, "bottom": 400.0,
        "width": 200, "height": 200,
    }
    mock_page = MagicMock()
    mock_page.images = [img_entry]
    mock_pdf = MagicMock()
    mock_pdf.pages = [mock_page]
    mock_pdf.__enter__ = lambda s: mock_pdf
    mock_pdf.__exit__ = MagicMock(return_value=False)

    with patch("fabric_kg_builder.enrichment.image_extractor.pdfplumber.open", return_value=mock_pdf):
        candidates = extract_visual_assets("fake.pdf", _SOURCE_FILE_ID)

    assert len(candidates) == 1
    candidate = candidates[0]

    mock_uploader = make_blob_uploader()
    image_id = make_image_id(_SOURCE_FILE_ID, candidate.image_hash)
    blob_url = mock_uploader.upload(image_id, candidate.image_bytes, "png")

    row = make_visual_asset_row(candidate, _SOURCE_FILE_ID, blob_url=blob_url, now=_NOW)

    assert row.image_id == image_id
    assert row.blob_url == blob_url
    assert row.source_file_id == _SOURCE_FILE_ID
    assert row.is_placeholder is False
