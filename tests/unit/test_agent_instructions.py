"""Unit tests for Data Agent instruction generation and extended DomainBrief."""

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
