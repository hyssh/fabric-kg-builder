"""L3 derives source occurrences, not new facts, from v2-sized support spans."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from fabric_kg_builder.domain.window_run_acceptance import WindowRunPrefixAcceptance
from fabric_kg_builder.enrichment.schema2_evidence import (
    EndpointGroundingRequest,
    ProposedOccurrenceAnchor,
    ground_endpoints,
)
from fabric_kg_builder.enrichment import schema2_validation_stage as stage
from tests.unit.test_schema2_evidence import _unit
from tests.unit import test_schema2_validation_stage as fixtures


def _anchor(text, start=0):
    return ProposedOccurrenceAnchor(start, start + len(text), text)


def _derive(unit, label, anchor=None, contract=None):
    return stage._scoped_label_occurrence(
        proposed_label=label,
        anchor=anchor or _anchor(unit.text),
        source_unit=unit,
        domain_contract=contract or SimpleNamespace(window_run_acceptance=None),
    )


def _shared(unit, labels, anchors=None):
    anchors = anchors or [_anchor(unit.text)] * len(labels)
    return stage._SharedContext(
        classification_by_entity={}, entity_type_by_id={},
        entity_anchor_by_key={
            (str(i), unit.source_unit_id): anchor for i, anchor in enumerate(anchors)
        },
        local_reference_index={}, local_keys_by_entity={},
        identity_conflict_entity_ids=frozenset(),
        relationship_identity_conflicts=frozenset(), entity_ids=frozenset(),
        entity_occurrence_by_key={
            (str(i), unit.source_unit_id): _derive(unit, label, anchor)
            for i, (label, anchor) in enumerate(zip(labels, anchors))
        },
        entity_context_by_key={
            (str(i), unit.source_unit_id): stage._scoped_entity_context(
                proposed_label=label, has_label=True, anchor=anchor, source_unit=unit,
                domain_contract=SimpleNamespace(window_run_acceptance=None),
            )
            for i, (label, anchor) in enumerate(zip(labels, anchors))
        },
    )


def _ground(unit, shared, start=0, end=None):
    end = len(unit.text) if end is None else end
    return stage._ground_relationship_endpoints(
        source_unit=unit, shared=shared, source_id="0", target_id="1",
        span_start=start, span_end=end,
    )


@pytest.mark.parametrize("text", [
    "Alpha drives Beta.",
    "<tr><td>Alpha</td><td>drives Beta</td></tr>",
    "<td>Alpha drives Beta.</td>",
])
def test_shared_support_resolves_two_distinct_mentions(text):
    unit = _unit(text)
    shared = _shared(unit, ["Alpha", "Beta"])
    # The old whole-context endpoint path maps both IDs onto one range.
    assert not _ground(unit, replace(shared, entity_occurrence_by_key={})).grounded
    outcome = _ground(unit, shared)
    assert outcome.grounded
    assert {unit.text[item.span_start:item.span_end] for item in outcome.occurrences} == {
        "Alpha", "Beta",
    }
    assert len({(item.span_start, item.span_end) for item in outcome.occurrences}) == 2


@pytest.mark.parametrize(("label", "text"), [
    ("Alpha", "Alpha drives Beta; Alpha is repeated."),
    ("Alpha One", "Alpha One drives Beta; Alpha\nOne is repeated."),
    ("Alpha One", "Alpha\tOne drives Beta; Alpha\nOne is repeated."),
    ("ana", "banana drives Beta."),
    ("invented alias", "Alpha drives Beta."),
    ("alpha", "Alpha drives Beta."),
    ("AlphaOne", "Alpha One drives Beta."),
    ("", "Alpha drives Beta."),
    (None, "Alpha drives Beta."),
])
def test_ambiguous_or_unsupported_label_has_no_context_quote_fallback(label, text):
    unit = _unit(text)
    shared = _shared(unit, [label, "Beta"])
    assert shared.entity_occurrence_by_key[("0", unit.source_unit_id)] is None
    assert not _ground(unit, shared).grounded
    assert stage._endpoint_terms(shared, "0", unit.source_unit_id) == ()


def test_identical_actual_occurrence_cannot_ground_distinct_entity_ids():
    unit = _unit("Alpha drives Beta.")
    assert not _ground(unit, _shared(unit, ["Alpha", "Alpha"])).grounded


def test_uniqueness_is_in_owner_context_not_entire_source():
    text = "Alpha drives Beta. Alpha drives Gamma."
    unit = _unit(text)
    anchors = [_anchor("Alpha drives Beta.")] * 2
    assert _ground(unit, _shared(unit, ["Alpha", "Beta"], anchors)).grounded


def test_narrow_relation_cannot_disambiguate_a_repeated_name_in_owner_context():
    unit = _unit("Alpha drives Beta. Alpha waits.")
    assert not _ground(
        unit, _shared(unit, ["Alpha", "Beta"]), end=len("Alpha drives Beta."),
    ).grounded


def test_source_scope_is_exact_even_when_other_source_text_is_identical():
    unit = _unit("Alpha drives Beta.")
    other = _unit(unit.text, ordinal=1)
    assert other.source_unit_id != unit.source_unit_id
    assert not _ground(other, _shared(unit, ["Alpha", "Beta"])).grounded


@pytest.mark.parametrize("anchor", [
    ProposedOccurrenceAnchor(1, 18, "Alpha drives Beta."),
    ProposedOccurrenceAnchor(0, 500, "Alpha drives Beta."),
    ProposedOccurrenceAnchor(0, 17, "Gamma drives Beta"),
    ProposedOccurrenceAnchor(-1, 16, "Alpha drives Beta."),
])
def test_mismatched_owner_range_or_quote_never_relocates(anchor):
    unit = _unit("Alpha drives Beta.")
    assert _derive(unit, "Alpha", anchor) is None
    assert not _ground(unit, _shared(unit, ["Alpha", "Beta"], [anchor, _anchor(unit.text)])).grounded


def test_label_elsewhere_cannot_escape_owner_context():
    unit = _unit("Alpha waits. Beta drives Gamma.")
    assert _derive(unit, "Beta", _anchor("Alpha waits.")) is None


def test_occurrence_outside_relation_never_relocates_to_same_label_inside():
    text = "Alpha drives Beta. Alpha drives Beta."
    unit = _unit(text)
    shared = _shared(unit, ["Alpha", "Beta"], [_anchor("Alpha drives Beta.")] * 2)
    assert not _ground(unit, shared, start=len("Alpha drives Beta. ")).grounded


def test_relation_excluding_one_endpoint_fails():
    unit = _unit("Alpha drives Beta.")
    assert not _ground(unit, _shared(unit, ["Alpha", "Beta"]), end=len("Alpha drives")).grounded


def test_whitespace_normalization_keeps_exact_unicode_codepoint_offsets():
    unit = _unit("😀 Préface. Café\t \nOne drives \u00a0Beta\n Two.")
    owner = _anchor(unit.text[len("😀 Préface. "):], len("😀 Préface. "))
    shared = _shared(unit, ["Cafe\u0301 One", "Beta Two"], [owner, owner])
    outcome = _ground(unit, shared)
    assert outcome.grounded
    occurrences = {item.endpoint_id: item for item in outcome.occurrences}
    for key, quote in [("0", "Café\t \nOne"), ("1", "Beta\n Two")]:
        item = occurrences[key]
        assert item.span_start == unit.text.index(quote)
        assert item.span_end == item.span_start + len(quote)
        anchor = shared.entity_occurrence_by_key[(key, unit.source_unit_id)]
        assert anchor.quote == unit.text[item.span_start:item.span_end] == quote
        assert anchor.model_authored_evidence_id is None


@pytest.mark.parametrize("updates", [
    {"text_content_hash": "0" * 64},
    {"offset_unit": "utf8_byte"},
])
def test_invalid_source_metadata_cannot_prove_occurrence(updates):
    original = _unit("Alpha drives Beta.")
    unit = type(original).model_construct(**{
        **original.model_dump(mode="python"), **updates,
    })
    assert _derive(unit, "Alpha") is None


@pytest.mark.parametrize("mismatch", ["range", "hash", "source"])
def test_entire_owner_context_must_be_inside_exact_approved_scope(mismatch):
    unit = _unit("Alpha drives Beta.")
    chunk = SimpleNamespace(
        source_unit_id=unit.source_unit_id, slice_start=0, slice_end=len(unit.text),
        source_text_hash=unit.text_content_hash,
    )
    if mismatch == "range":
        chunk.slice_end = len("Alpha")
    elif mismatch == "hash":
        chunk.source_text_hash = "0" * 64
    else:
        chunk.source_unit_id = "other-source"
    contract = SimpleNamespace(window_run_acceptance=WindowRunPrefixAcceptance.model_construct(
        selected_chunks=[chunk],
    ))
    assert _derive(unit, "Alpha", contract=contract) is None
    chunk.source_unit_id = unit.source_unit_id
    chunk.source_text_hash = unit.text_content_hash
    chunk.slice_end = len(unit.text)
    assert _derive(unit, "Alpha", contract=contract) == _anchor("Alpha")


def test_historical_explicit_small_anchors_still_work_without_label_carrier():
    unit = _unit("Alpha drives Beta.")
    anchors = [
        _anchor("Alpha"), _anchor("Beta", unit.text.index("Beta")),
    ]
    shared = _shared(unit, [None, None], anchors)
    contexts = {
        (str(i), unit.source_unit_id): stage._scoped_entity_context(
            proposed_label=None, has_label=False, anchor=anchor, source_unit=unit,
            domain_contract=SimpleNamespace(window_run_acceptance=None),
        )
        for i, anchor in enumerate(anchors)
    }
    assert _ground(unit, replace(
        shared, entity_occurrence_by_key={}, entity_context_by_key=contexts,
    )).grounded


@pytest.mark.parametrize(("text", "labels", "cell"), [
    ("<tr><td>Tool</td><td>Wrench</td></tr>", ("Wrench", "Wrench"), "<td>Wrench</td>"),
    ("<tr><td>Torque Wrench</td></tr>", ("Torque Wrench", "Wrench"), "<td>Torque Wrench</td>"),
    ("<tr><td>Wrench</td><td>Wrench required</td></tr>", ("Wrench", "Wrench"), "<td>Wrench</td>"),
    ("<tr><td>Torque\nWrench</td></tr>", ("Torque Wrench", "Wrench"), "<td>Torque\nWrench</td>"),
    ("<tr><td>Fastener</td><td>M123</td></tr>", ("M123", "M123"), "<td>M123</td>"),
    ("<tr><td>Consumable</td><td>Tape</td></tr>", ("Tape", "Tape"), "<td>Tape</td>"),
])
def test_reified_row_and_item_cell_preserve_legacy_explicit_context_proof(text, labels, cell):
    unit = _unit(text)
    anchors = [_anchor(text), _anchor(cell, text.index(cell))]
    shared = _shared(unit, labels, anchors)
    legacy = ground_endpoints(
        source_text=text, span_start=0, span_end=len(text),
        requests=tuple(
            EndpointGroundingRequest(endpoint_id=str(i), role=role, anchor=anchor)
            for i, (role, anchor) in enumerate(zip(("source", "target"), anchors))
        ),
    )
    assert legacy.grounded
    outcome = _ground(unit, shared)
    assert outcome.grounded
    assert outcome.occurrences == legacy.occurrences
    assert {
        (item.span_start, item.span_end) for item in outcome.occurrences
    } == {(anchor.span_start, anchor.span_end) for anchor in anchors}
    # Equal contexts cannot use the distinct row/cell proof to bless equal,
    # nested, or repeated labels.
    assert not _ground(unit, _shared(unit, labels)).grounded


@pytest.mark.parametrize("label", [None, "", "invented tool", "wrench"])
def test_modern_context_proof_cannot_bypass_an_ungrounded_label(label):
    text = "<tr><td>Tool</td><td>Wrench</td></tr>"
    unit = _unit(text)
    cell = "<td>Wrench</td>"
    shared = _shared(unit, [label, "Wrench"], [_anchor(text), _anchor(cell, text.index(cell))])
    assert shared.entity_context_by_key[("0", unit.source_unit_id)] is None
    assert not _ground(unit, shared).grounded


@pytest.mark.parametrize("legacy", [False, True])
def test_explicit_contexts_outside_relation_cannot_rebind_to_same_text_inside(legacy):
    sentence = "Alpha drives Beta."
    unit = _unit(sentence + " " + sentence)
    shared = _shared(unit, ["Alpha", "Beta"], [
        _anchor(sentence), _anchor("Beta", unit.text.index("Beta")),
    ])
    if legacy:
        shared = replace(shared, entity_occurrence_by_key={})
    assert _ground(unit, shared, end=len(sentence)).grounded
    assert not _ground(unit, shared, start=len(sentence) + 1).grounded


def test_shifted_historical_contexts_do_not_trigger_quote_relocation():
    unit = _unit("Alpha drives Beta.")
    anchors = [_anchor("Alpha", 1), _anchor("Beta", unit.text.index("Beta"))]
    shared = _shared(unit, ["Alpha", "Beta"], anchors)
    contexts = {
        (str(i), unit.source_unit_id): stage._scoped_entity_context(
            proposed_label=None, has_label=False, anchor=anchor, source_unit=unit,
            domain_contract=SimpleNamespace(window_run_acceptance=None),
        )
        for i, anchor in enumerate(anchors)
    }
    assert contexts[("0", unit.source_unit_id)] is None
    shared = replace(shared, entity_context_by_key=contexts, entity_occurrence_by_key={})
    assert not _ground(unit, shared).grounded


def test_smaller_relation_uses_unique_mentions_without_requiring_entire_owner_context():
    text = "Preamble. Alpha drives Beta. Postscript."
    unit = _unit(text)
    start = text.index("Alpha")
    outcome = _ground(
        unit, _shared(unit, ["Alpha", "Beta"]),
        start=start, end=start + len("Alpha drives Beta."),
    )
    assert outcome.grounded
    assert {text[item.span_start:item.span_end] for item in outcome.occurrences} == {"Alpha", "Beta"}


def _same_context(candidates, work_unit):
    anchor = dict(candidates[2]["anchor"])
    for candidate in candidates[:2]:
        candidate["anchors"] = [dict(anchor)]
    return candidates


def test_real_l3_shared_paragraph_relationship_asserts_and_reuses_fresh_cache(tmp_path):
    l1, domain, _ = fixtures._pipeline(tmp_path, "records", mutate=_same_context)
    result = fixtures._l3(tmp_path, l1, domain)
    relationships = [item for item in result.candidate_results if item.candidate_kind == "relationship"]
    assert relationships and all(item.current_state == "asserted" for item in relationships)
    shared = stage._build_shared_context(result.inputs)
    for (entity, source), occurrence in shared.entity_occurrence_by_key.items():
        assert occurrence is not None
        assert occurrence.span_end - occurrence.span_start < len(shared.entity_anchor_by_key[(entity, source)].quote)
    for span in result.evidence_spans:
        unit = result.inputs.source_units.require(span.source_unit_id)
        assert unit.text[span.span_start:span.span_end] == span.quote
    reused = fixtures._l3(tmp_path, l1, domain)
    assert reused.reused_leaf_count == len(reused.leaves)
    assert reused.candidate_results == result.candidate_results


@pytest.mark.parametrize("mode", ["equal-labels", "nested-labels", "repeated-label"])
def test_real_l3_keeps_contextual_reification_links_with_distinct_explicit_anchors(
    tmp_path, monkeypatch, mode,
):
    if mode == "repeated-label":
        monkeypatch.setattr(fixtures, "_SENTENCE", fixtures._SENTENCE + " A governed subject waits.")

    def mutate(candidates, work_unit):
        candidates[0]["anchors"] = [dict(candidates[2]["anchor"])]
        candidates[0]["label"] = "subject" if mode == "nested-labels" else "governed subject"
        return candidates

    l1, domain, _ = fixtures._pipeline(tmp_path, "records", mutate=mutate)
    result = fixtures._l3(tmp_path, l1, domain)
    relationships = [item for item in result.candidate_results if item.candidate_kind == "relationship"]
    assert relationships and all(item.current_state == "asserted" for item in relationships)


@pytest.mark.parametrize("mode", [
    "repeated", "same-occurrence", "overlapping-occurrences", "outside", "reverse", "opaque",
])
def test_real_l3_shared_context_still_rejects_unproved_relationships(tmp_path, monkeypatch, mode):
    if mode == "repeated":
        monkeypatch.setattr(fixtures, "_SENTENCE", fixtures._SENTENCE + " A governed record waits.")

    def mutate(candidates, work_unit):
        candidates = _same_context(candidates, work_unit)
        if mode == "same-occurrence":
            candidates[1]["label"] = candidates[0]["label"]
        elif mode == "overlapping-occurrences":
            candidates[0]["label"] = "subject"
        elif mode == "outside":
            text = "governed record"
            start = work_unit.slice_start + work_unit.text.index(text)
            candidates[2]["anchor"].update(span_start=start, span_end=start + len(text), quote=text)
        elif mode == "reverse":
            candidates[2].update(source_local_id="subject-1", target_local_id="record-1")
        elif mode == "opaque":
            candidates[0]["label"] = "unsupported invented record"
        return candidates

    l1, domain, _ = fixtures._pipeline(tmp_path, "records", mutate=mutate)
    result = fixtures._l3(tmp_path, l1, domain)
    relationships = [item for item in result.candidate_results if item.candidate_kind == "relationship"]
    assert relationships and all(item.current_state != "asserted" for item in relationships)
    expected = "DIRECTION_MISMATCH" if mode == "reverse" else "ENDPOINT_EVIDENCE_UNGROUNDED"
    assert all(expected in item.reason_codes for item in relationships)


def test_all_source_local_label_dependencies_and_occurrences_bind_context(tmp_path, monkeypatch):
    l1, domain, _ = fixtures._pipeline(tmp_path, "records", mutate=_same_context)
    inputs = stage.load_l3_inputs(
        l2_state_root=tmp_path / ".fkg" / "l2", l1_state_root=l1, domain_path=domain,
    )
    shared = stage._build_shared_context(inputs)
    batch_id = inputs.leaf_batch_ids[0]
    records = inputs.proposed_partitions[batch_id]
    owner = next(item for item in records if item.candidate_kind == "entity")
    before = shared.context_hash([], [], [owner.source_unit_id])
    changed = owner.model_copy(update={"proposed_label": "record"})
    modified = replace(inputs, proposed_partitions={
        **inputs.proposed_partitions,
        batch_id: tuple(changed if item is owner else item for item in records),
    })
    after = stage._build_shared_context(modified)
    assert after.context_hash([], [], [owner.source_unit_id]) != before
    assert stage._leaf_fingerprint(inputs=inputs, shared=shared, batch_id=batch_id) != (
        stage._leaf_fingerprint(inputs=inputs, shared=after, batch_id=batch_id)
    )
    assert after.context_hash([], [], ["unrelated-source"]) == shared.context_hash([], [], ["unrelated-source"])
    invalidated_context = replace(shared, entity_context_by_key={
        **shared.entity_context_by_key, (owner.semantic_id, owner.source_unit_id): None,
    })
    assert invalidated_context.context_hash([], [], [owner.source_unit_id]) != before
    assert "scoped-label-occurrence/1.1.0" in stage._verifier_binding()
    current_input = stage.l3_input_fingerprint(inputs)
    current_leaf = stage._leaf_fingerprint(inputs=inputs, shared=shared, batch_id=batch_id)
    historical_binding = [
        "scoped-label-occurrence/1.0.0" if item == "scoped-label-occurrence/1.1.0" else item
        for item in stage._verifier_binding()
    ]
    with monkeypatch.context() as patch:
        patch.setattr(stage, "_verifier_binding", lambda: historical_binding)
        assert stage.l3_input_fingerprint(inputs) != current_input
        assert stage._leaf_fingerprint(inputs=inputs, shared=shared, batch_id=batch_id) != current_leaf

    # A second carrier must not make occurrence selection depend on leaf order.
    duplicate = replace(inputs, proposed_partitions={
        **inputs.proposed_partitions, batch_id: (*records, changed),
    })
    conflicted = stage._build_shared_context(duplicate)
    assert conflicted.entity_occurrence_by_key[(owner.semantic_id, owner.source_unit_id)] is None
    reversed_inputs = replace(duplicate, proposed_partitions={
        **duplicate.proposed_partitions, batch_id: tuple(reversed(duplicate.proposed_partitions[batch_id])),
    })
    assert stage._build_shared_context(reversed_inputs).entity_occurrence_by_key == conflicted.entity_occurrence_by_key
