"""Tests for lineage/common.py — shared helpers for lineage v2."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from fabric_kg_builder.lineage.common import (
    TABLE_ID_FIELDS,
    build_source_locator,
    default_project_id,
    infer_media_type,
    normalize_source_uri,
    now_utc,
    safe_original_name,
)


class TestNowUtc:
    def test_returns_datetime(self):
        from datetime import datetime, timezone
        dt = now_utc()
        assert isinstance(dt, datetime)
        assert dt.tzinfo is not None

    def test_close_to_now(self):
        from datetime import datetime, timezone, timedelta
        dt = now_utc()
        now = datetime.now(timezone.utc)
        assert abs((dt - now).total_seconds()) < 5


class TestDefaultProjectId:
    def test_returns_string(self):
        pid = default_project_id()
        assert isinstance(pid, str)
        assert len(pid) > 0

    def test_env_var_override(self):
        os.environ["FABRIC_KG_PROJECT_ID"] = "test-project-id"
        try:
            pid = default_project_id()
            assert pid == "test-project-id"
        finally:
            del os.environ["FABRIC_KG_PROJECT_ID"]


class TestNormalizeSourceUri:
    def test_path_object_converts_to_uri(self, tmp_path):
        f = tmp_path / "test.pdf"
        f.touch()
        uri = normalize_source_uri(f)
        assert uri.startswith("file://")
        assert "test.pdf" in uri

    def test_already_uri_unchanged(self):
        uri = "https://example.com/doc.pdf"
        result = normalize_source_uri(uri)
        assert result == uri

    def test_relative_string_resolved(self):
        result = normalize_source_uri("test.pdf")
        # Should be resolved to absolute
        assert result.startswith("file://")

    def test_s3_uri_preserved(self):
        uri = "s3://my-bucket/prefix/file.pdf"
        result = normalize_source_uri(uri)
        assert result == uri


class TestInferMediaType:
    def test_pdf(self):
        assert infer_media_type("doc.pdf") == "application/pdf"

    def test_docx(self):
        mt = infer_media_type("doc.docx")
        assert "word" in mt or "openxml" in mt

    def test_unknown_extension(self):
        mt = infer_media_type("file.unknownext123")
        assert mt == "application/octet-stream"

    def test_csv(self):
        mt = infer_media_type("data.csv")
        assert "csv" in mt or "text" in mt

    def test_json(self):
        mt = infer_media_type("data.json")
        assert "json" in mt


class TestSafeOriginalName:
    def test_plain_filename(self):
        assert safe_original_name("document.pdf") == "document.pdf"

    def test_replaces_special_chars(self):
        result = safe_original_name("file with spaces.pdf")
        assert " " not in result
        assert result.endswith(".pdf")

    def test_extracts_basename(self):
        result = safe_original_name("/path/to/file.pdf")
        assert "/" not in result
        assert "file.pdf" in result

    def test_empty_name_defaults(self):
        result = safe_original_name("")
        assert result == "asset.bin"

    def test_preserves_hyphens_and_dots(self):
        assert safe_original_name("my-file.v1.pdf") == "my-file.v1.pdf"


class TestTableIdFields:
    def test_known_tables_present(self):
        assert "entities" in TABLE_ID_FIELDS
        assert "relationships" in TABLE_ID_FIELDS
        assert "chunks" in TABLE_ID_FIELDS
        assert "processing_runs" in TABLE_ID_FIELDS

    def test_entities_id_field(self):
        assert TABLE_ID_FIELDS["entities"] == "entity_id"

    def test_relationships_id_field(self):
        assert TABLE_ID_FIELDS["relationships"] == "relationship_id"


class TestBuildSourceLocator:
    def test_with_blob_uri(self):
        locator = build_source_locator(blob_uri="https://blob.example.com/file.pdf")
        assert isinstance(locator, dict)
        assert locator.get("blob_uri") == "https://blob.example.com/file.pdf"

    def test_empty_returns_dict(self):
        locator = build_source_locator()
        assert isinstance(locator, dict)

    def test_with_page(self):
        locator = build_source_locator(
            blob_uri="https://blob.example.com/doc.pdf",
            page=5,
        )
        assert locator.get("page") == 5

    def test_with_char_range(self):
        locator = build_source_locator(
            blob_uri="https://blob.example.com/doc.txt",
            char_start=100,
            char_end=200,
        )
        assert locator.get("char_start") == 100
        assert locator.get("char_end") == 200

    def test_with_sheet(self):
        locator = build_source_locator(
            blob_uri="https://blob.example.com/data.xlsx",
            sheet="Sheet1",
        )
        assert locator.get("sheet") == "Sheet1"
