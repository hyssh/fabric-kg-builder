"""Tests for the ontology compiler (SPEC-003 §6).

Verifies:
- compile() produces the expected directory structure for real model types
- Definition files reference the locked IDs from ids.lock.json
- blob_url properties emit format: uri
- inverseTypeId is present when inversePolicy is materialize/alias; absent for none
- get_rest_parts() produces InlineBase64 items that decode back to valid JSON
- The top-level definition.json lists every emitted part
- GUIDs are deterministic across calls
- Missing ID in ids.lock raises OntologyCompilerError
- Unknown relationship source/target raises OntologyCompilerError
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from fabric_kg_builder.ontology.compiler import (
    OntologyCompiler,
    OntologyCompilerError,
    _ONTOLOGY_NS,
    _derive_guid,
)

# ---------------------------------------------------------------------------
# Paths to the real ontology fixtures
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent
MODEL_YAML = REPO_ROOT / "ontology" / "model.yaml"
IDS_LOCK = REPO_ROOT / "ontology" / "ids.lock.json"


# ---------------------------------------------------------------------------
# Shared module-scoped fixtures (compile once per test session module)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ids_data() -> dict[str, Any]:
    return json.loads(IDS_LOCK.read_text())


@pytest.fixture(scope="module")
def compiler() -> OntologyCompiler:
    return OntologyCompiler(MODEL_YAML, IDS_LOCK)


@pytest.fixture(scope="module")
def rest_parts(compiler: OntologyCompiler) -> list[dict[str, Any]]:
    return compiler.get_rest_parts()


@pytest.fixture(scope="module")
def compiled_dir(compiler: OntologyCompiler, tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("compiled_ontology")
    compiler.compile(out)
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Directory structure
# ---------------------------------------------------------------------------


def test_platform_file_emitted(compiled_dir: Path) -> None:
    assert (compiled_dir / ".platform").exists()


def test_top_level_definition_json_emitted(compiled_dir: Path) -> None:
    assert (compiled_dir / "definition.json").exists()


def test_entity_types_dir_exists(compiled_dir: Path) -> None:
    assert (compiled_dir / "EntityTypes").is_dir()


def test_relationship_types_dir_exists(compiled_dir: Path) -> None:
    assert (compiled_dir / "RelationshipTypes").is_dir()


def test_device_entity_definition_exists(compiled_dir: Path) -> None:
    # Device → id 1000000000000000001 per ids.lock.json
    assert (compiled_dir / "EntityTypes" / "1000000000000000001" / "definition.json").exists()


def test_device_data_binding_exists(compiled_dir: Path) -> None:
    db_dir = compiled_dir / "EntityTypes" / "1000000000000000001" / "DataBindings"
    assert db_dir.is_dir()
    json_files = list(db_dir.glob("*.json"))
    assert len(json_files) == 1, f"Expected exactly 1 binding for Device, got {len(json_files)}"


def test_figure_entity_definition_exists(compiled_dir: Path, ids_data: dict) -> None:
    fig_id = ids_data["entityTypes"]["Figure"]
    assert (compiled_dir / "EntityTypes" / fig_id / "definition.json").exists()


def test_has_component_rel_definition_exists(compiled_dir: Path) -> None:
    # has_component → id 2000000000000000001 per ids.lock.json
    assert (
        compiled_dir / "RelationshipTypes" / "2000000000000000001" / "definition.json"
    ).exists()


def test_has_component_contextualization_exists(compiled_dir: Path) -> None:
    ctx_dir = (
        compiled_dir / "RelationshipTypes" / "2000000000000000001" / "Contextualizations"
    )
    assert ctx_dir.is_dir()
    json_files = list(ctx_dir.glob("*.json"))
    assert len(json_files) == 1, f"Expected 1 contextualization for has_component, got {len(json_files)}"


# ---------------------------------------------------------------------------
# Locked IDs in definitions
# ---------------------------------------------------------------------------


def test_entity_definition_uses_locked_id(compiled_dir: Path) -> None:
    defn = _load_json(compiled_dir / "EntityTypes" / "1000000000000000001" / "definition.json")
    assert defn["typeId"] == "1000000000000000001"
    assert defn["name"] == "Device"


def test_relationship_definition_uses_locked_id(compiled_dir: Path) -> None:
    defn = _load_json(
        compiled_dir / "RelationshipTypes" / "2000000000000000001" / "definition.json"
    )
    assert defn["typeId"] == "2000000000000000001"
    assert defn["name"] == "has_component"


def test_relationship_source_target_ids(compiled_dir: Path, ids_data: dict) -> None:
    defn = _load_json(
        compiled_dir / "RelationshipTypes" / "2000000000000000001" / "definition.json"
    )
    assert defn["sourceTypeId"] == ids_data["entityTypes"]["Device"]
    assert defn["targetTypeId"] == ids_data["entityTypes"]["Component"]


# ---------------------------------------------------------------------------
# Inverse type ID handling
# ---------------------------------------------------------------------------


def test_has_component_emits_inverse_type_id(compiled_dir: Path, ids_data: dict) -> None:
    """has_component (materialize → component_of) must have inverseTypeId."""
    defn = _load_json(
        compiled_dir / "RelationshipTypes" / "2000000000000000001" / "definition.json"
    )
    assert "inverseTypeId" in defn, "has_component must have inverseTypeId (policy: materialize)"
    assert defn["inverseTypeId"] == ids_data["relationshipTypes"]["component_of"]


def test_shown_in_emits_inverse_type_id(compiled_dir: Path, ids_data: dict) -> None:
    """shown_in (alias → shows) must have inverseTypeId."""
    shown_in_id = ids_data["relationshipTypes"]["shown_in"]
    defn = _load_json(
        compiled_dir / "RelationshipTypes" / shown_in_id / "definition.json"
    )
    assert "inverseTypeId" in defn, "shown_in must have inverseTypeId (policy: alias)"
    assert defn["inverseTypeId"] == ids_data["relationshipTypes"]["shows"]


def test_none_inverse_policy_omits_inverse_type_id(compiled_dir: Path, ids_data: dict) -> None:
    """has_part_number (policy: none) must NOT have inverseTypeId."""
    rel_id = ids_data["relationshipTypes"]["has_part_number"]
    defn = _load_json(
        compiled_dir / "RelationshipTypes" / rel_id / "definition.json"
    )
    assert "inverseTypeId" not in defn, "has_part_number must not have inverseTypeId (policy: none)"


# ---------------------------------------------------------------------------
# blob_url property format
# ---------------------------------------------------------------------------


def test_blob_url_property_has_format_uri_on_figure(compiled_dir: Path, ids_data: dict) -> None:
    """Figure.blob_url → type=String, format=uri per SPEC-003 §7."""
    fig_id = ids_data["entityTypes"]["Figure"]
    defn = _load_json(compiled_dir / "EntityTypes" / fig_id / "definition.json")
    blob_props = [p for p in defn["properties"] if p["name"] == "blob_url"]
    assert blob_props, "Figure definition must contain blob_url property"
    prop = blob_props[0]
    assert prop["type"] == "String"
    assert prop.get("format") == "uri", "blob_url property must have format: uri"


def test_blob_url_property_has_format_uri_on_image_asset(
    compiled_dir: Path, ids_data: dict
) -> None:
    """ImageAsset.blob_url → type=String, format=uri."""
    ia_id = ids_data["entityTypes"]["ImageAsset"]
    defn = _load_json(compiled_dir / "EntityTypes" / ia_id / "definition.json")
    blob_props = [p for p in defn["properties"] if p["name"] == "blob_url"]
    assert blob_props, "ImageAsset definition must contain blob_url property"
    assert blob_props[0].get("format") == "uri"


def test_blob_url_property_has_format_uri_on_visual_region(
    compiled_dir: Path, ids_data: dict
) -> None:
    """VisualRegion.blob_url → format=uri."""
    vr_id = ids_data["entityTypes"]["VisualRegion"]
    defn = _load_json(compiled_dir / "EntityTypes" / vr_id / "definition.json")
    blob_props = [p for p in defn["properties"] if p["name"] == "blob_url"]
    assert blob_props, "VisualRegion definition must contain blob_url property"
    assert blob_props[0].get("format") == "uri"


def test_plain_string_property_has_no_format(compiled_dir: Path) -> None:
    """Non-blob_url string property must NOT have a format field."""
    defn = _load_json(compiled_dir / "EntityTypes" / "1000000000000000001" / "definition.json")
    display_props = [p for p in defn["properties"] if p["name"] == "display_name"]
    assert display_props
    assert "format" not in display_props[0]


# ---------------------------------------------------------------------------
# REST InlineBase64 parts
# ---------------------------------------------------------------------------


def test_rest_parts_non_empty(rest_parts: list[dict]) -> None:
    assert len(rest_parts) > 0


def test_rest_parts_schema(rest_parts: list[dict]) -> None:
    for part in rest_parts:
        assert "path" in part, f"Part missing 'path': {part}"
        assert "payload" in part, f"Part missing 'payload': {part}"
        assert part["payloadType"] == "InlineBase64", f"Wrong payloadType: {part}"


def test_rest_parts_base64_decodes_to_valid_json(rest_parts: list[dict]) -> None:
    for part in rest_parts:
        try:
            decoded = base64.b64decode(part["payload"])
        except Exception as exc:
            pytest.fail(f"Part '{part['path']}' payload is not valid Base64: {exc}")
        try:
            obj = json.loads(decoded)
        except json.JSONDecodeError as exc:
            pytest.fail(f"Part '{part['path']}' decoded to invalid JSON: {exc}")
        assert isinstance(obj, dict), f"Part '{part['path']}' decoded to non-dict"


def test_rest_parts_platform_present(rest_parts: list[dict]) -> None:
    paths = {p["path"] for p in rest_parts}
    assert ".platform" in paths, ".platform must be present in REST parts"


def test_rest_parts_device_definition_present(rest_parts: list[dict]) -> None:
    paths = {p["path"] for p in rest_parts}
    assert "EntityTypes/1000000000000000001/definition.json" in paths


def test_rest_parts_has_component_definition_present(rest_parts: list[dict]) -> None:
    paths = {p["path"] for p in rest_parts}
    assert "RelationshipTypes/2000000000000000001/definition.json" in paths


# ---------------------------------------------------------------------------
# Top-level definition.json manifest
# ---------------------------------------------------------------------------


def test_top_level_definition_lists_all_rest_parts(
    compiled_dir: Path, rest_parts: list[dict]
) -> None:
    definition = _load_json(compiled_dir / "definition.json")
    listed_paths = {p["path"] for p in definition["parts"]}
    for part in rest_parts:
        assert part["path"] in listed_paths, (
            f"REST part '{part['path']}' is missing from definition.json"
        )


def test_top_level_definition_platform_schema(compiled_dir: Path) -> None:
    """definition.json parts must include .platform with InlineBase64 payload."""
    definition = _load_json(compiled_dir / "definition.json")
    platform_parts = [p for p in definition["parts"] if p["path"] == ".platform"]
    assert len(platform_parts) == 1
    assert platform_parts[0]["payloadType"] == "InlineBase64"
    # Payload must decode to valid JSON with Ontology metadata
    decoded = json.loads(base64.b64decode(platform_parts[0]["payload"]))
    assert decoded["metadata"]["type"] == "Ontology"


# ---------------------------------------------------------------------------
# .platform file content
# ---------------------------------------------------------------------------


def test_platform_contains_required_fields(compiled_dir: Path) -> None:
    plat = _load_json(compiled_dir / ".platform")
    assert plat["metadata"]["type"] == "Ontology"
    assert plat["metadata"]["displayName"] == "FabricKG"
    assert "logicalId" in plat["config"]
    assert plat["config"]["version"] == "2.0"


def test_platform_logical_id_is_stable(compiled_dir: Path, compiler: OntologyCompiler) -> None:
    """logicalId must be the same deterministic UUID on every compile."""
    plat1 = _load_json(compiled_dir / ".platform")
    # Get a fresh REST part (no disk I/O needed)
    platform_part = next(p for p in compiler.get_rest_parts() if p["path"] == ".platform")
    plat2 = json.loads(base64.b64decode(platform_part["payload"]))
    assert plat1["config"]["logicalId"] == plat2["config"]["logicalId"]


# ---------------------------------------------------------------------------
# Deterministic GUID derivation
# ---------------------------------------------------------------------------


def test_guid_derivation_is_stable() -> None:
    g1 = _derive_guid("Device", "entities")
    g2 = _derive_guid("Device", "entities")
    assert g1 == g2


def test_guid_derivation_differs_by_type_name() -> None:
    assert _derive_guid("Device", "entities") != _derive_guid("Component", "entities")


def test_guid_derivation_differs_by_table() -> None:
    assert _derive_guid("Device", "entities") != _derive_guid("Device", "relationships")


def test_data_binding_file_name_is_derived_guid(compiled_dir: Path, ids_data: dict) -> None:
    """The data binding file name equals _derive_guid('Device', 'entities')."""
    expected_guid = _derive_guid("Device", "entities")
    expected_file = (
        compiled_dir
        / "EntityTypes"
        / "1000000000000000001"
        / "DataBindings"
        / f"{expected_guid}.json"
    )
    assert expected_file.exists(), f"Expected DataBindings/{expected_guid}.json for Device"


def test_contextualization_file_name_is_derived_guid(compiled_dir: Path) -> None:
    """Contextualization file name = _derive_guid('has_component', 'relationships')."""
    expected_guid = _derive_guid("has_component", "relationships")
    expected_file = (
        compiled_dir
        / "RelationshipTypes"
        / "2000000000000000001"
        / "Contextualizations"
        / f"{expected_guid}.json"
    )
    assert expected_file.exists(), f"Expected Contextualizations/{expected_guid}.json"


# ---------------------------------------------------------------------------
# Data binding content
# ---------------------------------------------------------------------------


def test_data_binding_table_name(compiled_dir: Path) -> None:
    guid = _derive_guid("Device", "entities")
    binding = _load_json(
        compiled_dir
        / "EntityTypes"
        / "1000000000000000001"
        / "DataBindings"
        / f"{guid}.json"
    )
    assert binding["tableName"] == "entities"
    assert binding["entityIdColumn"] == "entity_id"
    assert binding["displayNameColumn"] == "display_name"


def test_data_binding_type_filter(compiled_dir: Path) -> None:
    guid = _derive_guid("Device", "entities")
    binding = _load_json(
        compiled_dir
        / "EntityTypes"
        / "1000000000000000001"
        / "DataBindings"
        / f"{guid}.json"
    )
    assert binding["typeFilterColumn"] == "entity_type"
    assert binding["typeFilterValue"] == "Device"


def test_data_binding_property_mappings_present(compiled_dir: Path) -> None:
    guid = _derive_guid("Device", "entities")
    binding = _load_json(
        compiled_dir
        / "EntityTypes"
        / "1000000000000000001"
        / "DataBindings"
        / f"{guid}.json"
    )
    assert "propertyMappings" in binding
    mapping_names = {m["propertyName"] for m in binding["propertyMappings"]}
    # Device has canonical_key and description in additionalColumns
    assert "canonical_key" in mapping_names


def test_data_binding_datasource_type(compiled_dir: Path) -> None:
    guid = _derive_guid("Device", "entities")
    binding = _load_json(
        compiled_dir
        / "EntityTypes"
        / "1000000000000000001"
        / "DataBindings"
        / f"{guid}.json"
    )
    assert binding["dataSourceType"] == "Lakehouse"


# ---------------------------------------------------------------------------
# Contextualization content
# ---------------------------------------------------------------------------


def test_contextualization_table_and_columns(compiled_dir: Path) -> None:
    guid = _derive_guid("has_component", "relationships")
    ctx = _load_json(
        compiled_dir
        / "RelationshipTypes"
        / "2000000000000000001"
        / "Contextualizations"
        / f"{guid}.json"
    )
    assert ctx["tableName"] == "relationships"
    assert ctx["typeFilterValue"] == "has_component"
    assert "contextualizationId" in ctx


# ---------------------------------------------------------------------------
# Validation error tests — use tiny inline models + id locks
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, model_dict: dict) -> Path:
    path.write_text(yaml.dump(model_dict), encoding="utf-8")
    return path


def _write_json(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _minimal_ids(entity_ids: dict | None = None, rel_ids: dict | None = None) -> dict:
    return {
        "entityTypes": entity_ids or {},
        "relationshipTypes": rel_ids or {},
        "properties": {},
    }


def test_missing_entity_id_raises(tmp_path: Path) -> None:
    """Entity type present in model.yaml but absent from ids.lock → OntologyCompilerError."""
    model = {
        "ontology": {
            "name": "TestOntology",
            "entityTypes": [
                {"name": "UnknownEntity", "description": "", "module": "m", "properties": []}
            ],
            "relationshipTypes": [],
        }
    }
    model_p = _write_yaml(tmp_path / "model.yaml", model)
    ids_p = _write_json(tmp_path / "ids.lock.json", _minimal_ids())

    with pytest.raises(OntologyCompilerError, match="UnknownEntity.*no matching ID"):
        OntologyCompiler(model_p, ids_p)


def test_missing_relationship_id_raises(tmp_path: Path) -> None:
    """Relationship type present in model.yaml but absent from ids.lock → OntologyCompilerError."""
    model = {
        "ontology": {
            "name": "TestOntology",
            "entityTypes": [
                {"name": "A", "description": "", "module": "m", "properties": []},
                {"name": "B", "description": "", "module": "m", "properties": []},
            ],
            "relationshipTypes": [
                {
                    "name": "rel_ab",
                    "description": "",
                    "module": "m",
                    "sourceType": "A",
                    "targetType": "B",
                    "inversePolicy": "none",
                    "evidenceLink": False,
                    "dataBinding": {
                        "table": "relationships",
                        "relationshipIdColumn": "id",
                        "sourceEntityIdColumn": "src",
                        "targetEntityIdColumn": "tgt",
                    },
                }
            ],
        }
    }
    model_p = _write_yaml(tmp_path / "model.yaml", model)
    ids_p = _write_json(
        tmp_path / "ids.lock.json",
        _minimal_ids(entity_ids={"A": "1000000000000000001", "B": "1000000000000000002"}),
    )

    with pytest.raises(OntologyCompilerError, match="rel_ab.*no matching ID"):
        OntologyCompiler(model_p, ids_p)


def test_unknown_relationship_source_raises(tmp_path: Path) -> None:
    """Relationship with unknown sourceType → OntologyCompilerError."""
    model = {
        "ontology": {
            "name": "TestOntology",
            "entityTypes": [
                {"name": "B", "description": "", "module": "m", "properties": []},
            ],
            "relationshipTypes": [
                {
                    "name": "rel_xb",
                    "description": "",
                    "module": "m",
                    "sourceType": "X",  # X does not exist
                    "targetType": "B",
                    "inversePolicy": "none",
                    "evidenceLink": False,
                    "dataBinding": {
                        "table": "relationships",
                        "relationshipIdColumn": "id",
                        "sourceEntityIdColumn": "src",
                        "targetEntityIdColumn": "tgt",
                    },
                }
            ],
        }
    }
    model_p = _write_yaml(tmp_path / "model.yaml", model)
    ids_p = _write_json(
        tmp_path / "ids.lock.json",
        _minimal_ids(
            entity_ids={"B": "1000000000000000002"},
            rel_ids={"rel_xb": "2000000000000000001"},
        ),
    )

    with pytest.raises(OntologyCompilerError, match="unknown sourceType.*X"):
        OntologyCompiler(model_p, ids_p)


def test_unknown_relationship_target_raises(tmp_path: Path) -> None:
    """Relationship with unknown targetType → OntologyCompilerError."""
    model = {
        "ontology": {
            "name": "TestOntology",
            "entityTypes": [
                {"name": "A", "description": "", "module": "m", "properties": []},
            ],
            "relationshipTypes": [
                {
                    "name": "rel_ax",
                    "description": "",
                    "module": "m",
                    "sourceType": "A",
                    "targetType": "X",  # X does not exist
                    "inversePolicy": "none",
                    "evidenceLink": False,
                    "dataBinding": {
                        "table": "relationships",
                        "relationshipIdColumn": "id",
                        "sourceEntityIdColumn": "src",
                        "targetEntityIdColumn": "tgt",
                    },
                }
            ],
        }
    }
    model_p = _write_yaml(tmp_path / "model.yaml", model)
    ids_p = _write_json(
        tmp_path / "ids.lock.json",
        _minimal_ids(
            entity_ids={"A": "1000000000000000001"},
            rel_ids={"rel_ax": "2000000000000000001"},
        ),
    )

    with pytest.raises(OntologyCompilerError, match="unknown targetType.*X"):
        OntologyCompiler(model_p, ids_p)


def test_duplicate_id_raises(tmp_path: Path) -> None:
    """Duplicate numeric ID in ids.lock.json → OntologyCompilerError."""
    model = {
        "ontology": {
            "name": "TestOntology",
            "entityTypes": [
                {"name": "A", "description": "", "module": "m", "properties": []},
                {"name": "B", "description": "", "module": "m", "properties": []},
            ],
            "relationshipTypes": [],
        }
    }
    model_p = _write_yaml(tmp_path / "model.yaml", model)
    # Both A and B share the same ID — invalid
    ids_p = _write_json(
        tmp_path / "ids.lock.json",
        _minimal_ids(
            entity_ids={
                "A": "1000000000000000001",
                "B": "1000000000000000001",  # duplicate!
            }
        ),
    )

    with pytest.raises(OntologyCompilerError, match="Duplicate.*ID"):
        OntologyCompiler(model_p, ids_p)
