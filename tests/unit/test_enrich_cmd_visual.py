"""Unit tests: Visual assets + visual regions extraction wiring.

Covers the figure-extraction path introduced in Sprint 4:
  extract_figures_from_di → upload → canonical JSON visual_assets / visual_regions.

All external I/O is mocked:
- fitz (PyMuPDF)  via ``_fitz_open`` kwarg injection on extract_figures_from_di.
- Blob uploader   via ``make_blob_uploader()`` from conftest.
- DI Layout       via ``make_document_intelligence_client()`` from conftest
                  (the default fixture contains 1 figure with a bounding polygon
                  and caption "Battery assembly diagram").
- FoundryClient   via ``make_foundry_client()`` from conftest.

NO live calls are made in any test in this file.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.config.schema import DocumentIntelligenceConfig, FoundryConfig
from fabric_kg_builder.enrichment.docintel import DocIntelClient
from fabric_kg_builder.enrichment.foundry_client import FoundryClient
from fabric_kg_builder.enrichment.image_extractor import (
    VisualAssetCandidate,
    extract_figures_from_di,
    make_visual_regions_for_figure,
)
from fabric_kg_builder.model.ids import make_image_id
from fabric_kg_builder.model.schemas import VisualAssetRow, VisualRegionRow

from tests.conftest import (
    make_blob_uploader,
    make_document_intelligence_client,
    make_foundry_client,
)

# ---------------------------------------------------------------------------
# Constants / fixtures
# ---------------------------------------------------------------------------

_SOURCE_FILE_ID = "src:visual_test_abc123"
_NOW_STR = "2026-06-24T22:30:00+00:00"

# Fake PNG: just enough bytes to serve as image content in mocks.
_FAKE_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50

_DUMMY_FOUNDRY_CFG = FoundryConfig(endpoint="https://test.endpoint/")
_DUMMY_DI_CFG = DocumentIntelligenceConfig(
    endpoint="https://fake-di.cognitiveservices.azure.com/"
)

# Minimal valid PDF (one page, "Hello World") — same as used in DI table tests.
_MINIMAL_PDF = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj
4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
5 0 obj<</Length 44>>
stream
BT /F1 12 Tf 100 700 Td (Hello World) Tj ET
endstream
endobj
xref
0 6
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000266 00000 n 
0000000342 00000 n 
trailer<</Size 6/Root 1 0 R>>
startxref
436
%%EOF"""

_MOCK_LLM_OUTPUT = {
    "source_file_id": "src:test",
    "pass": "p2",
    "entities": [],
    "relationships": [],
    "chunks": [
        {
            "id_hint": "chunk:intro",
            "chunk_type": "section_text",
            "content": "Hello World is a Surface device reference.",
        }
    ],
    "visual_assets": [],
    "visual_regions": [],
    "evidence": [],
    "placeholder_suggestions": [],
}


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_fitz_open_mock(
    png_bytes: bytes = _FAKE_PNG_BYTES,
    width: int = 300,
    height: int = 250,
    page_count: int = 1,
) -> MagicMock:
    """Build a mock for fitz.open that returns a single-page document.

    The mock document yields a mock pixmap whose ``tobytes("png")`` returns
    *png_bytes* and whose ``width``/``height`` match the given values.
    """
    mock_pix = MagicMock()
    mock_pix.tobytes.return_value = png_bytes
    mock_pix.width = width
    mock_pix.height = height

    mock_page = MagicMock()
    mock_page.get_pixmap.return_value = mock_pix

    mock_doc = MagicMock()
    mock_doc.page_count = page_count
    mock_doc.load_page.return_value = mock_page
    mock_doc.__enter__ = lambda s: mock_doc
    mock_doc.__exit__ = MagicMock(return_value=False)

    return MagicMock(return_value=mock_doc)


def _make_di_result_with_figure(
    page_number: int = 1,
    polygon: list[float] | None = None,
    caption: str = "Battery assembly diagram",
) -> MagicMock:
    """Inline DI result mock with one figure — does not depend on fixture files."""
    if polygon is None:
        polygon = [1.0, 2.0, 4.0, 2.0, 4.0, 5.0, 1.0, 5.0]  # inches

    result = MagicMock()
    result.figures = [
        {
            "id": "fig:0",
            "caption": {"content": caption},
            "bounding_regions": [
                {"page_number": page_number, "polygon": polygon}
            ],
        }
    ]
    result.pages = [
        {"page_number": page_number, "width": 8.5, "height": 11.0, "unit": "inch"}
    ]
    result.content = "Test content"
    return result


def _make_foundry_client_from_dict(fixture: dict) -> FoundryClient:
    content_str = json.dumps(fixture)
    mock_sdk = MagicMock()
    completion = MagicMock(
        choices=[MagicMock(message=MagicMock(content=content_str))]
    )
    mock_sdk.chat.completions.create.return_value = completion
    return FoundryClient(_DUMMY_FOUNDRY_CFG, _sdk_client=mock_sdk)


def _make_di_layout_client_with_figure() -> DocIntelClient:
    """DI layout client whose layout_analyze_raw returns a result with 1 figure."""
    raw_mock = make_document_intelligence_client()
    return DocIntelClient(_DUMMY_DI_CFG, _di_client=raw_mock)


# ---------------------------------------------------------------------------
# Tests: extract_figures_from_di (unit — no CLI, no blob)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_extract_figures_produces_one_candidate():
    """extract_figures_from_di yields one VisualAssetCandidate for 1 DI figure."""
    di_result = _make_di_result_with_figure()
    mock_open = _make_fitz_open_mock(_FAKE_PNG_BYTES, width=300, height=250)

    candidates = extract_figures_from_di(
        "fake.pdf", di_result, _SOURCE_FILE_ID, _fitz_open=mock_open
    )

    assert len(candidates) == 1
    c = candidates[0]
    assert c.image_bytes == _FAKE_PNG_BYTES
    assert c.image_hash == hashlib.sha256(_FAKE_PNG_BYTES).hexdigest()
    assert c.page_number == 1
    assert c.asset_type == "figure"
    assert c.width == 300
    assert c.height == 250
    assert c.caption == "Battery assembly diagram"


@pytest.mark.unit
def test_extract_figures_carries_polygon():
    """Candidate polygon matches the DI bounding region polygon."""
    polygon = [1.0, 2.0, 4.0, 2.0, 4.0, 5.0, 1.0, 5.0]
    di_result = _make_di_result_with_figure(polygon=polygon)
    mock_open = _make_fitz_open_mock()

    candidates = extract_figures_from_di(
        "fake.pdf", di_result, _SOURCE_FILE_ID, _fitz_open=mock_open
    )

    assert len(candidates) == 1
    assert candidates[0].polygon == polygon


@pytest.mark.unit
def test_extract_figures_no_figures_returns_empty():
    """When DI result has no figures, returns []."""
    di_result = MagicMock()
    di_result.figures = []
    di_result.pages = []

    mock_open = _make_fitz_open_mock()
    candidates = extract_figures_from_di(
        "fake.pdf", di_result, _SOURCE_FILE_ID, _fitz_open=mock_open
    )

    assert candidates == []


@pytest.mark.unit
def test_extract_figures_skips_empty_polygon():
    """Figure with no polygon (or too-short polygon) is silently skipped."""
    di_result = MagicMock()
    di_result.figures = [
        {
            "id": "fig:bad",
            "caption": None,
            "bounding_regions": [{"page_number": 1, "polygon": []}],
        }
    ]
    di_result.pages = []

    mock_open = _make_fitz_open_mock()
    candidates = extract_figures_from_di(
        "fake.pdf", di_result, _SOURCE_FILE_ID, _fitz_open=mock_open
    )

    assert candidates == []


@pytest.mark.unit
def test_extract_figures_deduplicates_identical_crops():
    """Two figures with identical crop bytes → only one candidate."""
    di_result = MagicMock()
    polygon = [1.0, 2.0, 4.0, 2.0, 4.0, 5.0, 1.0, 5.0]
    di_result.figures = [
        {
            "id": "fig:0",
            "caption": {"content": "Fig A"},
            "bounding_regions": [{"page_number": 1, "polygon": polygon}],
        },
        {
            "id": "fig:1",
            "caption": {"content": "Fig B"},
            "bounding_regions": [{"page_number": 1, "polygon": polygon}],
        },
    ]
    di_result.pages = []

    # Both figures will produce identical crop bytes → dedup to 1.
    mock_open = _make_fitz_open_mock(_FAKE_PNG_BYTES)
    candidates = extract_figures_from_di(
        "fake.pdf", di_result, _SOURCE_FILE_ID, _fitz_open=mock_open
    )

    assert len(candidates) == 1


@pytest.mark.unit
def test_extract_figures_page_out_of_range_returns_empty():
    """Figure on page_number > doc.page_count is skipped gracefully."""
    di_result = _make_di_result_with_figure(page_number=99)

    mock_open = _make_fitz_open_mock(page_count=1)  # only 1 page
    candidates = extract_figures_from_di(
        "fake.pdf", di_result, _SOURCE_FILE_ID, _fitz_open=mock_open
    )

    assert candidates == []


# ---------------------------------------------------------------------------
# Tests: make_visual_regions_for_figure (unit)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_make_visual_regions_produces_one_row():
    """make_visual_regions_for_figure returns exactly one VisualRegionRow per figure."""
    di_result = _make_di_result_with_figure()
    mock_open = _make_fitz_open_mock()

    candidates = extract_figures_from_di(
        "fake.pdf", di_result, _SOURCE_FILE_ID, _fitz_open=mock_open
    )
    assert len(candidates) == 1
    candidate = candidates[0]

    image_id = make_image_id(_SOURCE_FILE_ID, candidate.image_hash)
    regions = make_visual_regions_for_figure(
        image_id, candidate, di_result, blob_url="https://fake.blob/img.png"
    )

    assert len(regions) == 1
    r = regions[0]
    assert isinstance(r, VisualRegionRow)
    assert r.image_id == image_id
    assert r.region_type == "figure_region"


@pytest.mark.unit
def test_make_visual_regions_fk_matches_image_id():
    """visual_regions.image_id must equal the image_id from visual_assets."""
    di_result = _make_di_result_with_figure()
    mock_open = _make_fitz_open_mock()

    candidates = extract_figures_from_di(
        "fake.pdf", di_result, _SOURCE_FILE_ID, _fitz_open=mock_open
    )
    candidate = candidates[0]
    image_id = make_image_id(_SOURCE_FILE_ID, candidate.image_hash)

    regions = make_visual_regions_for_figure(image_id, candidate, di_result)

    assert regions[0].image_id == image_id


@pytest.mark.unit
def test_make_visual_regions_has_polygon_json():
    """polygon_json is a JSON-encoded list of [x,y] pairs."""
    di_result = _make_di_result_with_figure(
        polygon=[1.0, 2.0, 4.0, 2.0, 4.0, 5.0, 1.0, 5.0]
    )
    mock_open = _make_fitz_open_mock()
    candidates = extract_figures_from_di(
        "fake.pdf", di_result, _SOURCE_FILE_ID, _fitz_open=mock_open
    )
    candidate = candidates[0]
    image_id = make_image_id(_SOURCE_FILE_ID, candidate.image_hash)

    regions = make_visual_regions_for_figure(image_id, candidate, di_result)

    assert regions[0].polygon_json is not None
    pairs = json.loads(regions[0].polygon_json)
    assert isinstance(pairs, list)
    assert all(len(p) == 2 for p in pairs)


@pytest.mark.unit
def test_make_visual_regions_has_normalized_polygon_json():
    """normalized_polygon_json values are in [0.0, 1.0] when page geometry is present."""
    di_result = _make_di_result_with_figure(
        polygon=[1.0, 2.0, 4.0, 2.0, 4.0, 5.0, 1.0, 5.0]
    )
    mock_open = _make_fitz_open_mock()
    candidates = extract_figures_from_di(
        "fake.pdf", di_result, _SOURCE_FILE_ID, _fitz_open=mock_open
    )
    candidate = candidates[0]
    image_id = make_image_id(_SOURCE_FILE_ID, candidate.image_hash)

    regions = make_visual_regions_for_figure(image_id, candidate, di_result)

    assert regions[0].normalized_polygon_json is not None
    npairs = json.loads(regions[0].normalized_polygon_json)
    for x, y in npairs:
        assert 0.0 <= x <= 1.0
        assert 0.0 <= y <= 1.0


@pytest.mark.unit
def test_make_visual_regions_inherits_caption_as_text():
    """VisualRegionRow.text is populated from candidate caption."""
    di_result = _make_di_result_with_figure(caption="Battery assembly diagram")
    mock_open = _make_fitz_open_mock()
    candidates = extract_figures_from_di(
        "fake.pdf", di_result, _SOURCE_FILE_ID, _fitz_open=mock_open
    )
    candidate = candidates[0]
    image_id = make_image_id(_SOURCE_FILE_ID, candidate.image_hash)

    regions = make_visual_regions_for_figure(image_id, candidate, di_result)

    assert regions[0].text == "Battery assembly diagram"


@pytest.mark.unit
def test_make_visual_regions_inherits_blob_url():
    """VisualRegionRow.blob_url is set from the parent asset blob_url."""
    di_result = _make_di_result_with_figure()
    mock_open = _make_fitz_open_mock()
    candidates = extract_figures_from_di(
        "fake.pdf", di_result, _SOURCE_FILE_ID, _fitz_open=mock_open
    )
    candidate = candidates[0]
    image_id = make_image_id(_SOURCE_FILE_ID, candidate.image_hash)
    parent_url = "https://fake.blob.core.windows.net/kg-assets/figures/img.png"

    regions = make_visual_regions_for_figure(
        image_id, candidate, di_result, blob_url=parent_url
    )

    assert regions[0].blob_url == parent_url


# ---------------------------------------------------------------------------
# Tests: enrich_cmd integration — canonical JSON with visual_assets/regions
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enrich_with_di_and_blob_produces_visual_assets(tmp_path: Path) -> None:
    """Enrich path with DI+blob: canonical JSON contains visual_assets rows."""
    pdf_path = tmp_path / "tiny.pdf"
    pdf_path.write_bytes(_MINIMAL_PDF)
    out_dir = tmp_path / "enriched"
    out_dir.mkdir(parents=True)

    foundry_client = _make_foundry_client_from_dict(_MOCK_LLM_OUTPUT)
    di_layout_client = _make_di_layout_client_with_figure()
    blob_uploader = make_blob_uploader()

    mock_fitz_open = _make_fitz_open_mock(_FAKE_PNG_BYTES, width=300, height=250)

    runner = CliRunner()
    with patch("fitz.open", mock_fitz_open):
        result = runner.invoke(
            cli,
            ["enrich", "--input", str(pdf_path), "--out", str(out_dir)],
            obj={
                "_foundry_client": foundry_client,
                "_di_layout_client": di_layout_client,
                "_blob_uploader": blob_uploader,
            },
        )

    assert result.exit_code == 0, (
        f"enrich exited {result.exit_code}.\n"
        f"Output: {result.output}\nException: {result.exception}"
    )

    canonical_files = list(out_dir.glob("*_canonical.json"))
    assert canonical_files, "No _canonical.json written"

    data = json.loads(canonical_files[0].read_text())
    assert "visual_assets" in data, "Canonical JSON must have visual_assets key"
    assert len(data["visual_assets"]) >= 1, (
        "Expected at least 1 visual_asset from DI figure"
    )


@pytest.mark.unit
def test_enrich_with_di_and_blob_produces_visual_regions(tmp_path: Path) -> None:
    """Enrich path with DI+blob: canonical JSON contains visual_regions linked by image_id."""
    pdf_path = tmp_path / "tiny.pdf"
    pdf_path.write_bytes(_MINIMAL_PDF)
    out_dir = tmp_path / "enriched"
    out_dir.mkdir(parents=True)

    foundry_client = _make_foundry_client_from_dict(_MOCK_LLM_OUTPUT)
    di_layout_client = _make_di_layout_client_with_figure()
    blob_uploader = make_blob_uploader()

    mock_fitz_open = _make_fitz_open_mock(_FAKE_PNG_BYTES)

    runner = CliRunner()
    with patch("fitz.open", mock_fitz_open):
        runner.invoke(
            cli,
            ["enrich", "--input", str(pdf_path), "--out", str(out_dir)],
            obj={
                "_foundry_client": foundry_client,
                "_di_layout_client": di_layout_client,
                "_blob_uploader": blob_uploader,
            },
        )

    data = json.loads(list(out_dir.glob("*_canonical.json"))[0].read_text())
    assert "visual_regions" in data
    assert len(data["visual_regions"]) >= 1, "Expected at least 1 visual_region"


@pytest.mark.unit
def test_enrich_visual_regions_image_id_matches_visual_assets(tmp_path: Path) -> None:
    """visual_regions.image_id must resolve to an image_id in visual_assets."""
    pdf_path = tmp_path / "tiny.pdf"
    pdf_path.write_bytes(_MINIMAL_PDF)
    out_dir = tmp_path / "enriched"
    out_dir.mkdir(parents=True)

    foundry_client = _make_foundry_client_from_dict(_MOCK_LLM_OUTPUT)
    di_layout_client = _make_di_layout_client_with_figure()
    blob_uploader = make_blob_uploader()
    mock_fitz_open = _make_fitz_open_mock(_FAKE_PNG_BYTES)

    runner = CliRunner()
    with patch("fitz.open", mock_fitz_open):
        runner.invoke(
            cli,
            ["enrich", "--input", str(pdf_path), "--out", str(out_dir)],
            obj={
                "_foundry_client": foundry_client,
                "_di_layout_client": di_layout_client,
                "_blob_uploader": blob_uploader,
            },
        )

    data = json.loads(list(out_dir.glob("*_canonical.json"))[0].read_text())
    asset_ids = {a["image_id"] for a in data["visual_assets"]}
    for region in data["visual_regions"]:
        assert region["image_id"] in asset_ids, (
            f"visual_regions.image_id {region['image_id']!r} "
            f"not found in visual_assets"
        )


@pytest.mark.unit
def test_enrich_visual_asset_has_blob_url(tmp_path: Path) -> None:
    """visual_assets row must carry a non-empty blob_url after upload."""
    pdf_path = tmp_path / "tiny.pdf"
    pdf_path.write_bytes(_MINIMAL_PDF)
    out_dir = tmp_path / "enriched"
    out_dir.mkdir(parents=True)

    foundry_client = _make_foundry_client_from_dict(_MOCK_LLM_OUTPUT)
    di_layout_client = _make_di_layout_client_with_figure()
    blob_uploader = make_blob_uploader()
    mock_fitz_open = _make_fitz_open_mock(_FAKE_PNG_BYTES)

    runner = CliRunner()
    with patch("fitz.open", mock_fitz_open):
        runner.invoke(
            cli,
            ["enrich", "--input", str(pdf_path), "--out", str(out_dir)],
            obj={
                "_foundry_client": foundry_client,
                "_di_layout_client": di_layout_client,
                "_blob_uploader": blob_uploader,
            },
        )

    data = json.loads(list(out_dir.glob("*_canonical.json"))[0].read_text())
    for asset in data["visual_assets"]:
        assert asset.get("blob_url"), "visual_asset.blob_url must be non-empty"
        assert asset.get("image_hash"), "visual_asset.image_hash must be present"
        assert asset.get("page_number") is not None, "visual_asset.page_number required"


@pytest.mark.unit
def test_enrich_visual_asset_has_correct_caption(tmp_path: Path) -> None:
    """visual_assets row caption matches the DI figure caption from the fixture."""
    pdf_path = tmp_path / "tiny.pdf"
    pdf_path.write_bytes(_MINIMAL_PDF)
    out_dir = tmp_path / "enriched"
    out_dir.mkdir(parents=True)

    foundry_client = _make_foundry_client_from_dict(_MOCK_LLM_OUTPUT)
    di_layout_client = _make_di_layout_client_with_figure()
    blob_uploader = make_blob_uploader()
    mock_fitz_open = _make_fitz_open_mock(_FAKE_PNG_BYTES)

    runner = CliRunner()
    with patch("fitz.open", mock_fitz_open):
        runner.invoke(
            cli,
            ["enrich", "--input", str(pdf_path), "--out", str(out_dir)],
            obj={
                "_foundry_client": foundry_client,
                "_di_layout_client": di_layout_client,
                "_blob_uploader": blob_uploader,
            },
        )

    data = json.loads(list(out_dir.glob("*_canonical.json"))[0].read_text())
    # The default DI fixture has caption "Battery assembly diagram"
    captions = [a.get("caption") for a in data["visual_assets"]]
    assert "Battery assembly diagram" in captions, (
        f"Expected caption 'Battery assembly diagram' in visual_assets, got: {captions}"
    )


# ---------------------------------------------------------------------------
# Tests: graceful fallback when blob or DI is absent
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_enrich_without_blob_skips_visual_extraction(tmp_path: Path) -> None:
    """When blob_uploader is None, no visual_assets are produced (graceful skip)."""
    pdf_path = tmp_path / "tiny.pdf"
    pdf_path.write_bytes(_MINIMAL_PDF)
    out_dir = tmp_path / "enriched"
    out_dir.mkdir(parents=True)

    foundry_client = _make_foundry_client_from_dict(_MOCK_LLM_OUTPUT)
    di_layout_client = _make_di_layout_client_with_figure()
    # No _blob_uploader in ctx.obj → _build_blob_uploader returns None (no account_name)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["enrich", "--input", str(pdf_path), "--out", str(out_dir)],
        obj={
            "_foundry_client": foundry_client,
            "_di_layout_client": di_layout_client,
            # _blob_uploader intentionally absent
        },
    )

    assert result.exit_code == 0, (
        f"enrich should exit 0 without blob. "
        f"Exit: {result.exit_code}\nOutput: {result.output}\nException: {result.exception}"
    )

    canonical_files = list(out_dir.glob("*_canonical.json"))
    assert canonical_files, "Canonical JSON must still be written"
    data = json.loads(canonical_files[0].read_text())
    # visual_assets key present but empty (no blob = no uploads)
    assert data.get("visual_assets", []) == [], (
        "visual_assets must be empty when blob is not configured"
    )


@pytest.mark.unit
def test_enrich_without_di_skips_visual_extraction(tmp_path: Path) -> None:
    """When di_layout_client is None, no visual_assets are produced (graceful skip)."""
    pdf_path = tmp_path / "tiny.pdf"
    pdf_path.write_bytes(_MINIMAL_PDF)
    out_dir = tmp_path / "enriched"
    out_dir.mkdir(parents=True)

    foundry_client = _make_foundry_client_from_dict(_MOCK_LLM_OUTPUT)
    blob_uploader = make_blob_uploader()

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["enrich", "--input", str(pdf_path), "--out", str(out_dir)],
        obj={
            "_foundry_client": foundry_client,
            # _di_layout_client intentionally absent
            "_blob_uploader": blob_uploader,
        },
    )

    assert result.exit_code == 0, (
        f"enrich should exit 0 without DI. "
        f"Exit: {result.exit_code}\nOutput: {result.output}\nException: {result.exception}"
    )

    canonical_files = list(out_dir.glob("*_canonical.json"))
    assert canonical_files
    data = json.loads(canonical_files[0].read_text())
    assert data.get("visual_assets", []) == [], (
        "visual_assets must be empty when DI is not configured"
    )


@pytest.mark.unit
def test_enrich_without_di_and_blob_exits_zero(tmp_path: Path) -> None:
    """Pipeline works end-to-end without DI or blob — exit 0, canonical JSON written."""
    pdf_path = tmp_path / "tiny.pdf"
    pdf_path.write_bytes(_MINIMAL_PDF)
    out_dir = tmp_path / "enriched"
    out_dir.mkdir(parents=True)

    foundry_client = _make_foundry_client_from_dict(_MOCK_LLM_OUTPUT)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["enrich", "--input", str(pdf_path), "--out", str(out_dir)],
        obj={"_foundry_client": foundry_client},
    )

    assert result.exit_code == 0
    canonical_files = list(out_dir.glob("*_canonical.json"))
    assert canonical_files, "Canonical JSON must be written even without DI/blob"
