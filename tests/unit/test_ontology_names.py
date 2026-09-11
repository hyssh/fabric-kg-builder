"""Readable native presentation must never mutate publication identity or data."""

import base64
import copy
import json
import re

import pytest

from fabric_kg_builder.deploy.fabric_ontology_definition import compile_fabric_ontology_definition
from fabric_kg_builder.deploy.ontology_names import (
    NATIVE_NAME_PATTERN,
    allocate_readable_names,
    readable_catalog_from_domain,
    repair_ontology_presentation,
    validate_presentation_only,
)


def _fixtures():
    type_id, prop_id, rel_id = (
        "semantic-type:ws_" + "a" * 64,
        "property:ws_" + "b" * 64,
        "relationship-type:ws_" + "c" * 64,
    )
    domain = {"candidate_model": {
        "entity_types": [{
            "type_id": type_id, "display_name": "Battery Screw",
            "description": "A screw securing the battery.", "aliases": ["Battery fastener"],
            "declared_properties": [{"property_id": prop_id, "display_name": "Part Number"}],
        }],
        "relationship_types": [{
            "relationship_type_id": rel_id, "display_name": "Secures Battery",
            "description": "The fastener secures a battery.",
        }],
    }}
    ontology = {
        "scope_notice": "LIMITED PREFIX 302/1425; NOT full coverage",
        "entity_types": [{
            "id": "1000001", "canonical_semantic_type_id": type_id,
            "physical_table_id": "physical_ws_a", "physical_identity_column": "__canonical_id",
            "properties": [{
                "id": "2000001", "canonical_property_id": prop_id,
                "physical_column_id": "physical_ws_b",
            }],
        }],
        "relationship_types": [{
            "id": "3000001", "canonical_semantic_relationship_id": rel_id,
            "physical_table_id": "physical_ws_c", "allowed_source_semantic_type_ids": [type_id],
            "allowed_target_semantic_type_ids": [type_id],
            "source_identity_column": "__source_entity_id", "target_identity_column": "__target_entity_id",
        }],
    }
    return ontology, readable_catalog_from_domain(domain)


def _compile(ontology, **kwargs):
    return compile_fabric_ontology_definition(
        ontology, workspace_id="workspace", lakehouse_id="lakehouse", lakehouse="dbo",
        display_name="Test", description=ontology["scope_notice"], **kwargs,
    )


def _decode(parts):
    return {part["path"]: json.loads(base64.b64decode(part["payload"])) for part in parts}


def _replace(parts, path, edit):
    result = copy.deepcopy(list(parts))
    part = next(part for part in result if part["path"] == path)
    payload = json.loads(base64.b64decode(part["payload"]))
    edit(payload)
    part["payload"] = base64.b64encode(json.dumps(payload).encode()).decode()
    return result


def test_names_are_bounded_collision_safe_and_order_independent():
    labels = ["Shield", "shield", "Shield!", "12 mm Screw", "a" * 160, "a" * 159 + "b", "id"]
    metadata = {str(index): {"display_name": label} for index, label in enumerate(labels)}
    names = allocate_readable_names(metadata, reserved=["id"])
    assert names == allocate_readable_names(dict(reversed(list(metadata.items()))), reserved=["id"])
    assert len({name.casefold() for name in names.values()}) == len(names)
    assert all(re.fullmatch(NATIVE_NAME_PATTERN, name) for name in names.values())
    assert names["3"] == "Type_12_mm_Screw"
    assert names["0"].startswith("Shield_")
    assert names["6"].startswith("id_")


@pytest.mark.parametrize("label,expected", [
    ("local_environmental_or_e-waste_laws_and_guidelines",
     "local_environmental_or_e_waste_laws_and_guidelines"),
    ("3IP (Torx-Plus) driver", "Type_3IP_Torx_Plus_driver"),
    ("ESD-Safe mat", "ESD_Safe_mat"),
    ("USB-C connector", "USB_C_connector"),
])
def test_names_obey_stricter_graph_label_rule(label, expected):
    assert allocate_readable_names({"type:source": {"display_name": label}}) == {"type:source": expected}
    assert re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", expected)


def test_hyphen_normalization_collisions_remain_distinct_and_stable():
    catalog = {
        "a": {"display_name": "E-waste"},
        "b": {"display_name": "E_waste"},
        "c": {"display_name": "e waste"},
    }
    names = allocate_readable_names(catalog)
    assert names == allocate_readable_names(dict(reversed(list(catalog.items()))))
    assert len({name.casefold() for name in names.values()}) == 3
    assert all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", name) for name in names.values())
    assert all(name.lower().startswith("e_waste_") for name in names.values())


@pytest.mark.parametrize("kind", ["entity", "property", "relationship"])
def test_presentation_validation_rejects_graph_unsafe_hyphens(kind):
    ontology, catalog = _fixtures()
    parts = _compile(ontology, catalog=catalog).parts
    path = ("RelationshipTypes/3000001/definition.json"
            if kind == "relationship" else "EntityTypes/1000001/definition.json")
    def edit(value):
        target = value["properties"][2] if kind == "property" else value
        target["name"] = "invalid-name"
    changed = _replace(parts, path, edit)
    with pytest.raises(ValueError, match="Invalid or colliding"):
        validate_presentation_only(parts, changed)


@pytest.mark.parametrize("label", ["", "   ", "!!!", "电池", "ws_" + "f" * 64])
def test_missing_or_opaque_labels_fail_explicitly(label):
    with pytest.raises(ValueError):
        allocate_readable_names({"canonical-id": {"display_name": label}})


def test_only_approved_ascii_alias_can_replace_unsupported_label():
    names = allocate_readable_names({"battery": {"display_name": "电池", "aliases": ["Battery"]}})
    assert names == {"battery": "Battery"}


def test_compiler_defaults_to_approved_catalog_not_canonical_fallback():
    ontology, catalog = _fixtures()
    with pytest.raises(ValueError, match="presentation_catalog"):
        _compile(ontology)
    ontology["presentation_catalog"] = catalog
    compilation = _compile(ontology)
    native = _decode(compilation.parts)
    assert native["EntityTypes/1000001/definition.json"]["name"] == "Battery_Screw"
    assert native["EntityTypes/1000001/definition.json"]["semanticEnrichment"]["synonyms"] == [
        "Battery Screw", "Battery fastener",
    ]
    assert compilation.presentation_mapping


def test_readable_compilation_disambiguates_colliding_legacy_property_spellings():
    ontology, catalog = _fixtures()
    entity = ontology["entity_types"][0]
    entity["properties"] = [
        {"id": str(2000001 + index), "canonical_property_id": canonical, "physical_column_id": f"column_{index}"}
        for index, canonical in enumerate(("property:part-number", "property:part_number"))
    ]
    catalog["properties"] = {
        prop["canonical_property_id"]: {"display_name": "Part Number"}
        for prop in entity["properties"]
    }
    parts = _compile(ontology, catalog=catalog).parts
    properties = _decode(parts)["EntityTypes/1000001/definition.json"]["properties"][2:]
    assert len({prop["name"].casefold() for prop in properties}) == 2
    assert all(prop["name"].startswith("Part_Number_") for prop in properties)


def test_repair_retains_every_binding_identity_path_scope_and_source_object():
    ontology, catalog = _fixtures()
    original = _compile(ontology, legacy_names=True).parts
    original = _replace(original, "EntityTypes/1000001/definition.json", lambda item: item.update({
        "baseEntityTypeId": "1000000", "scope_notice": ontology["scope_notice"],
        "semanticEnrichment": {
            "synonyms": ["Existing synonym"],
            "customAttributes": {"scope_notice": ontology["scope_notice"]},
        },
    }))
    original_copy, source_copy = copy.deepcopy(original), copy.deepcopy(ontology)
    repair = repair_ontology_presentation(original, l5a_ontology=ontology, catalog=catalog)
    validate_presentation_only(original, repair.parts)
    assert original == original_copy and ontology == source_copy
    before, after = _decode(original), _decode(repair.parts)
    entity = after["EntityTypes/1000001/definition.json"]
    assert entity["name"] == "Battery_Screw"
    assert entity["entityIdParts"] == ["91000001"]
    assert entity["displayNamePropertyId"] == "81000001"
    assert entity["baseEntityTypeId"] == "1000000"
    assert entity["scope_notice"] == ontology["scope_notice"]
    assert entity["semanticEnrichment"]["description"] == "A screw securing the battery."
    assert entity["semanticEnrichment"]["synonyms"] == ["Existing synonym"]
    assert entity["semanticEnrichment"]["customAttributes"]["scope_notice"] == ontology["scope_notice"]
    prop = next(prop for prop in entity["properties"] if prop["id"] == "2000001")
    assert prop["name"] == "Part_Number"
    assert "semanticEnrichment" not in prop
    assert after["RelationshipTypes/3000001/definition.json"]["name"] == "Secures_Battery"
    assert after[".platform"] == before[".platform"]
    for old, new in zip(original, repair.parts):
        if "/DataBindings/" in old["path"] or "/Contextualizations/" in old["path"]:
            assert old == new
    assert repair_ontology_presentation(
        repair.parts, l5a_ontology=ontology, catalog=catalog,
    ).parts == repair.parts
    assert len([row for row in repair.mapping_report if row["kind"] == "property"]) == 1


def test_no_bound_label_keeps_identity_display_and_adds_no_property():
    ontology, catalog = _fixtures()
    parts = list(_compile(ontology, legacy_names=True).parts)
    binding = next(part["path"] for part in parts if part["path"].startswith("EntityTypes/1000001/DataBindings/"))
    parts = _replace(parts, binding, lambda item: item["dataBindingConfiguration"].update({
        "propertyBindings": [
            binding for binding in item["dataBindingConfiguration"]["propertyBindings"]
            if binding["sourceColumnName"] != "__label"
        ],
    }))
    repair = repair_ontology_presentation(parts, l5a_ontology=ontology, catalog=catalog)
    assert _decode(repair.parts)["EntityTypes/1000001/definition.json"]["displayNamePropertyId"] == "91000001"


def test_bound_display_label_is_selected_by_binding_not_property_spelling():
    ontology, catalog = _fixtures()
    parts = _compile(ontology, legacy_names=True).parts

    def rename_structural_properties(item):
        for prop in item["properties"]:
            if prop["id"] == "81000001":
                prop["name"] = "Part_Number"
            elif prop["id"] == "91000001":
                prop["name"] = "Canonical_Identity"

    parts = _replace(parts, "EntityTypes/1000001/definition.json", rename_structural_properties)
    repair = repair_ontology_presentation(parts, l5a_ontology=ontology, catalog=catalog)
    entity = _decode(repair.parts)["EntityTypes/1000001/definition.json"]
    assert entity["displayNamePropertyId"] == "81000001"
    assert entity["entityIdParts"] == ["91000001"]
    prop = next(prop for prop in entity["properties"] if prop["id"] == "2000001")
    assert prop["name"].startswith("Part_Number_")


@pytest.mark.parametrize("field,value", [
    ("id", "999"), ("entityIdParts", ["81000001"]),
    ("baseEntityTypeId", "999"), ("namespace", "other"), ("scope_notice", "full coverage"),
])
def test_guard_rejects_nonpresentation_definition_changes(field, value):
    ontology, catalog = _fixtures()
    parts = _compile(ontology, catalog=catalog).parts
    changed = _replace(parts, "EntityTypes/1000001/definition.json", lambda item: item.update({field: value}))
    with pytest.raises(ValueError):
        validate_presentation_only(parts, changed)


def test_guard_rejects_binding_reserialization_and_missing_catalog_entry():
    ontology, catalog = _fixtures()
    parts = _compile(ontology, legacy_names=True).parts
    binding = next(part["path"] for part in parts if "/DataBindings/" in part["path"])
    changed = _replace(parts, binding, lambda item: None)
    with pytest.raises(ValueError, match="non-definition"):
        validate_presentation_only(parts, changed)
    catalog["properties"] = {}
    with pytest.raises(ValueError, match="catalog missing"):
        repair_ontology_presentation(parts, l5a_ontology=ontology, catalog=catalog)


@pytest.mark.parametrize("field,value", [
    ("synonyms", ["New synonym"]),
    ("customAttributes", {"scope_notice": "changed"}),
])
def test_live_guard_rejects_synonym_and_custom_attribute_changes(field, value):
    ontology, _catalog = _fixtures()
    parts = _compile(ontology, legacy_names=True).parts
    changed = _replace(
        parts, "EntityTypes/1000001/definition.json",
        lambda item: item["semanticEnrichment"].update({field: value}),
    )
    with pytest.raises(ValueError, match="identities/bindings/schema"):
        validate_presentation_only(parts, changed)


def test_companion_reader_uses_actual_native_names_not_new_compiler_guesses(monkeypatch):
    from types import SimpleNamespace

    import pyarrow as pa

    from fabric_kg_builder.deploy import schema2_prototype as publication
    from fabric_kg_builder.deploy.fabric_ontology_definition import _part

    ontology, _catalog = _fixtures()
    ontology["relationship_types"] = []
    parts = _compile(ontology, legacy_names=True).parts
    parts = _replace(parts, "EntityTypes/1000001/definition.json", lambda item: item.update({
        "name": "Existing_Human_Name",
    }))
    native = _decode(parts)["EntityTypes/1000001/definition.json"]
    names = [prop["name"] for prop in native["properties"]]
    columns = ["__canonical_id", "__label", "physical_ws_b"]
    mappings = dict(zip(names, columns))
    graph = {"parts": [
        _part("dataSources.json", {
            "itemReferences": [{"name": "source", "item": {"workspaceId": "workspace", "itemId": "lakehouse"}}],
            "dataSources": [{"name": "table", "type": "DeltaTable", "properties": {
                "referenceName": "source", "path": "Tables/dbo/physical_ws_a",
            }}],
        }),
        _part("graphType.json", {
            "nodeTypes": [{
                "alias": "node", "labels": ["Existing_Human_Name"], "primaryKeyProperties": [names[0]],
                "properties": [{"name": name, "type": "STRING"} for name in names],
            }], "edgeTypes": [],
        }),
        _part("graphDefinition.json", {
            "nodeTables": [{
                "nodeTypeAlias": "node", "dataSourceName": "table",
                "propertyMappings": [
                    {"propertyName": name, "sourceColumn": column} for name, column in mappings.items()
                ],
            }], "edgeTables": [],
        }),
    ]}
    compilation = SimpleNamespace(
        definitions={"ontology": ontology},
        tables={"physical_ws_a": pa.table({column: ["value"] for column in columns})},
    )

    def no_recompile(*args, **kwargs):
        raise AssertionError("An existing ontology must not be renamed by a reader")

    monkeypatch.setattr(publication, "_ontology_parts", no_recompile)
    checks = publication._graph_readback_checks(
        graph, compilation, "workspace", "lakehouse", companion=True,
        native_ontology={"parts": parts},
    )
    assert len(checks) == 1
