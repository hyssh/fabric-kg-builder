"""Publication defense for provable local-reference fallback identity reuse."""

import pytest

from fabric_kg_builder.enrichment import schema2_validation_stage as stage
from fabric_kg_builder.enrichment.schema2_evidence import (
    SourceUnitIndex, derived_stable_source_identity, recompute_entity_id,
)
from fabric_kg_builder.enrichment.schema2_work_units import split_work_unit
from fabric_kg_builder.enrichment import window_prefix
from fabric_kg_builder.serving.lifecycle_projection import run_l4
from tests.unit import test_schema2_evidence as evidence
from tests.unit import test_schema2_validation_stage as fixtures
from tests.unit import test_schema2_work_unit_references as refs


def _proven_record(unit, hierarchy, *, work="work:one", local="e1", quote=None, business_key=None, native=None):
    policy = hierarchy.identity_policy_by_type["semantic-type:test.owner"]
    entity_id = recompute_entity_id(
        project_id=unit.identity.project_id, policy=policy,
        normalized_business_key=business_key,
        stable_source_identity=None if business_key else native or derived_stable_source_identity(
            source_unit_id=unit.source_unit_id, local_reference=local,
        ),
    )
    record = refs._record(
        unit, entity_id=entity_id, type_id="semantic-type:test.owner",
        work=work, local=local, quote=quote,
    )
    if business_key:
        record = record.model_copy(update={"normalized_business_key": tuple(business_key.items())})
    return record


@pytest.mark.parametrize("same_quote", [False, True])
@pytest.mark.parametrize("local", ["e1", "slice-0-5::e1"])
def test_proven_fallback_reuse_across_work_units_blocks_identity_even_same_label(same_quote, local):
    unit = evidence._unit("Alpha Beta")
    hierarchy = refs._hierarchy()
    first = _proven_record(unit, hierarchy, local=local, quote="Alpha")
    second = _proven_record(unit, hierarchy, local=local, work="work:two", quote="Alpha" if same_quote else "Beta")
    assert first.semantic_id == second.semantic_id
    shared = stage._build_shared_context(refs._inputs(unit, hierarchy, [first, second]))
    assert first.semantic_id in shared.identity_conflict_entity_ids
    assert refs._resolve(shared, first)[2] == ("IDENTITY_POLICY_VIOLATION",)
    assert refs._resolve(shared, second)[2] == ("IDENTITY_POLICY_VIOLATION",)


@pytest.mark.parametrize("mutation", ["anchor", "label", "payload_hash"])
def test_same_unit_fallback_duplicate_payloads_are_not_guessed_to_be_aliases(mutation):
    unit = evidence._unit("Alpha Beta")
    hierarchy = refs._hierarchy()
    first = _proven_record(unit, hierarchy)
    updates = {
        "anchor": {"proposed_anchor": stage.ProposedAnchorView(span_start=0, span_end=5, quote="Alpha")},
        "label": {"proposed_label": "Alpha"},
        "payload_hash": {"payload_hash": "b" * 64},
    }
    second = first.model_copy(update=updates[mutation])
    shared = stage._build_shared_context(refs._inputs(unit, hierarchy, [first, second]))
    assert first.semantic_id in shared.identity_conflict_entity_ids


def test_exact_same_unit_payload_duplicates_remain_unambiguous():
    unit = evidence._unit("Alpha")
    hierarchy = refs._hierarchy()
    record = _proven_record(unit, hierarchy)
    shared = stage._build_shared_context(refs._inputs(unit, hierarchy, [record, record]))
    assert record.semantic_id not in shared.identity_conflict_entity_ids
    assert refs._resolve(shared, record) == (record.semantic_id, record.approved_semantic_id, ())


@pytest.mark.parametrize("different_source", [False, True])
def test_qualified_or_source_distinct_fallback_references_remain_distinct(different_source):
    unit = evidence._unit("Alpha Beta")
    second_unit = evidence._unit(unit.text, ordinal=1) if different_source else unit
    hierarchy = refs._hierarchy()
    first = _proven_record(unit, hierarchy, local="slice-0-5::e1", quote="Alpha")
    second = _proven_record(
        second_unit, hierarchy, work="work:two",
        local="slice-0-5::e1" if different_source else "slice-6-10::e1", quote="Beta",
    )
    assert first.semantic_id != second.semantic_id
    inputs = refs._inputs(unit, hierarchy, [first, second])
    inputs.source_units = SourceUnitIndex([unit, second_unit])
    shared = stage._build_shared_context(inputs)
    assert not shared.identity_conflict_entity_ids
    assert refs._resolve(shared, first)[2] == refs._resolve(shared, second)[2] == ()
    assert refs._resolve(shared, first, work="work:two")[2] == ("ENDPOINT_UNRESOLVED",)


@pytest.mark.parametrize("different_source", [False, True])
def test_recomputed_business_identity_is_not_banned_across_work_units(different_source):
    unit = evidence._unit("Alpha Beta")
    second_unit = evidence._unit(unit.text, ordinal=1) if different_source else unit
    hierarchy = evidence._compiled((
        evidence._entity("semantic-type:test.owner", policy=evidence._business_policy()),
    ), ())
    first = _proven_record(unit, hierarchy, quote="Alpha", business_key={"serial": "native-42"})
    second = _proven_record(
        second_unit, hierarchy, quote="Beta", work="work:two", business_key={"serial": "native-42"},
    )
    assert first.semantic_id == second.semantic_id
    inputs = refs._inputs(unit, hierarchy, [first, second])
    inputs.source_units = SourceUnitIndex([unit, second_unit])
    shared = stage._build_shared_context(inputs)
    assert not shared.identity_conflict_entity_ids
    assert refs._resolve(shared, first)[2] == refs._resolve(shared, second)[2] == ()


def test_unpersisted_native_source_seed_is_not_invented_as_a_local_fallback():
    unit = evidence._unit("Alpha Beta")
    hierarchy = refs._hierarchy()
    first = _proven_record(unit, hierarchy, quote="Alpha", native="native-global-42")
    second = _proven_record(unit, hierarchy, quote="Beta", work="work:two", native="native-global-42")
    shared = stage._build_shared_context(refs._inputs(unit, hierarchy, [first, second]))
    assert not shared.identity_conflict_entity_ids
    assert stage._identity_witness(first, hierarchy=hierarchy, project_id=unit.identity.project_id) == (
        False, "opaque_source_identity", ("IDENTITY_POLICY_VIOLATION",),
    )


def test_conflict_payload_and_verifier_bind_cache():
    unit = evidence._unit("Alpha")
    hierarchy = refs._hierarchy()
    record = _proven_record(unit, hierarchy)
    before = stage._build_shared_context(refs._inputs(unit, hierarchy, [record]))
    after = stage._build_shared_context(refs._inputs(unit, hierarchy, [
        record, record.model_copy(update={"payload_hash": "b" * 64}),
    ]))
    assert before.context_hash([record.semantic_id], [], [unit.source_unit_id]) != (
        after.context_hash([record.semantic_id], [], [unit.source_unit_id])
    )
    assert "source-local-identity-collision/1.0.0" in stage._verifier_binding()


def _split_pipeline(tmp_path, monkeypatch, *, qualified=False, business=False, duplicate=False):
    if not duplicate:
        monkeypatch.setattr(fixtures, "_SENTENCE", fixtures._SENTENCE + "\n" + fixtures._SENTENCE)
        original = window_prefix.plan_approved_work_units

        def two_leaves(*args, **kwargs):
            return tuple(child for root in original(*args, **kwargs) for child in split_work_unit(root))

        monkeypatch.setattr(window_prefix, "plan_approved_work_units", two_leaves)

    def mutate(candidates, work_unit):
        owner = candidates[0]
        if qualified:
            owner["local_id"] = f"slice-{work_unit.slice_start}-{work_unit.slice_end}::record-1"
            candidates[2]["source_local_id"] = owner["local_id"]
        if business:
            owner["identity_key"] = {"property:records.reading": "record"}
        candidates[1]["local_id"] = f"subject-{work_unit.slice_start}"
        candidates[2]["target_local_id"] = candidates[1]["local_id"]
        candidates.append({
            "candidate_kind": "property", "owner_local_id": owner["local_id"],
            "observed_property": "Reading", "value": "record", "normalized_value": "record",
            "temporal_key": None, "anchor": dict(owner["anchors"][0]),
        })
        if duplicate:
            other = dict(owner)
            start = work_unit.slice_start + work_unit.text.index("record")
            other.update(label="record", aliases=["different payload"], anchors=[{
                "span_start": start, "span_end": start + len("record"), "quote": "record",
            }])
            candidates.append(other)
        return candidates

    l1, domain, _ = fixtures._pipeline(
        tmp_path, "records", mutate=mutate,
        type_properties={"semantic-type:records.record": ({
            "property_id": "property:records.reading", "display_name": "Reading",
            "value_type": "string", "required": False,
        },)},
        identity_business_keys={"semantic-type:records.record": ("property:records.reading",)} if business else None,
    )
    return fixtures._l3(tmp_path, l1, domain)


@pytest.mark.parametrize("duplicate", [False, True])
def test_raw_l2_to_l4_never_publishes_colliding_fallbacks_or_dependents(tmp_path, monkeypatch, duplicate):
    l3 = _split_pipeline(tmp_path, monkeypatch, duplicate=duplicate)
    records = [r for r in l3.candidate_results if r.approved_semantic_id == "semantic-type:records.record"]
    assert len(records) == 2
    assert len({r.semantic_id for r in records}) == 1
    assert all(r.current_state == "rejected" and "IDENTITY_POLICY_VIOLATION" in r.reason_codes for r in records)
    dependents = [r for r in l3.candidate_results if r.candidate_kind in ("property", "relationship")]
    assert dependents and all(r.current_state != "asserted" for r in dependents)
    assert all(r.evidence_span_ids for r in records)
    l4 = run_l4(l3, state_root=tmp_path / ".fkg" / "l4")
    assert not l4.rows.semantic_asserted_properties
    assert not l4.rows.semantic_asserted_relationships
    assert not ({r.semantic_id for r in records} & {r["entity_id"] for r in l4.rows.semantic_asserted_entities})
    assert all(r.candidate_id in {a["candidate_id"] for a in l4.rows.audit_candidates} for r in records)
    shared = stage._build_shared_context(l3.inputs)
    batch_id = l3.inputs.leaf_batch_ids[0]
    before = stage._leaf_fingerprint(inputs=l3.inputs, shared=shared, batch_id=batch_id)
    binding = [v for v in stage._verifier_binding() if v != "source-local-identity-collision/1.0.0"]
    with monkeypatch.context() as patch:
        patch.setattr(stage, "_verifier_binding", lambda: binding)
        assert stage._leaf_fingerprint(inputs=l3.inputs, shared=shared, batch_id=batch_id) != before


def test_qualified_references_publish_distinct_entities_and_dependents(tmp_path, monkeypatch):
    l3 = _split_pipeline(tmp_path, monkeypatch, qualified=True)
    records = [r for r in l3.candidate_results if r.approved_semantic_id == "semantic-type:records.record"]
    assert len(records) == 2 and len({r.semantic_id for r in records}) == 2
    assert all(r.current_state == "asserted" for r in l3.candidate_results)
    l4 = run_l4(l3, state_root=tmp_path / ".fkg" / "l4")
    assert len(l4.rows.semantic_asserted_properties) == 2
    assert len(l4.rows.semantic_asserted_relationships) == 2


def test_persisted_business_key_can_still_assert_one_entity_across_leaves(tmp_path, monkeypatch):
    l3 = _split_pipeline(tmp_path, monkeypatch, business=True)
    records = [r for r in l3.candidate_results if r.approved_semantic_id == "semantic-type:records.record"]
    assert len(records) == 2 and len({r.semantic_id for r in records}) == 1
    assert all(r.current_state == "asserted" and r.identity_witness_kind == "persisted_business_key" for r in records)
