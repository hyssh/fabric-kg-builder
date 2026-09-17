"""Grounded instance mentions stay separate from support and declared values."""

from __future__ import annotations

import dataclasses
import json

import pytest

from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
from fabric_kg_builder.enrichment import schema2_stage, schema2_validation_stage
from fabric_kg_builder.enrichment.schema2_extraction import (
    compile_closed_vocabulary,
    render_extraction_prompt,
)
from fabric_kg_builder.enrichment.schema2_validation_stage import grounded_instance_label
from fabric_kg_builder.serving.lifecycle_projection import L4ProjectionError, run_l4
from tests.unit import test_schema2_validation_stage as fixtures


@pytest.mark.parametrize(("label", "quote", "expected"), [
    ("Install the Surface Connect", "1. Install the Surface Connect securely.", "Install the Surface Connect"),
    ("governed  record", "A governed\nrecord describes a subject.", "governed record"),
    ("Cafe\u0301", "The Café is open.", "Café"),
    ("Surface Pro 99", "Install the device.", None),
    ("device", "Install the device.", "device"),
    ("install the device", "Install the device.", None),
    ("source summary", "This exact quote says something different.", None),
    ("x" * 121, "x" * 121, None),
    ("", "A governed record.", None),
    (None, "A governed record.", None),
])
def test_instance_labels_are_normalized_verbatim_mentions(label, quote, expected):
    assert grounded_instance_label(label, quote) == expected


def _long_quote_pipeline(tmp_path, monkeypatch, *, label="Install the Surface Connect"):
    sentence = (
        "A governed record describes a governed subject. "
        "1. Install the Surface Connect carefully, keeping all components aligned. "
        "The remaining instructions provide additional supporting context, not an instance name."
    )
    monkeypatch.setattr(fixtures, "_SENTENCE", sentence)

    def mutate(candidates, work_unit):
        text = work_unit.text.rstrip()
        candidates[0]["label"] = label
        candidates[0]["anchors"] = [{
            "span_start": work_unit.slice_start,
            "span_end": work_unit.slice_start + len(text),
            "quote": text,
            "model_authored_evidence_id": None,
        }]
        return candidates + [{
            "candidate_kind": "property",
            "owner_local_id": "record-1",
            "observed_property": "Reading",
            "value": "Surface Connect",
            "normalized_value": "Surface Connect",
            "temporal_key": None,
            "anchor": dict(candidates[0]["anchors"][0]),
        }]

    l1_root, domain_path, l2 = fixtures._pipeline(
        tmp_path, "records", mutate=mutate,
        type_properties={"semantic-type:records.record": ({
            "property_id": "property:records.reading",
            "display_name": "Reading",
            "value_type": "string",
            "required": False,
        },)},
    )
    return l2, fixtures._l3(tmp_path, l1_root, domain_path)


def test_long_support_retains_short_name_and_unchanged_declared_property(tmp_path, monkeypatch):
    l2, l3 = _long_quote_pipeline(tmp_path, monkeypatch)
    proposals = [
        record for records in l3.inputs.proposed_partitions.values() for record in records
        if record.approved_semantic_id == "semantic-type:records.record"
    ]
    assert proposals
    assert all(record.proposed_label == "Install the Surface Connect" for record in proposals)
    assert all(len(record.proposed_anchor.quote) > 120 for record in proposals)
    assert all(
        entry.contract_version == "1.2.0"
        for entry in l2.output_manifest.entries
        if entry.contract_kind == "l2.proposed_candidate_partition"
    )
    result = run_l4(l3, state_root=tmp_path / ".fkg" / "l4")
    rows = [
        row for row in result.rows.semantic_asserted_entities
        if row["most_specific_type_id"] == "semantic-type:records.record"
    ]
    assert rows and all(row["label"] == "Install the Surface Connect" for row in rows)
    spans = {span.evidence_span_id: span for span in l3.evidence_spans}
    for row in rows:
        support = spans[row["label_evidence_span_id"]]
        assert len(support.quote) > 120
        assert row["label"] != support.quote
        assert row["label"] in support.quote
    observations = [item for leaf in l3.leaves for item in leaf.property_observations]
    assert observations and all(item.observation_state == "asserted" for item in observations)
    assert all(item.value_json == item.normalized_value_json == '"Surface Connect"' for item in observations)
    assert result.rows.semantic_asserted_properties
    assert all(
        row["normalized_value_json"] == '"Surface Connect"'
        for row in result.rows.semantic_asserted_properties
    )
    for leaf in l3.leaves:
        assert schema2_validation_stage._leaf_from_dict(
            schema2_validation_stage._leaf_to_dict(leaf)
        ) == leaf


def test_unsupported_label_is_quarantined_without_quote_fallback(tmp_path, monkeypatch):
    _, l3 = _long_quote_pipeline(tmp_path, monkeypatch, label="Surface Pro 99")
    candidates = [
        item for item in l3.candidate_results
        if item.approved_semantic_id == "semantic-type:records.record"
    ]
    assert candidates and all(item.current_state == "unresolved" for item in candidates)
    assert all("ENTITY_LABEL_UNGROUNDED" in item.reason_codes for item in candidates)
    assert all(item.verified_label is None and item.label_evidence_span_id is None for item in candidates)
    assert all(item.evidence_span_ids for item in candidates)
    result = run_l4(l3, state_root=tmp_path / ".fkg" / "l4")
    quarantined_ids = {item.semantic_id for item in candidates}
    assert all(row["entity_id"] not in quarantined_ids for row in result.rows.semantic_asserted_entities)
    assert any(
        "ENTITY_LABEL_UNGROUNDED" in row["reason_codes"]
        for row in result.rows.audit_candidates
    )


@pytest.mark.parametrize("updates", [
    {"verified_label": "invented canonical name"},
    {"verified_label": None, "label_evidence_span_id": None},
    {"label_validation_version": None},
])
def test_l4_rechecks_new_label_proof_instead_of_falling_back(tmp_path, monkeypatch, updates):
    _, l3 = _long_quote_pipeline(tmp_path, monkeypatch)
    corrupted = dataclasses.replace(l3, leaves=tuple(
        dataclasses.replace(leaf, candidate_results=tuple(
            dataclasses.replace(item, **updates)
            if item.approved_semantic_id == "semantic-type:records.record" else item
            for item in leaf.candidate_results
        ))
        for leaf in l3.leaves
    ))
    with pytest.raises(L4ProjectionError, match="lacks sealed source grounding"):
        run_l4(corrupted, state_root=tmp_path / ".fkg" / "l4")


def test_historic_110_carrier_reads_with_exact_frozen_hash_and_bytes(tmp_path):
    l1_root, domain_path, l2 = fixtures._pipeline(tmp_path, "records")
    root = tmp_path / ".fkg" / "l2"
    entries = []
    original = {}
    for entry in l2.output_manifest.entries:
        if entry.contract_kind != "l2.proposed_candidate_partition":
            entries.append(entry)
            continue
        batch_id = entry.artifact_id.rpartition(":")[0]
        path = root / "proposed-candidates" / f"{schema2_validation_stage._safe_id(batch_id)}.json"
        raw = json.loads(path.read_text("utf-8"))
        for item in raw:
            item.pop("proposed_label")
        original[batch_id] = canonical_json(raw)
        path.write_text(original[batch_id] + "\n", encoding="utf-8")
        entries.append(entry.model_copy(update={
            "contract_version": "1.1.0",
            "schema_hash": "b13b4ebe78f1f8728e69d2996fae683c8aa0ce6b1725b103a1dd5118abc4de08",
            "content_hash": canonical_sha256(raw),
            "byte_count": len((original[batch_id] + "\n").encode("utf-8")),
        }))
    manifest = schema2_stage._manifest(
        identity=l2.output_manifest.identity, label="historic-label-fixture", entries=tuple(entries),
    )
    (root / "output-manifest.json").write_text(canonical_json(manifest), encoding="utf-8")
    receipt = l2.receipt.model_dump(mode="json", exclude={"receipt_hash"})
    receipt["accepted_contract_versions"]["l2.proposed_candidate_partition"] = "1.1.0"
    receipt["output_manifest_id"] = manifest.artifact_manifest_id
    receipt["output_manifest_hash"] = manifest.manifest_hash
    receipt["receipt_hash"] = canonical_sha256({
        key: value for key, value in receipt.items()
        if key not in {"started_at_utc", "completed_at_utc"}
    })
    (root / "stage-receipt.json").write_text(canonical_json(receipt), encoding="utf-8")
    l3 = fixtures._l3(tmp_path, l1_root, domain_path)
    for batch_id, records in l3.inputs.proposed_partitions.items():
        assert canonical_json([
            schema2_validation_stage.proposed_candidate_payload(item) for item in records
        ]) == original[batch_id]
        assert all("proposed_label" not in item.model_fields_set for item in records)
    assert all(item.label_validation_version is None for item in l3.candidate_results)
    assert all(
        "label_validation_version" not in schema2_validation_stage.candidate_validation_payload(item)
        for item in l3.candidate_results
    )
    result = run_l4(l3, state_root=tmp_path / ".fkg" / "l4")
    assert result.rows.semantic_asserted_entities
    assert all(row["label"] in {"governed record", "governed subject"} for row in result.rows.semantic_asserted_entities)


def test_new_carrier_cannot_omit_label_field_to_request_legacy_quote_fallback(tmp_path):
    _, _, l2 = fixtures._pipeline(tmp_path, "records")
    root = tmp_path / ".fkg" / "l2"
    entries = []
    for entry in l2.output_manifest.entries:
        if entry.contract_kind != "l2.proposed_candidate_partition":
            entries.append(entry)
            continue
        batch_id = entry.artifact_id.rpartition(":")[0]
        path = root / "proposed-candidates" / f"{schema2_validation_stage._safe_id(batch_id)}.json"
        raw = json.loads(path.read_text("utf-8"))
        for item in raw:
            item.pop("proposed_label")
        payload = canonical_json(raw) + "\n"
        path.write_text(payload, encoding="utf-8")
        entries.append(entry.model_copy(update={
            "content_hash": canonical_sha256(raw),
            "byte_count": len(payload.encode("utf-8")),
        }))
    manifest = schema2_stage._manifest(
        identity=l2.output_manifest.identity, label="missing-label-fixture", entries=tuple(entries),
    )
    with pytest.raises(schema2_validation_stage.L3StageError, match="successor label carrier field is missing"):
        schema2_validation_stage._load_candidate_partitions(root, manifest)


def test_extraction_prompt_distinguishes_mentions_quotes_and_required_fields(tmp_path):
    l1_root, domain_path, _ = fixtures._pipeline(tmp_path, "records")
    inputs = schema2_validation_stage.load_l3_inputs(
        l2_state_root=tmp_path / ".fkg" / "l2", l1_state_root=l1_root, domain_path=domain_path,
    )
    vocabulary = compile_closed_vocabulary(inputs.domain_contract)
    prompt = json.loads(render_extraction_prompt(
        vocabulary, source_unit_id="source:test", source_text_hash="a" * 64,
        source_text="Exact source.", slice_start=0, slice_end=13,
    ))
    rules = " ".join(prompt["rules"])
    assert "verbatim substring" in rules
    assert "generic source mention does not establish canonical product identity" in rules
    assert "every required declared effective property" in rules
    assert "field-level exact source evidence" in rules
    assert "generated summary as an exact quote" in rules
