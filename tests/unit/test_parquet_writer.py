"""Unit tests for fabric_kg_builder.parquet.writer — SPEC-002 §7 & §8."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fabric_kg_builder.model.arrow_schemas import TABLE_SCHEMAS
from fabric_kg_builder.model.ids import (
    content_hash as compute_content_hash,
    make_entity_id,
    make_relationship_id,
    make_chunk_id,
)
from fabric_kg_builder.parquet.writer import (
    write_all_placeholders,
    write_all_tables,
    write_placeholder,
    write_table,
    safe_json_str,
)

_UTC = timezone.utc
_NOW = datetime(2026, 6, 24, 12, 0, 0, tzinfo=_UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entity(
    entity_type: str = "Device",
    display_name: str = "Surface Laptop 5",
    source_file_id: str = "src:abc123",
) -> dict:
    eid = make_entity_id(entity_type, display_name)
    ch = compute_content_hash(f"{eid}{entity_type}{display_name}")
    return {
        "entity_id": eid,
        "entity_type": entity_type,
        "display_name": display_name,
        "canonical_key": f"{entity_type.lower()}:{display_name.lower().replace(' ', '-')}",
        "aliases": ["SL5"],
        "search_aliases": ["surface laptop 5", "sl5"],
        "description": "Flagship Surface laptop",
        "properties_json": safe_json_str({"sku": "RBH-00001"}),
        "source_file_id": source_file_id,
        "confidence": 0.95,
        "is_placeholder": False,
        "content_hash": ch,
        "created_at": _NOW,
        "updated_at": _NOW,
    }


def _make_relationship(
    source_id: str, target_id: str, rel_type: str = "HAS_COMPONENT"
) -> dict:
    rid = make_relationship_id(rel_type, source_id, target_id)
    ch = compute_content_hash(f"{rid}{rel_type}")
    return {
        "relationship_id": rid,
        "relationship_type": rel_type,
        "source_entity_id": source_id,
        "target_entity_id": target_id,
        "evidence_id": None,
        "properties_json": None,
        "confidence": 0.9,
        "is_placeholder": False,
        "content_hash": ch,
        "created_at": _NOW,
    }


def _make_chunk(source_file_id: str = "src:abc123") -> dict:
    content = "Surface Laptop 5 battery replacement procedure."
    ch = compute_content_hash(content)
    cid = make_chunk_id(source_file_id, "section_text", ch)
    return {
        "chunk_id": cid,
        "source_file_id": source_file_id,
        "document_element_id": None,
        "chunk_type": "section_text",
        "content": content,
        "content_html": f"<p>{content}</p>",
        "embedding_text": content,
        "blob_url": None,
        "page_number": None,
        "section_path": "Repair > Battery",
        "table_id": None,
        "figure_id": None,
        "image_id": None,
        "related_entity_ids": ["entity:abc", "entity:def"],
        "entity_search_keys": ["surface laptop 5", "battery"],
        "content_hash": ch,
        "created_at": _NOW,
    }


# ---------------------------------------------------------------------------
# safe_json_str
# ---------------------------------------------------------------------------


def test_safe_json_str_none() -> None:
    assert safe_json_str(None) is None


def test_safe_json_str_dict() -> None:
    result = safe_json_str({"b": 2, "a": 1})
    import json
    parsed = json.loads(result)
    assert parsed == {"a": 1, "b": 2}  # sort_keys=True


def test_safe_json_str_list() -> None:
    result = safe_json_str([1, 2, 3])
    import json
    assert json.loads(result) == [1, 2, 3]


# ---------------------------------------------------------------------------
# write_table — entities
# ---------------------------------------------------------------------------


class TestWriteEntities:
    def setup_method(self, tmp_path_factory) -> None:
        pass  # tmp_path is fixture-provided below

    def test_write_single_entity(self, tmp_path: Path) -> None:
        row = _make_entity()
        out = write_table("entities", [row], tmp_path)
        assert out.exists()
        assert out.name == "entities.parquet"

    def test_round_trip_entity(self, tmp_path: Path) -> None:
        row = _make_entity()
        write_table("entities", [row], tmp_path)
        table = pq.read_table(tmp_path / "entities.parquet")
        assert table.num_rows == 1
        col = table.schema.field("entity_id")
        assert col.type == pa.string()

    def test_list_columns_preserved(self, tmp_path: Path) -> None:
        row = _make_entity()
        write_table("entities", [row], tmp_path)
        table = pq.read_table(tmp_path / "entities.parquet")
        aliases = table.column("aliases")[0].as_py()
        assert isinstance(aliases, list)
        assert "SL5" in aliases

    def test_search_aliases_preserved(self, tmp_path: Path) -> None:
        row = _make_entity()
        write_table("entities", [row], tmp_path)
        table = pq.read_table(tmp_path / "entities.parquet")
        sa = table.column("search_aliases")[0].as_py()
        assert "surface laptop 5" in sa

    def test_timestamp_utc_preserved(self, tmp_path: Path) -> None:
        row = _make_entity()
        write_table("entities", [row], tmp_path)
        table = pq.read_table(tmp_path / "entities.parquet")
        ts = table.column("created_at")[0].as_py()
        assert ts.tzinfo is not None

    def test_null_nullable_columns_ok(self, tmp_path: Path) -> None:
        row = _make_entity()
        row["aliases"] = None
        row["description"] = None
        write_table("entities", [row], tmp_path)
        table = pq.read_table(tmp_path / "entities.parquet")
        assert table.column("aliases")[0].as_py() is None

    def test_not_null_violation_raises(self, tmp_path: Path) -> None:
        row = _make_entity()
        row["entity_id"] = None
        with pytest.raises(ValueError, match="NOT NULL"):
            write_table("entities", [row], tmp_path)

    def test_unknown_table_raises(self, tmp_path: Path) -> None:
        with pytest.raises(KeyError, match="Unknown table"):
            write_table("nonexistent_table", [{}], tmp_path)


# ---------------------------------------------------------------------------
# write_table — relationships
# ---------------------------------------------------------------------------


def test_round_trip_relationships(tmp_path: Path) -> None:
    e1 = _make_entity("Device", "Surface Laptop 5")
    e2 = _make_entity("Component", "Battery")
    row = _make_relationship(e1["entity_id"], e2["entity_id"])
    write_table("relationships", [row], tmp_path)
    table = pq.read_table(tmp_path / "relationships.parquet")
    assert table.num_rows == 1
    assert table.column("relationship_type")[0].as_py() == "HAS_COMPONENT"
    assert table.column("is_placeholder")[0].as_py() is False


# ---------------------------------------------------------------------------
# write_table — chunks
# ---------------------------------------------------------------------------


def test_round_trip_chunks(tmp_path: Path) -> None:
    chunk = _make_chunk()
    write_table("chunks", [chunk], tmp_path)
    table = pq.read_table(tmp_path / "chunks.parquet")
    assert table.num_rows == 1
    rel_ids = table.column("related_entity_ids")[0].as_py()
    assert isinstance(rel_ids, list)
    assert "entity:abc" in rel_ids
    keys = table.column("entity_search_keys")[0].as_py()
    assert "battery" in keys


# ---------------------------------------------------------------------------
# write_table — source_files
# ---------------------------------------------------------------------------


def test_round_trip_source_files(tmp_path: Path) -> None:
    ch = compute_content_hash("examples/csv/sample.csv")
    row = {
        "source_file_id": f"src:{ch[:32]}",
        "path": "examples/csv/sample.csv",
        "filename": "sample.csv",
        "source_type": "csv",
        "content_hash": ch,
        "byte_size": 512,
        "ingested_at": _NOW,
        "schema_profile_path": "build/enriched/sample_profile.json",
        "row_count": 6,
        "notes": None,
    }
    write_table("source_files", [row], tmp_path)
    table = pq.read_table(tmp_path / "source_files.parquet")
    assert table.num_rows == 1
    assert table.column("source_type")[0].as_py() == "csv"


# ---------------------------------------------------------------------------
# write_all_tables
# ---------------------------------------------------------------------------


def test_write_all_tables(tmp_path: Path) -> None:
    entity_row = _make_entity()
    chunk_row = _make_chunk()
    rel_row = _make_relationship(entity_row["entity_id"], entity_row["entity_id"])
    results = write_all_tables(
        {"entities": [entity_row], "chunks": [chunk_row], "relationships": [rel_row]},
        tmp_path,
    )
    assert len(results) == 3
    for path in results.values():
        assert path.exists()


# ---------------------------------------------------------------------------
# Placeholder tests — SPEC-002 §8
# ---------------------------------------------------------------------------


class TestWritePlaceholder:
    def test_writes_all_8_tables(self, tmp_path: Path) -> None:
        results = write_all_placeholders(tmp_path)
        assert len(results) == 8
        assert set(results.keys()) == set(TABLE_SCHEMAS.keys())

    def test_placeholder_files_exist(self, tmp_path: Path) -> None:
        results = write_all_placeholders(tmp_path)
        for name, path in results.items():
            assert path.exists(), f"Placeholder missing: {name}"
            assert path.name == "_placeholder.parquet"

    def test_placeholder_files_are_readable(self, tmp_path: Path) -> None:
        results = write_all_placeholders(tmp_path)
        for name, path in results.items():
            table = pq.read_table(path)
            assert table.num_rows == 0 or table.num_rows == 1  # spec says 1 sentinel row

    def test_placeholder_schemas_match_canonical(self, tmp_path: Path) -> None:
        results = write_all_placeholders(tmp_path)
        for name, path in results.items():
            table = pq.read_table(path)
            expected_schema = TABLE_SCHEMAS[name]
            # Column names must match
            assert list(table.schema.names) == list(expected_schema.names), (
                f"Schema mismatch for {name}: "
                f"{table.schema.names} != {expected_schema.names}"
            )

    def test_entities_placeholder_is_placeholder_true(self, tmp_path: Path) -> None:
        write_placeholder("entities", tmp_path)
        table = pq.read_table(tmp_path / "entities" / "_placeholder.parquet")
        assert table.column("is_placeholder")[0].as_py() is True

    def test_placeholder_unknown_table_raises(self, tmp_path: Path) -> None:
        with pytest.raises(KeyError, match="Unknown table"):
            write_placeholder("not_a_table", tmp_path)

    def test_single_table_placeholder(self, tmp_path: Path) -> None:
        for name in TABLE_SCHEMAS:
            path = write_placeholder(name, tmp_path / "single")
            assert path.exists()
            table = pq.read_table(path)
            # Verify schema field names
            assert set(table.schema.names) == set(TABLE_SCHEMAS[name].names)
