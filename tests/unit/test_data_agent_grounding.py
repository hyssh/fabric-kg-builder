"""Tests for compact Data Agent grounding boundaries."""

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
