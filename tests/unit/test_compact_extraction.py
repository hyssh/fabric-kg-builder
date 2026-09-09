"""Offline transport checks; expanded candidates are not verified evidence."""

from copy import deepcopy

import pytest

from fabric_kg_builder.enrichment.compact_extraction import (
    CompactExtractionResponseError,
    expand_compact_response,
)
from fabric_kg_builder.enrichment.schema2_extraction import RawCandidateResponse


def _wire():
    quote = " Task T uses 2 parts at 1.25 kg; code 00123. "
    return {
        "anchors": [{"key": "shared", "start": 0, "end": len(quote), "quote": quote}],
        "entities": [
            {
                "local_id": "part", "type": "Part", "label": "00123",
                "identity": [{"property_id": "property:part.code", "value": "00123"}],
                "anchor_keys": ["shared"],
            },
            {
                "local_id": "task", "type": "Task", "label": "Task T",
                "identity": [], "anchor_keys": ["shared"],
                "stable_source_identity": "task-t",
            },
        ],
        "properties": [
            {
                "owner": "part", "property": name, "value": value,
                "normalized_value": value, "anchor_key": "shared",
            }
            for name, value in [
                ("quantity", 2), ("mass", 1.25), ("optional", False), ("code", " 00123 "),
            ]
        ],
        "relationships": [{
            "source": "task", "target": "part", "predicate": "requires",
            "direction": "source_to_target", "anchor_key": "shared",
        }],
    }


def test_shared_anchor_expands_scalar_owners_values_and_relationships_without_inference():
    wire = _wire()
    original = deepcopy(wire)
    expanded = expand_compact_response(wire)
    candidates = expanded["candidates"]
    assert [item["candidate_kind"] for item in candidates] == [
        "entity", "entity", "property", "property", "property", "property", "relationship",
    ]
    expected_anchor = {
        "span_start": 0, "span_end": wire["anchors"][0]["end"],
        "quote": wire["anchors"][0]["quote"],
    }
    assert candidates[0]["identity_key"] == {"property:part.code": "00123"}
    assert candidates[0]["anchors"] == candidates[1]["anchors"] == [expected_anchor]
    for candidate, value in zip(candidates[2:6], [2, 1.25, False, " 00123 "]):
        assert candidate["owner_local_id"] == "part"
        assert candidate["value"] == candidate["normalized_value"] == value
        assert type(candidate["value"]) is type(value)
        assert candidate["anchor"] == expected_anchor
    assert candidates[-1]["source_local_id"] == "task"
    assert candidates[-1]["target_local_id"] == "part"
    assert candidates[-1]["anchor"] == expected_anchor
    assert len(RawCandidateResponse.model_validate(expanded).candidates) == 7
    assert wire == original
    candidates[2]["anchor"]["quote"] = "changed copy"
    assert candidates[0]["anchors"][0]["quote"] == expected_anchor["quote"]
    assert candidates[3]["anchor"]["quote"] == expected_anchor["quote"]


def test_duplicate_anchor_definition_rejects_whole_response():
    wire = _wire()
    wire["anchors"].append({**wire["anchors"][0], "quote": "Conflicting definition."})
    with pytest.raises(CompactExtractionResponseError, match="Duplicate anchor key"):
        expand_compact_response(wire)


def test_unknown_shared_anchor_rejects_whole_response():
    wire = _wire()
    wire["properties"][0]["anchor_key"] = "unknown"
    with pytest.raises(CompactExtractionResponseError, match="Unknown anchor key"):
        expand_compact_response(wire)
