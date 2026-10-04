"""Label derivation for asserted entities.

The graph exposed only opaque content hashes, so no natural-language question
could identify an entity (#105). A label is derived from evidence the pipeline
has already sealed and verified, which is why these tests care most about the
label remaining *verbatim* and the selection remaining *deterministic*.
"""

from __future__ import annotations

from dataclasses import dataclass

from fabric_kg_builder.deploy.fabric_ontology_definition import (
    BASE_LABEL_PROPERTY_ID,
    LABEL_PROPERTY_NAME,
    TYPED_LABEL_COLUMN,
    _data_binding_payload,
    _entity_type_payload,
)
from fabric_kg_builder.model.arrow_schemas import (
    SEMANTIC_ASSERTED_ENTITIES_SCHEMA,
)
from fabric_kg_builder.serving.lifecycle_projection import (
    _LABEL_MAX_CHARS,
    _derive_label,
)


@dataclass(frozen=True)
class _Span:
    quote: str


def _evidence(**quotes: str) -> dict[str, _Span]:
    return {span_id: _Span(quote) for span_id, quote in quotes.items()}


def test_shortest_quote_is_selected_as_the_tightest_mention() -> None:
    evidence = _evidence(
        s_long="Remove the display module before continuing with the repair",
        s_short="Display Module",
        s_mid="the display module assembly",
    )
    label, span_id = _derive_label(("s_long", "s_short", "s_mid"), evidence)

    assert label == "Display Module"
    assert span_id == "s_short"


def test_label_is_verbatim_apart_from_whitespace_normalisation() -> None:
    # Casing and punctuation must survive: the value's worth is that it is
    # literally what the source document says.
    evidence = _evidence(s1="  Anti-static\n wrist  strap ")
    label, _ = _derive_label(("s1",), evidence)

    assert label == "Anti-static wrist strap"


def test_selection_is_stable_when_lengths_tie() -> None:
    # Equal-length quotes must not let dict or set ordering leak into row_hash.
    evidence = _evidence(s_b="aaaa", s_a="bbbb")
    first, first_id = _derive_label(("s_b", "s_a"), evidence)
    second, second_id = _derive_label(("s_a", "s_b"), evidence)

    assert (first, first_id) == (second, second_id)
    assert first_id == "s_a"


def test_prose_yields_null_rather_than_a_truncated_sentence() -> None:
    # A cut-off sentence would read like a name without being one, so the
    # contract is null instead of a plausible-looking placeholder.
    evidence = _evidence(s1="x" * (_LABEL_MAX_CHARS + 1))
    assert _derive_label(("s1",), evidence) == (None, None)


def test_a_usable_span_still_wins_when_others_are_prose() -> None:
    evidence = _evidence(s_prose="y" * 400, s_ok="PSA strips")
    label, span_id = _derive_label(("s_prose", "s_ok"), evidence)

    assert (label, span_id) == ("PSA strips", "s_ok")


def test_missing_or_blank_evidence_is_null_not_empty_string() -> None:
    assert _derive_label((), {}) == (None, None)
    assert _derive_label(("absent",), {}) == (None, None)
    assert _derive_label(("s1",), _evidence(s1="   ")) == (None, None)


def test_label_columns_are_nullable_in_the_sealed_schema() -> None:
    # Nullability is the schema-level expression of "null, never a placeholder".
    for name in ("label", "label_evidence_span_id"):
        assert SEMANTIC_ASSERTED_ENTITIES_SCHEMA.field(name).nullable


def test_ontology_declares_and_binds_the_label_property() -> None:
    entity_type = {
        "id": "1001",
        "canonical_semantic_type_id": "semantic:device",
        "properties": (),
    }
    payload = _entity_type_payload(
        entity_type,
        identity_property_id="91001",
        label_property_id="81001",
    )
    names = [prop["name"] for prop in payload["properties"]]
    assert names == ["id", LABEL_PROPERTY_NAME]

    binding = _data_binding_payload(
        entity_type,
        identity_property_id="91001",
        workspace_id="ws",
        lakehouse_id="lh",
        lakehouse_schema=None,
        table_name="t",
        identity_column="__canonical_id",
        label_property_id="81001",
        label_column=TYPED_LABEL_COLUMN,
    )
    bound = {
        item["targetPropertyId"]: item["sourceColumnName"]
        for item in binding["dataBindingConfiguration"]["propertyBindings"]
    }
    assert bound["81001"] == TYPED_LABEL_COLUMN


def test_label_binding_is_omitted_when_no_label_column_exists() -> None:
    # Callers that have no label source must not emit a dangling binding to a
    # column Fabric would then fail to resolve.
    bare = {
        "id": "1",
        "canonical_semantic_type_id": "semantic:thing",
        "properties": (),
    }
    payload = _entity_type_payload(bare, identity_property_id="9")
    assert [prop["name"] for prop in payload["properties"]] == ["id"]

    binding = _data_binding_payload(
        bare,
        identity_property_id="9",
        workspace_id="ws",
        lakehouse_id="lh",
        lakehouse_schema=None,
        table_name="t",
        identity_column="entity_id",
    )
    assert len(
        binding["dataBindingConfiguration"]["propertyBindings"]
    ) == 1


def test_base_and_typed_label_property_ids_do_not_collide() -> None:
    # Typed ids are derived as f"8{type_id}"; the base constant must stay out of
    # that space or Fabric would see duplicate property ids.
    assert BASE_LABEL_PROPERTY_ID not in {f"8{n}" for n in range(1000, 1100)}
