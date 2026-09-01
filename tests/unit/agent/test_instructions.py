"""Regression tests for agent/instructions.py — the versioned routing and
grounded-answer system prompt injected into the Foundry Prompt Agent.

Covers v1.5's entity-id handoff requirement: when the Ontology resolves an
entity, its exact `entity:<hash>` id must be passed to AI Search as an
`entity_ids` filter rather than re-derived from the user's free-text phrase.
This closes a gap found via live smoke-testing where the agent stopped at a
natural-language "no data found" answer instead of using the resolved entity
id to look up detail via Search.

Also covers v1.7's named-entity routing floor: a query naming a specific
entity must be classified at least `mixed` so the Ontology is consulted even
when the primary content need is textual/verbatim — closes a gap found via
live testing where a device-named "warnings" query was classified pure
`search` and skipped the graph entirely.
"""

from __future__ import annotations

from fabric_kg_builder.agent.instructions import (
    INSTRUCTIONS_VERSION,
    build_routing_instructions,
)


def test_instructions_version_is_v1_7():
    assert INSTRUCTIONS_VERSION == "v1.7"


def test_build_routing_instructions_embeds_version_header():
    doc = build_routing_instructions()
    assert "instructions version v1.7" in doc


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
    assert INSTRUCTIONS_VERSION == "v1.7"


def test_named_entity_routing_floor_present_v1_7():
    """v1.7: any query naming a specific entity must be classified at least
    `mixed`, so the Ontology is always consulted even for content/verbatim
    needs (closes the gap where device-named "warnings"/verbatim questions
    were classified pure `search` and skipped the graph entirely)."""
    doc = build_routing_instructions()
    assert "NAMED-ENTITY ROUTING FLOOR" in doc
    assert "AT\n    LEAST `mixed`" in doc
    assert "Reserve pure `search` for entity-agnostic content questions" in doc


def test_named_entity_routing_floor_preserves_unsupported_semantics_v1_7():
    """The routing floor must not force a `mixed` answer when neither source
    actually has the named entity/content — unsupported must remain valid."""
    doc = build_routing_instructions()
    assert "still report" in doc
    assert "unsupported" in doc


def test_label_is_documented_as_a_reserved_keyword_needing_backticks():
    """issue #112: `label` is reserved in Fabric GQL and every entity carries a
    `label` property, so the prompt must show the back-ticked form. Unquoted
    n.label is a hard syntax error that surfaces as a false 'no data found'."""
    prompt = build_routing_instructions()
    assert "reserved keyword" in prompt.lower()
    assert "n.`label`" in prompt
    # The wrong form must be shown as wrong, so the model can recognise it.
    assert "WRONG:   RETURN n.label" in prompt


def test_filter_not_where_uses_backticked_label_in_its_example():
    """The FILTER example must itself model the back-ticking rule; an example
    that says FILTER n.name would teach the unquoted habit back."""
    prompt = build_routing_instructions()
    assert "FILTER n.`label`" in prompt
    assert "FILTER n.name" not in prompt


def test_entity_label_property_is_described_as_populated():
    """The label deployment made entity names readable from the graph alone.
    The prompt must not still claim entity properties are unpopulated, or the
    agent will pointlessly fall back to Search for naming questions."""
    prompt = build_routing_instructions()
    assert "not populated in this release" not in prompt
    assert "human-readable `label`" in prompt


def test_verbatim_quotation_is_required_for_evidence_questions():
    prompt = build_routing_instructions()
    assert "VERBATIM" in prompt
    assert "paraphrase is not evidence" in prompt


def test_both_grounding_tools_are_named_explicitly():
    """The model selects by tool name, so the prompt must map each tool name
    to its role rather than describing them only abstractly."""
    prompt = build_routing_instructions()
    assert "Fabric Data Agent tool" in prompt
    assert "Azure AI Search tool" in prompt


def test_relationship_types_are_injected_and_constrained():
    prompt = build_routing_instructions(
        relationship_types=["device_has_component", "procedure_requires_tool"]
    )
    assert "`device_has_component`" in prompt
    assert "`procedure_requires_tool`" in prompt
    assert "inventing an edge name" in prompt


def test_relationship_types_absent_when_not_supplied():
    prompt = build_routing_instructions()
    assert "VALID RELATIONSHIP TYPES" not in prompt


def test_entity_and_relationship_types_coexist():
    prompt = build_routing_instructions(
        entity_types=["surface_device"], relationship_types=["device_has_component"]
    )
    assert "VALID ENTITY TYPES" in prompt
    assert "VALID RELATIONSHIP TYPES" in prompt


def test_absence_claims_require_a_successful_query():
    """#105/#112: never report a subject absent on the strength of a failed query."""
    prompt = build_routing_instructions()
    assert "returned zero rows" in prompt
    assert "say which query you ran" in prompt
