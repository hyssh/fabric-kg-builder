"""Unit tests for the compile-data CLI command.

Tests written per SPEC-001 §7 compile-data contract and SPEC-002 §9
VAL-001..VAL-007 data-integrity gates.

Fixtures
--------
- ``_clean_fixture`` — minimal canonical enriched JSON, all IDs unique, FKs valid
- ``_dup_entity_fixture`` — same entity_id in two rows → triggers VAL-001
- ``_dangling_fk_fixture`` — relationship with non-existent source_entity_id → VAL-005
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli.compile_data_cmd import compile_data_cmd
from tests.conftest import combined_output, make_cli_runner  # noqa: F401
from fabric_kg_builder.model.ids import (
    content_hash,
    make_chunk_id,
    make_entity_id,
    make_evidence_id,
    make_relationship_id,
)

_UTC = timezone.utc
_NOW = "2026-06-24T12:00:00+00:00"

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _make_entity_row(
    entity_type: str = "Device",
    display_name: str = "Surface Laptop 5",
    source_file_id: str = "src:abc123",
    *,
    override_entity_id: str | None = None,
) -> dict:
    entity_id = override_entity_id or make_entity_id(entity_type, display_name)
    ck = f"{entity_type.lower()}:{display_name.lower().replace(' ', '-')}"
    ch = content_hash(ck)
    return {
        "entity_id": entity_id,
        "entity_type": entity_type,
        "display_name": display_name,
        "canonical_key": ck,
        "aliases": [],
        "search_aliases": None,
        "description": None,
        "properties_json": None,
        "source_file_id": source_file_id,
        "confidence": 0.9,
        "is_placeholder": False,
        "content_hash": ch,
        "created_at": _NOW,
        "updated_at": _NOW,
    }


def _make_relationship_row(
    source_entity_id: str,
    target_entity_id: str,
    rel_type: str = "HAS_COMPONENT",
    evidence_id: str | None = None,
) -> dict:
    rel_id = make_relationship_id(rel_type, source_entity_id, target_entity_id)
    ch = content_hash(f"{rel_type}:{source_entity_id}:{target_entity_id}")
    return {
        "relationship_id": rel_id,
        "relationship_type": rel_type,
        "source_entity_id": source_entity_id,
        "target_entity_id": target_entity_id,
        "evidence_id": evidence_id,
        "properties_json": None,
        "confidence": 0.85,
        "is_placeholder": False,
        "content_hash": ch,
        "created_at": _NOW,
    }


def _make_chunk_row(source_file_id: str = "src:abc123") -> dict:
    text = "Surface Laptop 5 has a replaceable battery."
    ch = content_hash(text)
    cid = make_chunk_id(source_file_id, "section_text", ch)
    return {
        "chunk_id": cid,
        "source_file_id": source_file_id,
        "document_element_id": None,
        "chunk_type": "section_text",
        "content": text,
        "content_html": None,
        "embedding_text": text,
        "blob_url": None,
        "page_number": None,
        "section_path": "Repair > Battery",
        "table_id": None,
        "figure_id": None,
        "image_id": None,
        "related_entity_ids": None,
        "entity_search_keys": None,
        "content_hash": ch,
        "created_at": _NOW,
    }


def _make_evidence_row(source_file_id: str = "src:abc123") -> dict:
    ev_id = make_evidence_id(source_file_id, "document_span", "1:0:1", content_hash("evidence text"))
    ch = content_hash("evidence text")
    return {
        "evidence_id": ev_id,
        "source_file_id": source_file_id,
        "source_type": "document_span",
        "document_element_id": None,
        "chunk_id": None,
        "page_number": 1,
        "section_path": None,
        "table_id": None,
        "row_index": None,
        "col_index": None,
        "figure_id": None,
        "image_id": None,
        "callout_id": None,
        "visual_region_id": None,
        "blob_url": None,
        "text": "evidence text",
        "content_hash": ch,
        "created_at": _NOW,
    }


def _clean_fixture() -> dict:
    """Minimal clean fixture: 2 entities, 1 relationship, 1 chunk, 1 evidence."""
    e1 = _make_entity_row("Device", "Surface Laptop 5")
    e2 = _make_entity_row("Component", "Battery")
    rel = _make_relationship_row(e1["entity_id"], e2["entity_id"])
    chunk = _make_chunk_row()
    evidence = _make_evidence_row()
    return {
        "source_file_id": "src:abc123",
        "pass": "p2",
        "entities": [e1, e2],
        "relationships": [rel],
        "chunks": [chunk],
        "evidence": [evidence],
    }


def _dup_entity_fixture() -> dict:
    """Fixture with duplicate entity_id — triggers VAL-001."""
    e1 = _make_entity_row("Device", "Surface Laptop 5")
    # Second row with SAME entity_id but different type  
    e2 = _make_entity_row("Device", "Surface Laptop 5")  # same ID
    e2["entity_type"] = "Product"  # mutate type — same ID, different content
    chunk = _make_chunk_row()
    return {
        "source_file_id": "src:dup",
        "pass": "p2",
        "entities": [e1, e2],
        "relationships": [],
        "chunks": [chunk],
        "evidence": [],
    }


def _dangling_fk_fixture() -> dict:
    """Fixture with a relationship pointing to a non-existent entity_id — VAL-005."""
    e1 = _make_entity_row("Device", "Surface Laptop 5")
    rel = _make_relationship_row(
        source_entity_id=e1["entity_id"],
        target_entity_id="entity:nonexistent_does_not_exist",
    )
    chunk = _make_chunk_row()
    return {
        "source_file_id": "src:dangle",
        "pass": "p2",
        "entities": [e1],
        "relationships": [rel],
        "chunks": [chunk],
        "evidence": [],
    }


# ---------------------------------------------------------------------------
# Helper: write fixture to a tmp input dir
# ---------------------------------------------------------------------------


def _write_input(tmp_path: Path, fixture: dict, filename: str = "batch_p2.json") -> Path:
    input_dir = tmp_path / "enriched"
    input_dir.mkdir(parents=True, exist_ok=True)
    (input_dir / filename).write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    return input_dir


# ---------------------------------------------------------------------------
# Tests: happy path
# ---------------------------------------------------------------------------


class TestCompileDataHappyPath:
    def test_exits_zero_with_clean_fixture(self, tmp_path: Path) -> None:
        input_dir = _write_input(tmp_path, _clean_fixture())
        out_dir = tmp_path / "parquet"

        runner = CliRunner()
        result = runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        assert result.exit_code == 0, (
            f"Expected exit 0, got {result.exit_code}.\nOutput:\n{result.output}"
        )

    def test_writes_8_parquet_files(self, tmp_path: Path) -> None:
        input_dir = _write_input(tmp_path, _clean_fixture())
        out_dir = tmp_path / "parquet"

        runner = CliRunner()
        runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        parquet_files = {p.stem for p in out_dir.glob("*.parquet")}
        expected = {
            "entities", "relationships", "chunks", "evidence",
            "source_files", "document_elements", "visual_assets", "visual_regions",
        }
        assert expected == parquet_files, (
            f"Missing or unexpected Parquet files.\n"
            f"Expected: {expected}\nGot: {parquet_files}"
        )

    def test_entities_parquet_is_readable(self, tmp_path: Path) -> None:
        input_dir = _write_input(tmp_path, _clean_fixture())
        out_dir = tmp_path / "parquet"

        runner = CliRunner()
        runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        table = pq.read_table(out_dir / "entities.parquet")
        assert table.num_rows == 2

    def test_relationships_parquet_is_readable(self, tmp_path: Path) -> None:
        input_dir = _write_input(tmp_path, _clean_fixture())
        out_dir = tmp_path / "parquet"

        runner = CliRunner()
        runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        table = pq.read_table(out_dir / "relationships.parquet")
        assert table.num_rows == 1

    def test_chunks_parquet_is_readable(self, tmp_path: Path) -> None:
        input_dir = _write_input(tmp_path, _clean_fixture())
        out_dir = tmp_path / "parquet"

        runner = CliRunner()
        runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        table = pq.read_table(out_dir / "chunks.parquet")
        assert table.num_rows == 1

    def test_summary_printed_on_success(self, tmp_path: Path) -> None:
        input_dir = _write_input(tmp_path, _clean_fixture())
        out_dir = tmp_path / "parquet"

        runner = CliRunner()
        result = runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        assert "Summary" in result.output
        assert "entities" in result.output
        assert "relationships" in result.output

    def test_multiple_json_files_merged(self, tmp_path: Path) -> None:
        """Two separate batch files with distinct entities are both written."""
        input_dir = tmp_path / "enriched"
        input_dir.mkdir(parents=True)

        e1 = _make_entity_row("Device", "Surface Laptop 5")
        e2 = _make_entity_row("Device", "Surface Pro 9")
        chunk1 = _make_chunk_row("src:file1")
        chunk2 = _make_chunk_row("src:file2")
        # Give chunk2 a unique content so it gets a different chunk_id
        chunk2["content"] = "Surface Pro 9 repair guide."
        chunk2["content_hash"] = content_hash(chunk2["content"])
        chunk2["chunk_id"] = make_chunk_id("src:file2", "section_text", chunk2["content_hash"])

        (input_dir / "batch1.json").write_text(
            json.dumps({"entities": [e1], "relationships": [], "chunks": [chunk1], "evidence": []}),
            encoding="utf-8",
        )
        (input_dir / "batch2.json").write_text(
            json.dumps({"entities": [e2], "relationships": [], "chunks": [chunk2], "evidence": []}),
            encoding="utf-8",
        )

        out_dir = tmp_path / "parquet"
        runner = CliRunner()
        result = runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        assert result.exit_code == 0, result.output
        table = pq.read_table(out_dir / "entities.parquet")
        assert table.num_rows == 2


# ---------------------------------------------------------------------------
# Tests: VAL-001 — duplicate entity_id
# ---------------------------------------------------------------------------


class TestValDuplicateEntityId:
    """Duplicate entity_id is resolved by MERGE (canonical entity resolution),
    not treated as a fatal error — the same entity extracted across sections is
    combined into one row. See _resolve_duplicates in compile_data_cmd."""

    def test_exits_zero_merging_duplicates(self, tmp_path: Path) -> None:
        input_dir = _write_input(tmp_path, _dup_entity_fixture())
        out_dir = tmp_path / "parquet"

        runner = CliRunner()
        result = runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        assert result.exit_code == 0, (
            f"Expected exit 0 (duplicate entity_id merged), got {result.exit_code}.\n"
            f"Output:\n{result.output}"
        )

    def test_reports_resolution(self, tmp_path: Path) -> None:
        input_dir = _write_input(tmp_path, _dup_entity_fixture())
        out_dir = tmp_path / "parquet"

        runner = CliRunner()
        result = runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        assert "Resolved duplicates" in result.output, (
            f"Expected duplicate-resolution notice.\nOutput:\n{result.output}"
        )

    def test_merges_to_single_entity_row(self, tmp_path: Path) -> None:
        input_dir = _write_input(tmp_path, _dup_entity_fixture())
        out_dir = tmp_path / "parquet"

        runner = CliRunner()
        result = runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        assert result.exit_code == 0, result.output
        table = pq.read_table(out_dir / "entities.parquet")
        eids = table.column("entity_id").to_pylist()
        assert len(eids) == len(set(eids)), (
            f"Expected unique entity_ids after merge, got {eids}"
        )


# ---------------------------------------------------------------------------
# Tests: VAL-005/VAL-006 — dangling relationship FK
# ---------------------------------------------------------------------------


class TestValDanglingRelFk:
    def test_exits_5_on_dangling_source_entity(self, tmp_path: Path) -> None:
        input_dir = _write_input(tmp_path, _dangling_fk_fixture())
        out_dir = tmp_path / "parquet"

        runner = CliRunner()
        result = runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        assert result.exit_code == 5, (
            f"Expected exit 5 for dangling FK, got {result.exit_code}.\n"
            f"Output:\n{result.output}"
        )

    def test_reports_val006_violation(self, tmp_path: Path) -> None:
        input_dir = _write_input(tmp_path, _dangling_fk_fixture())
        out_dir = tmp_path / "parquet"

        runner = make_cli_runner()
        result = runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        assert "VAL-006" in combined_output(result), (
            f"Expected VAL-006 in output.\nOutput:\n{combined_output(result)}"
        )


# ---------------------------------------------------------------------------
# Tests: edge cases
# ---------------------------------------------------------------------------


class TestCompileDataEdgeCases:
    def test_missing_input_dir_exits_nonzero(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            compile_data_cmd,
            ["--input", str(tmp_path / "does_not_exist"), "--out", str(tmp_path / "out")],
        )
        assert result.exit_code != 0

    def test_empty_input_dir_exits_zero(self, tmp_path: Path) -> None:
        """Empty enriched dir (no JSON files) should write empty Parquet and exit 0."""
        input_dir = tmp_path / "enriched"
        input_dir.mkdir()
        out_dir = tmp_path / "parquet"

        runner = CliRunner()
        result = runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        assert result.exit_code == 0, (
            f"Empty input dir should exit 0, got {result.exit_code}.\n"
            f"Output:\n{result.output}"
        )

    def test_checkpoint_json_is_skipped(self, tmp_path: Path) -> None:
        """build/enriched/.checkpoint.json must NOT be parsed as a batch file."""
        input_dir = tmp_path / "enriched"
        input_dir.mkdir()
        # Write a .checkpoint.json that looks like an orchestrator checkpoint
        (input_dir / ".checkpoint.json").write_text(
            json.dumps({"completed": ["src:abc"]}), encoding="utf-8"
        )
        out_dir = tmp_path / "parquet"

        runner = CliRunner()
        result = runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        assert result.exit_code == 0, (
            f".checkpoint.json must be skipped.\nOutput:\n{result.output}"
        )

    def test_output_dir_created_if_absent(self, tmp_path: Path) -> None:
        input_dir = _write_input(tmp_path, _clean_fixture())
        out_dir = tmp_path / "deep" / "nested" / "parquet"

        runner = CliRunner()
        result = runner.invoke(
            compile_data_cmd,
            ["--input", str(input_dir), "--out", str(out_dir)],
        )
        assert result.exit_code == 0
        assert out_dir.exists()
