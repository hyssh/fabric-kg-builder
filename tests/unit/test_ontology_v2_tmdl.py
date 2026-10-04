"""Unit tests for the v1 JSON -> v2 TMDL ontology definition converter."""

from __future__ import annotations

import base64
import json

import pytest

from fabric_kg_builder.deploy.ontology_v2_tmdl import (
    OntologyV2ConversionError,
    convert_v1_parts_to_v2_tmdl,
)

WS = "11111111-1111-1111-1111-111111111111"
LH = "22222222-2222-2222-2222-222222222222"


def _part(path: str, payload: dict) -> dict:
    return {
        "path": path,
        "payload": base64.b64encode(json.dumps(payload).encode()).decode(),
        "payloadType": "InlineBase64",
    }


def _prop(pid: int, name: str, *, redefines: int | None = None, description: str = "") -> dict:
    p: dict = {"id": str(pid), "name": name, "valueType": "String"}
    if redefines is not None:
        p["redefines"] = str(redefines)
    if description:
        p["semanticEnrichment"] = {"description": description}
    return p


def _entity(eid: int, name: str, props: list, *, base: int | None = None,
            key: int | None = None, description: str = "") -> dict:
    d: dict = {"id": str(eid), "name": name, "properties": props}
    if base is not None:
        d["baseEntityTypeId"] = str(base)
    if key is not None:
        d["entityIdParts"] = [str(key)]
    if description:
        d["semanticEnrichment"] = {"description": description}
    return _part(f"EntityTypes/{eid}/definition.json", d)


def _binding(eid: int, table: str, pairs: list[tuple[int, str]]) -> dict:
    return _part(
        f"EntityTypes/{eid}/DataBindings/b{eid}.json",
        {
            "dataBindingConfiguration": {
                "dataBindingType": "NonTimeSeries",
                "sourceTableProperties": {"sourceTableName": table},
                "propertyBindings": [
                    {"targetPropertyId": str(p), "sourceColumnName": c} for p, c in pairs
                ],
            }
        },
    )


def _relationship(rid: int, name: str, src: int, tgt: int, table: str) -> list[dict]:
    return [
        _part(
            f"RelationshipTypes/{rid}/definition.json",
            {"id": str(rid), "name": name,
             "source": {"entityTypeId": str(src)}, "target": {"entityTypeId": str(tgt)}},
        ),
        _part(
            f"RelationshipTypes/{rid}/Contextualizations/c{rid}.json",
            {
                "dataBindingTable": {"sourceTableName": table},
                "sourceKeyRefBindings": [{"sourceColumnName": "__source_entity_id"}],
                "targetKeyRefBindings": [{"sourceColumnName": "__target_entity_id"}],
            },
        ),
    ]


def _v1_parts() -> list[dict]:
    root = _entity(1, "surface_entity", [_prop(10, "id"), _prop(11, "label")], key=10,
                   description="Any published surface entity.")
    group = _entity(2, "group_Device", [_prop(20, "community_key")], base=1)
    device = _entity(
        3, "Device",
        [_prop(30, "id", redefines=10), _prop(31, "label", redefines=11, description="Name.")],
        base=2, key=30, description="A device.",
    )
    step = _entity(
        4, "Step", [_prop(40, "id", redefines=10), _prop(41, "label", redefines=11)],
        base=1, key=40,
    )
    return [
        step, device, group, root,
        _binding(3, "t_device", [(30, "__canonical_id"), (31, "label")]),
        _binding(4, "t_step", [(40, "__canonical_id"), (41, "label")]),
        *_relationship(100, "Step_On_Device", 4, 3, "rel_step_on_device"),
        *_relationship(101, "Step_Has_Substep", 4, 4, "rel_step_has_substep"),
    ]


def _convert(parts=None, **kw) -> dict[str, str]:
    out = convert_v1_parts_to_v2_tmdl(
        _v1_parts() if parts is None else parts,
        workspace_id=WS, lakehouse_id=LH, display_name="onto_v2", **kw,
    )
    assert all(p["payloadType"] == "InlineBase64" for p in out)
    return {p["path"]: base64.b64decode(p["payload"]).decode() for p in out}


def test_platform_is_v2_and_database_compat_level():
    files = _convert()
    platform = json.loads(files[".platform"])
    assert platform["config"]["version"] == "2.0"
    assert platform["metadata"] == {"type": "Ontology", "displayName": "onto_v2"}
    assert "platformProperties/2.0.0" in platform["$schema"]
    assert "compatibilityLevel: 1000000" in files["database.tmdl"]
    assert f"{WS}/{LH}" in files["expressions.tmdl"]


def test_conversion_is_deterministic_and_order_independent():
    a = _convert()
    b = _convert(list(reversed(_v1_parts())))
    assert a == b
    assert list(a) == list(_convert())


def test_entities_ordered_base_first_in_model():
    model = _convert()["model.tmdl"].splitlines()
    refs = [line.split()[-1] for line in model if line.startswith("ref entity")]
    assert refs.index("surface_entity") < refs.index("group_Device") < refs.index("Device")
    assert refs.index("surface_entity") < refs.index("Step")


def test_inheritance_redefines_and_override_marker_placement():
    files = _convert()
    root = files["entities/surface_entity.tmdl"]
    assert "baseEntityType" not in root and "backingTable" not in root
    assert "keyProperty: id" in root
    assert "overriddenMetadataField" not in root

    group = files["entities/group_Device.tmdl"]
    assert "baseEntityType: surface_entity" in group
    assert "backingTable" not in group and "backingConfiguration" not in group
    assert "overriddenMetadataField" not in group  # no local description

    device = files["entities/Device.tmdl"].splitlines()
    assert device[0] == "/// A device."
    assert "\tbaseEntityType: group_Device" in device
    assert "\tbackingTable: t_device" in device
    key_at = device.index("\tkeyProperty: id")
    marker_at = device.index("\toverriddenMetadataField description")
    first_prop = device.index("\tproperty id")
    # The marker is a child object: after all scalar properties, before properties.
    assert key_at < marker_at < first_prop
    assert "\t\tredefines: surface_entity.id" in device
    assert "\t\tredefines: surface_entity.label" in device
    assert "\t\t\tvalueColumn: t_device.__canonical_id" in device

    label_at = device.index("\tproperty label")
    tail = device[label_at:]
    assert tail.index("\t\tredefines: surface_entity.label") < tail.index(
        "\t\toverriddenMetadataField description"
    ) < tail.index("\t\tbackingConfiguration")

    step = files["entities/Step.tmdl"]
    assert "overriddenMetadataField" not in step


def test_junction_tables_and_tom_relationships():
    files = _convert()
    assert "tables/rel_step_on_device.tmdl" in files
    junction = files["tables/rel_step_on_device.tmdl"]
    assert "__source_entity_id" in junction and "__target_entity_id" in junction
    assert "dataType: string" in junction

    rels = files["relationships.tmdl"]
    assert (
        "relationship Step_On_Device__source\n"
        "\tfromColumn: rel_step_on_device.__source_entity_id\n"
        "\ttoColumn: t_step.__canonical_id\n"
    ) in rels
    assert (
        "relationship Step_On_Device__target\n"
        "\tfromColumn: rel_step_on_device.__target_entity_id\n"
        "\ttoColumn: t_device.__canonical_id\n"
    ) in rels
    # Self-relationship: the second TOM relationship must be inactive.
    assert "relationship Step_Has_Substep__source\n\tfromColumn" in rels
    assert "relationship Step_Has_Substep__target\n\tisActive: false\n" in rels
    assert rels.count("isActive: false") == 1

    er = files["entityRelationships.tmdl"]
    assert "entityRelationship Step_On_Device" in er
    assert "\tfromEntity: Step\n\ttoEntity: Device" in er
    assert "\t\ttype: table" in er
    assert "\t\tfromRelationship: Step_On_Device__source" in er
    assert "\t\ttoRelationship: Step_On_Device__target" in er


def test_column_type_overrides_apply():
    files = _convert(column_types={"t_device": {"label": "int64"}})
    assert "column label\n\t\tdataType: int64" in files["tables/t_device.tmdl"]


def test_no_relationships_omits_relationship_parts():
    parts = [p for p in _v1_parts() if not p["path"].startswith("RelationshipTypes/")]
    files = _convert(parts)
    assert "relationships.tmdl" not in files
    assert "entityRelationships.tmdl" not in files


def _replace(parts: list[dict], path: str, payload: dict) -> list[dict]:
    return [_part(path, payload) if p["path"] == path else p for p in parts]


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda parts: [], "no entity types"),
        (
            lambda parts: _replace(parts, "EntityTypes/2/definition.json",
                                   {"id": "2", "name": "group_Device", "properties": [],
                                    "baseEntityTypeId": "3"}),
            "cycle",
        ),
        (
            lambda parts: _replace(parts, "EntityTypes/4/definition.json",
                                   {"id": "4", "name": "Step", "baseEntityTypeId": "1",
                                    "entityIdParts": ["40"],
                                    "properties": [{"id": "40", "name": "id",
                                                    "valueType": "Weird"}]}),
            "unsupported valueType",
        ),
        (
            lambda parts: [p for p in parts if p["path"] != "EntityTypes/3/DataBindings/b3.json"],
            "unbound entity",
        ),
        (
            lambda parts: [p for p in parts
                           if p["path"] != "RelationshipTypes/100/Contextualizations/c100.json"],
            "exactly one contextualization",
        ),
        (
            lambda parts: parts + [_entity(5, "device", [_prop(50, "id")])],
            "collide",
        ),
    ],
)
def test_conversion_errors(mutate, message):
    with pytest.raises(OntologyV2ConversionError, match=message):
        _convert(mutate(_v1_parts()))


def test_column_types_from_parquet_dir(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    from fabric_kg_builder.deploy.ontology_v2_tmdl import column_types_from_parquet_dir

    table = pa.table({
        "label": pa.array(["a"]), "n": pa.array([1], pa.int32()), "f": pa.array([1.5]),
        "b": pa.array([True]), "ts": pa.array([0], pa.timestamp("us", tz="UTC")),
    })
    pq.write_table(table, tmp_path / "t_device.parquet")
    assert column_types_from_parquet_dir(tmp_path) == {"t_device": {
        "label": "string", "n": "int64", "f": "double", "b": "boolean", "ts": "dateTime",
    }}


def _invoke(args):
    from click.testing import CliRunner

    from fabric_kg_builder.cli.main import cli

    return CliRunner().invoke(cli, ["app", "convert-ontology-v2", *args])


def test_cli_convert_ontology_v2_writes_parts_and_tmdl(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    source = tmp_path / "ontology.json"
    source.write_text(json.dumps({"parts": _v1_parts()}))
    tables = tmp_path / "tables"
    tables.mkdir()
    pq.write_table(pa.table({"label": pa.array([7], pa.int64())}), tables / "t_device.parquet")
    out = tmp_path / "v2" / "def.json"
    result = _invoke([
        "--v1-ontology", str(source), "--workspace-id", WS, "--lakehouse-id", LH,
        "--display-name", "onto_v2", "--tables-dir", str(tables), "--out", str(out),
        "--explode-dir", str(tmp_path / "tmdl"),
    ])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "converted"
    parts = json.loads(out.read_text())["parts"]
    files = {p["path"]: base64.b64decode(p["payload"]).decode() for p in parts}
    assert files == _convert(column_types={"t_device": {"label": "int64"}})
    assert (tmp_path / "tmdl" / "tables" / "t_device.tmdl").read_text() == files["tables/t_device.tmdl"]


def test_cli_convert_ontology_v2_refuses_existing_out_and_bad_input(tmp_path):
    source = tmp_path / "ontology.json"
    source.write_text(json.dumps({"parts": _v1_parts()}))
    out = tmp_path / "def.json"
    out.write_text("sentinel")
    base = ["--v1-ontology", str(source), "--workspace-id", WS, "--lakehouse-id", LH,
            "--display-name", "onto_v2"]
    result = _invoke([*base, "--out", str(out)])
    assert result.exit_code != 0 and "already exists" in result.output
    assert out.read_text() == "sentinel"

    source.write_text(json.dumps({"no_parts": []}))
    result = _invoke([*base, "--out", str(tmp_path / "new.json")])
    assert result.exit_code != 0
    assert not (tmp_path / "new.json").exists()
