"""Pydantic models and JSON Schema for the SPEC-004 §4 intermediate LLM output contract.

The LLM enrichment stage produces one ``LLMOutput`` object per source file per
extraction pass.  The canonicalize step consumes this contract — it never reads
raw LLM text directly.

Contract summary (SPEC-004 §4)
------------------------------
- ``entities``          — candidate entities with id_hint, type, label, confidence.
- ``relationships``     — directed triples referencing entity id_hints.
- ``chunks``            — retrieval chunks with type, content, optional summary.
- ``visual_assets``     — figures/images with blob_url and description.
- ``visual_regions``    — sub-regions of visual assets (callouts, OCR, components).
- ``evidence``          — provenance pointers (row indices, spans, callout IDs).
- ``schema_profile``    — column-to-ontology mapping (P1 output).
- ``placeholder_suggestions`` — implied-but-missing concepts (P8 output).

id_hint semantics
-----------------
``id_hint`` values are scoped human-readable slugs chosen by the LLM for
internal referencing within a single extraction output.  They are NOT stable
IDs — the canonicalize step converts them to stable canonical IDs.

Usage
-----
::

    from fabric_kg_builder.enrichment.output_schema import validate, LLMOutput

    parsed: LLMOutput = validate(payload_dict)

    # Export JSON Schema for prompt injection (SPEC-004 §6.3):
    schema_dict = LLMOutput.model_json_schema()
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


#: Default confidence assigned when the LLM omits it on an entity/relationship.
#: Mid-range so downstream confidence thresholds neither auto-keep nor auto-drop.
DEFAULT_CONFIDENCE: float = 0.5


# ---------------------------------------------------------------------------
# Literal type aliases
# ---------------------------------------------------------------------------

PassType = Literal["p1", "p2", "p3", "p4", "p5", "p6", "p7", "p8"]

ChunkType = Literal[
    "section_text",
    "procedure_step",
    "table_html",
    "table_row",
    "figure_caption",
    "image_description",
    "ocr_text",
    "warning",
    "note",
    "raw_page_text",
]

AssetType = Literal[
    "figure",
    "inline_image",
    "screenshot",
    "diagram",
    "photo",
    "chart",
    "table_image",
]

RegionType = Literal[
    "callout",
    "ocr_text",
    "component_region",
    "connector_region",
    "warning_region",
    "table_region",
]

EvidenceSourceType = Literal[
    "csv_row",
    "document_span",
    "table_cell",
    "figure_callout",
    "image_region",
    "ocr_text",
    "chunk",
]

PropertyValueType = Literal[
    "string",
    "integer",
    "number",
    "boolean",
    "datetime",
    "date",
    "uri",
    "json",
]

ObservationAssertionState = Literal[
    "asserted",
    "normalized",
    "derived",
    "inferred",
    "unresolved",
    "rejected",
]

TemporalPrecision = Literal[
    "instant",
    "day",
    "month",
    "year",
    "interval",
    "unknown",
    "not_applicable",
]


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class EntityOccurrenceAnchor(BaseModel):
    """Model-proposed local entity mention verified against source text."""

    text_unit_id: str = ""
    span_start: Any = None
    span_end: Any = None
    quote: str = ""


class Entity(BaseModel):
    """Candidate entity extracted by the LLM (SPEC-004 §4.2).

    Minimum required: ``type`` and ``label``.  ``id_hint`` and ``confidence``
    are OPTIONAL — real models frequently omit them in large batches.  The
    canonicalize step synthesizes a stable ``entity_id`` from type+label and
    defaults missing confidence to ``DEFAULT_CONFIDENCE``.  Dropping otherwise
    valid entities because the model forgot a hint/confidence loses graph
    coverage, which is unacceptable (Surface live-run finding 2026-06-24).

    ``label`` also accepts the alias ``name`` — models often emit ``name``.
    """

    model_config = ConfigDict(populate_by_name=True)

    id_hint: Optional[str] = Field(
        default=None, description="Scoped slug; synthesized by canonicalize if absent"
    )
    type: str = Field(
        default="Entity",
        validation_alias=AliasChoices("type", "entity_type"),
        description="Ontology entity type; defaults to 'Entity' when omitted",
    )
    label: str = Field(
        validation_alias=AliasChoices("label", "name"),
        description="Display name (accepts 'name' alias)",
    )
    canonical_name: Optional[str] = Field(
        default=None, description="Normalized form produced by P4"
    )
    aliases: list[str] = Field(default_factory=list)
    description: Optional[str] = None
    confidence: float = Field(
        default=DEFAULT_CONFIDENCE,
        ge=0.0,
        le=1.0,
        description="Extraction confidence 0–1 (defaults when omitted)",
    )
    rationale: Optional[str] = None
    source_spans: list[Optional[str]] = Field(
        default_factory=list, description="Evidence id_hints or span refs"
    )
    evidence_id_hints: list[str] = Field(
        default_factory=list,
        description="Evidence id_hints supporting entity identity.",
    )
    occurrence_anchors: list[EntityOccurrenceAnchor] = Field(
        default_factory=list,
        description=(
            "Exact local mentions used to ground relationship endpoints. "
            "The runner verifies offsets and quote equality."
        ),
    )
    parent_id_hint: Optional[str] = Field(
        default=None,
        description="Optional parent entity reference used for disambiguation.",
    )
    location_id_hint: Optional[str] = Field(
        default=None,
        description="Optional location entity reference used for disambiguation.",
    )
    source_context: Optional[str] = Field(
        default=None,
        description="Source-provided context that prevents unsafe name-only merges.",
    )
    temporal_context: Optional[str] = Field(
        default=None,
        description="Source-provided temporal context used for entity resolution.",
    )
    stable_identifiers: dict[str, str] = Field(
        default_factory=dict,
        description="Observed business identifiers, subject to evidence validation.",
    )
    cannot_link_keys: list[str] = Field(
        default_factory=list,
        description="Explicit source-supported keys that must not be merged.",
    )
    semantic_type_id: Optional[str] = Field(
        default=None,
        description="Runner-assigned canonical semantic entity type ID.",
    )
    semantic_lane: Optional[Literal["authoritative", "discovery"]] = Field(
        default=None,
        description="Runner-assigned contract lane; never trusted from the model.",
    )
    assertion_state: Optional[Literal["asserted", "unresolved"]] = Field(
        default=None,
        description="Runner-assigned schema-2 entity publication state.",
    )
    review_status: Optional[Literal["approved", "needs_review"]] = Field(
        default=None,
        description="Runner-assigned review status for semantic publication.",
    )
    observed_type: Optional[str] = Field(
        default=None,
        description="Original extractor type before contract normalization.",
    )
    resolution_context_key: Optional[str] = Field(
        default=None,
        description="Runner-assigned deterministic contextual identity discriminator.",
    )
    description_evidence_id_hints: list[str] = Field(
        default_factory=list,
        description="Runner-assigned evidence supporting the compiled description.",
    )
    audit_reasons: list[str] = Field(
        default_factory=list,
        description="Runner-owned stable discovery or validation reasons.",
    )

    @field_validator(
        "evidence_id_hints",
        "cannot_link_keys",
        "description_evidence_id_hints",
        "audit_reasons",
    )
    @classmethod
    def _dedupe_string_lists(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class PropertyObservation(BaseModel):
    """Typed entity-property observation emitted by the enrichment model."""

    model_config = ConfigDict(populate_by_name=True)

    entity_id_hint: str = Field(
        validation_alias=AliasChoices(
            "entity_id_hint",
            "entity",
            "entity_id",
            "subject_id_hint",
        ),
        description="References entities[].id_hint.",
    )
    property_name: str = Field(
        validation_alias=AliasChoices(
            "property_name",
            "property",
            "name",
        ),
        description="Observed property name or approved alias.",
    )
    value: Any
    value_type: PropertyValueType
    normalized_value: Any | None = None
    unit: Optional[str] = None
    confidence: float = Field(default=DEFAULT_CONFIDENCE, ge=0.0, le=1.0)
    assertion_state: ObservationAssertionState = "asserted"
    evidence_id_hints: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("evidence_id_hints", "evidence_ids"),
    )
    source_span_ids: list[str] = Field(default_factory=list)
    observed_at: Optional[str] = None
    temporal_precision: TemporalPrecision = "not_applicable"
    semantic_property_id: Optional[str] = None
    semantic_owner_type_id: Optional[str] = None
    semantic_lane: Optional[Literal["authoritative", "discovery"]] = None
    review_status: Optional[Literal["approved", "needs_review"]] = None
    processing_status: Optional[
        Literal["accepted", "discovery", "unresolved", "rejected"]
    ] = None
    conflict_id: Optional[str] = None
    rejection_reasons: list[str] = Field(default_factory=list)
    observed_property_name: Optional[str] = None

    @field_validator(
        "evidence_id_hints",
        "source_span_ids",
        "rejection_reasons",
    )
    @classmethod
    def _dedupe_lists(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @field_validator("observed_at")
    @classmethod
    def _valid_observed_at(cls, value: Optional[str]) -> Optional[str]:
        if value is not None:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value

    @model_validator(mode="after")
    def _validate_value_type(self) -> "PropertyObservation":
        value = self.value
        if self.value_type in {"string", "datetime", "date", "uri"}:
            valid = isinstance(value, str)
        elif self.value_type == "integer":
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif self.value_type == "number":
            valid = isinstance(value, (int, float)) and not isinstance(value, bool)
        elif self.value_type == "boolean":
            valid = isinstance(value, bool)
        else:
            valid = True
        if not valid:
            raise ValueError(
                f"Property value does not match declared value_type "
                f"'{self.value_type}'."
            )
        return self


class ExactRelationshipEvidence(BaseModel):
    """Model-proposed exact source span; every field is verified by the runner."""

    text_unit_id: str = ""
    span_start: Any = None
    span_end: Any = None
    quote: str = ""
    source_file_id: str | None = None
    source_content_hash: str | None = None
    source_locator_json: str | None = None


class Relationship(BaseModel):
    """Directed relationship between entity id_hints (SPEC-004 §4.3).

    Minimum required: ``source_id_hint``, ``relation``, ``target_id_hint``.
    ``id_hint`` and ``confidence`` are OPTIONAL — canonicalize mints the stable
    relationship_id from relation+source+target and defaults missing confidence.

    ``relation`` also accepts the alias ``type`` (models often emit ``type``).
    """

    model_config = ConfigDict(populate_by_name=True)

    id_hint: Optional[str] = Field(
        default=None, description="Optional; canonicalize mints the stable ID"
    )
    source_id_hint: str = Field(
        validation_alias=AliasChoices(
            "source_id_hint", "source", "source_id", "from", "from_id_hint"
        ),
        description="References entities[].id_hint (accepts source/source_id/from)",
    )
    relation: str = Field(
        validation_alias=AliasChoices("relation", "type", "relation_type", "label"),
        description="Ontology relationship type (accepts type/relation_type/label)",
    )
    target_id_hint: str = Field(
        validation_alias=AliasChoices(
            "target_id_hint", "target", "target_id", "to", "to_id_hint"
        ),
        description="References entities[].id_hint (accepts target/target_id/to)",
    )
    evidence_id_hint: Optional[str] = Field(
        default=None, description="References evidence[].id_hint"
    )
    evidence_id_hints: list[str] = Field(
        default_factory=list,
        description="All evidence id_hints supporting this relationship.",
    )
    source_span_ids: list[str] = Field(default_factory=list)
    evidence: ExactRelationshipEvidence | None = Field(
        default=None,
        description=(
            "Schema-2.0 exact evidence candidate. The runner verifies every field "
            "and ignores model-authored evidence IDs."
        ),
    )
    direction: Literal["forward", "reverse", "unknown"] = "forward"
    observed_assertion_state: ObservationAssertionState = Field(
        default="asserted",
        validation_alias=AliasChoices(
            "observed_assertion_state",
            "assertion_state",
        ),
    )
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    temporal_precision: TemporalPrecision = "not_applicable"
    description: Optional[str] = None
    confidence: float = Field(default=DEFAULT_CONFIDENCE, ge=0.0, le=1.0)
    rationale: Optional[str] = None
    semantic_relationship_id: Optional[str] = Field(
        default=None,
        description="Runner-assigned canonical semantic relationship ID.",
    )
    semantic_lane: Optional[Literal["authoritative", "discovery"]] = Field(
        default=None,
        description="Runner-assigned contract lane; never trusted from the model.",
    )
    assertion_status: Optional[
        Literal[
            "asserted",
            "normalized",
            "derived",
            "inferred",
            "unresolved",
            "rejected",
        ]
    ] = Field(
        default=None,
        description="Runner-assigned assertion state after evidence checks.",
    )
    review_status: Optional[Literal["approved", "needs_review"]] = Field(
        default=None,
        description="Runner-assigned review status for semantic publication.",
    )
    observed_relation: Optional[str] = Field(
        default=None,
        description="Original extractor relation before contract normalization.",
    )
    source_semantic_type_id: Optional[str] = None
    target_semantic_type_id: Optional[str] = None
    semantic_category: Optional[
        Literal[
            "hierarchy",
            "containment",
            "dependency",
            "impact",
            "control",
            "support",
            "documentation",
            "temporal",
            "other",
        ]
    ] = None
    category_source: Optional[Literal["contract", "predicate", "unclassified"]] = None
    processing_status: Optional[
        Literal["accepted", "discovery", "unresolved", "rejected"]
    ] = None
    rejection_reasons: list[str] = Field(default_factory=list)
    description_evidence_id_hints: list[str] = Field(default_factory=list)
    verified_evidence_id: str | None = Field(
        default=None,
        description="Runner-minted evidence ID after exact local validation.",
    )
    resolved_source_type_id: str | None = None
    resolved_target_type_id: str | None = None
    source_inheritance_path: list[str] = Field(default_factory=list)
    target_inheritance_path: list[str] = Field(default_factory=list)
    validation_authority: Literal["schema2"] | None = None
    resolved_source_entity_id: str | None = None
    resolved_target_entity_id: str | None = None
    source_grounding_span_start: int | None = None
    source_grounding_span_end: int | None = None
    target_grounding_span_start: int | None = None
    target_grounding_span_end: int | None = None

    @model_validator(mode="after")
    def _merge_evidence_hints(self) -> "Relationship":
        hints = list(self.evidence_id_hints)
        if self.evidence_id_hint:
            hints.insert(0, self.evidence_id_hint)
        self.evidence_id_hints = list(
            dict.fromkeys(value.strip() for value in hints if value.strip())
        )
        self.source_span_ids = list(
            dict.fromkeys(
                value.strip() for value in self.source_span_ids if value.strip()
            )
        )
        self.rejection_reasons = list(
            dict.fromkeys(
                value.strip() for value in self.rejection_reasons if value.strip()
            )
        )
        self.description_evidence_id_hints = list(
            dict.fromkeys(
                value.strip()
                for value in self.description_evidence_id_hints
                if value.strip()
            )
        )
        return self

    @field_validator("valid_from", "valid_to")
    @classmethod
    def _valid_temporal_bound(cls, value: Optional[str]) -> Optional[str]:
        if value is not None:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value


class Chunk(BaseModel):
    """Retrieval chunk produced or summarised by the enrichment pass (SPEC-004 §4.4).

    Required: ``chunk_type``, ``content``.  ``id_hint`` is optional — the
    canonicalize step synthesizes it from a content hash when absent.
    """

    id_hint: Optional[str] = Field(
        default=None, description="Scoped slug; synthesized by canonicalize if absent"
    )
    # Both fields are Optional so that LLM outputs that omit them do not abort
    # pydantic validation — the canonicalize step drops chunks with no content
    # (with a warning) and synthesizes chunk_type from context when absent.
    chunk_type: Optional[str] = Field(
        default=None,
        description="ChunkType literal; defaults to 'raw_page_text' when absent",
    )
    content: Optional[str] = Field(default=None, description="Text for retrieval")
    content_html: Optional[str] = Field(
        default=None, description="HTML for table chunks"
    )
    summary: Optional[str] = Field(
        default=None, description="LLM-generated search-friendly summary from P7"
    )
    embedding_text: Optional[str] = Field(
        default=None, description="Text prepared for embedding (SPEC-004 §7.4)"
    )
    blob_url: Optional[str] = Field(
        default=None,
        description="Runner-injected Blob URL only; LLM must never mint this",
    )
    page_number: Optional[int] = None
    section_path: Optional[str] = None
    table_id: Optional[str] = None
    figure_id: Optional[str] = None
    image_id: Optional[str] = None
    related_entity_id_hints: list[str] = Field(default_factory=list)
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class VisualAsset(BaseModel):
    """Figure, image, or other visual asset (SPEC-004 §4.5).

    Required: ``id_hint``, ``asset_type``, ``blob_url``, ``confidence``.
    ``blob_url`` must be the runner-provided URL — never generated by the LLM.
    """

    id_hint: str
    asset_type: str  # AssetType Literal
    caption: Optional[str] = None
    alt_text: Optional[str] = None
    blob_url: str = Field(
        description="Runner-injected Blob URL; LLM echoes unchanged"
    )
    description: Optional[str] = Field(
        default=None, description="LLM-generated visual description from P6"
    )
    page_number: Optional[int] = None
    section_path: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0)


class VisualRegion(BaseModel):
    """Sub-region of a visual asset (callout, OCR text, component, …) (SPEC-004 §4.6).

    Required: ``id_hint``, ``image_id_hint``, ``region_type``, ``confidence``.
    """

    id_hint: str
    image_id_hint: str = Field(description="References visual_assets[].id_hint")
    region_type: str  # RegionType Literal
    label: Optional[str] = None
    text: Optional[str] = None
    polygon_json: Optional[str] = Field(
        default=None, description="JSON-encoded polygon or bounding box"
    )
    identified_entity_hint: Optional[str] = Field(
        default=None, description="References entities[].id_hint"
    )
    blob_url: Optional[str] = Field(
        default=None, description="Runner-injected only; never minted by LLM"
    )
    confidence: float = Field(ge=0.0, le=1.0)


class Evidence(BaseModel):
    """Provenance pointer for an entity or relationship claim (SPEC-004 §4.7).

    ``id_hint`` and ``source_type`` are OPTIONAL: real LLMs frequently omit them.
    The canonicalize step synthesizes ``id_hint`` from a content hash when absent,
    and defaults ``source_type`` to a context-appropriate value (e.g.
    ``"document_span"``).  See SPEC-004 §3-7 and the robustness fix for the
    Surface PDF live-run failure (2026-06-24).
    """

    id_hint: Optional[str] = Field(
        default=None, description="Scoped slug; synthesized by canonicalize if absent"
    )
    source_type: Optional[str] = Field(
        default=None,
        description="EvidenceSourceType; defaults to 'document_span' when absent",
    )  # EvidenceSourceType Literal
    page_number: Optional[int] = None
    section_path: Optional[str] = None
    table_id: Optional[str] = None
    row_index: Optional[int] = None
    col_index: Optional[int] = None
    figure_id: Optional[str] = None
    image_id: Optional[str] = None
    callout_id: Optional[str] = None
    visual_region_id_hint: Optional[str] = Field(
        default=None, description="References visual_regions[].id_hint"
    )
    blob_url: Optional[str] = Field(
        default=None, description="Runner-injected only; never minted by LLM"
    )
    text: Optional[str] = Field(
        default=None, description="Supporting text or value"
    )
    text_unit_id: Optional[str] = None
    span_start: Optional[int] = None
    span_end: Optional[int] = None
    source_content_hash: Optional[str] = None
    source_locator_json: Optional[str] = None
    runner_verified: bool = Field(
        default=False,
        description="Runner-owned marker for locally verified exact evidence.",
    )


class ColumnMapping(BaseModel):
    """Maps one source column to an ontology type/property (SPEC-004 §4.8)."""

    source_column: str
    ontology_type: Optional[str] = None
    ontology_property: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0)
    notes: Optional[str] = None


class SchemaProfile(BaseModel):
    """P1 output: column-to-ontology inference (SPEC-004 §4.8)."""

    inferred_domain: Optional[str] = None
    column_mappings: list[ColumnMapping] = Field(default_factory=list)
    inferred_entity_types: list[str] = Field(default_factory=list)
    inferred_relationship_types: list[str] = Field(default_factory=list)


class PlaceholderSuggestion(BaseModel):
    """P8 output: a concept strongly implied but not extracted (SPEC-004 §4.9).

    Required: ``concept``, ``reason``, ``confidence``.
    """

    concept: str
    reason: str
    example_labels: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Top-level envelope
# ---------------------------------------------------------------------------


class LLMOutput(BaseModel):
    """Top-level intermediate JSON contract (SPEC-004 §4.1).

    One object per source file per extraction pass.  All array fields default
    to empty list — the pass populates only what it produces.

    The field ``pass_`` (serialised as ``"pass"``) identifies which extraction
    pass produced this object.  ``"pass"`` is a Python reserved keyword so an
    alias is used with ``populate_by_name=True``.
    """

    model_config = ConfigDict(populate_by_name=True)

    source_file_id: str = Field(
        description="Injected by the runner; LLM echoes back for traceability"
    )
    semantic_contract_hash: Optional[str] = Field(
        default=None,
        description="Runner-assigned hash of the approved semantic contract.",
    )
    proposal_hash: Optional[str] = None
    source_profile_hash: Optional[str] = None
    prompt_version: Optional[str] = None
    model_version: Optional[str] = None
    pass_: str = Field(
        alias="pass",
        description="Extraction pass identifier: p1 | p2 | p3 | p4 | p5 | p6 | p7 | p8",
    )
    schema_profile: Optional[SchemaProfile] = None
    entities: list[Entity] = Field(default_factory=list)
    property_observations: list[PropertyObservation] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    chunks: list[Chunk] = Field(default_factory=list)
    visual_assets: list[VisualAsset] = Field(default_factory=list)
    visual_regions: list[VisualRegion] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    placeholder_suggestions: list[PlaceholderSuggestion] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Exported JSON Schema
# ---------------------------------------------------------------------------

#: JSON Schema dict suitable for injection into LLM prompts (SPEC-004 §6.3)
#: and for use as the ``json_schema`` argument to :meth:`FoundryClient.complete_json`.
LLM_OUTPUT_JSON_SCHEMA: dict[str, Any] = LLMOutput.model_json_schema()


# ---------------------------------------------------------------------------
# Validation helper
# ---------------------------------------------------------------------------


def validate(payload: dict) -> LLMOutput:
    """Validate *payload* against the SPEC-004 §4 intermediate JSON contract.

    Parameters
    ----------
    payload:
        Raw dict (e.g. parsed from the LLM response JSON string).

    Returns
    -------
    LLMOutput
        Parsed and validated object.

    Raises
    ------
    pydantic.ValidationError
        When required fields are missing, types are wrong, or ``confidence``
        is outside ``[0.0, 1.0]``.
    """
    return LLMOutput.model_validate(payload)


#: Collection field name -> item model, for item-level tolerant validation.
_COLLECTION_MODELS: dict[str, type[BaseModel]] = {
    "entities": Entity,
    "property_observations": PropertyObservation,
    "relationships": Relationship,
    "chunks": Chunk,
    "visual_assets": VisualAsset,
    "visual_regions": VisualRegion,
    "evidence": Evidence,
    "placeholder_suggestions": PlaceholderSuggestion,
}

_RUNNER_OWNED_FIELDS: dict[str, frozenset[str]] = {
    "entities": frozenset(
        {
            "semantic_type_id",
            "semantic_lane",
            "review_status",
            "observed_type",
            "resolution_context_key",
            "description_evidence_id_hints",
            "audit_reasons",
        }
    ),
    "property_observations": frozenset(
        {
            "semantic_property_id",
            "semantic_owner_type_id",
            "semantic_lane",
            "review_status",
            "processing_status",
            "conflict_id",
            "rejection_reasons",
            "observed_property_name",
        }
    ),
    "relationships": frozenset(
        {
            "semantic_relationship_id",
            "semantic_lane",
            "assertion_status",
            "review_status",
            "observed_relation",
            "source_semantic_type_id",
            "target_semantic_type_id",
            "semantic_category",
            "category_source",
            "processing_status",
            "rejection_reasons",
            "description_evidence_id_hints",
            "verified_evidence_id",
            "resolved_source_type_id",
            "resolved_target_type_id",
            "source_inheritance_path",
            "target_inheritance_path",
            "validation_authority",
            "resolved_source_entity_id",
            "resolved_target_entity_id",
            "source_grounding_span_start",
            "source_grounding_span_end",
            "target_grounding_span_start",
            "target_grounding_span_end",
        }
    ),
    "evidence": frozenset({"runner_verified"}),
}


def validate_tolerant(
    payload: dict,
    *,
    source_file_id: str | None = None,
    pass_name: str | None = None,
) -> tuple[LLMOutput, dict[str, int]]:
    """Validate *payload* item-by-item, dropping only invalid items.

    Unlike :func:`validate` (all-or-nothing), this never discards a whole
    collection because a single item is malformed.  Each item in every list
    field is validated independently; items that fail are dropped and counted.
    Real LLMs frequently emit one bad relationship/entity in an otherwise good
    payload — losing the entire pass (and its valid entities) is unacceptable.

    Missing envelope fields (``source_file_id``, ``pass``) are injected from the
    provided defaults so the runner's known values are used.

    Returns
    -------
    tuple[LLMOutput, dict[str, int]]
        The validated output (with only well-formed items) and a mapping of
        ``collection_name -> dropped_count``.
    """
    data = dict(payload) if isinstance(payload, dict) else {}
    data.pop("semantic_contract_hash", None)
    if source_file_id is not None:
        data["source_file_id"] = source_file_id
    if pass_name is not None:
        data["pass"] = pass_name

    dropped: dict[str, int] = {}
    for field_name, item_model in _COLLECTION_MODELS.items():
        raw_items = data.get(field_name)
        if not isinstance(raw_items, list):
            continue
        kept: list[dict] = []
        drop_count = 0
        for item in raw_items:
            if isinstance(item, dict):
                item = {
                    key: value
                    for key, value in item.items()
                    if key
                    not in _RUNNER_OWNED_FIELDS.get(
                        field_name,
                        frozenset(),
                    )
                }
            try:
                item_model.model_validate(item)
                kept.append(item)
            except Exception:
                drop_count += 1
        data[field_name] = kept
        if drop_count:
            dropped[field_name] = drop_count

    return LLMOutput.model_validate(data), dropped
