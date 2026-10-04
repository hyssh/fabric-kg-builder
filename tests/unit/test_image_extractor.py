"""Unit tests for fabric_kg_builder.enrichment.image_extractor.

Tests:
- extract_visual_assets returns VisualAssetCandidate records from mocked pdfplumber.
- Deduplication by image hash across pages.
- make_visual_asset_row assembles a VisualAssetRow with stable image_id.
- blob_url=None sets is_placeholder=True; blob_url set clears placeholder.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fabric_kg_builder.enrichment.image_extractor import (
    VisualAssetCandidate,
    _compute_image_hash,
    extract_visual_assets,
    make_visual_asset_row,
)
from fabric_kg_builder.model.ids import make_image_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SOURCE_FILE_ID = "src:test_img_abc123"
_NOW = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)

_IMG_BYTES_A = b"fake_png_image_bytes_a"
_IMG_BYTES_B = b"fake_png_image_bytes_b"
_HASH_A = hashlib.sha256(_IMG_BYTES_A).hexdigest()
_HASH_B = hashlib.sha256(_IMG_BYTES_B).hexdigest()


def _make_mock_pdf(pages_images: list[list[dict]]) -> MagicMock:
    """Build a pdfplumber mock with the given per-page image lists."""
    mock_pages = []
    for imgs in pages_images:
        page = MagicMock()
        page.images = imgs
        mock_pages.append(page)

    mock_pdf = MagicMock()
    mock_pdf.pages = mock_pages
    mock_pdf.__enter__ = lambda s: mock_pdf
    mock_pdf.__exit__ = MagicMock(return_value=False)
    return mock_pdf


def _img_entry(data: bytes, x0: float = 100.0, top: float = 200.0) -> dict:
    return {
        "stream": data,
        "x0": x0,
        "top": top,
        "x1": x0 + 100.0,
        "bottom": top + 100.0,
        "width": 100,
        "height": 100,
    }


# ---------------------------------------------------------------------------
# Tests: _compute_image_hash
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_compute_image_hash_is_sha256():
    result = _compute_image_hash(b"hello")
    expected = hashlib.sha256(b"hello").hexdigest()
    assert result == expected


@pytest.mark.unit
def test_compute_image_hash_different_for_different_bytes():
    assert _compute_image_hash(b"abc") != _compute_image_hash(b"xyz")


# ---------------------------------------------------------------------------
# Tests: extract_visual_assets
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_extract_returns_candidates_from_single_page():
    mock_pdf = _make_mock_pdf([[_img_entry(_IMG_BYTES_A)]])
    with patch("fabric_kg_builder.enrichment.image_extractor.pdfplumber.open", return_value=mock_pdf):
        candidates = extract_visual_assets("fake.pdf", _SOURCE_FILE_ID)

    assert len(candidates) == 1
    c = candidates[0]
    assert c.image_bytes == _IMG_BYTES_A
    assert c.image_hash == _HASH_A
    assert c.page_number == 1


@pytest.mark.unit
def test_extract_returns_multiple_images():
    mock_pdf = _make_mock_pdf([[_img_entry(_IMG_BYTES_A), _img_entry(_IMG_BYTES_B, x0=300.0)]])
    with patch("fabric_kg_builder.enrichment.image_extractor.pdfplumber.open", return_value=mock_pdf):
        candidates = extract_visual_assets("fake.pdf", _SOURCE_FILE_ID)

    assert len(candidates) == 2
    hashes = {c.image_hash for c in candidates}
    assert _HASH_A in hashes
    assert _HASH_B in hashes


@pytest.mark.unit
def test_extract_deduplicates_identical_images_across_pages():
    """Same image bytes on two pages → only one candidate."""
    mock_pdf = _make_mock_pdf([
        [_img_entry(_IMG_BYTES_A)],   # page 1
        [_img_entry(_IMG_BYTES_A)],   # page 2 — duplicate
    ])
    with patch("fabric_kg_builder.enrichment.image_extractor.pdfplumber.open", return_value=mock_pdf):
        candidates = extract_visual_assets("fake.pdf", _SOURCE_FILE_ID)

    assert len(candidates) == 1


@pytest.mark.unit
def test_extract_returns_empty_when_no_images():
    mock_pdf = _make_mock_pdf([[]])  # page with no images
    with patch("fabric_kg_builder.enrichment.image_extractor.pdfplumber.open", return_value=mock_pdf):
        candidates = extract_visual_assets("fake.pdf", _SOURCE_FILE_ID)

    assert candidates == []


@pytest.mark.unit
def test_extract_skips_entries_with_no_stream():
    no_stream_entry = {"x0": 0, "y0": 0, "width": 50, "height": 50}  # no "stream"
    mock_pdf = _make_mock_pdf([[no_stream_entry]])
    with patch("fabric_kg_builder.enrichment.image_extractor.pdfplumber.open", return_value=mock_pdf):
        candidates = extract_visual_assets("fake.pdf", _SOURCE_FILE_ID)

    assert candidates == []


@pytest.mark.unit
def test_extract_captures_page_number():
    mock_pdf = _make_mock_pdf([
        [],                         # page 1 — empty
        [_img_entry(_IMG_BYTES_A)], # page 2 — has image
    ])
    with patch("fabric_kg_builder.enrichment.image_extractor.pdfplumber.open", return_value=mock_pdf):
        candidates = extract_visual_assets("fake.pdf", _SOURCE_FILE_ID)

    assert len(candidates) == 1
    assert candidates[0].page_number == 2


@pytest.mark.unit
def test_extract_captures_bbox():
    entry = _img_entry(_IMG_BYTES_A, x0=50.0, top=100.0)
    mock_pdf = _make_mock_pdf([[entry]])
    with patch("fabric_kg_builder.enrichment.image_extractor.pdfplumber.open", return_value=mock_pdf):
        candidates = extract_visual_assets("fake.pdf", _SOURCE_FILE_ID)

    c = candidates[0]
    assert c.bbox is not None
    assert c.bbox[0] == pytest.approx(50.0)
    assert c.bbox[1] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Tests: make_visual_asset_row
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_make_visual_asset_row_stable_image_id():
    candidate = VisualAssetCandidate(
        image_bytes=_IMG_BYTES_A,
        image_hash=_HASH_A,
        page_number=1,
        asset_type="figure",
    )
    row = make_visual_asset_row(candidate, _SOURCE_FILE_ID, now=_NOW)
    expected_id = make_image_id(_SOURCE_FILE_ID, _HASH_A)
    assert row.image_id == expected_id


@pytest.mark.unit
def test_make_visual_asset_row_with_blob_url():
    candidate = VisualAssetCandidate(
        image_bytes=_IMG_BYTES_A,
        image_hash=_HASH_A,
        page_number=1,
        asset_type="figure",
    )
    url = "https://fake.blob.core.windows.net/kg-assets/img123.png"
    row = make_visual_asset_row(candidate, _SOURCE_FILE_ID, blob_url=url, now=_NOW)

    assert row.blob_url == url
    assert row.is_placeholder is False


@pytest.mark.unit
def test_make_visual_asset_row_no_blob_url_is_placeholder():
    candidate = VisualAssetCandidate(
        image_bytes=_IMG_BYTES_A,
        image_hash=_HASH_A,
        page_number=2,
        asset_type="inline_image",
        width=640,
        height=480,
    )
    row = make_visual_asset_row(candidate, _SOURCE_FILE_ID, now=_NOW)

    assert row.blob_url is None
    assert row.is_placeholder is True


@pytest.mark.unit
def test_make_visual_asset_row_preserves_metadata():
    candidate = VisualAssetCandidate(
        image_bytes=_IMG_BYTES_B,
        image_hash=_HASH_B,
        page_number=3,
        asset_type="diagram",
        caption="Figure 1: Battery assembly",
        alt_text="Exploded view of battery",
        width=800,
        height=600,
        document_element_id="elem:xyz",
    )
    row = make_visual_asset_row(candidate, _SOURCE_FILE_ID, now=_NOW)

    assert row.asset_type == "diagram"
    assert row.page_number == 3
    assert row.caption == "Figure 1: Battery assembly"
    assert row.alt_text == "Exploded view of battery"
    assert row.width == 800
    assert row.height == 600
    assert row.document_element_id == "elem:xyz"
    assert row.source_file_id == _SOURCE_FILE_ID
    assert row.image_hash == _HASH_B
    assert row.created_at == _NOW
