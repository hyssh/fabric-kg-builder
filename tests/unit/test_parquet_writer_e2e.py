"""Parquet writer e2e test — full 8-table round-trip including visual/evidence.

Sprint 2 scope: verify that write_table works for the full canonical set
(document_elements, visual_assets, visual_regions, evidence), and that
VAL-008..012 (visual/evidence integrity gates) are runnable.

SPEC-002 §9 validation rules covered:
  VAL-008  No dup image_id in visual_assets             (D-13)
  VAL-009  No dup visual_region_id in visual_regions    (D-14)
  VAL-010  visual_regions.image_id FK → visual_assets   (D-05)
  VAL-011  evidence.image_id FK → visual_assets         (D-05)
  VAL-012  evidence.visual_region_id FK → visual_regions (D-06)
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from fabric_kg_builder.model.ids import (
    content_hash,
    make_chunk_id,
    make_entity_id,
    make_evidence_id,
    make_image_id,
    make_visual_region_id,
)
from fabric_kg_builder.parquet.writer import write_all_tables, write_table
from fabric_kg_builder.validate.data_gates import run_gates

_UTC = timezone.utc
_NOW = datetime(2026, 6, 24, 14, 0, 0, tzinfo=_UTC)
_SRC_ID = "src:e2e_test_abc123"
_IMG_BYTES = b"fake_image_bytes_for_test"
_IMG_HASH = hashlib.sha256(_IMG_BYTES).hexdigest()


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------


def _make_visual_asset(image_id: str | None = None) -> dict:
    iid = image_id or make_image_id(_SRC_ID, _IMG_HASH)
    return {
        "image_id": iid,
        "source_file_id": _SRC_ID,
        "document_element_id": None,
        "asset_type": "diagram",
        "page_number": 3,
        "section_path": "Battery Replacement",
        "caption": "Figure 12: Battery connector",
        "alt_text": None,
        "blob_url": "https://fake.blob.core.windows.net/kg-assets/figure12.png",
        "image_path": "build/images/figure12.png",
        "image_hash": _IMG_HASH,
        "width": 1024,
        "height": 768,
        "description": "Exploded diagram of the Surface Laptop 5 main board.",
        "confidence": 0.94,
        "is_placeholder": False,
        "created_at": _NOW,
    }


def _make_visual_region(image_id: str, vr_id: str | None = None) -> dict:
    rid = vr_id or make_visual_region_id(image_id, "callout", "Battery connector", 0)
    return {
        "visual_region_id": rid,
        "image_id": image_id,
        "region_type": "callout",
        "label": "Battery connector",
        "text": "Callout B identifies the battery connector on the main board.",
        "polygon_json": "[[100, 200], [300, 200], [300, 400], [100, 400]]",
        "normalized_polygon_json": "[[0.1, 0.25], [0.3, 0.25], [0.3, 0.5], [0.1, 0.5]]",
        "identified_entity_id": None,
        "blob_url": "https://fake.blob.core.windows.net/kg-assets/figure12.png",
        "confidence": 0.88,
        "created_at": _NOW,
    }


def _make_evidence(
    evidence_id: str | None = None,
    image_id: str | None = None,
    vr_id: str | None = None,
) -> dict:
    eid = evidence_id or make_evidence_id(_SRC_ID, "figure_callout", "pg3:calloutB", _IMG_HASH[:16])
    return {
        "evidence_id": eid,
        "source_file_id": _SRC_ID,
        "source_type": "figure_callout",
        "document_element_id": None,
        "chunk_id": None,
        "page_number": 3,
        "section_path": "Battery Replacement",
        "table_id": None,
        "row_index": None,
        "col_index": None,
        "figure_id": None,
        "image_id": image_id,
        "callout_id": vr_id,
        "visual_region_id": vr_id,
        "blob_url": "https://fake.blob.core.windows.net/kg-assets/figure12.png",
        "text": "Callout B identifies the battery connector.",
        "content_hash": content_hash(f"{_SRC_ID}:figure_callout:Callout B"),
        "created_at": _NOW,
    }


# ---------------------------------------------------------------------------
# Tests: visual_assets round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_write_visual_assets_round_trip(tmp_path: Path) -> None:
    """visual_assets writes and reads back correctly."""
    asset = _make_visual_asset()
    out = write_table("visual_assets", [asset], tmp_path)
    assert out.exists()
    table = pq.read_table(out)
    assert table.num_rows == 1
    assert table.column("asset_type")[0].as_py() == "diagram"
    assert table.column("is_placeholder")[0].as_py() is False


@pytest.mark.unit
def test_write_visual_regions_round_trip(tmp_path: Path) -> None:
    """visual_regions writes and reads back with correct FK."""
    asset = _make_visual_asset()
    vr = _make_visual_region(asset["image_id"])
    write_table("visual_assets", [asset], tmp_path)
    out = write_table("visual_regions", [vr], tmp_path)
    table = pq.read_table(out)
    assert table.num_rows == 1
    assert table.column("region_type")[0].as_py() == "callout"
    assert table.column("image_id")[0].as_py() == asset["image_id"]


@pytest.mark.unit
def test_write_evidence_round_trip(tmp_path: Path) -> None:
    """evidence writes and reads back with visual FK columns populated."""
    asset = _make_visual_asset()
    vr = _make_visual_region(asset["image_id"])
    ev = _make_evidence(image_id=asset["image_id"], vr_id=vr["visual_region_id"])
    write_table("visual_assets", [asset], tmp_path)
    write_table("visual_regions", [vr], tmp_path)
    out = write_table("evidence", [ev], tmp_path)
    table = pq.read_table(out)
    assert table.num_rows == 1
    assert table.column("source_type")[0].as_py() == "figure_callout"
    assert table.column("image_id")[0].as_py() == asset["image_id"]
    assert table.column("visual_region_id")[0].as_py() == vr["visual_region_id"]


@pytest.mark.unit
def test_write_all_8_tables(tmp_path: Path) -> None:
    """write_all_tables writes all 8 canonical tables in one call."""
    asset = _make_visual_asset()
    vr = _make_visual_region(asset["image_id"])
    ev = _make_evidence(image_id=asset["image_id"], vr_id=vr["visual_region_id"])
    entity_id = make_entity_id("Device", "Surface Laptop 5")
    ck = "device:surface-laptop-5"
    ch_e = content_hash(ck)
    chunk_content = "Replace the battery connector by following these steps."
    ch_c = content_hash(chunk_content)
    chunk_id = make_chunk_id(_SRC_ID, "procedure_step", ch_c)

    table_rows = {
        "source_files": [{
            "source_file_id": _SRC_ID,
            "path": "sample_data/test.pdf",
            "filename": "test.pdf",
            "source_type": "pdf",
            "content_hash": _IMG_HASH,
            "byte_size": 1024,
            "ingested_at": _NOW,
            "schema_profile_path": None,
            "row_count": None,
            "notes": None,
        }],
        "document_elements": [{
            "document_element_id": "elem:test_elem_001",
            "source_file_id": _SRC_ID,
            "element_type": "section",
            "parent_element_id": None,
            "title": "Battery Replacement",
            "content": "Battery replacement section.",
            "content_html": None,
            "blob_url": None,
            "page_number": 3,
            "section_path": "Battery Replacement",
            "sort_order": 5,
            "row_index": None,
            "col_index": None,
            "content_hash": content_hash("Battery Replacement"),
            "extracted_at": _NOW,
        }],
        "chunks": [{
            "chunk_id": chunk_id,
            "source_file_id": _SRC_ID,
            "document_element_id": None,
            "chunk_type": "procedure_step",
            "content": chunk_content,
            "content_html": None,
            "embedding_text": chunk_content,
            "blob_url": None,
            "page_number": 3,
            "section_path": "Battery Replacement",
            "table_id": None,
            "figure_id": None,
            "image_id": None,
            "related_entity_ids": [entity_id],
            "entity_search_keys": ["device:surface-laptop-5", "surface laptop 5"],
            "content_hash": ch_c,
            "created_at": _NOW,
        }],
        "entities": [{
            "entity_id": entity_id,
            "entity_type": "Device",
            "display_name": "Surface Laptop 5",
            "canonical_key": ck,
            "aliases": ["SL5"],
            "search_aliases": ["device:surface-laptop-5", "surface laptop 5", "sl5"],
            "description": "Microsoft Surface Laptop 5",
            "properties_json": None,
            "source_file_id": _SRC_ID,
            "confidence": 0.95,
            "is_placeholder": False,
            "content_hash": ch_e,
            "created_at": _NOW,
            "updated_at": _NOW,
        }],
        "relationships": [],
        "evidence": [ev],
        "visual_assets": [asset],
        "visual_regions": [vr],
    }

    written = write_all_tables(table_rows, tmp_path)
    assert len(written) == 8
    for name, path in written.items():
        assert path.exists(), f"Missing: {name}.parquet"
        table = pq.read_table(path)
        assert table.num_rows == len(table_rows[name])


# ---------------------------------------------------------------------------
# Tests: VAL-008..012 (visual/evidence integrity gates)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestVisualEvidenceGates:
    """VAL-008..012 runnable as data-integrity gates."""

    def _base_rows(self) -> dict:
        asset = _make_visual_asset()
        vr = _make_visual_region(asset["image_id"])
        ev = _make_evidence(image_id=asset["image_id"], vr_id=vr["visual_region_id"])
        return {
            "visual_assets": [asset],
            "visual_regions": [vr],
            "evidence": [ev],
            "entities": [], "relationships": [], "chunks": [],
        }

    def test_val008_no_violation_on_unique_image_ids(self) -> None:
        rows = self._base_rows()
        violations = run_gates(rows)
        val008 = [v for v in violations if v.gate == "VAL-008"]
        assert val008 == []

    def test_val008_catches_dup_image_id(self) -> None:
        rows = self._base_rows()
        rows["visual_assets"].append(dict(rows["visual_assets"][0]))  # duplicate
        violations = run_gates(rows)
        val008 = [v for v in violations if v.gate == "VAL-008"]
        assert len(val008) == 1

    def test_val009_no_violation_on_unique_vr_ids(self) -> None:
        rows = self._base_rows()
        violations = run_gates(rows)
        val009 = [v for v in violations if v.gate == "VAL-009"]
        assert val009 == []

    def test_val009_catches_dup_visual_region_id(self) -> None:
        rows = self._base_rows()
        rows["visual_regions"].append(dict(rows["visual_regions"][0]))
        violations = run_gates(rows)
        val009 = [v for v in violations if v.gate == "VAL-009"]
        assert len(val009) == 1

    def test_val010_catches_dangling_image_fk_in_visual_regions(self) -> None:
        rows = self._base_rows()
        rows["visual_regions"][0]["image_id"] = "img:does_not_exist"
        violations = run_gates(rows)
        val010 = [v for v in violations if v.gate == "VAL-010"]
        assert len(val010) == 1

    def test_val010_passes_when_fk_valid(self) -> None:
        rows = self._base_rows()
        violations = run_gates(rows)
        val010 = [v for v in violations if v.gate == "VAL-010"]
        assert val010 == []

    def test_val011_catches_dangling_image_fk_in_evidence(self) -> None:
        rows = self._base_rows()
        rows["evidence"][0]["image_id"] = "img:ghost"
        violations = run_gates(rows)
        val011 = [v for v in violations if v.gate == "VAL-011"]
        assert len(val011) == 1

    def test_val011_passes_when_image_id_null(self) -> None:
        rows = self._base_rows()
        rows["evidence"][0]["image_id"] = None
        violations = run_gates(rows)
        val011 = [v for v in violations if v.gate == "VAL-011"]
        assert val011 == []

    def test_val012_catches_dangling_visual_region_fk_in_evidence(self) -> None:
        rows = self._base_rows()
        rows["evidence"][0]["visual_region_id"] = "vr:ghost"
        violations = run_gates(rows)
        val012 = [v for v in violations if v.gate == "VAL-012"]
        assert len(val012) == 1

    def test_val012_catches_dangling_callout_id_in_evidence(self) -> None:
        rows = self._base_rows()
        rows["evidence"][0]["callout_id"] = "vr:ghost_callout"
        violations = run_gates(rows)
        val012 = [v for v in violations if v.gate == "VAL-012"]
        assert len(val012) == 1

    def test_val012_passes_when_all_fks_null(self) -> None:
        rows = self._base_rows()
        rows["evidence"][0]["visual_region_id"] = None
        rows["evidence"][0]["callout_id"] = None
        violations = run_gates(rows)
        val012 = [v for v in violations if v.gate == "VAL-012"]
        assert val012 == []
