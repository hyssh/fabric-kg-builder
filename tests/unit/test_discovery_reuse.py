"""Offline replay mapping tests; no model, source acquisition or cloud calls."""

import copy

import pytest

from fabric_kg_builder.enrichment.discovery_reuse import map_discovery_candidates, reconcile_discovery_retry
from fabric_kg_builder.enrichment.schema2_extraction import compile_closed_vocabulary
from tests.unit.test_schema2_extraction import _domain, _response


def test_mapping_prefixes_chunk_local_references_without_mutating_observations():
    contract = _domain()
    vocabulary = compile_closed_vocabulary(contract)
    raw = {"candidates": _response()}
    before = copy.deepcopy(raw)
    first = map_discovery_candidates(raw, chunk_id="chunk:1", vocabulary=vocabulary, contract=contract)
    second = map_discovery_candidates(raw, chunk_id="chunk:2", vocabulary=vocabulary, contract=contract)
    assert raw == before
    assert first.response["candidates"][0]["local_id"] != second.response["candidates"][0]["local_id"]
    relationship = first.response["candidates"][2]
    assert relationship["source_local_id"] == first.response["candidates"][0]["local_id"]
    assert relationship["target_local_id"] == first.response["candidates"][1]["local_id"]
    assert relationship["anchor"] == raw["candidates"][2]["anchor"]
    assert first.reference_bindings[0]["original_local_id"] == "facility-a"
    assert first.response["candidates"][0]["identity_key"] == raw["candidates"][0]["identity_key"]
    assert "entity_alias_unmapped" in first.pending_reasons


def test_ambiguous_local_reference_cannot_be_guessed():
    contract = _domain()
    raw = {"candidates": _response()}
    raw["candidates"].append({**raw["candidates"][0], "label": "A different facility"})
    with pytest.raises(ValueError, match="LOCAL_REFERENCE_AMBIGUOUS"):
        map_discovery_candidates(
            raw, chunk_id="chunk:1", vocabulary=compile_closed_vocabulary(contract), contract=contract,
        )


def test_unmapped_numeric_observation_is_preserved_without_ontology_invention():
    contract = _domain()
    raw = {"candidates": [*_response(), {
        "candidate_kind": "property", "owner_local_id": "equipment-1",
        "observed_property": "Unapproved observed temperature",
        "value": 80.5, "normalized_value": 80.5, "temporal_key": None,
        "anchor": {"span_start": 0, "span_end": 4, "quote": "80.5", "model_authored_evidence_id": None},
    }]}
    mapped = map_discovery_candidates(
        raw, chunk_id="chunk:1", vocabulary=compile_closed_vocabulary(contract), contract=contract,
    )
    value = mapped.response["candidates"][-1]
    assert value["value"] == value["normalized_value"] == 80.5
    assert value["observed_property"] == raw["candidates"][-1]["observed_property"]
    assert value["owner_local_id"] == mapped.response["candidates"][1]["local_id"]
    assert "property_alias_unmapped" in mapped.pending_reasons


def test_empty_retry_retains_every_original_observation_and_pending_reason():
    contract = _domain()
    vocabulary = compile_closed_vocabulary(contract)
    original = {"candidates": _response()}
    before = copy.deepcopy(original)
    baseline = map_discovery_candidates(original, chunk_id="chunk:1", vocabulary=vocabulary, contract=contract)
    replay, original_rows, targeted_rows = reconcile_discovery_retry(
        original, {"candidates": []}, chunk_id="chunk:1", vocabulary=vocabulary, contract=contract,
    )
    assert replay == baseline and original == before
    assert len(original_rows) == len(original["candidates"])
    assert all(row["disposition"] == "retained" for row in original_rows)
    assert any(row["mapping_status"] == "pending" for row in original_rows)
    assert targeted_rows == []


def test_partial_retry_additions_do_not_remove_original_known_or_unknown_observations():
    from fabric_kg_builder.contracts.base import canonical_sha256

    contract = _domain()
    vocabulary = compile_closed_vocabulary(contract)
    original = {"candidates": _response()}
    baseline = map_discovery_candidates(original, chunk_id="chunk:1", vocabulary=vocabulary, contract=contract)
    addition = {
        "candidate_kind": "property", "owner_local_id": "equipment-1",
        "observed_property": "unapproved-temperature", "value": 80.5, "normalized_value": 80.5,
        "temporal_key": None, "anchor": None,
    }
    replay, original_rows, targeted_rows = reconcile_discovery_retry(
        original, {"candidates": [addition]}, chunk_id="chunk:1", vocabulary=vocabulary, contract=contract,
    )
    assert {canonical_sha256(item) for item in baseline.response["candidates"]} <= {
        canonical_sha256(item) for item in replay.response["candidates"]
    }
    assert len(replay.response["candidates"]) == len(baseline.response["candidates"]) + 1
    assert all(row["disposition"] == "retained" for row in original_rows)
    assert replay.pending_reasons and targeted_rows[0]["disposition"] == "addition"


def test_conflicting_known_entity_correction_cannot_rebind_new_dependent_observations():
    contract = _domain()
    vocabulary = compile_closed_vocabulary(contract)
    original = {"candidates": _response()}
    baseline = map_discovery_candidates(original, chunk_id="chunk:1", vocabulary=vocabulary, contract=contract)
    changed = {**original["candidates"][0], "observed_type": "Equipment"}
    dependent = {
        "candidate_kind": "property", "owner_local_id": changed["local_id"],
        "observed_property": "unapproved-temperature", "value": 80.5, "normalized_value": 80.5,
        "temporal_key": None, "anchor": None,
    }
    replay, original_rows, targeted_rows = reconcile_discovery_retry(
        original, {"candidates": [changed, dependent]},
        chunk_id="chunk:1", vocabulary=vocabulary, contract=contract,
    )
    assert replay.response == baseline.response
    assert all(row["disposition"] == "retained" for row in original_rows)
    assert [row["disposition"] for row in targeted_rows] == ["pending_correction", "pending_local_reference"]
