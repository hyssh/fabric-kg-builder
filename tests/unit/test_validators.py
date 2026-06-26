"""Unit tests for SPEC-005 validation gates.

Covers:
  - VAL-008  chunk.document_element_id FK → document_elements
  - VAL-010  visual_assets.blob_url non-null after upload
  - VAL-013  AI Search blob_url reference consistency
  - VAL-014  Ontology visual/image entity types declare blob_url
  - VAL-019  AI Search schema field alignment
  - VAL-023  No structured rows in chunk/visual AI Search indexes
  - VAL-024  Domain text in USER role only (prompt-builder gate)
  - VAL-025  Required env vars present
  - VAL-026  No secrets in YAML/JSON config files
  - VAL-027  Foundry chat deployment config non-empty
  - VAL-028  visual_region.polygon_json for document_intelligence source
  - D-31     chunk entity_search_keys present when related_entity_ids set
  - D-32     non-placeholder entities have search_aliases

Each gate has at minimum one "good data passes" case and one
"broken data trips the right rule_id + severity" case.
"""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

import pytest

from fabric_kg_builder.validate.suite import (
    ValidationViolation,
    _d31_chunk_entity_search_keys,
    _d32_entity_search_aliases,
    _val008_chunk_docelem_fk,
    _val010_blob_url_present,
    _val013_search_blob_url,
    _val014_ontology_blob_url_property,
    _val019_search_schema_alignment,
    _val023_no_structured_rows_in_search,
    _val024_domain_not_in_system_prompt,
    _val025_required_env_vars,
    _val026_no_secrets_in_config,
    _val027_foundry_config,
    _val028_polygon_json,
    validate_all,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _no_violations(result: list[ValidationViolation]) -> None:
    assert result == [], f"Expected no violations but got: {result}"


def _single_fail(result: list[ValidationViolation], rule_id: str) -> ValidationViolation:
    fails = [v for v in result if v.rule_id == rule_id and v.severity == "fail"]
    assert len(fails) >= 1, (
        f"Expected at least one FAIL violation with rule_id={rule_id!r}, got: {result}"
    )
    return fails[0]


def _single_warn(result: list[ValidationViolation], rule_id: str) -> ValidationViolation:
    warns = [v for v in result if v.rule_id == rule_id and v.severity == "warn"]
    assert len(warns) >= 1, (
        f"Expected at least one WARN violation with rule_id={rule_id!r}, got: {result}"
    )
    return warns[0]


# ---------------------------------------------------------------------------
# VAL-008 — chunk.document_element_id FK
# ---------------------------------------------------------------------------


class TestVal008:
    def test_good_data_passes(self):
        table_rows = {
            "document_elements": [{"document_element_id": "de:001"}],
            "chunks": [{"chunk_id": "ch:001", "document_element_id": "de:001"}],
        }
        _no_violations(_val008_chunk_docelem_fk(table_rows))

    def test_null_docelem_id_passes(self):
        """Null document_element_id is allowed (chunk may not link to a document element)."""
        table_rows = {
            "document_elements": [],
            "chunks": [{"chunk_id": "ch:001", "document_element_id": None}],
        }
        _no_violations(_val008_chunk_docelem_fk(table_rows))

    def test_dangling_fk_fails(self):
        table_rows = {
            "document_elements": [{"document_element_id": "de:001"}],
            "chunks": [{"chunk_id": "ch:002", "document_element_id": "de:MISSING"}],
        }
        v = _single_fail(_val008_chunk_docelem_fk(table_rows), "VAL-008")
        assert "de:MISSING" in v.message
        assert "ch:002" in v.message

    def test_empty_tables_pass(self):
        _no_violations(_val008_chunk_docelem_fk({}))


# ---------------------------------------------------------------------------
# VAL-010 — blob_url present after upload
# ---------------------------------------------------------------------------


class TestVal010:
    def test_good_data_passes(self):
        table_rows = {
            "visual_assets": [
                {"image_id": "img:001", "blob_url": "https://fake.blob/img001.png", "is_placeholder": False},
            ]
        }
        _no_violations(_val010_blob_url_present(table_rows))

    def test_placeholder_skipped(self):
        table_rows = {
            "visual_assets": [
                {"image_id": "img:placeholder", "blob_url": None, "is_placeholder": True},
            ]
        }
        _no_violations(_val010_blob_url_present(table_rows))

    def test_missing_blob_url_fails(self):
        table_rows = {
            "visual_assets": [
                {"image_id": "img:001", "blob_url": None, "is_placeholder": False},
            ]
        }
        v = _single_fail(_val010_blob_url_present(table_rows), "VAL-010")
        assert "img:001" in v.message

    def test_empty_blob_url_fails(self):
        table_rows = {
            "visual_assets": [
                {"image_id": "img:002", "blob_url": "", "is_placeholder": False},
            ]
        }
        v = _single_fail(_val010_blob_url_present(table_rows), "VAL-010")
        assert "img:002" in v.message


# ---------------------------------------------------------------------------
# VAL-013 — AI Search blob_url reference consistency
# ---------------------------------------------------------------------------


class TestVal013:
    def test_good_data_passes(self, tmp_path: Path):
        va_rows = [{"image_id": "img:001", "blob_url": "https://real.blob/img001.png"}]
        docs_dir = tmp_path / "kg-chunks" / "documents"
        docs_dir.mkdir(parents=True)
        (docs_dir / "doc1.json").write_text(json.dumps({
            "chunk_id": "ch:001",
            "image_id": "img:001",
            "blob_url": "https://real.blob/img001.png",
        }))
        table_rows = {"visual_assets": va_rows}
        _no_violations(_val013_search_blob_url(table_rows, tmp_path))

    def test_mismatched_blob_url_fails(self, tmp_path: Path):
        va_rows = [{"image_id": "img:001", "blob_url": "https://real.blob/img001.png"}]
        docs_dir = tmp_path / "kg-chunks" / "documents"
        docs_dir.mkdir(parents=True)
        (docs_dir / "doc1.json").write_text(json.dumps({
            "chunk_id": "ch:001",
            "image_id": "img:001",
            "blob_url": "https://WRONG.blob/img001.png",
        }))
        table_rows = {"visual_assets": va_rows}
        v = _single_fail(_val013_search_blob_url(table_rows, tmp_path), "VAL-013")
        assert "img:001" in v.message

    def test_nonexistent_dir_passes(self, tmp_path: Path):
        """If the search dir doesn't exist, no violations."""
        _no_violations(_val013_search_blob_url({}, tmp_path / "no_such_dir"))


# ---------------------------------------------------------------------------
# VAL-014 — Ontology blob_url property on visual types
# ---------------------------------------------------------------------------


class TestVal014:
    def _model_with_type(self, name: str, has_blob: bool) -> dict:
        props = [{"name": "entity_id"}]
        if has_blob:
            props.append({"name": "blob_url", "type": "blob_url"})
        return {"entityTypes": [{"name": name, "properties": props}]}

    def test_good_data_passes(self):
        model = {
            "entityTypes": [
                {"name": "ImageAsset", "properties": [{"name": "blob_url", "type": "blob_url"}]},
                {"name": "Figure", "properties": [{"name": "blob_url", "type": "blob_url"}]},
                {"name": "VisualRegion", "properties": [{"name": "blob_url", "type": "blob_url"}]},
            ]
        }
        _no_violations(_val014_ontology_blob_url_property(model))

    def test_imageasset_missing_blob_url_fails(self):
        model = self._model_with_type("ImageAsset", has_blob=False)
        v = _single_fail(_val014_ontology_blob_url_property(model), "VAL-014")
        assert "ImageAsset" in v.message

    def test_figure_missing_blob_url_fails(self):
        model = self._model_with_type("Figure", has_blob=False)
        v = _single_fail(_val014_ontology_blob_url_property(model), "VAL-014")
        assert "Figure" in v.message

    def test_non_visual_types_ignored(self):
        model = {
            "entityTypes": [
                {"name": "Device", "properties": [{"name": "entity_id"}]},
            ]
        }
        _no_violations(_val014_ontology_blob_url_property(model))


# ---------------------------------------------------------------------------
# VAL-019 — AI Search schema alignment
# ---------------------------------------------------------------------------


class TestVal019:
    def _make_index(self, tmp_path: Path, index_name: str, schema_fields: list[str],
                    doc_fields: list[str]) -> Path:
        index_dir = tmp_path / index_name
        (index_dir / "documents").mkdir(parents=True)
        schema = {"fields": [{"name": f} for f in schema_fields]}
        (index_dir / "schema.json").write_text(json.dumps(schema))
        doc = {f: "value" for f in doc_fields}
        (index_dir / "documents" / "doc1.json").write_text(json.dumps(doc))
        return index_dir

    def test_good_alignment_passes(self, tmp_path: Path):
        fields = ["chunk_id", "content", "entity_ids"]
        self._make_index(tmp_path, "kg-chunks", fields, fields)
        _no_violations(_val019_search_schema_alignment(tmp_path))

    def test_extra_doc_field_fails(self, tmp_path: Path):
        self._make_index(tmp_path, "kg-chunks",
                         schema_fields=["chunk_id", "content"],
                         doc_fields=["chunk_id", "content", "EXTRA_FIELD"])
        v = _single_fail(_val019_search_schema_alignment(tmp_path), "VAL-019")
        assert "EXTRA_FIELD" in v.message

    def test_missing_doc_field_fails(self, tmp_path: Path):
        self._make_index(tmp_path, "kg-chunks",
                         schema_fields=["chunk_id", "content", "REQUIRED"],
                         doc_fields=["chunk_id", "content"])
        v = _single_fail(_val019_search_schema_alignment(tmp_path), "VAL-019")
        assert "REQUIRED" in v.message

    def test_nonexistent_dir_passes(self, tmp_path: Path):
        _no_violations(_val019_search_schema_alignment(tmp_path / "no_such_dir"))


# ---------------------------------------------------------------------------
# VAL-023 — No structured rows in chunk/visual AI Search indexes
# ---------------------------------------------------------------------------


class TestVal023:
    def _make_index_doc(self, tmp_path: Path, index_name: str, doc: dict) -> None:
        docs_dir = tmp_path / index_name / "documents"
        docs_dir.mkdir(parents=True)
        (docs_dir / "doc.json").write_text(json.dumps(doc))

    def test_good_chunk_doc_passes(self, tmp_path: Path):
        self._make_index_doc(tmp_path, "kg-chunks", {
            "chunk_id": "ch:001",
            "content": "Surface Laptop 5",
            "entity_ids": ["e2e:device:surface-laptop-5"],
        })
        _no_violations(_val023_no_structured_rows_in_search(tmp_path))

    def test_entity_id_field_fails(self, tmp_path: Path):
        self._make_index_doc(tmp_path, "kg-chunks", {
            "chunk_id": "ch:001",
            "entity_id": "LEAKED_ENTITY",
        })
        v = _single_fail(_val023_no_structured_rows_in_search(tmp_path), "VAL-023")
        assert "entity_id" in v.message

    def test_relationship_fields_fail(self, tmp_path: Path):
        self._make_index_doc(tmp_path, "kg-visual", {
            "chunk_id": "ch:001",
            "relationship_type": "has_component",
            "source_entity_id": "e2e:device:X",
        })
        v = _single_fail(_val023_no_structured_rows_in_search(tmp_path), "VAL-023")
        assert "relationship_type" in v.message or "source_entity_id" in v.message


# ---------------------------------------------------------------------------
# VAL-024 — Domain text in USER role only
# ---------------------------------------------------------------------------


class TestVal024:
    def test_domain_in_user_role_passes(self):
        messages = [
            {"role": "system", "content": "You are an expert knowledge graph builder."},
            {"role": "user", "content": "INJECT_MARKER_XYZ — extract entities from this text."},
        ]
        _no_violations(_val024_domain_not_in_system_prompt(messages, "INJECT_MARKER_XYZ", "call:1"))

    def test_domain_in_system_role_fails(self):
        messages = [
            {"role": "system", "content": "Context: INJECT_MARKER_XYZ — domain info here."},
            {"role": "user", "content": "Extract entities."},
        ]
        v = _single_fail(
            _val024_domain_not_in_system_prompt(messages, "INJECT_MARKER_XYZ", "call:1"),
            "VAL-024",
        )
        assert "system prompt" in v.message.lower() or "system" in v.message.lower()

    def test_empty_domain_text_passes(self):
        messages = [{"role": "system", "content": "any content"}]
        _no_violations(_val024_domain_not_in_system_prompt(messages, "", "call:1"))

    def test_no_messages_passes(self):
        _no_violations(_val024_domain_not_in_system_prompt([], "MARKER", "call:1"))


# ---------------------------------------------------------------------------
# VAL-025 — Required env vars present
# ---------------------------------------------------------------------------


class TestVal025:
    _FULL_ENV = {
        "AZURE_AI_FOUNDRY_ENDPOINT": "https://fake.foundry.azure.com",
        "AZURE_AI_FOUNDRY_API_KEY": "fake-api-key-1234567890",
        "FABRIC_WORKSPACE_ID": "workspace-guid-1234",
        "AZURE_BLOB_CONNECTION_STRING": "DefaultEndpointsProtocol=https;AccountName=fake",
    }

    def test_all_present_passes(self):
        _no_violations(_val025_required_env_vars(self._FULL_ENV))

    def test_missing_foundry_endpoint_fails(self):
        env = {k: v for k, v in self._FULL_ENV.items() if k != "AZURE_AI_FOUNDRY_ENDPOINT"}
        v = _single_fail(_val025_required_env_vars(env), "VAL-025")
        assert "AZURE_AI_FOUNDRY_ENDPOINT" in v.message

    def test_missing_api_key_fails(self):
        env = {k: v for k, v in self._FULL_ENV.items() if k != "AZURE_AI_FOUNDRY_API_KEY"}
        v = _single_fail(_val025_required_env_vars(env), "VAL-025")
        assert "AZURE_AI_FOUNDRY_API_KEY" in v.message

    def test_empty_string_counts_as_missing(self):
        env = {**self._FULL_ENV, "FABRIC_WORKSPACE_ID": ""}
        v = _single_fail(_val025_required_env_vars(env), "VAL-025")
        assert "FABRIC_WORKSPACE_ID" in v.message

    def test_whitespace_only_counts_as_missing(self):
        env = {**self._FULL_ENV, "AZURE_BLOB_CONNECTION_STRING": "   "}
        v = _single_fail(_val025_required_env_vars(env), "VAL-025")
        assert "AZURE_BLOB_CONNECTION_STRING" in v.message


# ---------------------------------------------------------------------------
# VAL-026 — No secrets in config files
# ---------------------------------------------------------------------------


class TestVal026:
    def test_env_var_placeholder_passes(self, tmp_path: Path):
        cfg = tmp_path / "fabric-kg.yaml"
        cfg.write_text("foundry:\n  endpoint: ${AZURE_AI_FOUNDRY_ENDPOINT}\n  api_key: ${AZURE_AI_FOUNDRY_API_KEY}\n")
        _no_violations(_val026_no_secrets_in_config([cfg]))

    def test_raw_api_key_fails(self, tmp_path: Path):
        cfg = tmp_path / "fabric-kg.yaml"
        cfg.write_text("foundry:\n  api_key: sk-this-is-a-very-long-real-api-key-1234567890abcdef\n")
        v = _single_fail(_val026_no_secrets_in_config([cfg]), "VAL-026")
        assert str(cfg) in v.message

    def test_nonexistent_file_passes(self, tmp_path: Path):
        _no_violations(_val026_no_secrets_in_config([tmp_path / "no_such_file.yaml"]))

    def test_empty_file_passes(self, tmp_path: Path):
        cfg = tmp_path / "empty.yaml"
        cfg.write_text("")
        _no_violations(_val026_no_secrets_in_config([cfg]))


# ---------------------------------------------------------------------------
# VAL-027 — Foundry config non-empty
# ---------------------------------------------------------------------------


class TestVal027:
    def test_good_config_passes(self):
        config = {"foundry": {"endpoint": "https://fake.foundry.azure.com", "chat_deployment": "gpt-5-mini"}}
        _no_violations(_val027_foundry_config(config))

    def test_missing_deployment_fails(self):
        config = {"foundry": {"endpoint": "https://fake.foundry.azure.com", "chat_deployment": ""}}
        v = _single_fail(_val027_foundry_config(config), "VAL-027")
        assert "deployment" in v.message.lower()

    def test_missing_endpoint_fails(self):
        config = {"foundry": {"endpoint": "", "chat_deployment": "gpt-5-mini"}}
        v = _single_fail(_val027_foundry_config(config), "VAL-027")
        assert "endpoint" in v.message.lower()

    def test_empty_foundry_section_fails(self):
        violations = _val027_foundry_config({})
        rule_ids = {v.rule_id for v in violations}
        assert "VAL-027" in rule_ids


# ---------------------------------------------------------------------------
# VAL-028 — visual_region polygon_json for DI source
# ---------------------------------------------------------------------------


class TestVal028:
    def test_good_di_region_passes(self):
        table_rows = {
            "visual_regions": [{
                "visual_region_id": "vr:001",
                "source_type": "document_intelligence",
                "polygon_json": json.dumps([[0.1, 0.2], [0.3, 0.4]]),
            }]
        }
        _no_violations(_val028_polygon_json(table_rows))

    def test_non_di_source_null_polygon_passes(self):
        table_rows = {
            "visual_regions": [{
                "visual_region_id": "vr:001",
                "source_type": "manual",
                "polygon_json": None,
            }]
        }
        _no_violations(_val028_polygon_json(table_rows))

    def test_null_polygon_for_di_fails(self):
        table_rows = {
            "visual_regions": [{
                "visual_region_id": "vr:001",
                "source_type": "document_intelligence",
                "polygon_json": None,
            }]
        }
        v = _single_fail(_val028_polygon_json(table_rows), "VAL-028")
        assert "vr:001" in v.message

    def test_empty_polygon_list_fails(self):
        table_rows = {
            "visual_regions": [{
                "visual_region_id": "vr:002",
                "source_type": "document_intelligence",
                "polygon_json": "[]",
            }]
        }
        v = _single_fail(_val028_polygon_json(table_rows), "VAL-028")
        assert "vr:002" in v.message

    def test_invalid_json_fails(self):
        table_rows = {
            "visual_regions": [{
                "visual_region_id": "vr:003",
                "source_type": "document_intelligence",
                "polygon_json": "NOT_VALID_JSON{{",
            }]
        }
        v = _single_fail(_val028_polygon_json(table_rows), "VAL-028")
        assert "vr:003" in v.message


# ---------------------------------------------------------------------------
# D-31 — chunk entity_search_keys
# ---------------------------------------------------------------------------


class TestD31:
    def test_related_ids_and_search_keys_passes(self):
        table_rows = {
            "chunks": [{
                "chunk_id": "ch:001",
                "related_entity_ids": ["e:001"],
                "entity_search_keys": ["surface laptop 5"],
            }]
        }
        _no_violations(_d31_chunk_entity_search_keys(table_rows))

    def test_no_related_ids_passes(self):
        table_rows = {
            "chunks": [{
                "chunk_id": "ch:001",
                "related_entity_ids": None,
                "entity_search_keys": None,
            }]
        }
        _no_violations(_d31_chunk_entity_search_keys(table_rows))

    def test_related_ids_but_no_search_keys_warns(self):
        table_rows = {
            "chunks": [{
                "chunk_id": "ch:001",
                "related_entity_ids": ["e:001"],
                "entity_search_keys": None,
            }]
        }
        v = _single_warn(_d31_chunk_entity_search_keys(table_rows), "D-31")
        assert "ch:001" in v.message


# ---------------------------------------------------------------------------
# D-32 — entity search_aliases
# ---------------------------------------------------------------------------


class TestD32:
    def test_entity_with_search_aliases_passes(self):
        table_rows = {
            "entities": [{
                "entity_id": "e:001",
                "is_placeholder": False,
                "search_aliases": ["surface laptop 5", "sl5"],
            }]
        }
        _no_violations(_d32_entity_search_aliases(table_rows))

    def test_placeholder_without_aliases_passes(self):
        table_rows = {
            "entities": [{
                "entity_id": "e:placeholder",
                "is_placeholder": True,
                "search_aliases": None,
            }]
        }
        _no_violations(_d32_entity_search_aliases(table_rows))

    def test_real_entity_without_aliases_warns(self):
        table_rows = {
            "entities": [{
                "entity_id": "e:001",
                "is_placeholder": False,
                "search_aliases": None,
            }]
        }
        v = _single_warn(_d32_entity_search_aliases(table_rows), "D-32")
        assert "e:001" in v.message


# ---------------------------------------------------------------------------
# validate_all integration smoke test
# ---------------------------------------------------------------------------


class TestValidateAll:
    def test_clean_build_returns_empty_or_warn_only(self, tmp_path: Path):
        """validate_all on an empty build dir returns no FAIL violations."""
        violations = validate_all(
            build_dir=tmp_path,
            config={"foundry": {"endpoint": "https://fake.endpoint", "chat_deployment": "gpt-5"}},
            skip_env_check=True,
        )
        fail_violations = [v for v in violations if v.severity == "fail"]
        assert fail_violations == [], f"Expected no FAILs on empty build, got: {fail_violations}"

    def test_violations_have_correct_types(self, tmp_path: Path):
        """All violations from validate_all are ValidationViolation instances."""
        violations = validate_all(build_dir=tmp_path, skip_env_check=True)
        for v in violations:
            assert isinstance(v, ValidationViolation)
            assert v.severity in ("fail", "warn")
            assert v.rule_id
            assert v.message

    def test_val010_trips_on_broken_data(self, tmp_path: Path):
        """validate_all detects VAL-010 when blob_url is missing from a visual asset Parquet."""
        import pandas as pd  # noqa: PLC0415
        import pyarrow as pa  # noqa: PLC0415
        import pyarrow.parquet as pq  # noqa: PLC0415
        from fabric_kg_builder.model.arrow_schemas import TABLE_SCHEMAS  # noqa: PLC0415

        schema = TABLE_SCHEMAS["visual_assets"]
        from datetime import datetime, timezone  # noqa: PLC0415
        now = datetime(2026, 6, 24, tzinfo=timezone.utc)
        row = {
            "image_id": "img:test",
            "source_file_id": "sf:test",
            "document_element_id": None,
            "asset_type": "diagram",
            "page_number": None,
            "section_path": None,
            "caption": None,
            "alt_text": None,
            "blob_url": None,  # <-- missing blob_url, is_placeholder=False
            "image_path": None,
            "image_hash": "abc123",
            "width": None,
            "height": None,
            "description": None,
            "confidence": None,
            "is_placeholder": False,
            "created_at": now,
        }
        table = pa.Table.from_pylist([row], schema=schema)
        pq.write_table(table, tmp_path / "visual_assets.parquet")

        violations = validate_all(build_dir=tmp_path, skip_env_check=True)
        val010_fails = [v for v in violations if v.rule_id == "VAL-010" and v.severity == "fail"]
        assert len(val010_fails) >= 1, f"Expected VAL-010 FAIL, got: {violations}"
