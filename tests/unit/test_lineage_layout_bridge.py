"""Synthetic raw-DI locations, not extraction quality or visual claim proof."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fabric_kg_builder.contracts.evidence import EvidenceSpanV1_1, SourceUnit
from fabric_kg_builder.contracts.identity import ImmutableSourceLocator
from fabric_kg_builder.lineage.layout_bridge import raw_layout_location
from fabric_kg_builder.sources.docintel_cache import make_cached_layout
from tests.unit.test_docintel_cache import _identity as cache_identity
from tests.unit.test_schema2_evidence import _identity


def _case(*, index_type="unicodeCodePoint", raw_text="  Cell Caption  ", unit_text="Cell Caption"):
    raw = {
        "content": raw_text, "stringIndexType": index_type,
        "pages": [{
            "pageNumber": 1, "width": 8.5, "height": 11.0, "unit": "inch",
            "spans": [{"offset": 0, "length": len(raw_text)}],
        }],
        "tables": [{
            "id": "table-1", "rowCount": 1, "columnCount": 1,
            "cells": [{
                "rowIndex": 0, "columnIndex": 0, "rowSpan": 1, "columnSpan": 1,
                "spans": [{"offset": 2, "length": 4}],
                "boundingRegions": [{"pageNumber": 1, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}],
            }],
        }],
        "figures": [{
            "id": "figure-1", "spans": [{"offset": 7, "length": 7}],
            "boundingRegions": [{"pageNumber": 1, "polygon": [2, 2, 3, 2, 3, 3, 2, 3]}],
            "source_file": "/must/not/be/opened",
        }],
    }
    cached = make_cached_layout(b"synthetic-pdf-bytes", raw, cache_identity())
    locator = ImmutableSourceLocator.from_authority(
        blob_uri="https://storage.example/source", blob_version_id="v1", page=1,
        native_layer_id="docintel-pages/1.0.0",
        native_object_id=f"{cached.cache_key}/pages/1",
    )
    unit = SourceUnit.mint(
        identity=_identity(), unit_kind="paragraph", text=unit_text,
        ordinal=0, locator=locator,
    )
    span = EvidenceSpanV1_1.mint_verified(
        source_unit=unit, span_start=0, span_end=4,
        verifier_name="synthetic", verifier_version="1.0.0",
        verified_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
        purpose="extraction_assertion",
        verifier_purpose_version="1.0.0",
    )
    return cached, unit, span


def test_text_and_cell_raw_span_with_context_only_figure():
    cached, unit, span = _case()
    result = raw_layout_location(unit, span, [cached], cached.input_sha256)
    assert result["status"] == "EXACT_RAW_SPAN"
    assert result["raw_span"] == {"offset": 2, "length": 4}
    assert result["page_geometry"]["unit"] == "inch"
    cell, = result["exact_regions"]
    assert cell["raw_object_pointer"] == "/tables/0/cells/0"
    assert (cell["rowIndex"], cell["columnIndex"], cell["rowSpan"], cell["columnSpan"]) == (0, 0, 1, 1)
    figure, = result["page_context_regions"]
    assert figure["object_id"] == "figure-1"
    assert figure["claim_to_region_verified"] is False
    assert result["assertion_trust_promoted"] is False


def test_no_claim_span_keeps_all_modality_locations_context_only():
    cached, unit, _ = _case()
    result = raw_layout_location(unit, None, [cached], cached.input_sha256)
    assert result["exact_regions"] == []
    assert len(result["page_context_regions"]) == 2


@pytest.mark.parametrize("index_type", ["utf16CodeUnit", "textElements", None])
def test_unsupported_raw_index_not_silently_converted(index_type):
    cached, unit, span = _case(index_type=index_type)
    assert raw_layout_location(unit, span, [cached], cached.input_sha256)["status"] == "UNSUPPORTED"


def test_normalization_without_offset_map_is_incomplete():
    cached, unit, span = _case(raw_text="  Cell Cafe\u0301  ", unit_text="Cell Caf\u00e9")
    result = raw_layout_location(unit, span, [cached], cached.input_sha256)
    assert result["status"] == "INCOMPLETE"
    assert result["reason"] == "NORMALIZATION_OFFSET_MAP_NOT_RECORDED"


def test_exact_cache_input_and_recorded_key_are_required():
    cached, unit, span = _case()
    result = raw_layout_location(unit, span, [cached], "0" * 64)
    assert result["status"] == "UNAVAILABLE"
    result = raw_layout_location(unit, span, [], cached.input_sha256)
    assert result["status"] == "UNAVAILABLE"
