"""Reference scope is response-local; it must not redefine canonical identity."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from fabric_kg_builder.contracts.base import canonical_json, deterministic_contract_id
from fabric_kg_builder.domain.models import DomainPropertyV2
from fabric_kg_builder.enrichment import schema2_validation_stage as stage
from fabric_kg_builder.enrichment.schema2_evidence import (
    L3StageError, SourceUnitIndex, verify_and_mint_extraction_span,
)
from fabric_kg_builder.enrichment.schema2_extraction import (
    build_candidate_batch, compile_closed_vocabulary,
)
from tests.unit import test_schema2_evidence as evidence
from tests.unit import test_schema2_extraction as extraction
from tests.unit import test_schema2_validation_stage as fixtures


def _record(unit, *, entity_id, type_id, local="e1", work="work:one", quote=None):
    quote = unit.text if quote is None else quote
    start = unit.text.index(quote)
    return stage.ProposedCandidateView(
        input_candidate_id=f"input:{entity_id}:{work}",
        candidate_id=f"candidate:{entity_id}:{work}",
        candidate_version_id=f"version:{entity_id}:{work}",
        candidate_kind="entity", semantic_id=entity_id,
        approved_semantic_id=type_id, observed_term=type_id,
        source_unit_id=unit.source_unit_id, work_unit_id=work, local_reference=local,
        classification_version_id=None,
        proposed_anchor=stage.ProposedAnchorView(
            span_start=start, span_end=start + len(quote), quote=quote,
        ),
        proposed_label=quote, payload_hash="a" * 64,
    )


def _inputs(unit, hierarchy, records):
    return SimpleNamespace(
        source_units=SourceUnitIndex([unit]), hierarchy=hierarchy,
        domain_contract=SimpleNamespace(window_run_acceptance=None),
        leaf_batch_ids=("leaf",), proposed_partitions={"leaf": tuple(records)},
        batch_by_id={"leaf": SimpleNamespace(identity=unit.identity)},
    )


def _resolve(shared, record, *, work=None, source=None, entity_id=None):
    return stage._resolve_endpoint(
        shared=shared, entity_id=entity_id or record.semantic_id,
        source_unit_id=source or record.source_unit_id,
        work_unit_id=record.work_unit_id if work is None else work,
    )


def _hierarchy(properties=()):
    return evidence._compiled((
        evidence._entity("semantic-type:test.owner", policy=evidence._policy("owner"), properties=properties),
        evidence._entity("semantic-type:test.other", policy=evidence._policy("other")),
    ), (
        evidence._relationship(
            "relationship-type:test.link",
            sources=("semantic-type:test.owner",), targets=("semantic-type:test.other",),
        ),
    ))


@pytest.mark.parametrize("local", ["e1", "e13"])
def test_reused_local_ids_resolve_only_the_exact_current_work_unit_owner(local):
    unit = evidence._unit("Alpha Beta")
    first = _record(unit, entity_id="entity:one", type_id="semantic-type:test.owner", local=local, quote="Alpha")
    second = _record(unit, entity_id="entity:two", type_id="semantic-type:test.other", local=local.upper(), work="work:two", quote="Beta")
    shared = stage._build_shared_context(_inputs(unit, _hierarchy(), [first, second]))
    assert _resolve(shared, first) == ("entity:one", "semantic-type:test.owner", ())
    assert _resolve(shared, second) == ("entity:two", "semantic-type:test.other", ())
    assert _resolve(shared, first, entity_id=second.semantic_id)[2] == ("ENDPOINT_UNRESOLVED",)
    assert _resolve(shared, first, work="work:missing")[2] == ("ENDPOINT_UNRESOLVED",)
    assert _resolve(shared, first, work="")[2] == ("ENDPOINT_UNRESOLVED",)
    assert _resolve(shared, first, source="source:foreign")[2] == ("ENDPOINT_UNRESOLVED",)
    assert shared.local_reference_index[(unit.source_unit_id, "work:one", local)] == ("entity:one",)


@pytest.mark.parametrize("second_local", ["e1", "E1"])
def test_same_work_unit_duplicate_local_references_stay_unresolved(second_local):
    unit = evidence._unit("Alpha Beta")
    first = _record(unit, entity_id="entity:one", type_id="semantic-type:test.owner", quote="Alpha")
    second = _record(unit, entity_id="entity:two", type_id="semantic-type:test.other", local=second_local, quote="Beta")
    shared = stage._build_shared_context(_inputs(unit, _hierarchy(), [first, second]))
    assert _resolve(shared, first)[2] == ("ENDPOINT_UNRESOLVED",)
    assert _resolve(shared, second)[2] == ("ENDPOINT_UNRESOLVED",)


def test_no_reference_in_current_unit_has_no_source_wide_or_legacy_fallback():
    unit = evidence._unit("Alpha")
    owner = _record(unit, entity_id="entity:one", type_id="semantic-type:test.owner", local=None)
    shared = stage._build_shared_context(_inputs(unit, _hierarchy(), [owner]))
    assert _resolve(shared, owner)[2] == ("ENDPOINT_UNRESOLVED",)
    payload = stage.proposed_candidate_payload(owner)
    del payload["work_unit_id"]
    with pytest.raises(ValidationError):
        stage.ProposedCandidateView.model_validate(payload)


def test_conflicting_same_unit_mentions_do_not_hide_behind_one_canonical_id():
    unit = evidence._unit("Alpha Beta")
    first = _record(unit, entity_id="entity:one", type_id="semantic-type:test.owner", quote="Alpha")
    second = _record(unit, entity_id="entity:one", type_id="semantic-type:test.owner", quote="Beta")
    shared = stage._build_shared_context(_inputs(unit, _hierarchy(), [first, second]))
    assert _resolve(shared, first)[2] == ("ENDPOINT_UNRESOLVED",)


# Eight exact scalar/quote pairs from the sealed 124-root diagnosis. These are
# portable regression fixtures, not reads from live or historical run state.
@pytest.mark.parametrize(("property_id", "local", "value", "quote"), [
    ("property:consumable.name", "e13", "Cleaning swabs", "o Cleaning swabs"),
    ("property:device_model.name", "e1", "Model 1964", "<th>Model 1964</th>"),
    ("property:procedure.title", "e12", "Cosmetic Plate removal",
     "· Remove Cosmetic Plate - Refer to Cosmetic Plate removal on (page 19) for details."),
    ("property:service_part.part_number", "e9", "M1288973", "o M1288973 Foam x 1 (T3 Shield Foam #1)"),
    ("property:service_part.name", "e9", "Foam", "o M1288973 Foam x 1 (T3 Shield Foam #1)"),
    ("property:tool.name", "e7", "Anti-static wrist strap (1 MOhm resistance)",
     "o Anti-static wrist strap (1 MOhm resistance)"),
    ("property:procedure_step.title", "e6", "Remove Feet",
     "· Remove Feet - Refer to Feet removal on (page 16) for details."),
    ("property:procedure_step.instruction_text", "e6",
     "Remove Feet - Refer to Feet removal on (page 16) for details.",
     "· Remove Feet - Refer to Feet removal on (page 16) for details."),
])
def test_diagnosed_eight_properties_validate_without_changing_values(property_id, local, value, quote):
    unit = evidence._unit(quote + "\nOther")
    hierarchy = _hierarchy((DomainPropertyV2(
        property_id=property_id, display_name="Value", value_type="string", required=True,
    ),))
    owner = _record(unit, entity_id="entity:one", type_id="semantic-type:test.owner", local=local, quote=quote)
    other = _record(unit, entity_id="entity:two", type_id="semantic-type:test.other", local=local, work="work:two", quote="Other")
    shared = stage._build_shared_context(_inputs(unit, hierarchy, [owner, other]))
    prop = owner.model_copy(update={
        "candidate_kind": "property", "candidate_id": "property-candidate:one",
        "approved_semantic_id": property_id, "proposed_owner_entity_id": owner.semantic_id,
        "local_reference": None, "value_json": canonical_json(value),
        "normalized_value_json": canonical_json(value),
        "semantic_id": deterministic_contract_id("property-observation", {
            "entity_id": owner.semantic_id, "property_id": property_id,
            "normalized_value": value, "temporal_key": None,
        }),
    })
    span = verify_and_mint_extraction_span(
        source_unit=unit, anchor=prop.proposed_anchor.to_anchor(), verified_at_utc=evidence._NOW,
    ).span
    kwargs = dict(record=prop, hierarchy=hierarchy, shared=shared, source_unit=unit, evidence_span=span)
    assert stage._property_reasons(**kwargs, asserted_entity_ids={owner.semantic_id}) == ((), owner.semantic_id)
    assert prop.value_json == prop.normalized_value_json == canonical_json(value)
    assert "ENDPOINT_UNRESOLVED" in stage._property_reasons(**kwargs, asserted_entity_ids=set())[0]
    foreign = prop.model_copy(update={"work_unit_id": other.work_unit_id})
    assert "ENDPOINT_UNRESOLVED" in stage._property_reasons(
        **{**kwargs, "record": foreign}, asserted_entity_ids={owner.semantic_id},
    )[0]


def test_both_relationship_endpoints_use_the_proposing_work_unit():
    unit = evidence._unit("Alpha links Beta. Gamma links Delta.")
    hierarchy = _hierarchy()
    first = _record(unit, entity_id="entity:one", type_id="semantic-type:test.owner", local="e1", quote="Alpha")
    second = _record(unit, entity_id="entity:two", type_id="semantic-type:test.other", local="e13", quote="Beta")
    others = [
        _record(unit, entity_id="entity:three", type_id="semantic-type:test.other", local="e1", work="work:two", quote="Gamma"),
        _record(unit, entity_id="entity:four", type_id="semantic-type:test.owner", local="e13", work="work:two", quote="Delta"),
    ]
    shared = stage._build_shared_context(_inputs(unit, hierarchy, [first, second, *others]))
    rel = first.model_copy(update={
        "candidate_kind": "relationship", "approved_semantic_id": "relationship-type:test.link",
        "proposed_source_entity_id": first.semantic_id, "proposed_target_entity_id": second.semantic_id,
        "proposed_anchor": stage.ProposedAnchorView(span_start=0, span_end=len(unit.text), quote=unit.text),
    })
    span = verify_and_mint_extraction_span(
        source_unit=unit, anchor=rel.proposed_anchor.to_anchor(), verified_at_utc=evidence._NOW,
    ).span
    kwargs = dict(hierarchy=hierarchy, shared=shared, source_unit=unit, anchor=rel.proposed_anchor.to_anchor(), evidence_span=span)
    assert stage._relationship_reasons(record=rel, **kwargs)[0] == ()
    for field, foreign_id in [
        ("proposed_source_entity_id", others[1].semantic_id),
        ("proposed_target_entity_id", others[0].semantic_id),
    ]:
        assert "ENDPOINT_UNRESOLVED" in stage._relationship_reasons(
            record=rel.model_copy(update={field: foreign_id}), **kwargs,
        )[0]


@pytest.fixture
def pipeline(tmp_path):
    l1, domain, _ = fixtures._pipeline(tmp_path, "records")
    return fixtures._l3(tmp_path, l1, domain)


@pytest.mark.parametrize("mode", ["one-record-work", "all-records-work", "one-record-source", "all-records-source"])
def test_forged_scope_metadata_cannot_rebind_a_sealed_leaf(pipeline, mode):
    inputs = pipeline.inputs
    batch_id = inputs.leaf_batch_ids[0]
    records = list(inputs.proposed_partitions[batch_id])
    field = "work_unit_id" if mode.endswith("work") else "source_unit_id"
    for i in range(len(records) if mode.startswith("all") else 1):
        records[i] = records[i].model_copy(update={field: "forged:scope"})
    with pytest.raises(L3StageError, match="L3_INPUT_MANIFEST_INVALID"):
        stage._validate_leaf_work_unit_binding(inputs.batch_by_id[batch_id], records)
    changed = replace(inputs, proposed_partitions={**inputs.proposed_partitions, batch_id: tuple(records)})
    with pytest.raises(L3StageError, match="L3_INPUT_MANIFEST_INVALID"):
        stage._validate_accounting(changed)


def test_cache_binds_reference_work_unit_scope_and_verifier(pipeline, monkeypatch):
    inputs = pipeline.inputs
    shared = stage._build_shared_context(inputs)
    batch_id = inputs.leaf_batch_ids[0]
    record = next(r for r in inputs.proposed_partitions[batch_id] if r.candidate_kind == "entity")
    key = (record.source_unit_id, record.work_unit_id, record.local_reference.casefold())
    old = shared.context_hash([], [], [record.source_unit_id])
    index = dict(shared.local_reference_index)
    index[(key[0], "other:work", key[2])] = index.pop(key)
    changed = replace(shared, local_reference_index=index)
    assert changed.context_hash([], [], [record.source_unit_id]) != old
    assert stage._leaf_fingerprint(inputs=inputs, shared=shared, batch_id=batch_id) != (
        stage._leaf_fingerprint(inputs=inputs, shared=changed, batch_id=batch_id)
    )
    marker = "work-unit-local-reference/1.0.0"
    assert marker in stage._verifier_binding()
    before = stage.l3_input_fingerprint(inputs)
    old_binding = [v for v in stage._verifier_binding() if v != marker]
    monkeypatch.setattr(stage, "_verifier_binding", lambda: old_binding)
    assert stage.l3_input_fingerprint(inputs) != before


def test_frozen_l2_same_type_same_local_id_collision_is_explicitly_documented(pipeline):
    domain = pipeline.inputs.domain_contract
    unit = evidence._unit("Alpha record. Beta record.")

    def build(work, quote):
        start = unit.text.index(quote)
        candidates = [{
            "candidate_kind": "entity", "local_id": "e1", "observed_type": "Record",
            "label": quote, "identity_key": {}, "stable_source_identity": None,
            "anchors": [{"span_start": start, "span_end": start + len(quote), "quote": quote}],
        }]
        return build_candidate_batch(
            candidates, vocabulary=compile_closed_vocabulary(domain), contract=domain,
            authority=extraction._authority(domain), base_identity=extraction._identity(),
            source_unit_id=unit.source_unit_id, work_unit_id=work,
            classifier_version="1.0.0", prompt_hash="f" * 64, model_hash="1" * 64,
            extractor_name="l2-schema-constrained", extractor_version="1.0.0",
            occurred_at_utc=evidence._NOW,
        )

    first = build("work:one", "Alpha record.")
    # Work-unit scope is not an input to the frozen stable-source entity ID.
    second = build("work:two", "Beta record.")
    assert first.proposed_candidates[0].approved_semantic_id == "semantic-type:records.record"
    assert first.proposed_candidates[0].proposed_anchor != second.proposed_candidates[0].proposed_anchor
    assert first.batch.extraction_candidate_batch_id != second.batch.extraction_candidate_batch_id
    assert [r.semantic_id for r in first.proposed_candidates if r.candidate_kind == "entity"] == [
        r.semantic_id for r in second.proposed_candidates if r.candidate_kind == "entity"
    ]
    stage._validate_leaf_work_unit_binding(first.batch, first.proposed_candidates)
    stage._validate_leaf_work_unit_binding(second.batch, second.proposed_candidates)
