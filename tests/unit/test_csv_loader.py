"""Unit tests for fabric_kg_builder.sources.csv_loader — SPEC-002 §6."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fabric_kg_builder.sources.csv_loader import CsvLoaderError, load_csv


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_CSV = Path(__file__).parent.parent.parent / "examples" / "csv" / "sample.csv"
FIXTURES_CSV = Path(__file__).parent.parent / "fixtures" / "csv" / "sample.csv"


# ---------------------------------------------------------------------------
# Happy-path: parse examples/csv/sample.csv
# ---------------------------------------------------------------------------


class TestLoadSampleCsv:
    """Load the canonical sample CSV from examples/csv/sample.csv."""

    def setup_method(self) -> None:
        assert SAMPLE_CSV.exists(), f"Missing sample CSV: {SAMPLE_CSV}"
        self.result = load_csv(SAMPLE_CSV)

    def test_source_file_row_is_populated(self) -> None:
        sf = self.result.source_file
        assert sf.source_type == "csv"
        assert sf.filename == "sample.csv"
        assert sf.content_hash  # non-empty SHA-256
        assert sf.source_file_id.startswith("src:")
        assert sf.byte_size and sf.byte_size > 0
        assert sf.ingested_at is not None

    def test_row_count_matches_data_rows(self) -> None:
        # sample.csv has 6 data rows (not counting header)
        assert self.result.source_file.row_count == 6

    def test_document_elements_produced(self) -> None:
        # 1 header "table" element + 6 data "table_row" elements
        elements = self.result.document_elements
        assert len(elements) == 7

    def test_header_element_has_correct_type(self) -> None:
        header_elem = self.result.document_elements[0]
        assert header_elem.element_type == "table"
        assert header_elem.sort_order == 0
        assert "device_id" in (header_elem.content or "")

    def test_data_row_elements_have_correct_type(self) -> None:
        for elem in self.result.document_elements[1:]:
            assert elem.element_type == "table_row"
            assert elem.parent_element_id == self.result.document_elements[0].document_element_id

    def test_data_row_content_is_serialized(self) -> None:
        row_elem = self.result.document_elements[1]
        assert "surface-laptop-5" in (row_elem.content or "")

    def test_deterministic_ids(self) -> None:
        """Loading the same file twice produces the same IDs."""
        r2 = load_csv(SAMPLE_CSV)
        assert r2.source_file.source_file_id == self.result.source_file.source_file_id
        assert (
            r2.document_elements[0].document_element_id
            == self.result.document_elements[0].document_element_id
        )

    def test_schema_profile_structure(self) -> None:
        sp = self.result.schema_profile
        assert sp["schema_profile_version"] == "1"
        assert sp["source_type"] == "csv"
        assert sp["row_count"] == 6
        assert sp["column_count"] == 6
        assert len(sp["columns"]) == 6
        assert sp["warnings"] == []

    def test_schema_profile_column_names(self) -> None:
        col_names = [c["name"] for c in self.result.schema_profile["columns"]]
        assert "device_id" in col_names
        assert "part_number" in col_names
        assert "quantity" in col_names

    def test_schema_profile_type_inference(self) -> None:
        """quantity column should infer as integer."""
        cols = {c["name"]: c for c in self.result.schema_profile["columns"]}
        assert cols["quantity"]["inferred_type"] == "integer"
        assert cols["device_id"]["inferred_type"] == "string"

    def test_schema_profile_sample_values(self) -> None:
        cols = {c["name"]: c for c in self.result.schema_profile["columns"]}
        assert "surface-laptop-5" in cols["device_id"]["sample_values"]

    def test_schema_profile_unique_counts(self) -> None:
        cols = {c["name"]: c for c in self.result.schema_profile["columns"]}
        # device_id has 2 unique values (surface-laptop-5, surface-pro-9)
        assert cols["device_id"]["unique_count"] == 2

    def test_schema_profile_is_json_serialisable(self) -> None:
        """schema_profile must be JSON-safe."""
        json.dumps(self.result.schema_profile)


# ---------------------------------------------------------------------------
# Happy-path: fixture CSV (tests/fixtures/csv/sample.csv)
# ---------------------------------------------------------------------------


def test_load_fixture_csv() -> None:
    assert FIXTURES_CSV.exists(), f"Missing fixture CSV: {FIXTURES_CSV}"
    result = load_csv(FIXTURES_CSV)
    assert result.source_file.row_count == 3  # fixture has 3 data rows
    assert len(result.document_elements) == 4  # 1 header + 3 data rows


# ---------------------------------------------------------------------------
# TSV support
# ---------------------------------------------------------------------------


def test_load_tsv(tmp_path: Path) -> None:
    tsv = tmp_path / "parts.tsv"
    tsv.write_text("name\tpart_no\tqty\nBattery\tM001\t1\nDisplay\tM002\t1\n", encoding="utf-8")
    result = load_csv(tsv)
    assert result.source_file.source_type == "tsv"
    assert result.source_file.row_count == 2
    assert len(result.document_elements) == 3


# ---------------------------------------------------------------------------
# BOM handling
# ---------------------------------------------------------------------------


def test_bom_stripped(tmp_path: Path) -> None:
    bom_csv = tmp_path / "bom.csv"
    # Write UTF-8 BOM + content
    bom_csv.write_bytes(b"\xef\xbb\xbfname,value\nAlpha,1\nBeta,2\n")
    result = load_csv(bom_csv)
    col_names = [c["name"] for c in result.schema_profile["columns"]]
    assert col_names[0] == "name"  # BOM stripped — not '\ufeffname'


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_empty_file_raises(tmp_path: Path) -> None:
    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(CsvLoaderError, match="empty"):
        load_csv(empty)


def test_header_only_raises(tmp_path: Path) -> None:
    """A CSV with only a header row (no data) should return 0 data rows — not raise.
    
    The spec doesn't say header-only is invalid; it's a valid empty table.
    """
    header_only = tmp_path / "header.csv"
    header_only.write_text("name,value\n", encoding="utf-8")
    result = load_csv(header_only)
    assert result.source_file.row_count == 0
    # 1 header element, 0 data elements
    assert len(result.document_elements) == 1


def test_missing_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_csv("/nonexistent/path/to/file.csv")


def test_unsupported_extension_raises(tmp_path: Path) -> None:
    bad = tmp_path / "data.json"
    bad.write_text('{"a": 1}')
    with pytest.raises(CsvLoaderError, match="Unsupported"):
        load_csv(bad)


def test_whitespace_only_file_raises(tmp_path: Path) -> None:
    ws = tmp_path / "spaces.csv"
    ws.write_text("   \n\n   \n", encoding="utf-8")
    with pytest.raises(CsvLoaderError):
        load_csv(ws)


def test_schema_profile_path_stored(tmp_path: Path) -> None:
    csv_file = tmp_path / "data.csv"
    csv_file.write_text("a,b\n1,2\n")
    profile_path = "build/enriched/data_profile.json"
    result = load_csv(csv_file, schema_profile_path=profile_path)
    assert result.source_file.schema_profile_path == profile_path


# ---------------------------------------------------------------------------
# XLSX (openpyxl)
# ---------------------------------------------------------------------------


def test_load_xlsx(tmp_path: Path) -> None:
    pytest.importorskip("openpyxl")
    import openpyxl  # noqa: PLC0415

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Parts"
    ws.append(["name", "part_no", "qty"])
    ws.append(["Battery", "M001", 1])
    ws.append(["Display", "M002", 1])
    wb.save(tmp_path / "parts.xlsx")

    result = load_csv(tmp_path / "parts.xlsx")
    assert result.source_file.source_type == "xlsx"
    assert result.source_file.row_count == 2
    sp = result.schema_profile
    assert sp["column_count"] == 3
