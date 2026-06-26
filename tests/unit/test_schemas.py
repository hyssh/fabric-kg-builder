"""Tests for pyarrow schemas — all 8 canonical tables.

Verifies:
- Every table in TABLE_SCHEMAS has the exact columns per SPEC-002 §3
- Column types and nullability match the spec
- list<string> columns (aliases, related_entity_ids, etc.) use pa.list_(pa.string())
- §11 graph→search columns (search_aliases, entity_search_keys) are present
"""

import pyarrow as pa
import pytest

from fabric_kg_builder.model.arrow_schemas import TABLE_SCHEMAS

_TS_TYPE = pa.timestamp("us", tz="UTC")
_LIST_STR_TYPE = pa.list_(pa.string())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def field_map(schema: pa.Schema) -> dict[str, pa.Field]:
    return {f.name: f for f in schema}


# ---------------------------------------------------------------------------
# Registry completeness
# ---------------------------------------------------------------------------


ALL_TABLES = [
    "source_files",
    "document_elements",
    "chunks",
    "entities",
    "relationships",
    "evidence",
    "visual_assets",
    "visual_regions",
]


def test_registry_has_all_8_tables():
    assert set(TABLE_SCHEMAS.keys()) == set(ALL_TABLES)


@pytest.mark.parametrize("table", ALL_TABLES)
def test_schema_is_pyarrow_schema(table):
    assert isinstance(TABLE_SCHEMAS[table], pa.Schema)


# ---------------------------------------------------------------------------
# source_files (§3.2)
# ---------------------------------------------------------------------------


def test_source_files_required_columns():
    fm = field_map(TABLE_SCHEMAS["source_files"])
    assert "source_file_id" in fm
    assert "path" in fm
    assert "filename" in fm
    assert "source_type" in fm
    assert "content_hash" in fm
    assert "ingested_at" in fm


def test_source_files_not_null_columns():
    fm = field_map(TABLE_SCHEMAS["source_files"])
    for col in ("source_file_id", "path", "filename", "source_type", "content_hash", "ingested_at"):
        assert fm[col].nullable is False, f"{col} should be NOT NULL"


def test_source_files_nullable_columns():
    fm = field_map(TABLE_SCHEMAS["source_files"])
    for col in ("byte_size", "schema_profile_path", "row_count", "notes"):
        assert fm[col].nullable is True, f"{col} should be NULLABLE"


def test_source_files_ingested_at_type():
    fm = field_map(TABLE_SCHEMAS["source_files"])
    assert fm["ingested_at"].type == _TS_TYPE


# ---------------------------------------------------------------------------
# document_elements (§3.3)
# ---------------------------------------------------------------------------


def test_document_elements_pk():
    fm = field_map(TABLE_SCHEMAS["document_elements"])
    assert fm["document_element_id"].nullable is False


def test_document_elements_int32_columns():
    fm = field_map(TABLE_SCHEMAS["document_elements"])
    for col in ("page_number", "sort_order", "row_index", "col_index"):
        assert fm[col].type == pa.int32(), f"{col} should be int32"


# ---------------------------------------------------------------------------
# chunks (§3.4 + §11)
# ---------------------------------------------------------------------------


def test_chunks_required_columns():
    fm = field_map(TABLE_SCHEMAS["chunks"])
    for col in ("chunk_id", "source_file_id", "chunk_type", "content", "content_hash", "created_at"):
        assert col in fm
        assert fm[col].nullable is False, f"{col} should be NOT NULL"


def test_chunks_related_entity_ids_is_list_string():
    fm = field_map(TABLE_SCHEMAS["chunks"])
    assert fm["related_entity_ids"].type == _LIST_STR_TYPE


def test_chunks_entity_search_keys_present_and_list_string():
    """§11 AI Search linkage column."""
    fm = field_map(TABLE_SCHEMAS["chunks"])
    assert "entity_search_keys" in fm
    assert fm["entity_search_keys"].type == _LIST_STR_TYPE
    assert fm["entity_search_keys"].nullable is True


# ---------------------------------------------------------------------------
# entities (§3.5 + §11)
# ---------------------------------------------------------------------------


def test_entities_required_columns():
    fm = field_map(TABLE_SCHEMAS["entities"])
    for col in (
        "entity_id", "entity_type", "display_name", "canonical_key",
        "is_placeholder", "content_hash", "created_at", "updated_at",
    ):
        assert fm[col].nullable is False, f"{col} should be NOT NULL"


def test_entities_aliases_is_list_string():
    fm = field_map(TABLE_SCHEMAS["entities"])
    assert fm["aliases"].type == _LIST_STR_TYPE


def test_entities_search_aliases_present_and_list_string():
    """§11 AI Search linkage column."""
    fm = field_map(TABLE_SCHEMAS["entities"])
    assert "search_aliases" in fm
    assert fm["search_aliases"].type == _LIST_STR_TYPE
    assert fm["search_aliases"].nullable is True


def test_entities_confidence_is_float64():
    fm = field_map(TABLE_SCHEMAS["entities"])
    assert fm["confidence"].type == pa.float64()


def test_entities_is_placeholder_is_bool():
    fm = field_map(TABLE_SCHEMAS["entities"])
    assert fm["is_placeholder"].type == pa.bool_()


def test_entities_updated_at_type():
    fm = field_map(TABLE_SCHEMAS["entities"])
    assert fm["updated_at"].type == _TS_TYPE


# ---------------------------------------------------------------------------
# relationships (§3.6)
# ---------------------------------------------------------------------------


def test_relationships_required_columns():
    fm = field_map(TABLE_SCHEMAS["relationships"])
    for col in (
        "relationship_id", "relationship_type", "source_entity_id",
        "target_entity_id", "is_placeholder", "content_hash", "created_at",
    ):
        assert fm[col].nullable is False, f"{col} should be NOT NULL"


def test_relationships_evidence_id_nullable():
    fm = field_map(TABLE_SCHEMAS["relationships"])
    assert fm["evidence_id"].nullable is True


# ---------------------------------------------------------------------------
# evidence (§3.7)
# ---------------------------------------------------------------------------


def test_evidence_required_columns():
    fm = field_map(TABLE_SCHEMAS["evidence"])
    for col in ("evidence_id", "source_file_id", "source_type", "content_hash", "created_at"):
        assert fm[col].nullable is False, f"{col} should be NOT NULL"


def test_evidence_all_fk_cols_nullable():
    fm = field_map(TABLE_SCHEMAS["evidence"])
    for col in (
        "document_element_id", "chunk_id", "image_id",
        "callout_id", "visual_region_id", "figure_id",
    ):
        assert fm[col].nullable is True, f"{col} should be NULLABLE"


# ---------------------------------------------------------------------------
# visual_assets (§3.8)
# ---------------------------------------------------------------------------


def test_visual_assets_required_columns():
    fm = field_map(TABLE_SCHEMAS["visual_assets"])
    for col in ("image_id", "source_file_id", "asset_type", "image_hash", "is_placeholder", "created_at"):
        assert fm[col].nullable is False, f"{col} should be NOT NULL"


def test_visual_assets_dimensions_int32():
    fm = field_map(TABLE_SCHEMAS["visual_assets"])
    assert fm["width"].type == pa.int32()
    assert fm["height"].type == pa.int32()


# ---------------------------------------------------------------------------
# visual_regions (§3.9)
# ---------------------------------------------------------------------------


def test_visual_regions_required_columns():
    fm = field_map(TABLE_SCHEMAS["visual_regions"])
    for col in ("visual_region_id", "image_id", "region_type", "created_at"):
        assert fm[col].nullable is False, f"{col} should be NOT NULL"


def test_visual_regions_polygon_cols_nullable():
    fm = field_map(TABLE_SCHEMAS["visual_regions"])
    for col in ("polygon_json", "normalized_polygon_json", "label", "text", "blob_url"):
        assert fm[col].nullable is True, f"{col} should be NULLABLE"


def test_visual_regions_identified_entity_id_nullable():
    fm = field_map(TABLE_SCHEMAS["visual_regions"])
    assert fm["identified_entity_id"].nullable is True
