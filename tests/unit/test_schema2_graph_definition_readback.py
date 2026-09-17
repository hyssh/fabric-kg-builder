"""Service serialization defaults are not permission to ignore native drift."""

import copy

import pytest

from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.deploy import schema2_prototype as p
from fabric_kg_builder.serving.graph_model import encode_parts_for_api
from tests.unit.test_schema2_graph_compatibility import compiled  # noqa: F401
from tests.unit.test_schema2_prototype_agent import _Backend, published  # noqa: F401


SCHEMA_ROOT = "https://developer.microsoft.com/json-schemas/fabric/item/graphIndex/definition"


def test_semantic_model_terminal_newlines_preserve_raw_definitions():
    expected = {"definition/model.tmdl": "model Model\n\tculture: en-US\n"}
    actual = {"definition/model.tmdl": expected["definition/model.tmdl"] + "\n"}
    before = copy.deepcopy((expected, actual))
    assert p._native_definition_payloads_equal("semantic_model", expected, actual)
    assert p._native_definition_payloads_equal("semantic_model", actual, expected)
    assert (expected, actual) == before
    assert canonical_sha256(expected) != canonical_sha256(actual)
    for kind in ("ontology", "graph"):
        assert not p._native_definition_payloads_equal(kind, expected, actual)


@pytest.mark.parametrize("actual", [
    {"definition/model.tmdl": "model Model\n\tculture: ko-KR\n"},
    {"definition/model.tmdl": "model Model\nculture: en-US\n"},
    {"definition/model.tmdl": "model Model\n\n\tculture: en-US\n"},
    {"definition/model.tmdl": "model Model\n\tculture: en-US \n"},
    {"definition/model.tmdl": "model Model\n\tculture: en-US\n", "extra.tmdl": ""},
    {},
])
def test_semantic_model_newline_comparison_rejects_other_changes(actual):
    expected = {"definition/model.tmdl": "model Model\n\tculture: en-US\n"}
    assert not p._native_definition_payloads_equal("semantic_model", expected, actual)


def test_semantic_model_non_tmdl_text_remains_exact():
    assert not p._native_definition_payloads_equal(
        "semantic_model", {"unknown.txt": "value\n"}, {"unknown.txt": "value\n\n"},
    )


def _service_defaults(payloads):
    result = copy.deepcopy(payloads)
    result["graphSettings.json"] = {
        "$schema": f"{SCHEMA_ROOT}/graphSettings/1.0.0/schema.json",
    }
    for edge in result["graphDefinition.json"]["edgeTables"]:
        edge["edgeIdMapping"] = None
    styling = result["stylingConfiguration.json"]
    styling["visualFormat"] = None
    layout = styling["modelLayout"]
    for point in [layout["pan"], *layout["positions"].values()]:
        for axis in ("x", "y"):
            point[axis] = float(point[axis])
    layout["zoomLevel"] = float(layout["zoomLevel"])
    return result


def _encoded(payloads):
    return {"parts": encode_parts_for_api([
        {"path": name, "payload_json": value} for name, value in payloads.items()
    ])}


def test_observed_defaults_preserve_raw_evidence_and_exact_readback(compiled):
    compilation, definition = compiled
    expected = p._definition_payloads(definition)
    actual = _service_defaults(expected)
    before = copy.deepcopy((expected, actual))
    assert p._native_definition_payloads_equal("graph", expected, actual)
    assert p._native_definition_payloads_equal("graph", actual, expected)
    assert canonical_sha256(expected) != canonical_sha256(actual)
    assert (expected, actual) == before
    original = p._graph_readback_checks(
        definition, compilation, "workspace", p.LAKEHOUSE_REFERENCE, companion=False,
    )
    observed = p._graph_readback_checks(
        _encoded(actual), compilation, "workspace", p.LAKEHOUSE_REFERENCE, companion=False,
    )
    assert [list(check.windows(1)) for check in observed] == [
        list(check.windows(1)) for check in original
    ]


@pytest.mark.parametrize("mutation", [
    "mapping", "source", "node-key", "label", "type", "endpoint-type", "endpoint-key",
    "edge-id", "empty-edge-id", "format", "empty-format", "settings", "settings-version",
    "extra-part", "missing-part", "unknown-null", "position", "zoom-bool", "style-size",
    "missing-node",
])
def test_native_comparison_rejects_meaningful_or_unknown_drift(compiled, mutation):
    _, definition = compiled
    expected = p._definition_payloads(definition)
    actual = _service_defaults(expected)
    graph = actual["graphType.json"]
    bindings = actual["graphDefinition.json"]
    styling = actual["stylingConfiguration.json"]
    if mutation == "mapping":
        bindings["nodeTables"][0]["propertyMappings"][0]["sourceColumn"] = "__label"
    elif mutation == "source":
        actual["dataSources.json"]["dataSources"][0]["properties"]["path"] += "_other"
    elif mutation == "node-key":
        graph["nodeTypes"][0]["primaryKeyProperties"] = ["different"]
    elif mutation == "label":
        graph["nodeTypes"][0]["labels"] = ["Different"]
    elif mutation == "type":
        graph["nodeTypes"][0]["properties"][0]["type"] = "INT"
    elif mutation == "endpoint-type":
        graph["edgeTypes"][0]["destinationNodeType"]["alias"] = "Different"
    elif mutation == "endpoint-key":
        bindings["edgeTables"][0]["destinationNodeKeyColumns"] = ["__label"]
    elif mutation in ("edge-id", "empty-edge-id"):
        bindings["edgeTables"][0]["edgeIdMapping"] = ["different"] if mutation == "edge-id" else []
    elif mutation in ("format", "empty-format"):
        styling["visualFormat"] = {"different": {}} if mutation == "format" else {}
    elif mutation == "settings":
        actual["graphSettings.json"]["different"] = None
    elif mutation == "settings-version":
        actual["graphSettings.json"]["$schema"] = f"{SCHEMA_ROOT}/graphSettings/2.0.0/schema.json"
    elif mutation == "extra-part":
        actual["unknown.json"] = {}
    elif mutation == "missing-part":
        del actual["dataSources.json"]
    elif mutation == "unknown-null":
        styling["scenario"] = None
    elif mutation == "position":
        styling["modelLayout"]["pan"]["x"] = 0.5
    elif mutation == "zoom-bool":
        styling["modelLayout"]["zoomLevel"] = True
    elif mutation == "style-size":
        next(iter(styling["modelLayout"]["styles"].values()))["size"] += 1
    else:
        graph["nodeTypes"].pop()
    assert not p._native_definition_payloads_equal("graph", expected, actual)


def test_graph_defaults_do_not_apply_to_other_kinds(compiled):
    _, definition = compiled
    expected = p._definition_payloads(definition)
    actual = _service_defaults(expected)
    for kind in ("ontology", "semantic_model"):
        assert not p._native_definition_payloads_equal(kind, expected, actual)


def test_publisher_verifies_service_defaults_and_keeps_raw_hashes(monkeypatch, request):
    original = _Backend.request

    def get_definition(self, method, url, **kwargs):
        response = original(self, method, url, **kwargs)
        if method == "POST" and url.endswith("/getDefinition"):
            definition = response.body["definition"]
            payloads = p._definition_payloads(definition)
            if "graphType.json" in payloads:
                response.body["definition"] = _encoded(_service_defaults(payloads))
        return response

    monkeypatch.setattr(_Backend, "request", get_definition)
    case = request.getfixturevalue("published")
    journal = p._read_json(case.kwargs["prototype_journal"])
    action = journal["actions"]["create:graph"]
    expected = p._definition_payloads(case.backend.definitions[action["item_id"]])
    actual = _service_defaults(expected)
    assert action["definition_readback_hash"] == canonical_sha256(actual)
    assert action["definition_readback_hash"] != canonical_sha256(expected)
