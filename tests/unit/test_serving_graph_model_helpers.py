"""Tests for serving/graph_model.py — pure helpers and spec builders."""
from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.cli.deploy_cmd import (
    _require_fresh_ontology_projection,
    _require_matching_ontology_projection,
)
from fabric_kg_builder.serving.graph_model import (
    _graph_alias,
    _stable_id,
    build_graph_model_parts,
    encode_parts_for_api,
    extract_entity_types_from_parquet,
    extract_relationship_pairs_from_parquet,
    onelake_abfss_path,
    validate_graph_data_source_paths,
    write_graph_mapping_artifact,
)


def test_graph_preflight_requires_matching_ontology_projection_hashes():
    digest = "sha256:" + "a" * 64
    assert _require_matching_ontology_projection({
        "ontology_submitted_projection_hash": digest,
        "ontology_persisted_projection_hash": digest,
    }) == digest


def test_graph_preflight_rejects_stale_ontology_projection():
    with pytest.raises(
        click.ClickException,
        match="Graph mutation is blocked",
    ):
        _require_matching_ontology_projection({
            "ontology_submitted_projection_hash": "sha256:" + "a" * 64,
            "ontology_persisted_projection_hash": "sha256:" + "b" * 64,
        })


def test_graph_preflight_rejects_stale_fresh_ontology_readback():
    digest = "sha256:" + "a" * 64
    receipt = {
        "ontology_submitted_projection_hash": digest,
        "ontology_persisted_projection_hash": digest,
    }
    with pytest.raises(
        click.ClickException,
        match="Fresh Ontology read-back differs",
    ):
        _require_fresh_ontology_projection(
            receipt,
            "sha256:" + "b" * 64,
        )


# ---------------------------------------------------------------------------
# _stable_id
# ---------------------------------------------------------------------------


class TestStableId:
    def test_returns_16_char_hex(self):
        result = _stable_id("test-seed")
        assert len(result) == 16
        assert all(c in "0123456789abcdef" for c in result)

    def test_deterministic(self):
        assert _stable_id("same") == _stable_id("same")

    def test_different_seeds_different_ids(self):
        assert _stable_id("seed-a") != _stable_id("seed-b")


# ---------------------------------------------------------------------------
# _graph_alias
# ---------------------------------------------------------------------------


class TestGraphAlias:
    def test_valid_identifier_unchanged(self):
        result = _graph_alias("PersonType")
        assert result == "PersonType"

    def test_valid_with_underscores_unchanged(self):
        result = _graph_alias("person_type")
        assert result == "person_type"

    def test_spaces_replaced(self):
        result = _graph_alias("Person Type")
        assert " " not in result

    def test_hyphens_replaced(self):
        result = _graph_alias("person-type")
        # Should produce a safe alias
        assert "-" not in result

    def test_numeric_start_prefixed(self):
        result = _graph_alias("123person")
        # Should not start with a digit
        assert result[0].isalpha() or result.startswith("Type_")

    def test_empty_string_handled(self):
        result = _graph_alias("")
        assert isinstance(result, str)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# onelake_abfss_path
# ---------------------------------------------------------------------------


class TestOnelakeAbfssPath:
    def test_basic_path(self):
        result = onelake_abfss_path(
            workspace_id="ws-001",
            lakehouse_item_id="lh-001",
            table_name="entities",
        )
        assert result.startswith("abfss://")
        assert "ws-001" in result
        assert "lh-001" in result
        assert "entities" in result

    def test_default_schema(self):
        result = onelake_abfss_path("ws-001", "lh-001", "entities")
        assert "dbo" in result

    def test_custom_schema(self):
        result = onelake_abfss_path("ws-001", "lh-001", "entities", schema="custom")
        assert "custom" in result
        assert "entities" in result


# ---------------------------------------------------------------------------
# extract_entity_types_from_parquet
# ---------------------------------------------------------------------------


class TestExtractEntityTypes:
    def test_empty_rows(self):
        assert extract_entity_types_from_parquet([]) == []

    def test_extracts_unique_types(self):
        rows = [
            {"entity_type": "Person"},
            {"entity_type": "Company"},
            {"entity_type": "Person"},  # duplicate
        ]
        result = extract_entity_types_from_parquet(rows)
        assert result == ["Person", "Company"]

    def test_preserves_first_seen_order(self):
        rows = [
            {"entity_type": "B"},
            {"entity_type": "A"},
            {"entity_type": "C"},
        ]
        result = extract_entity_types_from_parquet(rows)
        assert result == ["B", "A", "C"]

    def test_skips_empty_type(self):
        rows = [
            {"entity_type": ""},
            {"entity_type": "Person"},
        ]
        result = extract_entity_types_from_parquet(rows)
        assert result == ["Person"]

    def test_skips_missing_field(self):
        rows = [{"name": "Alice"}, {"entity_type": "Person"}]
        result = extract_entity_types_from_parquet(rows)
        assert result == ["Person"]


# ---------------------------------------------------------------------------
# extract_relationship_pairs_from_parquet
# ---------------------------------------------------------------------------


class TestExtractRelationshipPairs:
    def test_empty_rows(self):
        assert extract_relationship_pairs_from_parquet([], {}) == []

    def test_extracts_pairs(self):
        entities = {
            "e-1": {"entity_type": "Person"},
            "e-2": {"entity_type": "Company"},
        }
        rels = [
            {
                "relationship_type": "EMPLOYS",
                "source_entity_id": "e-2",
                "target_entity_id": "e-1",
            }
        ]
        result = extract_relationship_pairs_from_parquet(rels, entities)
        assert len(result) == 1
        assert result[0]["name"] == "EMPLOYS"
        assert result[0]["source_type"] == "Company"
        assert result[0]["target_type"] == "Person"

    def test_filters_by_min_pair_count(self):
        entities = {
            "e-1": {"entity_type": "A"},
            "e-2": {"entity_type": "B"},
        }
        rels = [
            {"relationship_type": "R1", "source_entity_id": "e-1", "target_entity_id": "e-2"},
        ]
        # min_pair_count=2 should exclude R1 (only 1 occurrence)
        result = extract_relationship_pairs_from_parquet(rels, entities, min_pair_count=2)
        assert result == []

    def test_missing_entity_skips(self):
        rels = [
            {"relationship_type": "R1", "source_entity_id": "missing-1", "target_entity_id": "missing-2"},
        ]
        result = extract_relationship_pairs_from_parquet(rels, {})
        assert result == []


# ---------------------------------------------------------------------------
# build_graph_model_parts
# ---------------------------------------------------------------------------


class TestBuildGraphModelParts:
    def test_returns_list(self):
        parts = build_graph_model_parts(entity_types=["Person", "Company"])
        assert isinstance(parts, list)
        assert len(parts) > 0

    def test_parts_have_path_and_payload(self):
        parts = build_graph_model_parts(entity_types=["Person"])
        for part in parts:
            assert "path" in part
            assert "payload_json" in part

    def test_empty_entity_types(self):
        parts = build_graph_model_parts(entity_types=[])
        assert isinstance(parts, list)

    def test_with_workspace_lakehouse(self):
        parts = build_graph_model_parts(
            entity_types=["Person"],
            workspace_id="ws-001",
            lakehouse_item_id="lh-001",
        )
        assert isinstance(parts, list)
        # Workspace and lakehouse should appear in datasources
        all_content = str(parts)
        assert "ws-001" in all_content or "lakehouse" in all_content.lower()

    def test_with_relationship_pairs(self):
        pairs = [{"name": "EMPLOYS", "source_type": "Company", "target_type": "Person"}]
        parts = build_graph_model_parts(
            entity_types=["Person", "Company"],
            relationship_pairs=pairs,
        )
        assert isinstance(parts, list)
        assert len(parts) > 0


class TestGraphDataSourcePaths:
    def test_accepts_relative_schema_table_path(self):
        parts = build_graph_model_parts(
            entity_types=["Person"],
            workspace_id="ws-001",
            lakehouse_item_id="lh-001",
        )
        validate_graph_data_source_paths(parts)

    def test_rejects_absolute_path_with_item_reference(self):
        parts = build_graph_model_parts(
            entity_types=["Person"],
            workspace_id="ws-001",
            lakehouse_item_id="lh-001",
        )
        data_source = parts[0]["payload_json"]["dataSources"][0]
        data_source["properties"]["path"] = (
            "abfss://ws-001@onelake.dfs.fabric.microsoft.com/"
            "lh-001/Tables/dbo/entities"
        )

        with pytest.raises(ValueError, match="absolute path"):
            validate_graph_data_source_paths(parts)


class TestDeployGraphCompiledArtifact:
    def test_dry_run_accepts_compiled_graph_definition(self, tmp_path, monkeypatch):
        env_dir = tmp_path / "ontology" / "environments"
        env_dir.mkdir(parents=True)
        (env_dir / "dev.json").write_text(
            json.dumps(
                {
                    "fabric": {
                        "workspace_id": "ws-001",
                        "lakehouse_item_id": "lh-001",
                        "graph_model_item_id": "gm-001",
                        "graph_model_display_name": "KG Graph",
                        "schema_name": "dbo",
                    }
                }
            ),
            encoding="utf-8",
        )
        graph_dir = tmp_path / "build" / "graph"
        graph_dir.mkdir(parents=True)
        parts = build_graph_model_parts(
            entity_types=["Person"],
            workspace_id="ws-001",
            lakehouse_item_id="lh-001",
        )
        graph_definition = graph_dir / "graph-definition.json"
        graph_definition.write_text(
            json.dumps({"parts": parts}),
            encoding="utf-8",
        )
        (graph_dir / "label-catalog.json").write_text(
            json.dumps({"contract_hash": "sha256:test"}),
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)

        result = CliRunner().invoke(
            cli,
            [
                "deploy-graph",
                "--env",
                "dev",
                "--graph-definition-file",
                str(graph_definition),
                "--dry-run",
            ],
        )

        assert result.exit_code == 0, result.output
        assert str(graph_definition) in result.output
        assert "SUCCESS (dry-run)" in result.output


# ---------------------------------------------------------------------------
# encode_parts_for_api
# ---------------------------------------------------------------------------


class TestEncodePartsForApi:
    def test_encodes_to_inline_base64(self):
        parts = [{"path": "Files/dataSources.json", "payload_json": {"key": "value"}}]
        encoded = encode_parts_for_api(parts)
        assert len(encoded) == 1
        assert encoded[0]["path"] == "Files/dataSources.json"
        assert encoded[0]["payloadType"] == "InlineBase64"
        assert "payload" in encoded[0]

    def test_encoded_payload_is_valid_base64_json(self):
        import base64
        parts = [{"path": "test/path.json", "payload_json": {"a": 1, "b": "two"}}]
        encoded = encode_parts_for_api(parts)
        decoded = json.loads(base64.b64decode(encoded[0]["payload"]))
        assert decoded["a"] == 1
        assert decoded["b"] == "two"

    def test_empty_parts(self):
        assert encode_parts_for_api([]) == []


# ---------------------------------------------------------------------------
# write_graph_mapping_artifact
# ---------------------------------------------------------------------------


class TestWriteGraphMappingArtifact:
    def test_writes_to_directory(self, tmp_path):
        parts = build_graph_model_parts(entity_types=["Person"])
        result_path = write_graph_mapping_artifact(tmp_path, parts)
        assert isinstance(result_path, Path)
        assert result_path.exists()

    def test_writes_to_file_path(self, tmp_path):
        parts = build_graph_model_parts(entity_types=["Person"])
        target = tmp_path / "output.json"
        result_path = write_graph_mapping_artifact(target, parts)
        assert result_path.exists()
        data = json.loads(result_path.read_text())
        assert "_schema" in data

    def test_content_is_valid_json(self, tmp_path):
        parts = build_graph_model_parts(entity_types=["Person", "Company"])
        result_path = write_graph_mapping_artifact(tmp_path, parts)
        data = json.loads(result_path.read_text())
        assert "model_name" in data
        assert "parts" in data or "_schema" in data
