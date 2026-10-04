"""Leiden community -> native Ontology ``baseEntityTypeId`` projection."""

from __future__ import annotations

import base64
import json
import re

import pyarrow as pa
import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli.app_cmd import app_cmd
from fabric_kg_builder.deploy import schema2_prototype as prototype
from fabric_kg_builder.deploy.fabric_ontology_definition import (
    BASE_ENTITY_TYPE_ID,
    BASE_IDENTITY_PROPERTY_ID,
    _part,
)
from fabric_kg_builder.deploy.ontology_inheritance import (
    COMMUNITY_TYPE_BIGINT_BASE,
    OntologyInheritanceError,
    apply_community_inheritance,
)
from fabric_kg_builder.deploy.ontology_names import NATIVE_NAME_PATTERN
from fabric_kg_builder.semantic.source_tables import SealedL4ServingSource
from tests.unit.test_l5a_hierarchy_publication import WORKSPACE, hierarchy_input  # noqa: F401


def _payloads(parts):
    return {
        part["path"]: json.loads(base64.b64decode(part["payload"]))
        for part in parts if part["path"].endswith(".json")
    }


def _entity(type_id, name, *, base=None, label=True, extra=()):
    props = [{"id": f"9{type_id}", "name": "id", "redefines": None,
              "baseTypeNamespaceType": None, "valueType": "String"}]
    if label:
        props.append({"id": f"8{type_id}", "name": "label", "redefines": None,
                      "baseTypeNamespaceType": None, "valueType": "String"})
    props.extend(extra)
    return {"id": type_id, "name": name, "baseEntityTypeId": base, "properties": props,
            "entityIdParts": [f"9{type_id}"]}


def _base(label=True):
    props = [{"id": BASE_IDENTITY_PROPERTY_ID, "name": "id", "redefines": None,
              "baseTypeNamespaceType": None, "valueType": "String"}]
    if label:
        props.append({"id": "3000000", "name": "label", "redefines": None,
                      "baseTypeNamespaceType": None, "valueType": "String"})
    return {"id": BASE_ENTITY_TYPE_ID, "name": "surface_entity", "baseEntityTypeId": None,
            "properties": props, "entityIdParts": [BASE_IDENTITY_PROPERTY_ID]}


def _synthetic(label=True):
    ontology = {"entity_types": [
        {"id": "11", "canonical_semantic_type_id": "device"},
        {"id": "12", "canonical_semantic_type_id": "component"},
        {"id": "13", "canonical_semantic_type_id": "symptom"},
        {"id": "14", "canonical_semantic_type_id": "orphan"},
    ]}
    parts = [
        _part("definition.json", {}),
        _part(f"EntityTypes/{BASE_ENTITY_TYPE_ID}/definition.json", _base(label)),
        _part("EntityTypes/11/definition.json", _entity("11", "Device", label=label)),
        _part("EntityTypes/12/definition.json", _entity("12", "Component", label=label)),
        _part("EntityTypes/13/definition.json", _entity("13", "Symptom", label=label)),
        _part("EntityTypes/14/definition.json", _entity("14", "Orphan", label=label)),
        _part("RelationshipTypes/21/definition.json", {"id": "21", "name": "has_part"}),
    ]
    communities = [
        {"community_id": "root", "level": 2, "parent_community_id": None},
        {"community_id": "a", "level": 1, "parent_community_id": "root"},
        {"community_id": "b", "level": 1, "parent_community_id": "root"},
        {"community_id": "a1", "level": 0, "parent_community_id": "a"},
    ]
    members = [
        *({"community_id": "a", "entity_type": "device", "entity_id": f"d{i}"} for i in range(3)),
        {"community_id": "b", "entity_type": "device", "entity_id": "d9"},
        {"community_id": "a", "entity_type": "component", "entity_id": "c1"},
        {"community_id": "b", "entity_type": "component", "entity_id": "c2"},
        *({"community_id": "b", "entity_type": "symptom", "entity_id": f"s{i}"} for i in range(2)),
        {"community_id": "a1", "entity_type": "symptom", "entity_id": "s9"},
        {"community_id": "root", "entity_type": "symptom", "entity_id": "s0"},
    ]
    tables = {
        "graph_communities": pa.Table.from_pylist(communities),
        "graph_community_members": pa.Table.from_pylist(members),
    }
    return parts, ontology, tables


def test_frontier_plurality_assignment_and_redefines():
    parts, ontology, tables = _synthetic()
    result, report = apply_community_inheritance(parts, l5a_ontology=ontology, community_tables=tables)
    payloads = _payloads(result)
    assert report["frontier_community_ids"] == ["a", "b"]
    by_type = {item["canonical_semantic_type_id"]: item for item in report["entity_types"]}
    # device: a=3,b=1 -> a; component tie a=1,b=1 -> smallest id a; symptom b=2 -> b.
    assert by_type["device"]["community_id"] == "a"
    assert by_type["component"]["community_id"] == "a"
    assert by_type["symptom"]["community_id"] == "b"
    assert by_type["orphan"]["community_id"] is None
    assert by_type["orphan"]["base_entity_type_id"] == BASE_ENTITY_TYPE_ID
    names = {item["community_id"]: item["name"] for item in report["community_types"]}
    assert names == {"a": "group_Device", "b": "group_Symptom"}
    a_type = str(COMMUNITY_TYPE_BIGINT_BASE + 1)
    community = payloads[f"EntityTypes/{a_type}/definition.json"]
    assert community["baseEntityTypeId"] == BASE_ENTITY_TYPE_ID
    assert community["entityIdParts"] == [] and community["displayNamePropertyId"] is None
    assert not any(f"EntityTypes/{a_type}/DataBindings" in part["path"] for part in result)
    device = payloads["EntityTypes/11/definition.json"]
    assert device["baseEntityTypeId"] == a_type
    redefines = {prop["id"]: (prop["redefines"], prop["baseTypeNamespaceType"]) for prop in device["properties"]}
    assert redefines == {"911": ("2000000", "Custom"), "811": ("3000000", "Custom")}
    paths = [part["path"] for part in result]
    assert paths.index(f"EntityTypes/{a_type}/definition.json") < paths.index("RelationshipTypes/21/definition.json")
    for payload in payloads.values():
        if "name" in payload and "properties" in payload:
            assert re.fullmatch(NATIVE_NAME_PATTERN, payload["name"])


def test_projection_is_deterministic_and_input_parts_unchanged():
    parts, ontology, tables = _synthetic()
    snapshot = json.dumps(parts, sort_keys=True)
    first = apply_community_inheritance(parts, l5a_ontology=ontology, community_tables=tables)
    second = apply_community_inheritance(parts, l5a_ontology=ontology, community_tables=tables)
    assert first == second
    assert json.dumps(parts, sort_keys=True) == snapshot


def test_missing_label_only_redefines_identity():
    parts, ontology, tables = _synthetic(label=False)
    result, _ = apply_community_inheritance(parts, l5a_ontology=ontology, community_tables=tables)
    device = _payloads(result)["EntityTypes/11/definition.json"]
    assert [(p["id"], p["redefines"]) for p in device["properties"]] == [("911", "2000000")]


def test_shadowing_inherited_name_and_missing_tables_fail_closed():
    parts, ontology, tables = _synthetic(label=False)
    parts[2] = _part("EntityTypes/11/definition.json", _entity("11", "Device", label=False, extra=[
        {"id": "x1", "name": "ID", "redefines": None, "baseTypeNamespaceType": None, "valueType": "String"},
    ]))
    with pytest.raises(OntologyInheritanceError, match="shadows"):
        apply_community_inheritance(parts, l5a_ontology=ontology, community_tables=tables)
    with pytest.raises(OntologyInheritanceError, match="hierarchy"):
        apply_community_inheritance(parts, l5a_ontology=ontology, community_tables={})


def test_community_name_collision_is_suffixed():
    parts, ontology, tables = _synthetic()
    parts[5] = _part("EntityTypes/14/definition.json", _entity("14", "group_Device"))
    _, report = apply_community_inheritance(parts, l5a_ontology=ontology, community_tables=tables)
    names = [item["name"] for item in report["community_types"]]
    assert names[0].startswith("group_Device_") and len(set(n.casefold() for n in names)) == 2


def test_prototype_parts_flag_off_identical_and_on_projects_tree(hierarchy_input, tmp_path, monkeypatch):  # noqa: F811
    inputs, root, _ = hierarchy_input
    source = inputs["source"]
    monkeypatch.setattr(SealedL4ServingSource, "from_run", lambda *args, **kwargs: source)
    with_hierarchy = prototype._compile(source.root, tmp_path, WORKSPACE, "h", hierarchy_state=root)
    without = prototype._compile(source.root, tmp_path, WORKSPACE, "h")
    args = (WORKSPACE, WORKSPACE, "onto", "desc")
    off = prototype._ontology_parts(with_hierarchy, *args)
    assert off == prototype._ontology_parts(without, *args)
    on = prototype._ontology_parts(with_hierarchy, *args, inheritance=True)
    payloads = _payloads(on)
    entity_types = {
        path: payload for path, payload in payloads.items()
        if re.fullmatch(r"EntityTypes/\d+/definition\.json", path)
    }
    assert f"EntityTypes/{BASE_ENTITY_TYPE_ID}/definition.json" in entity_types
    assert not any(part["path"].startswith(f"EntityTypes/{BASE_ENTITY_TYPE_ID}/DataBindings") for part in on)
    assert all(payload["baseEntityTypeId"] is not None for path, payload in entity_types.items()
               if payload["id"] != BASE_ENTITY_TYPE_ID)
    communities = [p for p in entity_types.values() if int(p["id"]) > COMMUNITY_TYPE_BIGINT_BASE]
    assert communities and all(p["entityIdParts"] == [] for p in communities)
    # Non-ontology/off-path parts are untouched by the projection.
    off_other = {p["path"]: p for p in off if not p["path"].startswith("EntityTypes/")}
    on_other = {p["path"]: p for p in on if not p["path"].startswith("EntityTypes/")}
    assert off_other == on_other
    with pytest.raises(prototype.PrototypePublicationError, match="hierarchy"):
        prototype._ontology_parts(without, *args, inheritance=True)


def test_plan_records_inheritance_and_requires_hierarchy(hierarchy_input, tmp_path, monkeypatch):  # noqa: F811
    inputs, root, _ = hierarchy_input
    source = inputs["source"]
    monkeypatch.setattr(SealedL4ServingSource, "from_run", lambda *args, **kwargs: source)
    compilation = prototype._compile(source.root, tmp_path, WORKSPACE, "h", hierarchy_state=root)
    kwargs = dict(
        l4_run=source.root, l3_root=tmp_path, workspace_id=WORKSPACE, name_prefix="h",
        materialize_dir=tmp_path / "data", journal_path=tmp_path / "journal.json",
        approved_limitations=tuple(sorted(set(compilation.limitations))),
        dry_run=True, approve_live=None,
    )
    plan = prototype.publish_schema2_prototype(
        **kwargs, plan_path=tmp_path / "plan.json", hierarchy_state=root, ontology_inheritance=True,
    )
    assert not plan["blockers"]
    assert plan["ontology_inheritance"] == "leiden_community"
    ontology = _payloads(plan["native_definition_templates"]["ontology"]["parts"])
    assert any(p.get("baseEntityTypeId") for p in ontology.values())
    with pytest.raises(prototype.PrototypePublicationError, match="hierarchy"):
        prototype.publish_schema2_prototype(
            **kwargs, plan_path=tmp_path / "plan2.json", ontology_inheritance=True,
        )


def test_cli_rejects_inheritance_without_prototype(tmp_path):
    result = CliRunner().invoke(app_cmd, [
        "publish-structured", "--l4-run", str(tmp_path), "--l3-root", str(tmp_path),
        "--workspace-id", WORKSPACE, "--plan", str(tmp_path / "plan.json"), "--ontology-inheritance",
    ])
    assert result.exit_code != 0
    assert "--ontology-inheritance requires --prototype-create-only" in result.output
    assert not (tmp_path / "plan.json").exists()
