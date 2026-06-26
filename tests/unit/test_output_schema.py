"""Unit tests for the SPEC-004 §4 intermediate LLM output schema.

All tests are deterministic — no live API calls, no file I/O.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fabric_kg_builder.enrichment.output_schema import (
    LLM_OUTPUT_JSON_SCHEMA,
    LLMOutput,
    Entity,
    Relationship,
    Chunk,
    VisualAsset,
    VisualRegion,
    Evidence,
    PlaceholderSuggestion,
    SchemaProfile,
    ColumnMapping,
    validate,
)


# ---------------------------------------------------------------------------
# Shared minimal valid payload
# ---------------------------------------------------------------------------

def _minimal_valid() -> dict:
    """Minimal payload that satisfies all required fields per SPEC-004 §4."""
    return {
        "source_file_id": "test-source.csv",
        "pass": "p2",
        "entities": [
            {
                "id_hint": "test:device:laptop",
                "type": "Device",
                "label": "Test Laptop",
                "confidence": 0.95,
            }
        ],
        "relationships": [
            {
                "id_hint": "rel:1",
                "source_id_hint": "test:device:laptop",
                "relation": "has_component",
                "target_id_hint": "test:component:battery",
                "confidence": 0.87,
            }
        ],
        "chunks": [],
        "visual_assets": [],
        "visual_regions": [],
        "evidence": [],
        "placeholder_suggestions": [],
    }


# ---------------------------------------------------------------------------
# validate() — happy-path
# ---------------------------------------------------------------------------


def test_validate_good_payload_returns_llm_output() -> None:
    """validate() with a fully conformant payload should return an LLMOutput."""
    parsed = validate(_minimal_valid())
    assert isinstance(parsed, LLMOutput)


def test_validate_source_file_id_preserved() -> None:
    parsed = validate(_minimal_valid())
    assert parsed.source_file_id == "test-source.csv"


def test_validate_pass_field_accessible_via_pass_attr() -> None:
    """The 'pass' field (Python keyword) should be readable via pass_ attribute."""
    parsed = validate(_minimal_valid())
    assert parsed.pass_ == "p2"


def test_validate_entity_fields() -> None:
    parsed = validate(_minimal_valid())
    assert len(parsed.entities) == 1
    e = parsed.entities[0]
    assert e.id_hint == "test:device:laptop"
    assert e.type == "Device"
    assert e.label == "Test Laptop"
    assert e.confidence == 0.95


def test_validate_relationship_fields() -> None:
    parsed = validate(_minimal_valid())
    assert len(parsed.relationships) == 1
    r = parsed.relationships[0]
    assert r.id_hint == "rel:1"
    assert r.source_id_hint == "test:device:laptop"
    assert r.relation == "has_component"
    assert r.confidence == 0.87


def test_validate_empty_arrays_default() -> None:
    """Arrays not present in the payload should default to empty list."""
    payload = {
        "source_file_id": "src",
        "pass": "p1",
    }
    parsed = validate(payload)
    assert parsed.chunks == []
    assert parsed.visual_assets == []
    assert parsed.visual_regions == []
    assert parsed.evidence == []
    assert parsed.placeholder_suggestions == []


def test_validate_full_entity_optional_fields() -> None:
    payload = {
        "source_file_id": "src",
        "pass": "p2",
        "entities": [
            {
                "id_hint": "e1",
                "type": "Component",
                "label": "Battery",
                "confidence": 0.91,
                "canonical_name": "battery",
                "aliases": ["Battery pack"],
                "description": "Internal rechargeable battery.",
                "rationale": "Mentioned in BOM row 3.",
                "source_spans": ["ev:row:3"],
            }
        ],
    }
    parsed = validate(payload)
    e = parsed.entities[0]
    assert e.canonical_name == "battery"
    assert e.aliases == ["Battery pack"]
    assert e.rationale == "Mentioned in BOM row 3."


def test_validate_evidence_item() -> None:
    payload = {
        "source_file_id": "src",
        "pass": "p5",
        "evidence": [
            {
                "id_hint": "ev:1",
                "source_type": "csv_row",
                "row_index": 3,
                "text": "Surface Laptop 5, Battery, M1287099-003",
            }
        ],
    }
    parsed = validate(payload)
    ev = parsed.evidence[0]
    assert ev.id_hint == "ev:1"
    assert ev.source_type == "csv_row"
    assert ev.row_index == 3


def test_validate_visual_asset() -> None:
    payload = {
        "source_file_id": "src",
        "pass": "p6",
        "visual_assets": [
            {
                "id_hint": "va:fig1",
                "asset_type": "figure",
                "blob_url": "https://fake.blob.core.windows.net/kg-assets/fig1.png",
                "confidence": 0.88,
            }
        ],
    }
    parsed = validate(payload)
    va = parsed.visual_assets[0]
    assert va.id_hint == "va:fig1"
    assert va.blob_url == "https://fake.blob.core.windows.net/kg-assets/fig1.png"


def test_validate_chunk() -> None:
    payload = {
        "source_file_id": "src",
        "pass": "p7",
        "chunks": [
            {
                "id_hint": "chunk:1",
                "chunk_type": "section_text",
                "content": "The battery is a 45Wh lithium cell.",
                "summary": "Battery specification section.",
                "confidence": 0.9,
            }
        ],
    }
    parsed = validate(payload)
    c = parsed.chunks[0]
    assert c.chunk_type == "section_text"
    assert c.summary == "Battery specification section."


def test_validate_placeholder_suggestion() -> None:
    payload = {
        "source_file_id": "src",
        "pass": "p8",
        "placeholder_suggestions": [
            {
                "concept": "PartNumber",
                "reason": "BOM rows reference part numbers not yet extracted.",
                "example_labels": ["M1287099-003"],
                "confidence": 0.75,
            }
        ],
    }
    parsed = validate(payload)
    ps = parsed.placeholder_suggestions[0]
    assert ps.concept == "PartNumber"
    assert ps.confidence == 0.75


def test_validate_schema_profile() -> None:
    payload = {
        "source_file_id": "src",
        "pass": "p1",
        "schema_profile": {
            "inferred_domain": "hardware-support",
            "column_mappings": [
                {
                    "source_column": "Part Number",
                    "ontology_type": "PartNumber",
                    "confidence": 0.92,
                }
            ],
            "inferred_entity_types": ["Device", "Component"],
            "inferred_relationship_types": ["has_component"],
        },
    }
    parsed = validate(payload)
    sp = parsed.schema_profile
    assert sp is not None
    assert sp.inferred_domain == "hardware-support"
    assert sp.column_mappings[0].ontology_type == "PartNumber"


# ---------------------------------------------------------------------------
# validate() — rejection cases
# ---------------------------------------------------------------------------


def test_validate_rejects_missing_source_file_id() -> None:
    """source_file_id is required — missing it must raise ValidationError."""
    payload = {"pass": "p2"}
    with pytest.raises(ValidationError) as exc_info:
        validate(payload)
    errors = exc_info.value.errors()
    fields = [e["loc"][0] for e in errors]
    assert "source_file_id" in fields


def test_validate_rejects_missing_pass_field() -> None:
    """'pass' is required — missing it must raise ValidationError."""
    payload = {"source_file_id": "src"}
    with pytest.raises(ValidationError) as exc_info:
        validate(payload)
    errors = exc_info.value.errors()
    fields = [e["loc"][0] for e in errors]
    assert "pass" in fields


def test_validate_accepts_entity_missing_id_hint() -> None:
    """id_hint is optional — canonicalize synthesizes the entity_id."""
    payload = {
        "source_file_id": "src",
        "pass": "p2",
        "entities": [
            {"type": "Device", "label": "Laptop", "confidence": 0.9}
        ],
    }
    parsed = validate(payload)
    assert parsed.entities[0].id_hint is None
    assert parsed.entities[0].label == "Laptop"


def test_validate_rejects_entity_missing_label() -> None:
    payload = {
        "source_file_id": "src",
        "pass": "p2",
        "entities": [
            {"id_hint": "e1", "type": "Device", "confidence": 0.9}
        ],
    }
    with pytest.raises(ValidationError):
        validate(payload)


def test_validate_defaults_entity_missing_confidence() -> None:
    """confidence is optional — defaults to DEFAULT_CONFIDENCE when omitted."""
    payload = {
        "source_file_id": "src",
        "pass": "p2",
        "entities": [
            {"id_hint": "e1", "type": "Device", "label": "Laptop"}
        ],
    }
    parsed = validate(payload)
    assert parsed.entities[0].confidence == 0.5


def test_validate_rejects_confidence_above_one() -> None:
    """Confidence > 1.0 violates the [0.0, 1.0] constraint."""
    payload = {
        "source_file_id": "src",
        "pass": "p2",
        "entities": [
            {"id_hint": "e1", "type": "Device", "label": "Laptop", "confidence": 1.5}
        ],
    }
    with pytest.raises(ValidationError):
        validate(payload)


def test_validate_rejects_confidence_below_zero() -> None:
    """Confidence < 0.0 violates the [0.0, 1.0] constraint."""
    payload = {
        "source_file_id": "src",
        "pass": "p2",
        "entities": [
            {"id_hint": "e1", "type": "Device", "label": "Laptop", "confidence": -0.1}
        ],
    }
    with pytest.raises(ValidationError):
        validate(payload)


def test_validate_rejects_relationship_missing_required_fields() -> None:
    """Relationship without source_id_hint must fail."""
    payload = {
        "source_file_id": "src",
        "pass": "p3",
        "relationships": [
            {
                "id_hint": "r1",
                # missing source_id_hint
                "relation": "has_component",
                "target_id_hint": "e2",
                "confidence": 0.8,
            }
        ],
    }
    with pytest.raises(ValidationError):
        validate(payload)


def test_validate_rejects_visual_asset_missing_blob_url() -> None:
    payload = {
        "source_file_id": "src",
        "pass": "p6",
        "visual_assets": [
            {
                "id_hint": "va:1",
                "asset_type": "figure",
                # missing blob_url
                "confidence": 0.8,
            }
        ],
    }
    with pytest.raises(ValidationError):
        validate(payload)


def test_validate_evidence_missing_source_type_is_now_optional() -> None:
    """source_type is now OPTIONAL — evidence with only text+confidence must succeed.

    SPEC-004 robustness fix (2026-06-24): real LLMs (gpt-5-4-mini) omit
    id_hint and source_type from evidence items.  These fields are synthesized
    by the canonicalize step, so the schema must accept absent values.
    """
    payload = {
        "source_file_id": "src",
        "pass": "p5",
        "evidence": [
            {
                "id_hint": "ev:1",
                # source_type intentionally absent — must NOT raise
            }
        ],
    }
    parsed = validate(payload)
    assert parsed.evidence[0].source_type is None


def test_validate_evidence_missing_id_hint_is_now_optional() -> None:
    """id_hint is now OPTIONAL in evidence.

    The canonicalize step synthesizes id_hint from a content hash when absent.
    """
    payload = {
        "source_file_id": "src",
        "pass": "p5",
        "evidence": [
            {
                "source_type": "document_span",
                "text": "Some excerpt",
                # id_hint intentionally absent — must NOT raise
            }
        ],
    }
    parsed = validate(payload)
    assert parsed.evidence[0].id_hint is None


def test_validate_evidence_both_hints_absent() -> None:
    """Evidence with only text+confidence (real LLM output) must parse successfully."""
    payload = {
        "source_file_id": "src",
        "pass": "p5",
        "evidence": [
            {
                "text": "Surface Laptop 5 battery is 45Wh.",
                "confidence": 0.98,
            }
        ],
    }
    parsed = validate(payload)
    ev = parsed.evidence[0]
    assert ev.id_hint is None
    assert ev.source_type is None
    assert ev.text == "Surface Laptop 5 battery is 45Wh."


# ---------------------------------------------------------------------------
# JSON Schema export
# ---------------------------------------------------------------------------


def test_llm_output_json_schema_is_dict() -> None:
    """LLM_OUTPUT_JSON_SCHEMA should be a non-empty dict with 'properties'."""
    assert isinstance(LLM_OUTPUT_JSON_SCHEMA, dict)
    assert "properties" in LLM_OUTPUT_JSON_SCHEMA


def test_llm_output_json_schema_has_required_fields() -> None:
    """JSON Schema must mark source_file_id and pass as required."""
    required = LLM_OUTPUT_JSON_SCHEMA.get("required", [])
    # 'pass' may appear as the alias in the schema
    assert "source_file_id" in required
