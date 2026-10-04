"""Synthetic, contract-bound formatting examples for opt-in approved extraction."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .schema2_evidence import _VALUE_TYPE_CHECKS, property_scalar_grounding_reasons
from .schema2_extraction import RawCandidateResponse, compile_closed_vocabulary

PLACEHOLDER = "{{a few shot}}"


class SyntheticExample(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    synthetic: Literal[True]
    source_text: str
    slice_start: int = Field(ge=0)
    slice_end: int = Field(ge=0)
    response: RawCandidateResponse


def _choices(contract, vocabulary):
    concrete = {
        item["type_id"]: item for item in vocabulary.prompt_payload["entity_types"]
        if not item["abstract"]
    }
    relationships = []
    closure = contract.hierarchy_closure
    for rel in sorted(contract.candidate_model.relationship_types, key=lambda item: item.relationship_type_id):
        sources = sorted(set(closure.compatible_source_type_ids_by_relationship[rel.relationship_type_id]) & concrete.keys())
        targets = sorted(set(closure.compatible_target_type_ids_by_relationship[rel.relationship_type_id]) & concrete.keys())
        if sources and targets:
            relationships.append((rel, sources[0], targets[0]))
    return concrete, relationships


def generate_examples(contract, *, unique_quotes=False) -> list[dict]:
    """Deterministic structural fixtures, never observations of a real SourceUnit."""
    vocabulary = compile_closed_vocabulary(contract)
    concrete, relationships = _choices(contract, vocabulary)
    selected = list(relationships[0][1:]) if relationships else sorted(concrete)[:1]
    with_properties = sorted(key for key, item in concrete.items() if item["effective_properties"])
    if with_properties and not any(concrete[key]["effective_properties"] for key in selected):
        selected.append(with_properties[0])
    text = ""
    candidates = []
    offset = 100
    illustrated_value_types = set()

    def append(quote):
        nonlocal text
        if unique_quotes and quote in text:
            start = offset + text.index(quote)
            return {"span_start": start, "span_end": start + len(quote),
                    "quote": quote, "model_authored_evidence_id": None}
        start = offset + len(text)
        text += quote + "\n"
        return {"span_start": start, "span_end": start + len(quote),
                "quote": quote, "model_authored_evidence_id": None}

    for index, type_id in enumerate(selected, 1):
        definition = concrete[type_id]
        label = f"Synthetic example {index}"
        local_id = f"example-{index}"
        anchor = append(f"{label} is a synthetic instance of {type_id}.")
        values = {
            "string": label, "integer": index, "number": index + 0.5,
            "boolean": True, "date": "2000-01-01", "datetime": "2000-01-01T00:00:00Z",
        }
        effective = sorted(definition["effective_properties"], key=lambda item: item["property_id"])
        policy = definition["identity_key_policy"]
        identity_fields = policy["business_key_fields"] if policy["key_mode"] == "business_key" else []
        # Identity properties remain explicit. Optional fields illustrate a scalar
        # shape once across the example, not once for every field or endpoint.
        properties = [prop for prop in effective if prop["property_id"] in identity_fields]
        illustrated_value_types.update(prop["value_type"] for prop in properties)
        for prop in effective:
            if prop["value_type"] not in illustrated_value_types:
                properties.append(prop)
                illustrated_value_types.add(prop["value_type"])
        property_values = {prop["property_id"]: values[prop["value_type"]] for prop in properties}
        identity = {}
        identity_anchors = []
        for field in identity_fields:
            value = property_values.get(field, label)
            identity[field] = value if isinstance(value, str) else json.dumps(value)
            identity_anchors.append(append(f"{label}: {field} = {identity[field]}"))
        candidates.append({
            "candidate_kind": "entity", "local_id": local_id, "observed_type": type_id,
            "label": label, "aliases": [], "identity_key": identity,
            "stable_source_identity": None, "anchors": [anchor, *identity_anchors],
        })
        for prop in properties:
            value = property_values[prop["property_id"]]
            literal = value if isinstance(value, str) else json.dumps(value)
            property_anchor = append(f"{label}: {prop['property_id']} = {literal}")
            candidates.append({
                "candidate_kind": "property", "owner_local_id": local_id,
                "observed_property": prop["property_id"], "value": value,
                "normalized_value": value, "temporal_key": None, "anchor": property_anchor,
            })
    if relationships:
        rel = relationships[0][0]
        anchor = append(f"Synthetic example 1 {rel.relationship_type_id} Synthetic example 2.")
        candidates.append({
            "candidate_kind": "relationship", "source_local_id": "example-1",
            "target_local_id": "example-2", "observed_predicate": rel.relationship_type_id,
            "direction": "source_to_target", "governed_context": None,
            "member_role_id": None, "member_order": None, "anchor": anchor,
        })
    return [
        {"synthetic": True, "source_text": text, "slice_start": offset,
         "slice_end": offset + len(text), "response": {"candidates": candidates}},
        {"synthetic": True, "source_text": "", "slice_start": 0, "slice_end": 0,
         "response": {"candidates": []}},
    ]


def validate_examples(contract, examples) -> list[dict]:
    """Validate the raw envelope plus IDs, identities, ownership and exact anchors."""
    def require(condition, reason):
        if not condition:
            raise ValueError(reason)

    try:
        # A JSON round trip rejects non-JSON configuration (including NaN), not repairs it.
        serialized = json.dumps(examples, allow_nan=False)
        require(PLACEHOLDER not in serialized, "unresolved placeholder in examples")
        parsed = TypeAdapter(list[SyntheticExample]).validate_json(serialized)
        require(all(item.get("synthetic") is True for item in examples), "synthetic must be true")
        require(len(parsed) >= 2, "provide a supported-response example and an abstention example")
        vocabulary = compile_closed_vocabulary(contract)
        concrete, relationships = _choices(contract, vocabulary)
        kinds = set()
        abstention = False
        for example in parsed:
            require(example.slice_end == example.slice_start + len(example.source_text), "slice bounds")
            records = example.response.candidates
            abstention |= not records
            entities = {}

            def check_anchor(anchor):
                require(anchor is not None, "missing anchor")
                require(anchor.model_authored_evidence_id is None, "invented evidence ID")
                require(example.slice_start <= anchor.span_start < anchor.span_end <= example.slice_end,
                        "anchor outside synthetic slice")
                require(example.source_text[anchor.span_start - example.slice_start:
                                            anchor.span_end - example.slice_start] == anchor.quote,
                        "anchor quote mismatch")

            for record in records:
                kinds.add(record.candidate_kind)
                if record.candidate_kind != "entity":
                    continue
                require(record.observed_type in concrete, "unapproved or abstract entity type")
                require(record.local_id.casefold() not in entities, "duplicate local ID")
                require(record.anchors, "missing entity anchors")
                for anchor in record.anchors:
                    check_anchor(anchor)
                quotes = [anchor.quote for anchor in record.anchors]
                require(len(record.label) <= 120 and any(record.label in quote for quote in quotes),
                        "label must come from an entity quote")
                require(not record.aliases, "synthetic examples must not invent aliases")
                policy = concrete[record.observed_type]["identity_key_policy"]
                fields = set(policy["business_key_fields"]) if policy["key_mode"] == "business_key" else set()
                require(set(record.identity_key) == fields and record.stable_source_identity is None,
                        "identity policy mismatch")
                require(all(value.strip() and any(value in quote for quote in quotes)
                            for value in record.identity_key.values()), "ungrounded business key")
                entities[record.local_id.casefold()] = record
            for record in records:
                if record.candidate_kind == "entity":
                    continue
                check_anchor(record.anchor)
                if record.candidate_kind == "property":
                    owner = entities.get(record.owner_local_id.casefold())
                    require(owner is not None, "unresolved property owner")
                    props = vocabulary.properties_by_type_and_alias[owner.observed_type]
                    prop = props.get(record.observed_property.casefold())
                    require(prop is not None and prop.property_id == record.observed_property,
                            "unapproved property or incorrect ownership")
                    require(_VALUE_TYPE_CHECKS[prop.value_type](record.value)
                            and _VALUE_TYPE_CHECKS[prop.value_type](record.normalized_value),
                            "property value type mismatch")
                    require(record.value == record.normalized_value and record.temporal_key is None,
                            "synthetic examples must not invent normalization or temporal context")
                    require(not property_scalar_grounding_reasons(
                        value_json=json.dumps(record.value, ensure_ascii=False, separators=(",", ":")),
                        normalized_value_json=json.dumps(record.normalized_value, ensure_ascii=False, separators=(",", ":")),
                        quote=record.anchor.quote,
                    ), "ungrounded property value or unsupported normalization")
                else:
                    source = entities.get(record.source_local_id.casefold())
                    target = entities.get(record.target_local_id.casefold())
                    require(source is not None and target is not None, "unresolved relationship endpoints")
                    closure = contract.hierarchy_closure
                    rel_id = record.observed_predicate
                    require(rel_id in closure.compatible_source_type_ids_by_relationship,
                            "unapproved relationship")
                    require(source.observed_type in closure.compatible_source_type_ids_by_relationship[rel_id]
                            and target.observed_type in closure.compatible_target_type_ids_by_relationship[rel_id],
                            "incompatible relationship endpoint types")
                    require(record.direction == "source_to_target" and record.governed_context is None
                            and record.member_role_id is None and record.member_order is None,
                            "unsupported synthetic relationship context")
                    require(source.label in record.anchor.quote and target.label in record.anchor.quote,
                            "relationship quote must identify both endpoints")
        required = {"entity"} if concrete else set()
        if any(item["effective_properties"] for item in concrete.values()):
            required.add("property")
        if relationships:
            required.add("relationship")
        require(required <= kinds and abstention, "missing supported record kinds or abstention")
        return [example.model_dump(mode="json") for example in parsed]
    except (ValueError, TypeError) as exc:
        raise ValueError(f"APPROVED_FEW_SHOT_INVALID: {exc}") from exc


def render_system_prompt(template: str, contract, *, few_shot=None, quote_only=False,
                         source_spans=False, source_span_mode=None) -> tuple[str, list[dict]]:
    """Replace exactly one slot; invalid explicit replacements never use defaults."""
    if template.count(PLACEHOLDER) != 1:
        raise ValueError("APPROVED_FEW_SHOT_INVALID: template must contain exactly one {{a few shot}}")
    if quote_only and source_spans:
        raise ValueError("APPROVED_FEW_SHOT_INVALID: conflicting anchor projections")
    if source_span_mode is not None and not source_spans:
        raise ValueError("APPROVED_FEW_SHOT_INVALID: source span mode requires source span projection")
    supplied = generate_examples(contract, unique_quotes=quote_only) if few_shot is None else few_shot
    examples = validate_examples(contract, supplied)
    if quote_only or source_spans:
        from .approved_quote_anchors import quote_only_examples

        # Canonical proposal parsing strips boundary whitespace. Never silently
        # change the caller's example quotation while rendering another contract.
        for original, parsed in zip(supplied, examples):
            for before, after in zip(original["response"]["candidates"], parsed["response"]["candidates"]):
                key = "anchors" if before["candidate_kind"] == "entity" else "anchor"
                before_anchors = before[key] if key == "anchors" else [before[key]]
                after_anchors = after[key] if key == "anchors" else [after[key]]
                if any(a["quote"] != b["quote"] for a, b in zip(before_anchors, after_anchors)):
                    raise ValueError("APPROVED_FEW_SHOT_INVALID: quote boundary whitespace")
        if quote_only:
            examples = quote_only_examples(examples)
        else:
            from .approved_quote_anchors import SOURCE_SPANS_MODE, source_span_examples

            examples = source_span_examples(
                examples, anchor_mode=SOURCE_SPANS_MODE if source_span_mode is None else source_span_mode,
            )
    return template.replace(PLACEHOLDER, json.dumps(
        examples, separators=(",", ":"), ensure_ascii=False, sort_keys=True,
    )), examples
