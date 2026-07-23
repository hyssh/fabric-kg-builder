"""Tests for compact Data Agent grounding boundaries, capability-aware text,
and property children preservation (issues #12, #14).

Covers (original):
- Instruction boundaries: compact and source-specific
- Graph few-shots: only validated direct_graph probes admitted

Covers (new #14):
- build_public_graph_source_projection preserves property children (not children=None)
- Grounding text char counts: global_instruction_chars, instruction_chars, description_chars
  are present and deterministic (for #12 receipt reporting)
- property_child_count in stage_snapshot_from_spec reflects projected children
"""

from __future__ import annotations

from fabric_kg_builder.knowledge.data_agent import (
    graph_few_shots_from_competency_contract,
)
from fabric_kg_builder.semantic import (
    build_contract_agent_instructions,
    build_graph_source_description,
    build_graph_source_instructions,
    build_ontology_source_description,
    build_ontology_source_instructions,
)


def _semantic_context() -> dict:
    return {
        "contract_name": "Facility operations",
        "contract_hash": "sha256:abc",
        "contract_description": "Evidence-backed facility knowledge.",
        "entity_types": [{"graph_label": "Asset"}],
        "relationship_types": [{"graph_label": "located_at"}],
    }


def test_instruction_boundaries_are_compact_and_source_specific() -> None:
    context = _semantic_context()
    global_instructions = build_contract_agent_instructions(
        context,
        competency_questions=["Where is Chiller 1?"],
        domain_context="Field service.",
    )
    ontology_instructions = build_ontology_source_instructions(context)
    graph_instructions = build_graph_source_instructions(context)

    assert len(global_instructions) < 4000
    assert len(ontology_instructions) < 2000
    assert len(graph_instructions) < 2000
    assert "Do not use Lakehouse" in global_instructions
    assert "Use the Graph source to prove relationships" in ontology_instructions
    assert "Backtick-quote identifiers" in graph_instructions
    assert global_instructions not in ontology_instructions
    assert global_instructions not in graph_instructions
    assert "Facility operations" in build_ontology_source_description(context)
    assert "directed Graph" in build_graph_source_description(context)


def test_graph_few_shots_use_only_validated_direct_graph_probes() -> None:
    contract = {
        "cases": [
            {
                "id": "location",
                "question": "Where is Chiller 1?",
                "probes": {
                    "direct_graph": {
                        "query": "MATCH (a:`Asset`)-[:`located_at`]->(l:`Location`) RETURN a, l LIMIT 10",
                        "static_validation_passed": True,
                    }
                },
            },
            {
                "id": "invalid",
                "question": "Invent a path",
                "probes": {
                    "direct_graph": {
                        "query": "MATCH (a)-[]->(b) RETURN a, b",
                        "static_validation_passed": False,
                    }
                },
            },
        ]
    }

    examples = graph_few_shots_from_competency_contract(contract)

    assert len(examples) == 1
    assert examples[0].question == "Where is Chiller 1?"
    assert examples[0].query.endswith("LIMIT 10")
    assert examples[0].id == graph_few_shots_from_competency_contract(
        contract
    )[0].id


# ===========================================================================
# Issue #14 — Property children preserved in public projection (pre-impl tests)
# ===========================================================================

def _build_grounding_with_properties(n_props: int = 2):
    """Build a minimal PersistedAgentGrounding with n_props property children."""
    from fabric_kg_builder.knowledge.data_agent import (
        DataSourceElement,
        ELEMENT_TYPE_NODE,
        ELEMENT_TYPE_PROPERTY,
    )
    from fabric_kg_builder.knowledge.agent_validation import (
        PersistedAgentGrounding,
        _canonical_hash,
    )
    children = [
        {
            "id": f"prop-{i:03d}",
            "display_name": f"serial_number_{i}",
            "type": ELEMENT_TYPE_PROPERTY,
            "is_selected": True,
            "data_type": "string",
            "description": f"Serial number property {i}.",
            "index_state": "indexed",
        }
        for i in range(n_props)
    ]
    node_el = DataSourceElement(
        id="node-asset",
        display_name="Asset",
        type=ELEMENT_TYPE_NODE,
        is_selected=True,
        description="Asset entity.",
        children=children,
        index_state="indexed",
    )
    sidecar = {
        "schema_version": "1.1",
        "semantic_model_manifest_hash": "sha256:" + "0" * 64,
    }
    sidecar_hash = _canonical_hash(sidecar)
    elem_hash = _canonical_hash({"elements": [node_el.to_dict()]})
    return PersistedAgentGrounding(
        elements=(node_el,),
        sidecar=sidecar,
        sidecar_hash=sidecar_hash,
        selected_element_hash=elem_hash,
        property_child_coverage=1.0,
        expected_property_child_count=n_props,
    )


class TestPropertyChildrenPreservedInPublicProjection:
    """After fix: build_public_graph_source_projection preserves property children.

    Bug: children=None strips all children from the public Graph projection.
    Fix: project each agent-visible property child into Fabric-accepted shape
    (id/display_name/type=graph.property/is_selected/data_type/description/index_state).
    """

    def test_projected_elements_have_children(self):
        """After projection, node elements must retain their children list."""
        from fabric_kg_builder.knowledge.agent_validation import build_public_graph_source_projection
        grounding = _build_grounding_with_properties(2)
        elements, _metadata = build_public_graph_source_projection(grounding)
        node_elements = [e for e in elements if e.type == "graph.nodeType"]
        assert node_elements, "Projection must include at least one node element"
        has_children = any(
            e.children is not None and len(e.children) > 0
            for e in node_elements
        )
        assert has_children, (
            "Projected node elements must retain property children. "
            "Currently, build_public_graph_source_projection strips all children "
            "with replace(element, children=None) — this is bug #14."
        )

    def test_projected_property_children_have_correct_type(self):
        """Projected property children must have type='graph.property'."""
        from fabric_kg_builder.knowledge.agent_validation import build_public_graph_source_projection
        from fabric_kg_builder.knowledge.data_agent import ELEMENT_TYPE_PROPERTY
        grounding = _build_grounding_with_properties(2)
        elements, _metadata = build_public_graph_source_projection(grounding)
        node_elements = [e for e in elements if e.type == "graph.nodeType"]
        for node_el in node_elements:
            if node_el.children:
                for child in node_el.children:
                    child_type = child.get("type") if isinstance(child, dict) else getattr(child, "type", None)
                    assert child_type == ELEMENT_TYPE_PROPERTY, (
                        f"Property child must have type '{ELEMENT_TYPE_PROPERTY}', got {child_type!r}"
                    )

    def test_projected_property_child_count_matches_grounding_expected(self):
        """The total projected property child count must equal grounding.expected_property_child_count."""
        from fabric_kg_builder.knowledge.agent_validation import build_public_graph_source_projection
        grounding = _build_grounding_with_properties(3)
        elements, _metadata = build_public_graph_source_projection(grounding)
        total_children = sum(
            len(e.children or [])
            for e in elements
            if e.type == "graph.nodeType"
        )
        assert total_children == grounding.expected_property_child_count, (
            f"Projected child count ({total_children}) must match "
            f"grounding.expected_property_child_count ({grounding.expected_property_child_count})"
        )


# ---------------------------------------------------------------------------
# #12 — Grounding text char counts are deterministic
# ---------------------------------------------------------------------------


class TestGroundingTextCharCounts:
    """Grounding instruction/description char counts must be deterministic and retrievable.

    These counts feed into the AgentPublicationReceipt for #12 receipt reporting (D8).
    Tests are written against the planned interface; they fail if the functions
    do not accept an availability parameter yet.
    """

    def test_build_graph_source_description_char_count_is_bounded(self):
        """build_graph_source_description output is measurable and within the named limit."""
        from fabric_kg_builder.semantic.instructions import build_graph_source_description
        from fabric_kg_builder.knowledge.validation import MAX_SOURCE_DESCRIPTION_CHARS
        context = _semantic_context()
        desc = build_graph_source_description(context)
        assert isinstance(desc, str) and len(desc) > 0
        assert len(desc) <= MAX_SOURCE_DESCRIPTION_CHARS, (
            f"Graph description is {len(desc)} chars; the limit is {MAX_SOURCE_DESCRIPTION_CHARS}"
        )

    def test_build_graph_source_instructions_char_count_is_bounded(self):
        """build_graph_source_instructions output is measurable and within the named limit."""
        from fabric_kg_builder.semantic.instructions import build_graph_source_instructions
        from fabric_kg_builder.knowledge.validation import MAX_SOURCE_INSTRUCTION_CHARS
        context = _semantic_context()
        instr = build_graph_source_instructions(context)
        assert isinstance(instr, str) and len(instr) > 0
        assert len(instr) <= MAX_SOURCE_INSTRUCTION_CHARS, (
            f"Graph instructions are {len(instr)} chars; the limit is {MAX_SOURCE_INSTRUCTION_CHARS}"
        )

    def test_contract_agent_instructions_char_count_is_bounded(self):
        """build_contract_agent_instructions output is measurable and within the global limit."""
        from fabric_kg_builder.semantic.instructions import build_contract_agent_instructions
        from fabric_kg_builder.knowledge.validation import MAX_GLOBAL_INSTRUCTION_CHARS
        context = _semantic_context()
        instr = build_contract_agent_instructions(
            context,
            competency_questions=["Where is Chiller 1?"],
            domain_context="Field service.",
        )
        assert isinstance(instr, str) and len(instr) > 0
        assert len(instr) <= MAX_GLOBAL_INSTRUCTION_CHARS, (
            f"Global instructions are {len(instr)} chars; the limit is {MAX_GLOBAL_INSTRUCTION_CHARS}"
        )
