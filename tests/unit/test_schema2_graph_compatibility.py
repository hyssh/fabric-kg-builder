"""Native Graph transport names must preserve complete typed source readback."""

import copy
import hashlib
import json
import re
from pathlib import Path

import pyarrow as pa
import pytest

from fabric_kg_builder.contracts.base import canonical_json
from fabric_kg_builder.deploy import schema2_prototype as prototype
from fabric_kg_builder.deploy.ontology_names import allocate_readable_names
from fabric_kg_builder.serving.graph_model import encode_parts_for_api
from fabric_kg_builder.serving.structured_publication import L5aPublicationError
from tests.unit.test_l5a_structured_publication import _inputs
from tests.unit.test_schema2_validation_stage import _subtypes


@pytest.fixture
def compiled(tmp_path):
    source = _inputs(
        tmp_path / "sealed",
        extra_type_properties={
            "semantic-type:manufacturing.record": ({
                "property_id": "property:record:canonical-id",
                "display_name": "Record ID", "value_type": "string", "required": True,
            },) + tuple({
                "property_id": f"property:record:{data_type}",
                "display_name": f"Record {data_type}",
                "value_type": data_type,
                "required": data_type == "integer",
            } for data_type in ("string", "integer", "number", "boolean", "datetime")),
        },
    )["source"]
    l3 = tmp_path / "sealed" / ".fkg" / "l3"
    next(l3.rglob("output-manifest.json")).write_text(
        canonical_json(source.input_manifest.model_dump(mode="json")) + "\n",
    )
    compilation = prototype._compile(source.root, l3, "workspace", "test")
    assert not compilation.blockers
    definition = {"parts": encode_parts_for_api(compilation.graph_parts)}
    return compilation, definition


def test_native_names_types_and_complete_typed_readback(compiled):
    compilation, definition = compiled
    proofs = {name: prototype._table_proof(table) for name, table in compilation.tables.items()}
    payloads = prototype._definition_payloads(definition)
    graph = payloads["graphType.json"]
    bindings = payloads["graphDefinition.json"]
    observed_types = set()
    for native, binding in zip(graph["nodeTypes"], bindings["nodeTables"]):
        mapped = {item["propertyName"]: item["sourceColumn"] for item in binding["propertyMappings"]}
        assert mapped[native["primaryKeyProperties"][0]] == "__canonical_id"
        assert "__label" in mapped.values()
        for prop in native["properties"]:
            assert re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", prop["name"])
            observed_types.add(prop["type"])
    assert {"STRING", "INT", "DOUBLE", "BOOLEAN", "ZONED DATETIME"}.issubset(observed_types)
    checks = prototype._graph_readback_checks(
        definition, compilation, "workspace", prototype.LAKEHOUSE_REFERENCE, companion=False,
    )
    assert sum(len(check.rows) for check in checks) == (
        compilation.graph_catalog["expected_node_count"] + compilation.graph_catalog["expected_edge_count"]
    )
    assert any(check.match.startswith("(s:") for check in checks)
    for check in checks:
        for query, expected in check.windows(1):
            assert "__canonical_id" not in query
            assert "__source_entity_id" not in query
            assert "__target_entity_id" not in query
            assert "__label" not in query
            wire = [
                {key: value.isoformat() if hasattr(value, "isoformat") else value for key, value in row.items()}
                for row in expected
            ]
            assert prototype._graph_row_fingerprint(wire, check.fields, from_wire=True) == (
                prototype._graph_row_fingerprint(expected, check.fields, from_wire=False)
            )
            if expected:
                tampered = copy.deepcopy(wire)
                tampered[0]["c0"] = "entity:wrong"
                assert prototype._graph_row_fingerprint(tampered, check.fields, from_wire=True) != (
                    prototype._graph_row_fingerprint(expected, check.fields, from_wire=False)
                )
                assert prototype._graph_row_fingerprint(wire + wire, check.fields, from_wire=True) != (
                    prototype._graph_row_fingerprint(expected, check.fields, from_wire=False)
                )
    assert {name: prototype._table_proof(table) for name, table in compilation.tables.items()} == proofs


def test_graph_type_labels_use_approved_concepts_not_l5_names(compiled):
    compilation, definition = compiled
    catalog = compilation.definitions["ontology"]["presentation_catalog"]
    expected_nodes = allocate_readable_names(catalog["entity_types"])
    actual_nodes = {
        node["canonical_type_id"]: node["graph_label"]
        for node in compilation.graph_catalog["nodes"]
    }
    assert actual_nodes == expected_nodes
    assert all(
        node["label"] != actual_nodes[node["canonical_semantic_type_id"]]
        for node in compilation.definitions["graph"]["node_types"]
    )
    graph_type = prototype._definition_payloads(definition)["graphType.json"]
    assert {item["labels"][0] for item in graph_type["nodeTypes"]} == set(expected_nodes.values())
    edges = compilation.graph_catalog["edges"]
    assert edges
    for edge in edges:
        approved = catalog["relationship_types"][edge["canonical_relationship_id"]]["display_name"]
        assert edge["graph_label"] == allocate_readable_names({"edge": {"display_name": approved}})["edge"]
    assert {item["labels"][0] for item in graph_type["edgeTypes"]} == {
        edge["graph_label"] for edge in edges
    }
    for check in prototype._graph_readback_checks(
        definition, compilation, "workspace", prototype.LAKEHOUSE_REFERENCE, companion=False,
    ):
        assert "L5_" not in check.match
        assert "L5A_" not in check.match


def test_readable_labels_preserve_every_nonlabel_field_and_all_data(tmp_path, monkeypatch):
    source = _inputs(tmp_path / "sealed")["source"]
    l3 = tmp_path / "sealed" / ".fkg" / "l3"
    next(l3.rglob("output-manifest.json")).write_text(
        canonical_json(source.input_manifest.model_dump(mode="json")) + "\n",
    )
    before = prototype._compile(source.root, l3, "workspace", "test")
    original = prototype.compile_l5a_publication

    def renamed(*args, **kwargs):
        compilation = original(*args, **kwargs)
        catalog = compilation.definitions["ontology"]["presentation_catalog"]
        for index, metadata in enumerate(catalog["entity_types"].values()):
            metadata["display_name"] = "Part-Number" if index == 0 else "part number"
        for metadata in catalog["relationship_types"].values():
            metadata["display_name"] = "Part_Number"
        return compilation

    monkeypatch.setattr(prototype, "compile_l5a_publication", renamed)
    after = prototype._compile(source.root, l3, "workspace", "test")
    before_parts, after_parts = copy.deepcopy(before.graph_parts), copy.deepcopy(after.graph_parts)
    labels = []
    for parts in (before_parts, after_parts):
        graph_type = next(part["payload_json"] for part in parts if part["path"] == "graphType.json")
        current_labels = []
        for item in graph_type["nodeTypes"] + graph_type["edgeTypes"]:
            current_labels.extend(item.pop("labels"))
        labels.append(current_labels)
    assert labels[0] != labels[1]
    assert len(set(label.casefold() for label in labels[1])) == len(labels[1])
    assert all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", label) for label in labels[1])
    assert before_parts == after_parts
    assert before.definitions["graph"] == after.definitions["graph"]
    assert {name: prototype._table_proof(table) for name, table in before.tables.items()} == {
        name: prototype._table_proof(table) for name, table in after.tables.items()
    }


def test_expanded_relationships_use_readable_endpoint_names(tmp_path):
    source = _inputs(tmp_path / "sealed", extra_types=_subtypes("manufacturing"))["source"]
    l3 = tmp_path / "sealed" / ".fkg" / "l3"
    next(l3.rglob("output-manifest.json")).write_text(
        canonical_json(source.input_manifest.model_dump(mode="json")) + "\n",
    )
    compilation = prototype._compile(source.root, l3, "workspace", "test")
    labels = {
        node["canonical_type_id"]: node["graph_label"]
        for node in compilation.graph_catalog["nodes"]
    }
    catalog = compilation.definitions["ontology"]["presentation_catalog"]
    expanded = [
        edge for edge in compilation.graph_catalog["edges"]
        if edge["physical_table_id"] != edge["source_table_id"]
    ]
    assert len(expanded) > 1
    for edge in expanded:
        approved = catalog["relationship_types"][edge["canonical_relationship_id"]]["display_name"]
        assert edge["graph_label"] == allocate_readable_names({
            "edge": {"display_name": f"{labels[edge['source_type']]} {approved} {labels[edge['target_type']]}"},
        })["edge"]
    native = {"parts": encode_parts_for_api(compilation.graph_parts)}
    checks = prototype._graph_readback_checks(
        native, compilation, "workspace", prototype.LAKEHOUSE_REFERENCE, companion=False,
    )
    assert sum(len(check.rows) for check in checks) == (
        compilation.graph_catalog["expected_node_count"] + compilation.graph_catalog["expected_edge_count"]
    )


@pytest.mark.parametrize("change", ["key", "mapping", "type", "name"])
def test_readback_rejects_changed_native_semantics(compiled, change):
    compilation, definition = compiled
    parts = copy.deepcopy(compilation.graph_parts)
    payloads = {part["path"]: part["payload_json"] for part in parts}
    node = payloads["graphType.json"]["nodeTypes"][0]
    binding = payloads["graphDefinition.json"]["nodeTables"][0]
    if change == "key":
        node["primaryKeyProperties"] = [node["properties"][1]["name"]]
    elif change == "mapping":
        binding["propertyMappings"][0]["sourceColumn"] = "__label"
    elif change == "type":
        node["properties"][0]["type"] = "INT"
    else:
        node["properties"][0]["name"] = "__canonical_id"
        binding["propertyMappings"][0]["propertyName"] = "__canonical_id"
    with pytest.raises(prototype.PrototypePublicationError):
        prototype._graph_readback_checks(
            {"parts": encode_parts_for_api(parts)}, compilation,
            "workspace", prototype.LAKEHOUSE_REFERENCE, companion=False,
        )


def test_readback_retains_exact_integer_and_boolean_types():
    fields = [pa.field("integer", pa.int64()), pa.field("boolean", pa.bool_())]
    expected = [{"c0": 2**53 + 1, "c1": True}]
    assert prototype._graph_row_fingerprint(expected, fields, from_wire=True)
    for row in ({"c0": float(2**53 + 1), "c1": True}, {"c0": 2**53 + 1, "c1": 1}):
        with pytest.raises(L5aPublicationError):
            prototype._graph_row_fingerprint([row], fields, from_wire=True)


def test_exact_first_party_schema_snapshot():
    root = Path(__file__).parents[1] / "fixtures" / "fabric_graph_schema"
    snapshot = json.loads((root / "snapshot.json").read_text())
    assert len(snapshot["documents"]) == 7
    for document in snapshot["documents"]:
        content = (root / document["file"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == document["sha256"]
        assert json.loads(content)["$id"] == document["url"]
    # Do not invent the missing service identifier regex or claim full validation.
    assert snapshot["unavailable_references"] == [{
        "url": "https://developer.microsoft.com/json-schemas/fabric/item/graphIndex/common/identifiers/1.0.0/schema.json",
        "http_status": 404,
    }]
