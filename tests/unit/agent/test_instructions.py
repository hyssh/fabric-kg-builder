"""Regression tests for agent/instructions.py — the versioned routing and
grounded-answer system prompt injected into the Foundry Prompt Agent.

Covers v1.5's entity-id handoff requirement: when the Ontology resolves an
entity, its exact `entity:<hash>` id must be passed to AI Search as an
`entity_ids` filter rather than re-derived from the user's free-text phrase.
This closes a gap found via live smoke-testing where the agent stopped at a
natural-language "no data found" answer instead of using the resolved entity
id to look up detail via Search.
"""

from __future__ import annotations

from fabric_kg_builder.agent.instructions import (
    INSTRUCTIONS_VERSION,
    build_routing_instructions,
)


def test_instructions_version_is_v1_5():
    assert INSTRUCTIONS_VERSION == "v1.5"


def test_build_routing_instructions_embeds_version_header():
    doc = build_routing_instructions()
    assert "instructions version v1.5" in doc


def test_entity_id_handoff_section_present():
    """v1.5 must explicitly require passing resolved entity ids to Search."""
    doc = build_routing_instructions()
    assert "ENTITY-ID HANDOFF" in doc
    assert "entity_ids" in doc
    assert "entity:<hash>" in doc


def test_entity_id_handoff_forbids_pure_free_text_fallback():
    """The instruction must tell the model not to re-derive from free text
    once an entity id has been resolved by the Ontology."""
    doc = build_routing_instructions()
    assert "Do NOT re-derive the Search query purely from the user's original phrase" in doc


def test_entity_id_handoff_defines_genuine_data_gap_condition():
    """Only a resolved-entity-with-no-search-chunks case is a genuine gap."""
    doc = build_routing_instructions()
    assert "genuine data gap" in doc


def test_two_stage_tool_order_still_present_v1_4():
    """v1.4's ontology-first / search-fallback ordering must survive the v1.5 bump."""
    doc = build_routing_instructions()
    assert "TWO-STAGE TOOL ORDER" in doc
    assert "ALWAYS query the Ontology" in doc


def test_gql_dialect_pitfalls_still_present_v1_4():
    """v1.4's Fabric GQL dialect pitfall guidance must survive the v1.5 bump."""
    doc = build_routing_instructions()
    assert "FABRIC GQL DIALECT" in doc
    assert "back-tick quoted" in doc
    assert "FILTER, not WHERE" in doc


def test_no_data_found_vs_syntax_error_guidance_still_present_v1_4():
    doc = build_routing_instructions()
    assert 'DO NOT CONFUSE "NO DATA FOUND" WITH A QUERY SYNTAX ERROR' in doc


def test_entity_types_and_domain_context_still_appended():
    doc = build_routing_instructions(
        entity_types=["surface_device", "surface_component"],
        domain_context="Surface repair knowledge graph.",
    )
    assert "VALID ENTITY TYPES" in doc
    assert "`surface_device`" in doc
    assert "APPROVED DOMAIN CONTEXT" in doc
    assert "Surface repair knowledge graph." in doc


def test_custom_version_override_renders_in_header_not_module_constant():
    """version= override only changes the rendered header; INSTRUCTIONS_VERSION
    (used by the deployer for hashing/audit) is unaffected."""
    doc = build_routing_instructions(version="v9.9-test")
    assert "instructions version v9.9-test" in doc
    assert INSTRUCTIONS_VERSION == "v1.5"
