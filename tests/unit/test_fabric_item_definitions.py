"""Tests for the Fabric-native ontology and semantic model definition compilers."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fabric_kg_builder.deploy.fabric_ontology_definition import (
    BASE_ENTITY_TYPE_NAME,
    compile_fabric_ontology_definition,
)
from fabric_kg_builder.deploy.fabric_semantic_model_definition import (
    compile_fabric_semantic_model_definition,
)

WORKSPACE_ID = "00000000-0000-0000-0000-0000000000ws"
LAKEHOUSE_ID = "00000000-0000-0000-0000-0000000000lh"


def _l5a_ontology() -> dict[str, object]:
    return {
        "entity_types": [
            {
                "id": "1000001",
                "canonical_semantic_type_id": "semantic-type:surface-component",
                "physical_table_id": "l5a_type_semantic_type_surface_component",
                "physical_identity_column": "__canonical_id",
                "properties": [
                    {
                        "id": "2000001",
                        "canonical_property_id": "property:component-id",
                        "physical_column_id": "l5a_prop_property_component_id",
                    }
                ],
            },
            {
                "id": "1000003",
                "canonical_semantic_type_id": "semantic-type:surface-evidence",
                "physical_table_id": "l5a_type_semantic_type_surface_evidence",
                "physical_identity_column": "__canonical_id",
                "properties": [],
            },
        ],
        "relationship_types": [
            {
                "id": "3000002",
                "canonical_semantic_relationship_id": (
                    "relationship-type:device-has-component"
                ),
                "physical_table_id": (
                    "l5a_rel_relationship_type_device_has_component"
                ),
                "allowed_source_semantic_type_ids": [
                    "semantic-type:surface-component"
                ],
                "allowed_target_semantic_type_ids": [
                    "semantic-type:surface-evidence"
                ],
                "source_identity_column": "__source_entity_id",
                "target_identity_column": "__target_entity_id",
            },
            {
                "id": "3000001",
                "canonical_semantic_relationship_id": (
                    "relationship-type:assertion-supported-by-evidence"
                ),
                "physical_table_id": (
                    "l5a_rel_relationship_type_assertion_supported_by_evidence"
                ),
                "allowed_source_semantic_type_ids": [
                    "semantic-type:surface-component",
                    "semantic-type:surface-evidence",
                ],
                "allowed_target_semantic_type_ids": [
                    "semantic-type:surface-evidence"
                ],
                "source_identity_column": "__source_entity_id",
                "target_identity_column": "__target_entity_id",
            },
        ],
    }


def _compile():
    return compile_fabric_ontology_definition(
        _l5a_ontology(),
        workspace_id=WORKSPACE_ID,
        lakehouse_id=LAKEHOUSE_ID,
        display_name="fabric_kg_024_ontology",
        description="test",
    )


def _payload(parts, path: str) -> dict:
    for part in parts:
        if part["path"] == path:
            return json.loads(base64.b64decode(part["payload"]))
    raise AssertionError(f"no part at {path}: {[p['path'] for p in parts]}")


def _raw_payload(parts, path: str) -> str:
    for part in parts:
        if part["path"] == path:
            return base64.b64decode(part["payload"]).decode()
    raise AssertionError(f"no part at {path}")


def test_source_table_properties_lead_with_the_source_type_discriminator() -> None:
    """Fabric deserializes ``sourceTableProperties`` polymorphically.

    ``sourceType`` is the discriminator and Fabric rejects the whole item import
    with ``ALMOperationImportFailed`` when it is not the first key of the object.
    Sorting keys anywhere in this compiler reintroduces that failure, so pin the
    ordering in the emitted bytes rather than in the in-memory dict.
    """
    parts = _compile().parts
    binding_paths = [
        p["path"] for p in parts if "/DataBindings/" in p["path"]
    ]
    assert binding_paths, "expected at least one data binding part"
    for path in binding_paths:
        raw = _raw_payload(parts, path)
        offset = raw.index('"sourceTableProperties"')
        body = raw[offset:]
        first_key = body.split("{", 1)[1].lstrip().split('"', 2)[1]
        assert first_key == "sourceType", (
            f"{path}: sourceTableProperties must lead with sourceType, got {first_key}"
        )


def test_data_bindings_reference_the_target_lakehouse_and_workspace() -> None:
    parts = _compile().parts
    for part in parts:
        if "/DataBindings/" not in part["path"]:
            continue
        source = json.loads(base64.b64decode(part["payload"]))[
            "dataBindingConfiguration"
        ]["sourceTableProperties"]
        assert source["sourceType"] == "LakehouseTable"
        assert source["workspaceId"] == WORKSPACE_ID
        assert source["itemId"] == LAKEHOUSE_ID


def test_polymorphic_relationship_is_widened_and_reported_never_silent() -> None:
    """A multi-source relationship must widen to the base type *and* be reported."""
    result = _compile()
    assert result.widened_relationships == (
        "relationship-type:assertion-supported-by-evidence",
    )
    rel_paths = [
        p["path"]
        for p in result.parts
        if p["path"].startswith("RelationshipTypes/")
        and p["path"].endswith("definition.json")
    ]
    widened = [
        _payload(result.parts, path)
        for path in rel_paths
        if _payload(result.parts, path)["name"] == "assertion_supported_by_evidence"
    ]
    assert len(widened) == 1
    base_id = [
        _payload(result.parts, p["path"])["id"]
        for p in result.parts
        if p["path"].startswith("EntityTypes/")
        and p["path"].endswith("definition.json")
        and _payload(result.parts, p["path"])["name"] == BASE_ENTITY_TYPE_NAME
    ]
    assert widened[0]["source"]["entityTypeId"] == base_id[0]


def test_single_source_relationship_is_not_widened() -> None:
    result = _compile()
    assert (
        "relationship-type:device-has-component" not in result.widened_relationships
    )


def test_binding_identifiers_are_guids_and_stable_across_compilations() -> None:
    """Fabric rejects non-GUID binding ids, and recompiles must not churn them."""
    import uuid as _uuid

    first = _compile().parts
    second = _compile().parts
    assert [p["path"] for p in first] == [p["path"] for p in second]
    for part in first:
        if "/DataBindings/" not in part["path"] and (
            "/Contextualizations/" not in part["path"]
        ):
            continue
        identifier = json.loads(base64.b64decode(part["payload"]))["id"]
        _uuid.UUID(identifier)
        assert part["path"].endswith(f"{identifier}.json")


def test_every_entity_type_gets_exactly_one_data_binding() -> None:
    parts = _compile().parts
    entity_ids = {
        p["path"].split("/")[1]
        for p in parts
        if p["path"].startswith("EntityTypes/") and p["path"].endswith("definition.json")
    }
    bound_ids = {
        p["path"].split("/")[1] for p in parts if "/DataBindings/" in p["path"]
    }
    assert entity_ids == bound_ids


def _write_tables(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table(
            {
                "entity_id": pa.array(["e1"], pa.string()),
                "candidate_ids": pa.array([["c1"]], pa.list_(pa.string())),
                "depth": pa.array([1], pa.int32()),
                "is_sealed": pa.array([True], pa.bool_()),
            }
        ),
        root / "l4_semantic_asserted_entities.parquet",
    )
    pq.write_table(
        pa.table(
            {
                "__canonical_id": pa.array(["x"], pa.string()),
                "l5a_prop_property_component_id": pa.array(["p"], pa.string()),
            }
        ),
        root / "l5a_type_semantic_type_surface_component.parquet",
    )


def test_semantic_model_excludes_complex_columns_and_reports_them(
    tmp_path: Path,
) -> None:
    """DirectLake cannot project array columns; they must be dropped *and* reported."""
    _write_tables(tmp_path)
    result = compile_fabric_semantic_model_definition(
        tables_root=tmp_path,
        workspace_id=WORKSPACE_ID,
        lakehouse_id=LAKEHOUSE_ID,
    )
    assert [
        (e.table_name, e.column_name) for e in result.excluded_columns
    ] == [("l4_semantic_asserted_entities", "candidate_ids")]
    entities = _raw_payload(
        result.parts, "definition/tables/l4_semantic_asserted_entities.tmdl"
    )
    assert "candidate_ids" not in entities
    assert "entity_id" in entities


def test_semantic_model_emits_direct_lake_partitions_for_every_table(
    tmp_path: Path,
) -> None:
    _write_tables(tmp_path)
    result = compile_fabric_semantic_model_definition(
        tables_root=tmp_path,
        workspace_id=WORKSPACE_ID,
        lakehouse_id=LAKEHOUSE_ID,
    )
    assert sorted(result.table_names) == [
        "l4_semantic_asserted_entities",
        "l5a_type_semantic_type_surface_component",
    ]
    for name in result.table_names:
        tmdl = _raw_payload(result.parts, f"definition/tables/{name}.tmdl")
        assert "mode: directLake" in tmdl
        assert f"entityName: {name}" in tmdl
    model = _raw_payload(result.parts, "definition/model.tmdl")
    for name in result.table_names:
        assert f"ref table {name}" in model
    expressions = _raw_payload(result.parts, "definition/expressions.tmdl")
    assert f"onelake.dfs.fabric.microsoft.com/{WORKSPACE_ID}/{LAKEHOUSE_ID}" in (
        expressions
    )


def test_semantic_model_maps_scalar_arrow_types_to_tmdl_types(tmp_path: Path) -> None:
    _write_tables(tmp_path)
    result = compile_fabric_semantic_model_definition(
        tables_root=tmp_path,
        workspace_id=WORKSPACE_ID,
        lakehouse_id=LAKEHOUSE_ID,
    )
    tmdl = _raw_payload(
        result.parts, "definition/tables/l4_semantic_asserted_entities.tmdl"
    )
    assert "dataType: int64" in tmdl
    assert "dataType: boolean" in tmdl
    assert "dataType: string" in tmdl


def test_semantic_model_emits_underscore_prefixed_columns_as_bare_identifiers(
    tmp_path: Path,
) -> None:
    """``__``-prefixed physical columns are valid bare TMDL identifiers.

    Quoting them is accepted but Fabric normalizes the quotes away on ingest,
    which would make a recompile differ from a live ``getDefinition`` readback
    for no semantic reason and mask real drift.
    """
    _write_tables(tmp_path)
    result = compile_fabric_semantic_model_definition(
        tables_root=tmp_path,
        workspace_id=WORKSPACE_ID,
        lakehouse_id=LAKEHOUSE_ID,
    )
    tmdl = _raw_payload(
        result.parts,
        "definition/tables/l5a_type_semantic_type_surface_component.tmdl",
    )
    assert "column __canonical_id" in tmdl
    assert "column '__canonical_id'" not in tmdl
    assert "sourceColumn: __canonical_id" in tmdl


def test_semantic_model_refuses_an_empty_tables_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no materialized tables"):
        compile_fabric_semantic_model_definition(
            tables_root=tmp_path,
            workspace_id=WORKSPACE_ID,
            lakehouse_id=LAKEHOUSE_ID,
        )
