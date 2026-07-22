"""Tests for deploy/data_agent.py — build_semantic_data_agent_spec helpers."""
from __future__ import annotations

import pytest

from fabric_kg_builder.deploy.data_agent import (
    _edge_elements,
    _edge_triples,
    _node_elements,
    _graph_type,
    build_semantic_data_agent_spec,
)
from fabric_kg_builder.knowledge.data_agent import DataAgentSpec


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _sample_graph_type() -> dict:
    return {
        "nodeTypes": [
            {"alias": "alias_eq", "labels": ["Equipment"]},
            {"alias": "alias_fa", "labels": ["Facility"]},
            {"alias": "alias_loc", "labels": ["Location"]},
        ],
        "edgeTypes": [
            {
                "alias": "e_located",
                "labels": ["located_at"],
                "sourceNodeType": {"alias": "alias_fa"},
                "destinationNodeType": {"alias": "alias_loc"},
            },
            {
                "alias": "e_contains",
                "labels": ["contains_equipment"],
                "sourceNodeType": {"alias": "alias_loc"},
                "destinationNodeType": {"alias": "alias_eq"},
            },
        ],
    }


def _sample_parts() -> list[dict]:
    import json
    graph_type = _sample_graph_type()
    return [
        {
            "path": "graphType.json",
            "payload_json": graph_type,
        }
    ]


def _make_ontology_plan():
    from unittest.mock import MagicMock
    plan = MagicMock()
    plan.entity_types = [
        MagicMock(type_name="Equipment", count=50),
        MagicMock(type_name="Facility", count=20),
    ]
    plan.relationship_pairs = []
    return plan


# ---------------------------------------------------------------------------
# _graph_type
# ---------------------------------------------------------------------------

class TestGraphType:
    def test_extracts_graph_type(self):
        parts = _sample_parts()
        gt = _graph_type(parts)
        assert "nodeTypes" in gt
        assert len(gt["nodeTypes"]) == 3

    def test_raises_if_missing(self):
        with pytest.raises(ValueError, match="graphType.json"):
            _graph_type([{"path": "other.json", "payload_json": {}}])

    def test_raises_if_empty_parts(self):
        with pytest.raises(ValueError):
            _graph_type([])


# ---------------------------------------------------------------------------
# _node_elements
# ---------------------------------------------------------------------------

class TestNodeElements:
    def test_returns_one_element_per_node(self):
        gt = _sample_graph_type()
        elements = _node_elements(gt)
        assert len(elements) == 3

    def test_element_type(self):
        gt = _sample_graph_type()
        elements = _node_elements(gt)
        for el in elements:
            assert el.type == "graph.nodeType"

    def test_element_is_selected(self):
        gt = _sample_graph_type()
        elements = _node_elements(gt)
        assert all(el.is_selected for el in elements)

    def test_display_name_matches_label(self):
        gt = _sample_graph_type()
        elements = _node_elements(gt)
        names = {el.display_name for el in elements}
        assert "Equipment" in names
        assert "Facility" in names

    def test_known_entity_has_description(self):
        gt = _sample_graph_type()
        elements = _node_elements(gt)
        equipment_el = next(e for e in elements if e.display_name == "Equipment")
        assert len(equipment_el.description) > 10

    def test_empty_graph_type(self):
        assert _node_elements({"nodeTypes": []}) == []


# ---------------------------------------------------------------------------
# _edge_elements
# ---------------------------------------------------------------------------

class TestEdgeElements:
    def test_returns_one_element_per_edge(self):
        gt = _sample_graph_type()
        elements = _edge_elements(gt)
        assert len(elements) == 2

    def test_element_type(self):
        gt = _sample_graph_type()
        for el in _edge_elements(gt):
            assert el.type == "graph.edgeType"

    def test_display_name_matches_edge_label(self):
        gt = _sample_graph_type()
        names = {el.display_name for el in _edge_elements(gt)}
        assert "located_at" in names
        assert "contains_equipment" in names

    def test_empty_graph_type(self):
        assert _edge_elements({"nodeTypes": [], "edgeTypes": []}) == []


# ---------------------------------------------------------------------------
# _edge_triples
# ---------------------------------------------------------------------------

class TestEdgeTriples:
    def test_returns_triples(self):
        gt = _sample_graph_type()
        triples = _edge_triples(gt)
        assert len(triples) == 2

    def test_triple_structure(self):
        gt = _sample_graph_type()
        triples = _edge_triples(gt)
        sources = {t[0] for t in triples}
        labels = {t[1] for t in triples}
        targets = {t[2] for t in triples}
        assert "Facility" in sources
        assert "located_at" in labels
        assert "Location" in targets


# ---------------------------------------------------------------------------
# build_semantic_data_agent_spec
# ---------------------------------------------------------------------------

class TestBuildSemanticDataAgentSpec:
    def test_returns_data_agent_spec(self):
        plan = _make_ontology_plan()
        spec = build_semantic_data_agent_spec(
            display_name="Test Agent",
            workspace_id="ws-001",
            ontology_id="ont-001",
            ontology_name="MyOntology",
            graph_model_id="gm-001",
            graph_model_name="MyGraph",
            ontology_plan=plan,
            graph_parts=_sample_parts(),
        )
        assert isinstance(spec, DataAgentSpec)

    def test_display_name_preserved(self):
        plan = _make_ontology_plan()
        spec = build_semantic_data_agent_spec(
            display_name="My Agent",
            workspace_id="ws-001",
            ontology_id="ont-001",
            ontology_name="MyOntology",
            graph_model_id="gm-001",
            graph_model_name="MyGraph",
            ontology_plan=plan,
            graph_parts=_sample_parts(),
        )
        assert spec.display_name == "My Agent"

    def test_two_sources(self):
        plan = _make_ontology_plan()
        spec = build_semantic_data_agent_spec(
            display_name="Agent",
            workspace_id="ws-001",
            ontology_id="ont-001",
            ontology_name="Ont",
            graph_model_id="gm-001",
            graph_model_name="Graph",
            ontology_plan=plan,
            graph_parts=_sample_parts(),
        )
        assert len(spec.sources) == 2
        source_types = {s.source_type for s in spec.sources}
        assert "ontology" in source_types
        assert "graph" in source_types

    def test_instruction_contains_entity_labels(self):
        plan = _make_ontology_plan()
        spec = build_semantic_data_agent_spec(
            display_name="Agent",
            workspace_id="ws-001",
            ontology_id="ont-001",
            ontology_name="Ont",
            graph_model_id="gm-001",
            graph_model_name="Graph",
            ontology_plan=plan,
            graph_parts=_sample_parts(),
        )
        assert "Equipment" in spec.instruction
        assert "Facility" in spec.instruction

    def test_graph_source_has_node_and_edge_elements(self):
        plan = _make_ontology_plan()
        spec = build_semantic_data_agent_spec(
            display_name="Agent",
            workspace_id="ws-001",
            ontology_id="ont-001",
            ontology_name="Ont",
            graph_model_id="gm-001",
            graph_model_name="Graph",
            ontology_plan=plan,
            graph_parts=_sample_parts(),
        )
        graph_source = next(s for s in spec.sources if s.source_type == "graph")
        assert len(graph_source.elements) > 0
        element_types = {el.type for el in graph_source.elements}
        assert "graph.nodeType" in element_types
        assert "graph.edgeType" in element_types
