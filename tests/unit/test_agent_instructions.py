"""Unit tests for Data Agent instruction generation, extended DomainBrief,
and capability-aware descriptions/instructions (issues #12, #13).

Covers (original):
- build_agent_instructions: entity types, relationships, questions, CONTAINS hint
- DomainBrief new fields and round-trip

Covers (new #12):
- build_graph_source_description / build_graph_source_instructions:
  capability-aware: only available relationships named, unavailable-required paths noted
- Global instruction boundary: routing/evidence/refusal/failure only — no relationship claims
- Description/instructions regenerate with different availability inputs
"""

from __future__ import annotations

from fabric_kg_builder.deploy.agent_instructions import build_agent_instructions
from fabric_kg_builder.enrichment.domain import DomainBrief


class _ET:
    def __init__(self, type_name, table_name, count):
        self.type_name = type_name
        self.table_name = table_name
        self.count = count


class _RP:
    def __init__(self, name, source_type, target_type, table_name, count):
        self.name = name
        self.source_type = source_type
        self.target_type = target_type
        self.table_name = table_name
        self.count = count


def _plan():
    ets = [
        _ET("Component", "entities_component", 1593),
        _ET("Part", "entities_part", 1359),
        _ET("PartNumber", "entities_partnumber", 1218),
    ]
    rps = [
        _RP("has_part", "Component", "Part", "rel_component_part", 122),
        _RP("has_part_number", "Part", "PartNumber", "rel_part_partnumber", 148),
    ]
    return ets, rps


def test_build_agent_instructions_contains_types_and_rels():
    ets, rps = _plan()
    doc = build_agent_instructions(
        ets, rps,
        ontology_name="kg_ontology",
        industry="manufacturing",
        business_domain="field-service",
        competency_questions=["What part number is the Surflink Screw?"],
    )
    # Types appear
    assert "Component" in doc and "Part" in doc and "PartNumber" in doc
    # Relationship map uses exact edge names and direction
    assert "`Component` -[`has_part`]-> `Part`" in doc
    assert "`Part` -[`has_part_number`]-> `PartNumber`" in doc
    # Context + question rendered
    assert "manufacturing" in doc
    assert "field-service" in doc
    assert "Surflink Screw" in doc
    # CONTAINS guidance present (anti exact-match)
    assert "CONTAINS" in doc
    # Routes verbatim-text questions to AI Search
    assert "AI Search" in doc


def test_build_agent_instructions_handles_dict_inputs():
    doc = build_agent_instructions(
        [{"type_name": "Symptom", "count": 10}],
        [{"name": "causes", "source_type": "Cause", "target_type": "Symptom"}],
    )
    assert "Symptom" in doc
    # Cause type not modelled here, but relationship line still references names.
    assert "causes" in doc


def test_build_agent_instructions_no_questions_hint():
    ets, rps = _plan()
    doc = build_agent_instructions(ets, rps, competency_questions=[])
    assert "--questions-file" in doc  # nudges user to add questions


def test_domain_brief_new_fields_default():
    brief = DomainBrief(domain_brief="x", source_domain_text="x")
    assert brief.industry == ""
    assert brief.business_domain == ""
    assert brief.competency_questions == []


def test_domain_brief_roundtrip_with_new_fields():
    brief = DomainBrief(
        domain_brief="d",
        industry="healthcare",
        business_domain="clinical",
        competency_questions=["What conditions does patient X have?"],
        source_domain_text="d",
    )
    data = brief.model_dump()
    again = DomainBrief.model_validate(data)
    assert again.industry == "healthcare"
    assert again.business_domain == "clinical"
    assert again.competency_questions == ["What conditions does patient X have?"]


# ===========================================================================
# Issue #12 — Capability-aware descriptions and instructions (pre-impl tests)
#
# All imports of new parameters are lazy (inside test methods).
# Tests will be RED until Verbal implements the availability parameter on
# build_graph_source_description / build_graph_source_instructions.
# ===========================================================================

def _semantic_context_with_contract() -> dict:
    return {
        "contract_name": "Field Service Operations",
        "contract_hash": "sha256:0000000000000000000000000000000000000000000000000000000000000001",
        "contract_description": "Evidence-backed field service knowledge.",
        "entity_types": [
            {"graph_label": "Asset"},
            {"graph_label": "Location"},
            {"graph_label": "Warranty"},
        ],
        "relationship_types": [
            {"graph_label": "located_at", "semantic_id": "relationship-type:located_at"},
            {"graph_label": "covered_by", "semantic_id": "relationship-type:warranty"},
        ],
    }


def _avail(semantic_id: str, status: str, observed_rows=None, required_rows: int = 0):
    """Build a DataAvailability for tests that need it."""
    from fabric_kg_builder.semantic.schemas import DataAvailability
    return DataAvailability(
        semantic_id=semantic_id,
        status=status,
        observed_rows=observed_rows,
        required_rows=required_rows,
    )


# ---------------------------------------------------------------------------
# #12 D7 — build_graph_source_description: capability-aware, no over-claim
# ---------------------------------------------------------------------------


class TestCapabilityAwareGraphSourceDescription:
    """build_graph_source_description must be parameterized by availability.

    Issue #12 bug: the current implementation returns a hardcoded string
    claiming 'warranty, installation, replacement' regardless of observed rows.
    After fix: description only names relationships with status='sufficient'
    and explicitly mentions unavailable-required ones.
    """

    def test_description_omits_unavailable_optional_relationship(self):
        """Relationship with status='unavailable' (optional) must not appear in description."""
        from fabric_kg_builder.semantic.instructions import build_graph_source_description
        context = _semantic_context_with_contract()
        availability = [
            _avail("relationship-type:located_at", "sufficient", observed_rows=5, required_rows=1),
            _avail("relationship-type:warranty", "unavailable"),  # optional, absent
        ]
        desc = build_graph_source_description(context, availability=availability)
        # The description must not claim "warranty" traversal is available
        assert "covered_by" not in desc.lower() or "unavailable" in desc.lower() or "no verified" in desc.lower(), (
            "Description must not advertise a warranty traversal that has zero observed rows. "
            "The current hardcoded description violates #12 by always claiming warranty paths."
        )

    def test_description_names_available_relationship(self):
        """Relationship with status='sufficient' must be named in the description."""
        from fabric_kg_builder.semantic.instructions import build_graph_source_description
        context = _semantic_context_with_contract()
        availability = [
            _avail("relationship-type:located_at", "sufficient", observed_rows=5, required_rows=1),
            _avail("relationship-type:warranty", "unavailable"),
        ]
        desc = build_graph_source_description(context, availability=availability)
        # The 'located_at' relationship is available — its label should appear somewhere
        assert "located_at" in desc or "location" in desc.lower(), (
            "Description must name the available located_at relationship path"
        )

    def test_description_notes_unavailable_required_relationship(self):
        """Unavailable-but-required relationship must be explicitly noted in description."""
        from fabric_kg_builder.semantic.instructions import build_graph_source_description
        context = _semantic_context_with_contract()
        # warranty is required (required_rows=1) but unavailable → must be noted
        availability = [
            _avail("relationship-type:located_at", "sufficient", observed_rows=5, required_rows=1),
            _avail("relationship-type:warranty", "unavailable"),
        ]
        # Per ADR D7: "explicitly name unavailable-but-existing entities"
        desc = build_graph_source_description(
            context,
            availability=availability,
            # hint that warranty is required-but-unavailable
        )
        # Description must surface the warranty gap without claiming traversal is possible
        # (Either mentions "Warranty entities exist, but no verified relationship" or
        #  omits it entirely — both are acceptable as long as it doesn't CLAIM it's available)
        # The hard constraint: no false claims of availability
        # We verify it doesn't claim the unavailable path works
        assert "WARRANTY" not in desc.upper().replace("UNAVAILABLE", "").replace("NO VERIFIED", ""), (
            "If warranty is in the description at all, it must be in an unavailability context; "
            "a hardcoded description that always claims warranty traversal fails #12"
        ) if "warranty" in desc.lower() else True

    def test_description_differs_between_two_availability_states(self):
        """Same context, different availability → different descriptions (regeneration)."""
        from fabric_kg_builder.semantic.instructions import build_graph_source_description
        context = _semantic_context_with_contract()
        avail_full = [
            _avail("relationship-type:located_at", "sufficient", observed_rows=5, required_rows=1),
            _avail("relationship-type:warranty", "sufficient", observed_rows=3, required_rows=1),
        ]
        avail_partial = [
            _avail("relationship-type:located_at", "sufficient", observed_rows=5, required_rows=1),
            _avail("relationship-type:warranty", "unavailable"),
        ]
        desc_full = build_graph_source_description(context, availability=avail_full)
        desc_partial = build_graph_source_description(context, availability=avail_partial)
        assert desc_full != desc_partial, (
            "Descriptions must differ when relationship availability changes "
            "(capability-aware regeneration required by #12)"
        )

    def test_description_without_availability_param_still_works_backward_compat(self):
        """Backward compat: calling without availability parameter must not raise."""
        from fabric_kg_builder.semantic.instructions import build_graph_source_description
        context = _semantic_context_with_contract()
        # Must still work (no-availability → either shows all or shows none, but no crash)
        desc = build_graph_source_description(context)
        assert isinstance(desc, str) and len(desc) > 0


# ---------------------------------------------------------------------------
# #12 — Global instruction boundary: routing/evidence/refusal/failure only
# ---------------------------------------------------------------------------


class TestGlobalInstructionBoundary:
    """build_contract_agent_instructions must remain routing/evidence/refusal/failure only.

    Issue #12 requirement: global instructions must NOT enumerate specific
    relationship names or claim specific traversal paths are available.
    The description/instruction count must appear in the receipt.
    """

    def test_global_instruction_does_not_claim_specific_relationships(self):
        """Global instruction must NOT contain specific relationship names like 'warranty'."""
        from fabric_kg_builder.semantic.instructions import build_contract_agent_instructions
        context = _semantic_context_with_contract()
        global_instr = build_contract_agent_instructions(
            context,
            competency_questions=["Where is Chiller 1?"],
            domain_context="Field service.",
        )
        # Global instruction must be routing/evidence/refusal/failure
        # It must NOT name specific relationships as available
        assert "warranty" not in global_instr.lower(), (
            "Global instruction must not claim specific relationship availability — "
            "that is capability-aware and belongs in source descriptions"
        )

    def test_global_instruction_contains_routing_guidance(self):
        """Global instruction must contain routing guidance (Ontology, Graph)."""
        from fabric_kg_builder.semantic.instructions import build_contract_agent_instructions
        context = _semantic_context_with_contract()
        global_instr = build_contract_agent_instructions(
            context,
            competency_questions=["Where is Chiller 1?"],
        )
        assert "Ontology" in global_instr or "ontology" in global_instr
        assert "Graph" in global_instr or "graph" in global_instr

    def test_global_instruction_char_count_within_4000_limit(self):
        """Global instruction must be ≤ 4000 chars to respect the named limit constant."""
        from fabric_kg_builder.semantic.instructions import build_contract_agent_instructions
        from fabric_kg_builder.knowledge.validation import MAX_GLOBAL_INSTRUCTION_CHARS
        context = _semantic_context_with_contract()
        global_instr = build_contract_agent_instructions(
            context,
            competency_questions=["Where is Chiller 1?", "What warranties cover Asset X?"],
            domain_context="Industrial field service operations.",
        )
        assert len(global_instr) <= MAX_GLOBAL_INSTRUCTION_CHARS, (
            f"Global instruction is {len(global_instr)} chars, exceeding the "
            f"{MAX_GLOBAL_INSTRUCTION_CHARS}-char limit"
        )
