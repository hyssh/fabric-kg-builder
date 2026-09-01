"""Regression tests for agent/instructions.py — the versioned routing and
grounded-answer system prompt injected into the Foundry Prompt Agent.

Covers v1.5's entity-id handoff requirement: when the Ontology resolves an
entity, its resolved label/id must anchor the follow-up Knowledge Base query
rather than the query being re-derived purely from the user's free-text
phrase. This closes a gap found via live smoke-testing where the agent
stopped at a natural-language "no data found" answer instead of using the
resolved entity to look up detail via the Knowledge Base.

Also covers v1.7's named-entity routing floor: a query naming a specific
entity must be classified at least `mixed` so the Ontology is consulted even
when the primary content need is textual/verbatim — closes a gap found via
live testing where a device-named "warnings" query was classified pure
`search` and skipped the graph entirely.

Also covers v1.8's migration from the single-index `azure_ai_search` tool to
the Foundry IQ Knowledge Base MCP tool (`knowledge_base_retrieve`), and the
no-substitution rule closing a live-found hallucination gap where the model
would copy a hardcoded example entity id, or fabricate a smoother-looking
answer, when real tool output looked messy or incomplete.
"""

from __future__ import annotations

from fabric_kg_builder.agent.instructions import (
    INSTRUCTIONS_VERSION,
    build_routing_instructions,
)


def test_instructions_version_is_v1_10():
    assert INSTRUCTIONS_VERSION == "v1.10"


def test_build_routing_instructions_embeds_version_header():
    doc = build_routing_instructions()
    assert "instructions version v1.10" in doc


def test_entity_id_handoff_section_present():
    """v1.5/v1.8 must explicitly require anchoring the Knowledge Base query
    to a resolved Ontology entity."""
    doc = build_routing_instructions()
    assert "ENTITY-ID HANDOFF" in doc
    assert "entity:<hash>" in doc


def test_entity_id_handoff_forbids_pure_free_text_fallback():
    """The instruction must tell the model not to rely on the user's original
    phrase alone once an entity has been resolved by the Ontology."""
    doc = build_routing_instructions()
    assert (
        "do not rely\n    on the user's original phrasing alone once the Ontology has already\n"
        "    resolved a more specific name." in doc
    )


def test_entity_id_handoff_defines_genuine_data_gap_condition():
    """Only a resolved-entity-with-no-knowledge-base-results case is a genuine gap."""
    doc = build_routing_instructions()
    assert "genuine data gap" in doc


def test_entity_id_handoff_does_not_use_odata_filter_syntax_v1_8():
    """v1.8: the Knowledge Base tool takes natural language, not a raw OData
    filter string — the old entity_ids/any(...) syntax must not be taught."""
    doc = build_routing_instructions()
    assert "entity_ids/any(" not in doc
    assert "does not accept one" in doc


def test_no_hardcoded_example_entity_id_leaks_into_prompt_v1_8():
    """P0 regression: instructions.py must never contain a concrete-looking
    example entity id that the model could copy verbatim into unrelated
    answers. Live testing found the model reusing a literal example id
    (entity:6d22b714699d237f96eb43c291b4abdd) as a fabricated citation across
    multiple unrelated conversations."""
    doc = build_routing_instructions()
    assert "6d22b714699d237f96eb43c291b4abdd" not in doc


def test_no_real_format_hex_ids_anywhere_in_prompt_v1_10():
    """P0 regression, broadened per orchestrator request: scan the ENTIRE
    rendered prompt for any long hex string that could be mistaken for a
    real `entity:<hash>`-style id copied from an example. Only the generic
    `<hash>` placeholder token (non-hex) is allowed."""
    import re

    doc = build_routing_instructions()
    hex_like = re.findall(r"[0-9a-f]{8,}", doc)
    assert hex_like == [], f"found real-format hex ids in prompt: {hex_like}"


def test_no_domain_specific_example_terms_that_could_be_mistaken_for_real_data_v1_10():
    """P0 regression: illustrative examples must use an obviously-synthetic
    placeholder term, not a real domain word (e.g. 'kickstand', a real
    Surface component name) that the model could pattern-match and reuse as
    if it were retrieved data."""
    doc = build_routing_instructions()
    assert "kickstand" not in doc
    assert "example-part-x9" in doc


def test_no_substitution_rule_present_v1_8():
    """P0: the model must never substitute a cleaner-looking value for a
    tool's actual (possibly messy/incomplete) returned data, and must never
    state a fact/id not literally present in the current turn's tool output."""
    doc = build_routing_instructions()
    assert "NEVER SUBSTITUTE" in doc
    assert "illustrative ONLY" in doc


def test_two_stage_tool_order_still_present_v1_4():
    """v1.4's ontology-first / knowledge-base-fallback ordering must survive
    the v1.8 bump."""
    doc = build_routing_instructions()
    assert "TWO-STAGE TOOL ORDER" in doc
    assert "ALWAYS query the Ontology" in doc


def test_unsupported_gate_checklist_present_v1_10():
    """P0 routing regression fix: a mandatory, ordered pre-condition
    checklist must exist and must be phrased as a hard gate — closes the bug
    where the model reported "unsupported" immediately after the Ontology
    returned zero rows/a confused response on a named-entity question,
    without ever calling the Knowledge Base as a fallback."""
    doc = build_routing_instructions()
    assert "UNSUPPORTED-GATE CHECKLIST" in doc
    assert "hard gate, not optional guidance" in doc
    assert "Was `knowledge_base_retrieve` ALSO called THIS turn?" in doc


def test_unsupported_gate_checklist_allows_entity_agnostic_shortcut_v1_10():
    """A purely conceptual/definitional question with no named entity may
    still answer from the Knowledge Base alone and skip the rest of the
    checklist — this exception must remain intact."""
    doc = build_routing_instructions()
    assert "this is a purely conceptual/definitional question" in doc


def test_unsupported_gate_checklist_allows_complete_graph_only_shortcut_v1_10():
    """A pure label/existence/count/connection lookup that the Ontology fully
    answers may stop there without an extra Knowledge Base call — this
    exception must remain intact so the model doesn't over-call the KB on
    already-answered graph-only questions."""
    doc = build_routing_instructions()
    assert "you may stop at the Ontology alone; cite it and answer" in doc


def test_unsupported_gate_checklist_forbids_graph_only_unsupported_v1_10():
    """The core fix: a graph-only attempt (zero rows or a confused response)
    must never, by itself, be treated as sufficient grounds to report
    "unsupported" on a named-entity question."""
    doc = build_routing_instructions()
    assert "never, by itself, sufficient grounds to stop" in doc
    assert "is a defect" in doc


def test_gql_dialect_pitfalls_still_present_v1_4():
    """v1.4's Fabric GQL dialect pitfall guidance must survive the v1.8 bump."""
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
    assert INSTRUCTIONS_VERSION == "v1.10"


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
    assert "Knowledge Base tool" in prompt
    assert "knowledge_base_retrieve" in prompt


def test_image_citation_guidance_present_v1_8():
    """v1.8: when the Knowledge Base returns a visual asset, its link must be
    surfaced as an additional citation — but never fabricated or reused
    across unrelated questions."""
    prompt = build_routing_instructions()
    assert "IMAGE CITATIONS" in prompt
    assert "visual_asset" in prompt
    assert "Never fabricate, guess, or reuse an image" in prompt


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
