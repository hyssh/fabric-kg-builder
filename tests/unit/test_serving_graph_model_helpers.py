"""Tests for serving/graph_model.py — pure helpers and spec builders."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.serving.graph_model import (
    _graph_alias,
    _graph_property_names,
    _stable_id,
    build_graph_model_parts,
    encode_parts_for_api,
    extract_entity_types_from_parquet,
    extract_relationship_pairs_from_parquet,
    onelake_abfss_path,
    validate_graph_data_source_paths,
    validate_graph_model_schema,
    write_graph_mapping_artifact,
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


def test_graph_property_names_are_deterministic_and_preserve_valid_columns():
    columns = [
        "__canonical_id", "__label", "__semantic_relationship_id",
        "__source_entity_id", "__target_entity_id", "canonical_id", "label",
        "bad-name", "bad name", "1start", "é", "___", "a" * 150 + "-",
        _graph_alias("__canonical_id"), _graph_alias("__canonical_id") + "_2",
    ]
    names = _graph_property_names(columns)
    assert names == _graph_property_names(list(reversed(columns)) + columns)
    assert len(names) == len(set(names.values()))
    assert all(_graph_alias(name) == name for name in names.values())
    assert names["canonical_id"] == "canonical_id"
    assert names[_graph_alias("__canonical_id")] == _graph_alias("__canonical_id")
    assert names["__canonical_id"] == _graph_alias("__canonical_id") + "_3"
    assert names["__label"] == _graph_alias("__label")


@pytest.mark.parametrize("columns", [[""], [None]])
def test_graph_property_names_reject_empty_source_columns(columns):
    with pytest.raises(ValueError, match="nonempty"):
        _graph_property_names(columns)


def test_graph_property_mappings_include_implicit_identity_and_preserve_source():
    binding = {"table": "nodes", "entity_id_column": "__canonical_id", "property_columns": ["__label"]}
    parts = build_graph_model_parts(
        entity_types=["Node"], node_table_bindings={"Node": binding},
        relationship_pairs=[{
            "name": "Link", "source_type": "Node", "target_type": "Node",
            "property_columns": ["__canonical_id", "__source_entity_id", "__target_entity_id"],
            "source_entity_id_column": "__source_entity_id",
            "target_entity_id_column": "__target_entity_id",
        }],
    )
    assert binding["property_columns"] == ["__label"]
    decoded = {part["path"]: part["payload_json"] for part in parts}
    node = decoded["graphType.json"]["nodeTypes"][0]
    node_binding = decoded["graphDefinition.json"]["nodeTables"][0]
    mapped = {item["sourceColumn"]: item["propertyName"] for item in node_binding["propertyMappings"]}
    assert node["primaryKeyProperties"] == [mapped["__canonical_id"]]
    assert {prop["name"] for prop in node["properties"]} == set(mapped.values())
    edge_binding = decoded["graphDefinition.json"]["edgeTables"][0]
    assert edge_binding["sourceNodeKeyColumns"] == ["__source_entity_id"]
    assert edge_binding["destinationNodeKeyColumns"] == ["__target_entity_id"]
    assert edge_binding["propertyMappings"][0] == {
        "sourceColumn": "__canonical_id", "propertyName": mapped["__canonical_id"],
    }


@pytest.mark.parametrize("label", ["_Node", "__Node", "1Node", "Node-name"])
def test_graph_rejects_invalid_contract_owned_labels(label):
    with pytest.raises(ValueError, match="Invalid contract-owned Graph .* label"):
        build_graph_model_parts(entity_types=["Node"], node_labels={"Node": label})
    with pytest.raises(ValueError, match="Invalid contract-owned Graph .* label"):
        build_graph_model_parts(entity_types=["Node"], relationship_pairs=[{
            "name": "Link", "source_type": "Node", "target_type": "Node", "graph_label": label,
        }])


def test_graph_rejects_alias_collisions_and_native_schema_type_conflicts():
    with pytest.raises(ValueError, match="Duplicate Graph type alias"):
        build_graph_model_parts(entity_types=["Node"], relationship_pairs=[{
            "name": "Node", "source_type": "Node", "target_type": "Node", "graph_alias": "Node_nodeType",
        }])
    parts = build_graph_model_parts(entity_types=["A", "B"])
    graph = next(part["payload_json"] for part in parts if part["path"] == "graphType.json")
    graph["nodeTypes"][0]["properties"][0]["type"] = "INTEGER"
    with pytest.raises(ValueError, match="Unsupported native Graph property type: INTEGER"):
        validate_graph_model_schema(parts)
    graph["nodeTypes"][0]["properties"][0]["type"] = "INT"
    with pytest.raises(ValueError, match="conflicting types"):
        validate_graph_model_schema(parts)


@pytest.mark.parametrize("change", ["limit", "key", "mapping", "endpoint", "arity", "empty-column"])
def test_graph_schema_rejects_invalid_native_bindings(change):
    parts = build_graph_model_parts(entity_types=["Node"], relationship_pairs=[{
        "name": "Link", "source_type": "Node", "target_type": "Node",
    }])
    decoded = {part["path"]: part["payload_json"] for part in parts}
    graph = decoded["graphType.json"]
    definition = decoded["graphDefinition.json"]
    if change == "limit":
        graph["nodeTypes"] *= 1001
    elif change == "key":
        graph["nodeTypes"][0]["primaryKeyProperties"] = []
    elif change == "mapping":
        definition["nodeTables"][0]["propertyMappings"].pop()
    elif change == "endpoint":
        graph["edgeTypes"][0]["sourceNodeType"]["alias"] = "Missing"
    elif change == "arity":
        definition["edgeTables"][0]["sourceNodeKeyColumns"] = []
    else:
        definition["edgeTables"][0]["destinationNodeKeyColumns"] = [""]
    with pytest.raises(ValueError):
        validate_graph_model_schema(parts)


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
