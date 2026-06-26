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

from typing import Any, Literal, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


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


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


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
    confidence: float = Field(default=DEFAULT_CONFIDENCE, ge=0.0, le=1.0)
    rationale: Optional[str] = None


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
    pass_: str = Field(
        alias="pass",
        description="Extraction pass identifier: p1 | p2 | p3 | p4 | p5 | p6 | p7 | p8",
    )
    schema_profile: Optional[SchemaProfile] = None
    entities: list[Entity] = Field(default_factory=list)
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
    "relationships": Relationship,
    "chunks": Chunk,
    "visual_assets": VisualAsset,
    "visual_regions": VisualRegion,
    "evidence": Evidence,
    "placeholder_suggestions": PlaceholderSuggestion,
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
    if source_file_id is not None:
        data.setdefault("source_file_id", source_file_id)
    if pass_name is not None:
        data.setdefault("pass", pass_name)

    dropped: dict[str, int] = {}
    for field_name, item_model in _COLLECTION_MODELS.items():
        raw_items = data.get(field_name)
        if not isinstance(raw_items, list):
            continue
        kept: list[dict] = []
        drop_count = 0
        for item in raw_items:
            try:
                item_model.model_validate(item)
                kept.append(item)
            except Exception:
                drop_count += 1
        data[field_name] = kept
        if drop_count:
            dropped[field_name] = drop_count

    return LLMOutput.model_validate(data), dropped
