"""Optional L2 wire compression; this module grants no semantic/evidence authority.

The CLI adapter substitutes shared, untrusted anchor records and converts fixed
identity pairs into RawCandidateResponse dictionaries. It preserves literal
quotes/scalars and local IDs, never verifies evidence, supplies instance facts,
normalizes values, infers identity properties, or sets lifecycle state.

Expansion order is entities, properties, relationships, retaining each array's
order and every observation. Structured governed context can be carried as an
explicit JSON-object string; literal context strings are never interpreted.
Callers must still run the ordinary RawCandidateResponse and L2/L3 validators.
Bind the separate transport hash to the model request, not in place of the
existing expanded-response/carrier schema hashes.
"""

from __future__ import annotations

import json
import math
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from fabric_kg_builder.contracts.base import canonical_sha256

COMPACT_EXTRACTION_TRANSPORT_VERSION = "l2-compact-response/1.0.0"


class CompactExtractionResponseError(ValueError):
    """A compact wire record is ambiguous or references an absent local record."""


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("text must not be blank")
    return value


def _wire_key(value: str) -> str:
    if not value or value != value.strip():
        raise ValueError("wire keys must be nonempty and have no surrounding whitespace")
    return value


Text = Annotated[str, Field(min_length=1), AfterValidator(_nonblank)]
Key = Annotated[str, Field(min_length=1), AfterValidator(_wire_key)]
Scalar = str | int | float | bool


class _WireRecord(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, str_strip_whitespace=False, allow_inf_nan=False
    )


class _Anchor(_WireRecord):
    key: Key
    start: int = Field(ge=0, description="Untrusted SourceUnit-relative Unicode codepoint offset.")
    end: int = Field(gt=0, description="Exclusive SourceUnit-relative codepoint offset.")
    quote: Text = Field(description="Exact literal source text, including whitespace; not verified evidence.")


class _IdentityField(_WireRecord):
    property_id: Key
    value: str = Field(
        description="Source-grounded identity string required by RawEntityCandidate. Emit separate typed property observations as well."
    )


class _Entity(_WireRecord):
    local_id: Key
    type: Text = Field(description="Observed semantic type or source term; unknown terms are preserved.")
    label: Text
    identity: list[_IdentityField]
    anchor_keys: list[Key]
    aliases: list[str] = Field(default_factory=list)
    stable_source_identity: str | None = None


class _Property(_WireRecord):
    owner: Key
    property: Text = Field(description="Observed property ID or term; must not be inferred from identity fields.")
    value: Scalar
    normalized_value: Scalar = Field(description="Model-proposed normalized scalar, passed unchanged to ordinary validation.")
    temporal_key: str | None = None
    anchor_key: Key | None = None


class _Relationship(_WireRecord):
    source: Key
    target: Key
    predicate: Text
    direction: Literal["source_to_target", "reverse", "unknown"]
    context: str | None = Field(
        default=None,
        description="Literal governed-context string, copied unchanged. Null when context_json is used.",
    )
    context_json: str | None = Field(
        default=None,
        description="Optional JSON-object text for structured governed context. No duplicate keys/nonfinite numbers. Mutually exclusive with context.",
    )
    member_role_id: str | None = None
    member_order: int | None = Field(default=None, ge=0)
    anchor_key: Key | None = None


class _CompactExtractionResponse(_WireRecord):
    anchors: list[_Anchor]
    entities: list[_Entity]
    properties: list[_Property]
    relationships: list[_Relationship]


def compact_extraction_response_schema() -> dict[str, Any]:
    """Return a fixed-field response schema without dynamic identity dictionaries."""
    schema = _CompactExtractionResponse.model_json_schema()
    definitions = schema.get("$defs", {})
    field_count = len(schema["properties"]) + sum(
        len(definition.get("properties", {})) for definition in definitions.values()
    )
    if field_count >= 100:
        raise ValueError("compact extraction schema must have fewer than 100 properties")

    def depth(value: Any, active: frozenset[str] = frozenset()) -> int:
        if not isinstance(value, dict):
            return 0
        if "$ref" in value:
            key = value["$ref"].rsplit("/", 1)[-1]
            if key in active:
                raise ValueError("compact extraction schema cannot be recursive")
            return depth(definitions[key], active | {key})
        children = [*value.get("properties", {}).values(), *value.get("anyOf", [])]
        if isinstance(value.get("items"), dict):
            children.append(value["items"])
        return int(value.get("type") in ("object", "array")) + max(
            (depth(child, active) for child in children), default=0
        )

    if depth(schema) > 5:
        raise ValueError("compact extraction schema exceeds five nesting levels")
    return schema


COMPACT_EXTRACTION_SCHEMA_HASH = canonical_sha256(compact_extraction_response_schema())
COMPACT_EXTRACTION_TRANSPORT_HASH = canonical_sha256(
    {
        "transport_version": COMPACT_EXTRACTION_TRANSPORT_VERSION,
        "schema_hash": COMPACT_EXTRACTION_SCHEMA_HASH,
        "expansion": {
            "candidate_order": ["entity", "property", "relationship"],
            "anchors": "key substitution into unverified ProposedAnchor fields only",
            "identity": "duplicate-checked property_id/value pairs; no normalization",
            "context": "literal string or explicit duplicate-checked JSON object; mutually exclusive",
            "values": "literal scalars, including whitespace and scalar types, unchanged",
            "authority": "ordinary RawCandidateResponse and L2/L3 gates still required",
        },
    }
)


def _context_object(text: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise CompactExtractionResponseError(f"Duplicate structured-context key: {key!r}")
            result[key] = value
        return result

    def nonfinite(value: str) -> Any:
        raise CompactExtractionResponseError("Structured context cannot contain nonfinite numbers")

    try:
        result = json.loads(text, object_pairs_hook=pairs, parse_constant=nonfinite)
    except (ValueError, RecursionError) as exc:
        raise CompactExtractionResponseError(f"Invalid context_json: {exc}") from exc
    if not isinstance(result, dict):
        raise CompactExtractionResponseError("context_json must encode an object, not a scalar or array")
    pending: list[Any] = [result]
    while pending:
        item = pending.pop()
        if isinstance(item, float) and not math.isfinite(item):
            raise CompactExtractionResponseError("Structured context cannot contain nonfinite numbers")
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return result


def expand_compact_response(raw: dict[str, Any]) -> dict[str, Any]:
    """Substitute wire references only, returning the ordinary response envelope.

    Duplicate/unknown local references reject the whole compact response rather
    than silently dropping observations or picking a last definition. Ranges and
    quotes are NOT matched against source text here; the evidence verifier retains
    that authority. Do not serialize through RawCandidateResponse here: its
    downstream reference trimming must not modify the transport's literal data.
    """
    wire = _CompactExtractionResponse.model_validate(raw)
    anchors: dict[str, dict[str, Any]] = {}
    for anchor in wire.anchors:
        if anchor.key in anchors:
            raise CompactExtractionResponseError(f"Duplicate anchor key: {anchor.key!r}")
        anchors[anchor.key] = {
            "span_start": anchor.start,
            "span_end": anchor.end,
            "quote": anchor.quote,
        }

    entity_ids: set[str] = set()
    for entity in wire.entities:
        if entity.local_id in entity_ids:
            raise CompactExtractionResponseError(f"Duplicate entity local_id: {entity.local_id!r}")
        entity_ids.add(entity.local_id)

    def anchor_value(key: str | None) -> dict[str, Any] | None:
        if key is None:
            return None
        if key not in anchors:
            raise CompactExtractionResponseError(f"Unknown anchor key: {key!r}")
        return dict(anchors[key])

    def local_ref(key: str, location: str) -> str:
        if key not in entity_ids:
            raise CompactExtractionResponseError(f"{location} references unknown entity local_id: {key!r}")
        return key

    candidates: list[dict[str, Any]] = []
    for entity in wire.entities:
        identity: dict[str, str] = {}
        for item in entity.identity:
            if item.property_id in identity:
                raise CompactExtractionResponseError(
                    f"Entity {entity.local_id!r} repeats identity property {item.property_id!r}"
                )
            identity[item.property_id] = item.value
        if len(entity.anchor_keys) != len(set(entity.anchor_keys)):
            raise CompactExtractionResponseError(f"Entity {entity.local_id!r} repeats an anchor key")
        candidates.append(
            {
                "candidate_kind": "entity",
                "local_id": entity.local_id,
                "observed_type": entity.type,
                "label": entity.label,
                "aliases": list(entity.aliases),
                "identity_key": identity,
                "stable_source_identity": entity.stable_source_identity,
                "anchors": [anchor_value(key) for key in entity.anchor_keys],
            }
        )
    for prop in wire.properties:
        candidates.append(
            {
                "candidate_kind": "property",
                "owner_local_id": local_ref(prop.owner, "Property owner"),
                "observed_property": prop.property,
                "value": prop.value,
                "normalized_value": prop.normalized_value,
                "temporal_key": prop.temporal_key,
                "anchor": anchor_value(prop.anchor_key),
            }
        )
    for relationship in wire.relationships:
        if relationship.context is not None and relationship.context_json is not None:
            raise CompactExtractionResponseError("Specify context or context_json, not both")
        context = (
            _context_object(relationship.context_json)
            if relationship.context_json is not None else relationship.context
        )
        candidates.append(
            {
                "candidate_kind": "relationship",
                "source_local_id": local_ref(relationship.source, "Relationship source"),
                "target_local_id": local_ref(relationship.target, "Relationship target"),
                "observed_predicate": relationship.predicate,
                "direction": relationship.direction,
                "governed_context": context,
                "member_role_id": relationship.member_role_id,
                "member_order": relationship.member_order,
                "anchor": anchor_value(relationship.anchor_key),
            }
        )
    return {"candidates": candidates}
