"""Tests for deploy/data_agent.py — build_semantic_data_agent_spec helpers
and AgentPublicationReceipt property count fields (issues #14, #12).

Covers (original):
- _graph_type, _node_elements, _edge_elements, _edge_triples
- build_semantic_data_agent_spec: spec structure, source types, element types

Covers (new #14/#12):
- AgentPublicationReceipt: required/compiled/draft/published property counts
- AgentPublicationReceipt: compiled/published property selection hashes
- AgentPublicationReceipt: global_instruction_chars, instruction_chars, description_chars
- Backward compat: zero agent-visible properties by contract → required_count=0, no false-fail
"""
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
from fabric_kg_builder.knowledge.data_agent import (
    DataAgentLroFailedError,
    FabricDataAgentClient,
)
from fabric_kg_builder.knowledge.transport import FakeTransport, HttpResponse


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


# ===========================================================================
# Issues #14, #12 — AgentPublicationReceipt property count fields
# (pre-implementation tests — fail RED until Verbal adds the new schema fields)
# ===========================================================================

_H = "sha256:" + "a" * 64
_H2 = "sha256:" + "b" * 64


def _base_receipt_kwargs() -> dict:
    """Minimal valid kwargs for AgentPublicationReceipt including planned #14/#12 fields."""
    return dict(
        semantic_model_manifest_hash=_H,
        persisted_projection_receipt_hash=_H,
        ontology_persisted_projection_hash=_H,
        graph_persisted_projection_hash=_H,
        workspace_name="ws-name",
        workspace_id="ws-001",
        data_agent_name="My Agent",
        data_agent_item_id="item-001",
        target_mode="create",
        actions=["create", "publish"],
        selected_sources=[{
            "source_type": "graph",
            "source_name": "g",
            "workspace_id": "ws-001",
            "artifact_id": "art-001",
            "selected_element_count": 3,
            "property_child_count": 2,
        }],
        package_instruction_hash=_H,
        compiled_instruction_hash=_H,
        draft_instruction_hash=_H,
        published_instruction_hash=_H,
        compiled_source_selection_hash=_H,
        draft_source_selection_hash=_H,
        published_source_selection_hash=_H,
        compiled_selected_element_hash=_H,
        published_selected_element_hash=_H,
        agent_schema_sidecar_hash=_H,
        property_child_coverage=1.0,
        publication_status="published",
        validated_at_utc="2026-07-23T12:00:00Z",
        # New #14 property count fields:
        required_property_count=8,
        compiled_property_count=8,
        draft_property_count=8,
        published_property_count=8,
        compiled_property_selection_hash=_H2,
        published_property_selection_hash=_H2,
        # New #12 grounding text count fields:
        global_instruction_chars=1420,
        instruction_chars={"graph": 800, "ontology": 500},
        description_chars={"graph": 165, "ontology": 180},
    )


class TestPublicationReceiptPropertyCountFields:
    """AgentPublicationReceipt must carry required/compiled/draft/published property counts
    and property selection hashes for #14 three-way comparison and dry-run reporting.

    Tests fail with pydantic ValidationError (extra="forbid") until Verbal adds the fields.
    """

    def test_receipt_accepts_property_count_fields(self):
        """Receipt must accept and store the four property count fields."""
        from fabric_kg_builder.semantic.schemas import AgentPublicationReceipt
        receipt = AgentPublicationReceipt.model_validate(_base_receipt_kwargs())
        assert receipt.required_property_count == 8
        assert receipt.compiled_property_count == 8
        assert receipt.draft_property_count == 8
        assert receipt.published_property_count == 8

    def test_receipt_accepts_property_selection_hashes(self):
        """Receipt must accept compiled_ and published_property_selection_hash fields."""
        from fabric_kg_builder.semantic.schemas import AgentPublicationReceipt
        receipt = AgentPublicationReceipt.model_validate(_base_receipt_kwargs())
        assert receipt.compiled_property_selection_hash.startswith("sha256:")
        assert receipt.published_property_selection_hash.startswith("sha256:")

    def test_receipt_accepts_grounding_text_char_counts(self):
        """Receipt must accept global_instruction_chars, instruction_chars, description_chars."""
        from fabric_kg_builder.semantic.schemas import AgentPublicationReceipt
        receipt = AgentPublicationReceipt.model_validate(_base_receipt_kwargs())
        assert receipt.global_instruction_chars == 1420
        assert receipt.instruction_chars["graph"] == 800
        assert receipt.description_chars["ontology"] == 180

    def test_zero_agent_visible_properties_required_count_is_zero(self):
        """When no agent-visible properties exist in the contract (required=0),
        the check trivially passes — must not raise or false-fail."""
        from fabric_kg_builder.semantic.schemas import AgentPublicationReceipt
        kwargs = _base_receipt_kwargs()
        kwargs["required_property_count"] = 0
        kwargs["compiled_property_count"] = 0
        kwargs["draft_property_count"] = 0
        kwargs["published_property_count"] = 0
        # Must not raise — zero properties is a valid backward-compat state
        receipt = AgentPublicationReceipt.model_validate(kwargs)
        assert receipt.required_property_count == 0
        assert receipt.published_property_count == 0


class TestDataAgentLroDiagnostics:
    def test_failed_lro_preserves_operation_and_request_details(
        self, monkeypatch
    ):
        transport = FakeTransport().register(
            "GET",
            "/operations/op-123",
            HttpResponse(
                status_code=200,
                headers={"x-ms-request-id": "req-456"},
                body={
                    "status": "Failed",
                    "error": {
                        "errorCode": "UnknownError",
                        "message": "An error occurred while processing the operation",
                    },
                },
            ),
        )
        client = FabricDataAgentClient(
            workspace_id="ws-001",
            transport=transport,
            token="test-token",
            lro_poll_interval=1,
        )
        monkeypatch.setattr("time.sleep", lambda _: None)

        with pytest.raises(DataAgentLroFailedError) as exc_info:
            client._poll_lro(
                "https://api.fabric.microsoft.com/v1/operations/op-123",
                1,
            )

        error = exc_info.value
        assert error.operation_url.endswith("/operations/op-123")
        assert error.request_id == "req-456"
        assert error.body["error"]["errorCode"] == "UnknownError"
        assert "UnknownError" in str(error)

    def test_failed_create_lro_deletes_created_shell(self, monkeypatch):
        transport = (
            FakeTransport()
            .register(
                "POST",
                "/dataAgents",
                HttpResponse(
                    status_code=202,
                    headers={"Location": "https://fabric/operations/op-123"},
                    body={},
                ),
            )
            .register(
                "GET",
                "/operations/op-123",
                HttpResponse(
                    status_code=200,
                    headers={"x-ms-request-id": "req-456"},
                    body={
                        "status": "Failed",
                        "error": {
                            "errorCode": "UnknownError",
                            "message": "definition rejected",
                        },
                    },
                ),
            )
            .register(
                "GET",
                "/items",
                HttpResponse(
                    status_code=200,
                    body={
                        "value": [
                            {
                                "id": "agent-shell",
                                "displayName": "Agent",
                                "type": "DataAgent",
                            }
                        ]
                    },
                ),
            )
            .register(
                "DELETE",
                "/dataAgents/agent-shell",
                HttpResponse(status_code=204, body={}),
            )
        )
        client = FabricDataAgentClient(
            workspace_id="ws-001",
            transport=transport,
            token="test-token",
            lro_poll_interval=1,
        )
        monkeypatch.setattr("time.sleep", lambda _: None)

        with pytest.raises(DataAgentLroFailedError):
            client._create(
                DataAgentSpec(
                    display_name="Agent",
                    instruction="Use grounded sources.",
                )
            )

        assert any(
            call.method == "DELETE" and call.url.endswith("/dataAgents/agent-shell")
            for call in transport.calls
        )
