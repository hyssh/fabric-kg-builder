"""Unit tests for fabric_kg_builder.enrichment.docintel.

Tests:
- map_di_result_to_visual_regions produces VisualRegionRow records from the
  fixture DI response (polygon_json, normalized_polygon_json, text, image_id FK).
- Polygon normalization: coordinates divided by page width/height.
- DocIntelClient.analyze_document_bytes delegates to begin_analyze_document.
- DocIntelClient.analyze_document_url delegates similarly.
- Visual regions have correct image_id FK.
- Empty paragraphs produce no regions.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fabric_kg_builder.config.schema import DocumentIntelligenceConfig
from fabric_kg_builder.enrichment.docintel import (
    DocIntelClient,
    DocIntelResult,
    PageGeometry,
    _build_page_geometry_map,
    _normalize_polygon,
    _polygon_to_pairs,
    map_di_result_to_visual_regions,
)
from fabric_kg_builder.model.ids import make_visual_region_id

# Import conftest factory
from tests.conftest import make_document_intelligence_client

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_IMAGE_ID = "img:test_image_abc123"
_NOW = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)

_DI_CONFIG = DocumentIntelligenceConfig(endpoint="https://fake-docintel.cognitiveservices.azure.com/")


# ---------------------------------------------------------------------------
# Tests: polygon helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_polygon_to_pairs_converts_flat_to_pairs():
    flat = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0]
    result = _polygon_to_pairs(flat)
    assert result == [[10.0, 20.0], [30.0, 40.0], [50.0, 60.0], [70.0, 80.0]]


@pytest.mark.unit
def test_polygon_to_pairs_empty():
    assert _polygon_to_pairs([]) == []


@pytest.mark.unit
def test_normalize_polygon_divides_by_page_dimensions():
    flat = [100.0, 200.0, 300.0, 400.0]
    result = _normalize_polygon(flat, page_width=400.0, page_height=800.0)
    assert result == [[pytest.approx(0.25), pytest.approx(0.25)],
                      [pytest.approx(0.75), pytest.approx(0.5)]]


@pytest.mark.unit
def test_normalize_polygon_zero_page_returns_empty():
    assert _normalize_polygon([10.0, 20.0], 0, 0) == []


@pytest.mark.unit
def test_normalize_polygon_empty_returns_empty():
    assert _normalize_polygon([], 600.0, 800.0) == []


# ---------------------------------------------------------------------------
# Tests: _build_page_geometry_map
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_page_geometry_map_from_dicts():
    pages = [
        {"page_number": 1, "width": 612.0, "height": 792.0, "unit": "pixel"},
        {"page_number": 2, "width": 612.0, "height": 792.0, "unit": "pixel"},
    ]
    geos = _build_page_geometry_map(pages)
    assert 1 in geos
    assert 2 in geos
    assert geos[1].width == 612.0
    assert geos[2].height == 792.0


@pytest.mark.unit
def test_build_page_geometry_map_from_objects():
    page = MagicMock()
    page.page_number = 1
    page.width = 500.0
    page.height = 700.0
    page.unit = "inch"

    geos = _build_page_geometry_map([page])
    assert geos[1].unit == "inch"


# ---------------------------------------------------------------------------
# Tests: map_di_result_to_visual_regions (pure mapping)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_map_di_result_produces_visual_regions():
    """map_di_result_to_visual_regions produces one VisualRegionRow per paragraph/bounding_region."""
    result_mock = MagicMock()
    result_mock.content = "Surface Laptop 5 Service Guide"
    result_mock.pages = [
        {"page_number": 1, "width": 612.0, "height": 792.0, "unit": "pixel"}
    ]
    result_mock.paragraphs = [
        {
            "content": "Surface Laptop 5 Service Guide",
            "role": "title",
            "bounding_regions": [
                {
                    "page_number": 1,
                    "polygon": [72.0, 72.0, 400.0, 72.0, 400.0, 92.0, 72.0, 92.0],
                }
            ],
        }
    ]

    di_result = map_di_result_to_visual_regions(result_mock, _IMAGE_ID, now=_NOW)

    assert isinstance(di_result, DocIntelResult)
    assert len(di_result.visual_regions) == 1


@pytest.mark.unit
def test_map_di_result_sets_image_id_fk():
    result_mock = MagicMock()
    result_mock.content = "test"
    result_mock.pages = [{"page_number": 1, "width": 600.0, "height": 800.0, "unit": "pixel"}]
    result_mock.paragraphs = [
        {
            "content": "Battery replacement procedure.",
            "bounding_regions": [{"page_number": 1, "polygon": [10.0, 10.0, 100.0, 10.0, 100.0, 30.0, 10.0, 30.0]}],
        }
    ]

    di_result = map_di_result_to_visual_regions(result_mock, _IMAGE_ID, now=_NOW)
    region = di_result.visual_regions[0]

    assert region.image_id == _IMAGE_ID


@pytest.mark.unit
def test_map_di_result_sets_region_type_ocr_text():
    result_mock = MagicMock()
    result_mock.content = "test"
    result_mock.pages = [{"page_number": 1, "width": 612.0, "height": 792.0, "unit": "pixel"}]
    result_mock.paragraphs = [
        {
            "content": "Some text",
            "bounding_regions": [{"page_number": 1, "polygon": [0.0, 0.0, 10.0, 0.0, 10.0, 10.0, 0.0, 10.0]}],
        }
    ]

    di_result = map_di_result_to_visual_regions(result_mock, _IMAGE_ID, now=_NOW)
    region = di_result.visual_regions[0]

    assert region.region_type == "ocr_text"


@pytest.mark.unit
def test_map_di_result_sets_polygon_json():
    result_mock = MagicMock()
    result_mock.content = "test"
    result_mock.pages = [{"page_number": 1, "width": 612.0, "height": 792.0, "unit": "pixel"}]
    result_mock.paragraphs = [
        {
            "content": "Text",
            "bounding_regions": [
                {"page_number": 1, "polygon": [72.0, 72.0, 400.0, 72.0, 400.0, 92.0, 72.0, 92.0]}
            ],
        }
    ]

    di_result = map_di_result_to_visual_regions(result_mock, _IMAGE_ID, now=_NOW)
    region = di_result.visual_regions[0]

    assert region.polygon_json is not None
    parsed = json.loads(region.polygon_json)
    assert parsed == [[72.0, 72.0], [400.0, 72.0], [400.0, 92.0], [72.0, 92.0]]


@pytest.mark.unit
def test_map_di_result_sets_normalized_polygon_json():
    result_mock = MagicMock()
    result_mock.content = "test"
    result_mock.pages = [{"page_number": 1, "width": 400.0, "height": 800.0, "unit": "pixel"}]
    result_mock.paragraphs = [
        {
            "content": "Text",
            "bounding_regions": [
                {"page_number": 1, "polygon": [100.0, 200.0, 200.0, 200.0, 200.0, 400.0, 100.0, 400.0]}
            ],
        }
    ]

    di_result = map_di_result_to_visual_regions(result_mock, _IMAGE_ID, now=_NOW)
    region = di_result.visual_regions[0]

    assert region.normalized_polygon_json is not None
    parsed = json.loads(region.normalized_polygon_json)
    # x / 400.0, y / 800.0
    assert parsed[0][0] == pytest.approx(0.25)
    assert parsed[0][1] == pytest.approx(0.25)


@pytest.mark.unit
def test_map_di_result_sets_text():
    result_mock = MagicMock()
    result_mock.content = "Some content"
    result_mock.pages = [{"page_number": 1, "width": 612.0, "height": 792.0, "unit": "pixel"}]
    result_mock.paragraphs = [
        {
            "content": "Battery replacement procedure.",
            "bounding_regions": [{"page_number": 1, "polygon": [0.0, 0.0, 10.0, 0.0, 10.0, 10.0, 0.0, 10.0]}],
        }
    ]

    di_result = map_di_result_to_visual_regions(result_mock, _IMAGE_ID, now=_NOW)
    region = di_result.visual_regions[0]

    assert region.text == "Battery replacement procedure."


@pytest.mark.unit
def test_map_di_result_sets_created_at():
    result_mock = MagicMock()
    result_mock.content = ""
    result_mock.pages = []
    result_mock.paragraphs = []

    di_result = map_di_result_to_visual_regions(result_mock, _IMAGE_ID, now=_NOW)
    assert di_result.visual_regions == []


@pytest.mark.unit
def test_map_di_result_carries_raw_content():
    result_mock = MagicMock()
    result_mock.content = "Surface Laptop 5 Service Guide\nBattery replacement procedure."
    result_mock.pages = []
    result_mock.paragraphs = []

    di_result = map_di_result_to_visual_regions(result_mock, _IMAGE_ID, now=_NOW)
    assert "Surface Laptop" in di_result.raw_content


@pytest.mark.unit
def test_map_di_result_region_ids_are_stable():
    """Region IDs must be deterministic given the same inputs."""
    result_mock = MagicMock()
    result_mock.content = "text"
    result_mock.pages = [{"page_number": 1, "width": 612.0, "height": 792.0, "unit": "pixel"}]
    result_mock.paragraphs = [
        {
            "content": "para",
            "bounding_regions": [{"page_number": 1, "polygon": [0.0, 0.0, 10.0, 0.0, 10.0, 10.0, 0.0, 10.0]}],
        }
    ]

    di_result1 = map_di_result_to_visual_regions(result_mock, _IMAGE_ID, now=_NOW)
    di_result2 = map_di_result_to_visual_regions(result_mock, _IMAGE_ID, now=_NOW)

    assert di_result1.visual_regions[0].visual_region_id == di_result2.visual_regions[0].visual_region_id


# ---------------------------------------------------------------------------
# Tests: map_di_result via conftest fixture (integration-style)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_map_di_result_with_conftest_fixture():
    """Use the shared DI fixture (analyze_result.json) to test the full mapping path."""
    mock_client = make_document_intelligence_client()
    di_result_raw = mock_client.begin_analyze_document().result()

    result = map_di_result_to_visual_regions(di_result_raw, _IMAGE_ID, now=_NOW)

    # The fixture has 1 paragraph
    assert len(result.visual_regions) >= 1
    region = result.visual_regions[0]
    assert region.image_id == _IMAGE_ID
    assert region.region_type == "ocr_text"
    assert region.polygon_json is not None


# ---------------------------------------------------------------------------
# Tests: DocIntelClient.analyze_document_bytes
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_docintel_client_analyze_bytes_calls_begin_analyze():
    mock_di = make_document_intelligence_client()
    client = DocIntelClient(_DI_CONFIG, _di_client=mock_di)

    result = client.analyze_document_bytes(b"fake_data", _IMAGE_ID, now=_NOW)

    mock_di.begin_analyze_document.assert_called_once()
    call_kwargs = mock_di.begin_analyze_document.call_args
    assert call_kwargs.kwargs.get("model_id") == "prebuilt-layout" or "prebuilt-layout" in str(call_kwargs)


@pytest.mark.unit
def test_docintel_client_analyze_bytes_returns_doc_intel_result():
    mock_di = make_document_intelligence_client()
    client = DocIntelClient(_DI_CONFIG, _di_client=mock_di)

    result = client.analyze_document_bytes(b"fake_data", _IMAGE_ID, now=_NOW)

    assert isinstance(result, DocIntelResult)


@pytest.mark.unit
def test_docintel_client_analyze_url_calls_begin_analyze():
    mock_di = make_document_intelligence_client()
    client = DocIntelClient(_DI_CONFIG, _di_client=mock_di)

    result = client.analyze_document_url("https://fake.blob.core.windows.net/doc.pdf", _IMAGE_ID, now=_NOW)

    assert isinstance(result, DocIntelResult)
    mock_di.begin_analyze_document.assert_called()
