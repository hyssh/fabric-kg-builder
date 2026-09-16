from dataclasses import replace
from types import SimpleNamespace

import pytest

from fabric_kg_builder.enrichment.schema2_evidence import ProposedOccurrenceAnchor
from fabric_kg_builder.enrichment.schema2_evidence import property_scalar_grounding_reasons
from fabric_kg_builder.contracts.base import canonical_json
from fabric_kg_builder.enrichment.schema2_validation_stage import _SharedContext, _property_inside_owner


def _anchor(text, start=0):
    return ProposedOccurrenceAnchor(
        span_start=start, span_end=start + len(text), quote=text,
    )


def _check(text, field, *, owner=None, source="source:one", others=None):
    anchors = {("owner", "source:one"): owner or _anchor(text)}
    anchors.update(others or {})
    shared = SimpleNamespace(
        entity_anchor_by_key=anchors,
        entity_type_by_id={entity: "type:part" for entity, _ in anchors},
    )
    start = text.index(field)
    return _property_inside_owner(
        shared=shared, owner_id="owner", source_unit_id=source,
        source_text=text, span_start=start, span_end=start + len(field),
    )


@pytest.mark.parametrize(("text", "field"), [
    ("<tr><td>Screws</td><td>M123</td></tr>", "M123"),
    ("1. Disconnect power\nUnplug the cable.", "Unplug the cable."),
    ("IMPORTANT\nStop if the battery is damaged.", "Stop if the battery is damaged."),
    ("Model 1964", "Model 1964"),
    ("Outil \u00e9lectrique\nR\u00e9f. A1", "R\u00e9f. A1"),
])
def test_field_can_be_smaller_than_its_owner(text, field):
    assert _check(text, field)


def test_same_text_in_other_source_is_not_owner_proof():
    assert not _check("Screws M123", "M123", source="source:two")


def test_disjoint_or_partial_owner_is_not_proof():
    assert not _check("Screws M123; Tape M456", "M456", owner=_anchor("Screws M123"))
    assert not _check("Screws M123", "M123", owner=_anchor("Screws M1"))


def test_anchor_offsets_must_match_the_exact_source():
    assert not _check("Screws M123", "M123", owner=_anchor("Different!!"))


def test_multiple_table_rows_need_finer_owner_evidence():
    text = "<tr><td>A M123</td></tr><tr><td>B M456</td></tr>"
    assert not _check(text, "M456")


def test_partial_first_row_cannot_authorize_second_row_value():
    text = "<tr><td>Screws</td><td>M123</td></tr><tr><td>Tape</td><td>M456</td></tr>"
    start = text.index("Screws")
    assert not _check(text, "M456", owner=_anchor(text[start:], start))
    partial_end = text.index("M456") + len("M456")
    assert not _check(text, "M456", owner=_anchor(text[start:partial_end], start))


def test_competing_owner_occurrence_is_not_unique():
    text = "Screws M123"
    assert not _check(text, "M123", others={("other", "source:one"): _anchor(text)})


def test_repeated_value_elsewhere_does_not_relocate_explicit_owner():
    text = "Screws M123; Tape M123"
    assert _check(text, "M123", owner=_anchor("Screws M123"))
    assert not _check(text, "M123", owner=_anchor("Tape M123", len("Screws M123; ")))


@pytest.mark.parametrize(("value", "quote", "valid"), [
    ("left Fan FPC", "Disconnect left Fan\nFPC carefully.", True),
    ("Do not disconnect", "Do  not\tdisconnect", True),
    ("M123", "M 123", False),
    ("1.5", "1,5", False),
    ("fan", "Fan", False),
    ("Do not disconnect", "Do disconnect", False),
    (" ", "nowhitespace", False),
])
def test_scalar_layout_equivalence_is_not_semantic_correction(value, quote, valid):
    reasons = property_scalar_grounding_reasons(
        value_json=canonical_json(value),
        normalized_value_json=canonical_json(value),
        quote=quote,
    )
    assert ("PROPERTY_VALUE_UNGROUNDED" not in reasons) is valid


def test_competing_owner_dependencies_invalidate_leaf_context():
    shared = _SharedContext(
        classification_by_entity={},
        entity_type_by_id={"owner": "part", "other": "part"},
        entity_anchor_by_key={("owner", "source:one"): _anchor("Screws M123")},
        local_reference_index={}, local_keys_by_entity={},
        identity_conflict_entity_ids=frozenset(),
        relationship_identity_conflicts=frozenset(),
        entity_ids=frozenset({"owner", "other"}),
    )
    before = shared.context_hash(["owner"], [], ["source:one"])
    competing = replace(shared, entity_anchor_by_key={
        **shared.entity_anchor_by_key, ("other", "source:one"): _anchor("Screws M123"),
    })
    after = competing.context_hash(["owner"], [], ["source:one"])
    assert before != after
    changed_type = replace(competing, entity_type_by_id={"owner": "part", "other": "tool"})
    assert changed_type.context_hash(["owner"], [], ["source:one"]) != after
