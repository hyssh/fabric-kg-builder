"""Versioned Pydantic contracts for semantic model manifest artifacts.

Implements S8A-AUD-002 (SPEC-008A §4–10).  Provides five canonical contracts:

    SemanticModelManifest      – one approved semantic authority (§6)
    SemanticCrosswalk          – canonical-to-physical crosswalk (§4.2)
    MaterializationPlan        – contract-owned materialization plan (§7.2)
    PersistedProjectionReceipt – persisted semantic projection receipt (§7.4)
    SemanticQueryPlan          – source-independent semantic query plan (§9.1)

Supporting types cover projection sub-models, complexity budgets, failure
taxonomy, and diagnostic records needed for downstream synthetic tests.
All shapes are deterministic and round-trip through canonical JSON (sort_keys).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from .models import (
    AssertionPolicy,
    Cardinality,
    EvidencePolicy,
    PropertyType,
    PublicationStatus,
    StrictModel,
)

# ---------------------------------------------------------------------------
# Persisted-boundary base model (Blocker 2 — S8A-AUD-002 hardening)
# ---------------------------------------------------------------------------


class _StrictPersistedModel(StrictModel):
    """Base for all persisted-boundary schema models.

    Adds ``strict=True`` (no implicit type coercion) on top of
    StrictModel's ``extra="forbid"`` and ``str_strip_whitespace=True``.

    Draft and partial in-progress types (DraftProjectionReceipt,
    PartialDiagnosticExport) intentionally remain lenient; all five core
    persisted artifacts and their nested sub-models use this base.
    """

    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, strict=True
    )

# ---------------------------------------------------------------------------
# Schema versioning
# ---------------------------------------------------------------------------

SEMANTIC_SCHEMAS_VERSION = "1.1"

# ---------------------------------------------------------------------------
# Internal validators
# ---------------------------------------------------------------------------

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SEMANTIC_TYPE_ID_RE = re.compile(
    r"^(?:entity-type|relationship-type):[a-z0-9][a-z0-9._-]*$"
)


def _check_hash(value: str) -> str:
    """Accept an empty string (unsealed) or a canonical sha256 hash."""
    if value and not _HASH_RE.fullmatch(value):
        raise ValueError(
            "Hash must be 'sha256:<64 lower-hex chars>' or an empty string."
        )
    return value


def _check_nonempty_hash(value: str) -> str:
    """Require a canonical sha256 hash; empty strings are rejected."""
    if not _HASH_RE.fullmatch(value):
        raise ValueError(
            "Hash must be 'sha256:<64 lower-hex chars>' (empty string not accepted "
            "at this persisted boundary)."
        )
    return value


def _check_utc_timestamp(value: str) -> str:
    """Require a non-empty, valid, timezone-aware UTC ISO 8601 timestamp.

    Rejects naive timestamps (no tzinfo) and timestamps whose UTC offset is
    not exactly zero.  The aware-vs-naive TypeError that can arise from
    comparing naive and aware datetimes is prevented at ingestion time.
    """
    if not value:
        raise ValueError("UTC timestamp must not be empty.")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"UTC timestamp is not valid ISO 8601: {exc}") from exc
    if dt.tzinfo is None:
        raise ValueError(
            f"UTC timestamp must be timezone-aware (UTC), got naive value: "
            f"{value!r}. Add 'Z' or '+00:00' suffix."
        )
    utc_offset = dt.utcoffset()
    if utc_offset is not None and utc_offset.total_seconds() != 0.0:
        raise ValueError(
            f"UTC timestamp must have zero UTC offset, got {utc_offset}: "
            f"{value!r}."
        )
    return value


def _check_semantic_type_id(value: str) -> str:
    if not _SEMANTIC_TYPE_ID_RE.fullmatch(value):
        raise ValueError(
            f"Semantic type ID '{value}' must be "
            "'entity-type:<slug>' or 'relationship-type:<slug>'."
        )
    return value


def _canonicalize_string_list(value: Any, *, field_name: str) -> Any:
    """Canonicalize a set-like string list without weakening strict typing."""
    if not isinstance(value, list):
        return value
    canonical: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError(
                f"{field_name} entries must be strings; got "
                f"{type(item).__name__}."
            )
        normalized = item.strip()
        if not normalized:
            raise ValueError(f"{field_name} entries must not be empty.")
        canonical.add(normalized)
    return sorted(canonical)


# ---------------------------------------------------------------------------
# Cross-layer projection sub-models
# ---------------------------------------------------------------------------


class OntologyEntityProjection(_StrictPersistedModel):
    """Fabric Ontology physical bindings for one entity type."""

    ontology_type_id: str | None = None
    property_ids: dict[str, str] = Field(
        default_factory=dict,
        description="Map of property name -> Fabric BigInt property ID.",
    )
    binding_id: str | None = None


class OntologyRelationshipProjection(_StrictPersistedModel):
    """Fabric Ontology physical bindings for one relationship type."""

    ontology_rel_type_id: str | None = None
    source_ontology_type_id: str | None = None
    target_ontology_type_id: str | None = None
    contextualization_id: str | None = None


class OntologyPropertyProjection(_StrictPersistedModel):
    """Fabric Ontology BigInt property ID for one business property."""

    ontology_property_id: str | None = None


class GraphNodeProjection(_StrictPersistedModel):
    """Graph database label and property key bindings for one entity type."""

    label: str | None = None
    alias: str | None = None
    property_keys: list[str] = Field(default_factory=list)

    @field_validator("property_keys", mode="before")
    @classmethod
    def _canonicalize_property_keys(cls, value: Any) -> Any:
        return _canonicalize_string_list(value, field_name="property_keys")


class GraphEdgeProjection(_StrictPersistedModel):
    """Graph database label and endpoint labels for one relationship type."""

    label: str | None = None
    alias: str | None = None
    source_label: str | None = None
    target_label: str | None = None


class GraphPropertyProjection(_StrictPersistedModel):
    """Graph database property key for one business property."""

    property_key: str | None = None


class SearchLinkageSpec(_StrictPersistedModel):
    """AI Search index linkage for one entity type."""

    index_name: str | None = None
    entity_id_field: str | None = None
    type_filter_field: str | None = None
    type_filter_value: str | None = None

    @model_validator(mode="after")
    def _filter_pair(self) -> "SearchLinkageSpec":
        if bool(self.type_filter_field) != (self.type_filter_value is not None):
            raise ValueError(
                "Search type_filter_field and type_filter_value must be "
                "provided together."
            )
        return self


class AgentElementProjection(_StrictPersistedModel):
    """Data Agent element identity for one entity or relationship type."""

    element_id: str | None = None
    element_name: str | None = None
    element_category: Literal["entity", "relationship"] | None = None


class AgentPropertyChildProjection(_StrictPersistedModel):
    """Data Agent property-child identity for one business property."""

    child_id: str | None = None
    child_name: str | None = None


class HierarchyMetadata(_StrictPersistedModel):
    """Hierarchy and inheritance context for one entity type."""

    parent_type_id: str | None = None
    depth: int = Field(default=0, ge=0)
    is_abstract: bool = False

    @field_validator("parent_type_id")
    @classmethod
    def _valid_parent(cls, value: str | None) -> str | None:
        if value is not None:
            _check_semantic_type_id(value)
        return value


# ---------------------------------------------------------------------------
# §6.4 – Property entry in the manifest
# ---------------------------------------------------------------------------


class ManifestPropertyEntry(_StrictPersistedModel):
    """One agent-visible or lineage property in the semantic model manifest."""

    property_id: str = Field(min_length=1)
    owner_type_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    business_description: str = Field(min_length=1)
    value_type: PropertyType
    unit_policy: str | None = None
    required: bool = False
    agent_visible: bool = True
    evidence_policy: EvidencePolicy = "optional"
    physical_source_column: str | None = None
    ontology_projection: OntologyPropertyProjection = Field(
        default_factory=OntologyPropertyProjection
    )
    graph_projection: GraphPropertyProjection = Field(
        default_factory=GraphPropertyProjection
    )
    agent_projection: AgentPropertyChildProjection = Field(
        default_factory=AgentPropertyChildProjection
    )

    @field_validator("owner_type_id")
    @classmethod
    def _valid_owner(cls, value: str) -> str:
        _check_semantic_type_id(value)
        return value


# ---------------------------------------------------------------------------
# §6.2 – Entity type entry in the manifest
# ---------------------------------------------------------------------------


class ManifestEntityTypeEntry(_StrictPersistedModel):
    """One entity type in the semantic model manifest (§6.2)."""

    semantic_id: str = Field(min_length=1)
    canonical_name: str = Field(min_length=1)
    business_name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    description: str = Field(min_length=1)
    identifier_properties: list[str] = Field(min_length=1)
    published_properties: list[str] = Field(default_factory=list)
    hierarchy: HierarchyMetadata = Field(default_factory=HierarchyMetadata)
    evidence_policy: EvidencePolicy = "optional"
    publication_status: PublicationStatus = "core"
    physical_source_table: str | None = None
    ontology_projection: OntologyEntityProjection = Field(
        default_factory=OntologyEntityProjection
    )
    graph_projection: GraphNodeProjection = Field(
        default_factory=GraphNodeProjection
    )
    search_linkage: SearchLinkageSpec = Field(default_factory=SearchLinkageSpec)
    agent_projection: AgentElementProjection = Field(
        default_factory=lambda: AgentElementProjection(element_category="entity")
    )

    @field_validator("semantic_id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not value.startswith("entity-type:"):
            raise ValueError("Entity type semantic_id must use 'entity-type:<slug>'.")
        _check_semantic_type_id(value)
        return value

    @model_validator(mode="before")
    @classmethod
    def _canonicalize_set_fields(cls, data: Any) -> Any:
        """Sort and deduplicate set-like list fields at construction time (Blocker 5)."""
        if isinstance(data, dict):
            for field in ("identifier_properties", "published_properties", "aliases"):
                if field in data:
                    data[field] = _canonicalize_string_list(
                        data[field], field_name=field
                    )
        return data


# ---------------------------------------------------------------------------
# §6.3 – Relationship type entry in the manifest
# ---------------------------------------------------------------------------


class InversePolicy(_StrictPersistedModel):
    """Approved inverse predicate and materialization policy."""

    predicate: str | None = None
    materialization: Literal["virtual", "materialized", "none"] = "none"


class TransitivityPolicy(_StrictPersistedModel):
    """Transitivity and closure policy for one relationship type."""

    transitive: bool = False
    closure_via: Literal["query_time", "derived_rule", "none"] = "none"


class ManifestRelationshipEntry(_StrictPersistedModel):
    """One relationship type in the semantic model manifest (§6.3)."""

    semantic_id: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    business_name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    source_type_id: str = Field(min_length=1)
    target_type_id: str = Field(min_length=1)
    direction: Literal["source_to_target"] = "source_to_target"
    cardinality: Cardinality = Field(default_factory=Cardinality)
    optional: bool = True
    inverse_policy: InversePolicy = Field(default_factory=InversePolicy)
    transitivity_policy: TransitivityPolicy = Field(
        default_factory=TransitivityPolicy
    )
    assertion_policy: AssertionPolicy = Field(default_factory=AssertionPolicy)
    evidence_policy: EvidencePolicy = "required_for_asserted"
    publication_status: PublicationStatus = "core"
    physical_source_table: str | None = None
    source_endpoint_column: str | None = None
    target_endpoint_column: str | None = None
    ontology_projection: OntologyRelationshipProjection = Field(
        default_factory=OntologyRelationshipProjection
    )
    graph_projection: GraphEdgeProjection = Field(
        default_factory=GraphEdgeProjection
    )
    agent_projection: AgentElementProjection = Field(
        default_factory=lambda: AgentElementProjection(
            element_category="relationship"
        )
    )

    @field_validator("semantic_id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not value.startswith("relationship-type:"):
            raise ValueError(
                "Relationship type semantic_id must use 'relationship-type:<slug>'."
            )
        _check_semantic_type_id(value)
        return value

    @field_validator("source_type_id", "target_type_id")
    @classmethod
    def _valid_endpoint_id(cls, value: str) -> str:
        _check_semantic_type_id(value)
        return value


# ---------------------------------------------------------------------------
# §6.1 – Semantic model manifest
# ---------------------------------------------------------------------------


class PublicationProfile(_StrictPersistedModel):
    """Publication targets and coverage status for one manifest."""

    ontology_enabled: bool = True
    graph_enabled: bool = True
    search_enabled: bool = True
    agent_enabled: bool = True
    published_entity_type_count: int = Field(default=0, ge=0)
    published_relationship_count: int = Field(default=0, ge=0)
    published_property_count: int = Field(default=0, ge=0)


class CompetencyRecord(_StrictPersistedModel):
    """Coverage state for one competency question."""

    competency_id: str = Field(min_length=1)
    satisfied: bool = False
    blocking_reason: str | None = None
    required_types: list[str] = Field(default_factory=list)
    required_relationships: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _canonicalize_set_fields(cls, data: Any) -> Any:
        """Sort and deduplicate required_types/required_relationships (Blocker 5)."""
        if isinstance(data, dict):
            for field in ("required_types", "required_relationships"):
                if field in data:
                    data[field] = _canonicalize_string_list(
                        data[field], field_name=field
                    )
        return data


class ModelQualityMetrics(_StrictPersistedModel):
    """Model quality summary included in the manifest."""

    duplicate_description_count: int = Field(default=0, ge=0)
    missing_evidence_count: int = Field(default=0, ge=0)
    discovery_type_count: int = Field(default=0, ge=0)
    discovery_relationship_count: int = Field(default=0, ge=0)
    canonical_id_crosswalk_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    agent_property_projection_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    relationship_endpoint_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    accepted_property_evidence_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    accepted_relationship_evidence_coverage: float = Field(
        default=0.0, ge=0.0, le=1.0
    )


class SemanticModelManifest(_StrictPersistedModel):
    """Versioned semantic model manifest (SPEC-008A §6.1).

    One approved semantic authority from extraction through runtime query.
    Physical compilers for Ontology, Graph, Search, and Data Agent SHALL
    consume this manifest and SHALL NOT independently infer semantic definitions.

    Seal the manifest by calling ``compute_manifest_hash(manifest)`` after
    all fields are populated and assigning the result to ``manifest_hash``.
    """

    schema_version: Literal[SEMANTIC_SCHEMAS_VERSION] = SEMANTIC_SCHEMAS_VERSION
    semantic_contract_hash: str = Field(
        default="",
        description="sha256 hash of the approved semantic contract.",
    )
    stable_id_lock_hash: str = Field(
        default="",
        description="sha256 hash of the stable ID lock.",
    )
    data_version: str = Field(
        default="",
        description="Opaque data-run version label.",
    )
    entity_types: list[ManifestEntityTypeEntry] = Field(default_factory=list)
    property_definitions: list[ManifestPropertyEntry] = Field(
        default_factory=list
    )
    relationship_types: list[ManifestRelationshipEntry] = Field(
        default_factory=list
    )
    publication_profile: PublicationProfile = Field(
        default_factory=PublicationProfile
    )
    competency_coverage: list[CompetencyRecord] = Field(default_factory=list)
    model_quality: ModelQualityMetrics = Field(default_factory=ModelQualityMetrics)
    manifest_hash: str = Field(
        default="",
        description="sha256 hash of this manifest excluding this field. "
        "Set by compute_manifest_hash().",
    )

    _check_semantic_contract_hash = field_validator(
        "semantic_contract_hash", mode="after"
    )(_check_hash)
    _check_stable_id_lock_hash = field_validator(
        "stable_id_lock_hash", mode="after"
    )(_check_hash)
    _check_manifest_hash = field_validator("manifest_hash", mode="after")(
        _check_hash
    )

    @model_validator(mode="after")
    def _validate_manifest_integrity(self) -> "SemanticModelManifest":
        # Unique entity type semantic IDs
        entity_ids = [e.semantic_id for e in self.entity_types]
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError(
                "SemanticModelManifest contains duplicate entity type semantic IDs."
            )

        # Unique relationship type semantic IDs
        rel_ids = [r.semantic_id for r in self.relationship_types]
        if len(rel_ids) != len(set(rel_ids)):
            raise ValueError(
                "SemanticModelManifest contains duplicate relationship type "
                "semantic IDs."
            )

        # Unique property IDs within their owner type (canonical key: owner + property_id)
        seen_props: set[tuple[str, str]] = set()
        seen_prop_names: dict[str, set[str]] = {}
        for prop in self.property_definitions:
            key = (prop.owner_type_id, prop.property_id)
            if key in seen_props:
                raise ValueError(
                    f"Duplicate property_id '{prop.property_id}' on owner "
                    f"'{prop.owner_type_id}'."
                )
            seen_props.add(key)
            # Property names must be unique within an owner; cross-owner reuse is allowed
            if prop.owner_type_id not in seen_prop_names:
                seen_prop_names[prop.owner_type_id] = set()
            if prop.name in seen_prop_names[prop.owner_type_id]:
                raise ValueError(
                    f"Duplicate property name '{prop.name}' on owner "
                    f"'{prop.owner_type_id}'. Property names must be unique "
                    "within an owner type (cross-owner reuse is permitted)."
                )
            seen_prop_names[prop.owner_type_id].add(prop.name)

        # Relationship endpoints must reference known entity types (when populated)
        if self.entity_types:
            known_entity_ids = set(entity_ids)
            for rel in self.relationship_types:
                if rel.source_type_id not in known_entity_ids:
                    raise ValueError(
                        f"Relationship '{rel.predicate}' source_type_id "
                        f"'{rel.source_type_id}' is not in entity_types."
                    )
                if rel.target_type_id not in known_entity_ids:
                    raise ValueError(
                        f"Relationship '{rel.predicate}' target_type_id "
                        f"'{rel.target_type_id}' is not in entity_types."
                    )

        # Distinct published descriptions must not be byte-identical (§5.7)
        published_descs = [
            e.description
            for e in self.entity_types
            if e.publication_status != "excluded"
        ]
        if len(published_descs) != len(set(published_descs)):
            raise ValueError(
                "Published entity types contain byte-identical descriptions. "
                "Each published type must have a unique description."
            )

        return self


# ---------------------------------------------------------------------------
# §4.2 – Canonical-to-physical crosswalk
# ---------------------------------------------------------------------------


CrosswalkElementKind = Literal["entity_type", "relationship_type", "property"]


class CrosswalkEntry(_StrictPersistedModel):
    """One element's mapping from canonical semantic ID to all physical targets.

    Represents the full cross-layer identity required by §4.2:
        semantic_id -> ontology_type_id, graph_label, graph_alias,
                       data_agent_element_id, search_field_or_filter,
                       physical_table
    """

    semantic_id: str = Field(min_length=1)
    element_kind: CrosswalkElementKind
    owner_type_id: str | None = None
    source_type_id: str | None = None
    target_type_id: str | None = None
    ontology_type_id: str | None = None
    graph_label: str | None = None
    graph_alias: str | None = None
    data_agent_element_id: str | None = None
    search_field_or_filter: str | None = None
    physical_table: str | None = None
    direction: Literal["source_to_target"] | None = None

    @model_validator(mode="after")
    def _validate_scoped_identity(self) -> "CrosswalkEntry":
        if self.element_kind == "property":
            if self.owner_type_id is None:
                raise ValueError(
                    "Property crosswalk entries require owner_type_id so "
                    "same-named properties remain owner-scoped."
                )
            _check_semantic_type_id(self.owner_type_id)
            if self.source_type_id is not None or self.target_type_id is not None:
                raise ValueError(
                    "Property crosswalk entries cannot declare relationship endpoints."
                )
        elif self.owner_type_id is not None:
            raise ValueError(
                f"{self.element_kind} crosswalk entries cannot declare owner_type_id."
            )

        if self.element_kind == "relationship_type":
            if self.source_type_id is None or self.target_type_id is None:
                raise ValueError(
                    "Relationship crosswalk entries require canonical source_type_id "
                    "and target_type_id."
                )
            _check_semantic_type_id(self.source_type_id)
            _check_semantic_type_id(self.target_type_id)
            if self.direction is None:
                raise ValueError(
                    "Relationship crosswalk entries require an explicit direction."
                )
        elif self.source_type_id is not None or self.target_type_id is not None:
            raise ValueError(
                f"{self.element_kind} crosswalk entries cannot declare "
                "relationship endpoints."
            )
        return self


class SemanticCrosswalk(_StrictPersistedModel):
    """Canonical-to-physical crosswalk for all published semantic elements (§4.2).

    Cross-layer alignment is valid only when every selected element maps through
    canonical semantic IDs and no semantic_id maps to multiple incompatible
    physical IDs.
    """

    schema_version: Literal[SEMANTIC_SCHEMAS_VERSION] = SEMANTIC_SCHEMAS_VERSION
    manifest_hash: str = Field(default="")
    entity_type_entries: list[CrosswalkEntry] = Field(default_factory=list)
    relationship_type_entries: list[CrosswalkEntry] = Field(default_factory=list)
    property_entries: list[CrosswalkEntry] = Field(default_factory=list)

    _check_manifest_hash = field_validator("manifest_hash", mode="after")(
        _check_hash
    )

    @model_validator(mode="after")
    def _validate_crosswalk_integrity(self) -> "SemanticCrosswalk":
        # Entity and relationship IDs are globally canonical. Properties are
        # owner-scoped because common names such as "id" legitimately repeat.
        for label, entries in (
            ("entity_type_entries", self.entity_type_entries),
            ("relationship_type_entries", self.relationship_type_entries),
        ):
            ids = [e.semantic_id for e in entries]
            if len(ids) != len(set(ids)):
                raise ValueError(
                    f"SemanticCrosswalk.{label} contains duplicate semantic IDs."
                )
        property_keys = [
            (entry.owner_type_id, entry.semantic_id)
            for entry in self.property_entries
        ]
        if len(property_keys) != len(set(property_keys)):
            raise ValueError(
                "SemanticCrosswalk.property_entries contains duplicate "
                "owner-scoped property IDs."
            )

        # element_kind must match the list it appears in
        _expected_kinds: dict[str, str] = {
            "entity_type_entries": "entity_type",
            "relationship_type_entries": "relationship_type",
            "property_entries": "property",
        }
        for list_name, expected_kind in _expected_kinds.items():
            entries = getattr(self, list_name)
            for entry in entries:
                if entry.element_kind != expected_kind:
                    raise ValueError(
                        f"SemanticCrosswalk.{list_name} entry '{entry.semantic_id}' "
                        f"has element_kind='{entry.element_kind}' but must be "
                        f"'{expected_kind}'."
                    )

        # Physical identifier uniqueness across all entries (§4.2)
        all_entries = (
            self.entity_type_entries
            + self.relationship_type_entries
            + self.property_entries
        )
        seen_ontology_ids: set[str] = set()
        seen_agent_ids: set[str] = set()
        seen_graph_labels: set[str] = set()
        seen_graph_aliases: set[str] = set()
        seen_table_search_pairs: set[tuple[str, str, str]] = set()
        for entry in all_entries:
            if entry.ontology_type_id is not None:
                if entry.ontology_type_id in seen_ontology_ids:
                    raise ValueError(
                        f"Duplicate ontology_type_id '{entry.ontology_type_id}' "
                        f"in crosswalk (entry '{entry.semantic_id}'). Physical "
                        "identifiers must not map to multiple semantic IDs."
                    )
                seen_ontology_ids.add(entry.ontology_type_id)
            if entry.data_agent_element_id is not None:
                if entry.data_agent_element_id in seen_agent_ids:
                    raise ValueError(
                        f"Duplicate data_agent_element_id "
                        f"'{entry.data_agent_element_id}' in crosswalk (entry "
                        f"'{entry.semantic_id}'). Physical identifiers must not "
                        "map to multiple semantic IDs."
                    )
                seen_agent_ids.add(entry.data_agent_element_id)
            if (
                entry.element_kind != "property"
                and entry.graph_label is not None
            ):
                if entry.graph_label in seen_graph_labels:
                    raise ValueError(
                        f"Duplicate graph_label '{entry.graph_label}' in "
                        f"crosswalk (entry '{entry.semantic_id}')."
                    )
                seen_graph_labels.add(entry.graph_label)
            if entry.graph_alias is not None:
                if entry.graph_alias in seen_graph_aliases:
                    raise ValueError(
                        f"Duplicate graph_alias '{entry.graph_alias}' in "
                        f"crosswalk (entry '{entry.semantic_id}')."
                    )
                seen_graph_aliases.add(entry.graph_alias)
            if (
                entry.physical_table is not None
                and entry.search_field_or_filter is not None
            ):
                pair = (
                    entry.element_kind,
                    entry.physical_table,
                    entry.search_field_or_filter,
                )
                if pair in seen_table_search_pairs:
                    raise ValueError(
                        "Duplicate element_kind/physical_table/"
                        "search_field_or_filter mapping "
                        f"{pair!r} in crosswalk."
                    )
                seen_table_search_pairs.add(pair)

        return self


# ---------------------------------------------------------------------------
# §7.2 – Contract-owned materialization plan
# ---------------------------------------------------------------------------

ApprovalState = Literal["approved", "discovery", "excluded"]
DataAvailabilityStatus = Literal[
    "sufficient", "insufficient", "unavailable", "not_observed"
]


class ColumnSpec(_StrictPersistedModel):
    """One physical column in a materialized table."""

    column_name: str = Field(min_length=1)
    semantic_property_id: str | None = None
    data_type: str = Field(min_length=1)
    nullable: bool = True


class EntityTableSpec(_StrictPersistedModel):
    """Materialization spec for one entity type's physical table."""

    semantic_id: str = Field(min_length=1)
    table_name: str = Field(min_length=1)
    source_table_name: str | None = Field(default=None, min_length=1)
    source_filter_column: str | None = None
    source_filter_value: str | None = None
    required: bool = True
    approval_state: ApprovalState = "approved"
    entity_id_column: str = Field(default="entity_id")
    display_name_column: str = Field(default="display_name")
    columns: list[ColumnSpec] = Field(default_factory=list)

    @field_validator("semantic_id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not value.startswith("entity-type:"):
            raise ValueError(
                "EntityTableSpec.semantic_id must use 'entity-type:<slug>'."
            )
        _check_semantic_type_id(value)
        return value

    @model_validator(mode="after")
    def _validate_source_filter(self) -> "EntityTableSpec":
        if bool(self.source_filter_column) != (
            self.source_filter_value is not None
        ):
            raise ValueError(
                "EntityTableSpec source_filter_column and "
                "source_filter_value must be provided together."
            )
        return self


class RelationshipTableSpec(_StrictPersistedModel):
    """Materialization spec for one relationship type's physical table."""

    semantic_id: str = Field(min_length=1)
    table_name: str = Field(min_length=1)
    source_table_name: str | None = Field(default=None, min_length=1)
    source_filter_column: str | None = None
    source_filter_value: str | None = None
    required: bool = True
    approval_state: ApprovalState = "approved"
    relationship_id_column: str = Field(default="relationship_id")
    source_column: str = Field(default="source_entity_id")
    target_column: str = Field(default="target_entity_id")
    evidence_column: str | None = None
    columns: list[ColumnSpec] = Field(default_factory=list)

    @field_validator("semantic_id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not value.startswith("relationship-type:"):
            raise ValueError(
                "RelationshipTableSpec.semantic_id must use "
                "'relationship-type:<slug>'."
            )
        _check_semantic_type_id(value)
        return value

    @model_validator(mode="after")
    def _validate_source_filter(self) -> "RelationshipTableSpec":
        if bool(self.source_filter_column) != (
            self.source_filter_value is not None
        ):
            raise ValueError(
                "RelationshipTableSpec source_filter_column and "
                "source_filter_value must be provided together."
            )
        return self


class DataAvailability(_StrictPersistedModel):
    """Observed data availability for one semantic type (per §7.2).

    Row-count observations MAY warn or suppress but MUST NOT silently
    remove approved Ontology, Graph, or Data Agent schema.
    """

    semantic_id: str = Field(min_length=1)
    observed_rows: int | None = None
    required_rows: int = Field(default=0, ge=0)
    status: DataAvailabilityStatus = "not_observed"

    @model_validator(mode="after")
    def _validate_status_consistency(self) -> "DataAvailability":
        if self.observed_rows is not None and self.observed_rows < 0:
            raise ValueError(
                f"DataAvailability observed_rows must be nonnegative, "
                f"got {self.observed_rows}."
            )
        if self.status == "sufficient":
            if self.observed_rows is None:
                raise ValueError(
                    "DataAvailability status='sufficient' requires observed_rows."
                )
            if self.observed_rows < self.required_rows:
                raise ValueError(
                    f"DataAvailability status='sufficient' but "
                    f"observed_rows={self.observed_rows} < "
                    f"required_rows={self.required_rows}."
                )
        elif self.status == "insufficient":
            if self.observed_rows is None:
                raise ValueError(
                    "DataAvailability status='insufficient' requires "
                    "observed_rows to be set."
                )
            if self.observed_rows >= self.required_rows:
                raise ValueError(
                    f"DataAvailability status='insufficient' but "
                    f"observed_rows={self.observed_rows} >= "
                    f"required_rows={self.required_rows}."
                )
        elif self.status in {"unavailable", "not_observed"}:
            if self.observed_rows is not None:
                raise ValueError(
                    f"DataAvailability status='{self.status}' requires "
                    "observed_rows=None."
                )
        else:  # pragma: no cover - Literal validation rejects this first
            raise ValueError(
                f"Unsupported DataAvailability status: {self.status}."
            )
        return self


class MaterializationPlan(_StrictPersistedModel):
    """Contract-owned materialization plan (SPEC-008A §7.2).

    Separates approved semantic definitions from observed row availability and
    physical optimization decisions.  Downstream deployment SHALL require this
    plan before creating Ontology tables.
    """

    schema_version: Literal[SEMANTIC_SCHEMAS_VERSION] = SEMANTIC_SCHEMAS_VERSION
    manifest_hash: str = Field(default="")
    entity_tables: list[EntityTableSpec] = Field(default_factory=list)
    relationship_tables: list[RelationshipTableSpec] = Field(default_factory=list)
    data_availability: list[DataAvailability] = Field(default_factory=list)
    blocked_competencies: list[str] = Field(default_factory=list)

    _check_manifest_hash = field_validator("manifest_hash", mode="after")(
        _check_hash
    )

    @model_validator(mode="before")
    @classmethod
    def _canonicalize_blocked_competencies(cls, data: Any) -> Any:
        """Sort and deduplicate blocked_competencies (Blocker 5)."""
        if isinstance(data, dict) and "blocked_competencies" in data:
            data["blocked_competencies"] = _canonicalize_string_list(
                data["blocked_competencies"],
                field_name="blocked_competencies",
            )
        return data

    @model_validator(mode="after")
    def _validate_plan_integrity(self) -> "MaterializationPlan":
        entity_ids = [t.semantic_id for t in self.entity_tables]
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError(
                "MaterializationPlan.entity_tables contains duplicate semantic IDs."
            )
        rel_ids = [t.semantic_id for t in self.relationship_tables]
        if len(rel_ids) != len(set(rel_ids)):
            raise ValueError(
                "MaterializationPlan.relationship_tables contains duplicate "
                "semantic IDs."
            )
        avail_ids = [a.semantic_id for a in self.data_availability]
        if len(avail_ids) != len(set(avail_ids)):
            raise ValueError(
                "MaterializationPlan.data_availability contains duplicate "
                "semantic IDs."
            )
        table_ids = set(entity_ids) | set(rel_ids)
        availability_ids = set(avail_ids)
        if availability_ids != table_ids:
            missing = sorted(table_ids - availability_ids)
            extra = sorted(availability_ids - table_ids)
            raise ValueError(
                "MaterializationPlan.data_availability must contain exactly one "
                "entry for every materialized entity and relationship table. "
                f"Missing: {missing}; extra: {extra}."
            )
        return self


# ---------------------------------------------------------------------------
# §6/§7 – Model quality and dependency/invalidation artifacts
# ---------------------------------------------------------------------------


class ModelQualityFinding(_StrictPersistedModel):
    """One source-content-free semantic model quality finding."""

    code: str = Field(min_length=1)
    severity: Literal["warning", "error"]
    semantic_id: str | None = None
    message: str = Field(min_length=1)


class SemanticModelQualityReport(_StrictPersistedModel):
    """Deterministic model-level quality report derived from sealed inputs."""

    schema_version: Literal[SEMANTIC_SCHEMAS_VERSION] = SEMANTIC_SCHEMAS_VERSION
    semantic_contract_hash: str = Field(default="")
    manifest_hash: str = Field(default="")
    status: Literal["passed", "failed"]
    metrics: ModelQualityMetrics = Field(default_factory=ModelQualityMetrics)
    findings: list[ModelQualityFinding] = Field(default_factory=list)
    source_quality_report_hash: str | None = None
    contains_source_content: Literal[False] = False
    report_hash: str = Field(default="")

    _check_semantic_contract_hash = field_validator(
        "semantic_contract_hash", mode="after"
    )(_check_hash)
    _check_manifest_hash = field_validator("manifest_hash", mode="after")(
        _check_hash
    )
    _check_report_hash = field_validator("report_hash", mode="after")(
        _check_hash
    )


class DependencyNode(_StrictPersistedModel):
    """One immutable input or derived artifact in the invalidation graph."""

    artifact_id: str = Field(min_length=1)
    artifact_hash: str = Field(default="")
    depends_on: list[str] = Field(default_factory=list)
    invalidates: list[str] = Field(default_factory=list)

    _check_artifact_hash = field_validator("artifact_hash", mode="after")(
        _check_hash
    )

    @model_validator(mode="before")
    @classmethod
    def _canonicalize_dependencies(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for field_name in ("depends_on", "invalidates"):
                if field_name in data:
                    data[field_name] = _canonicalize_string_list(
                        data[field_name],
                        field_name=field_name,
                    )
        return data


class SemanticDependencyGraph(_StrictPersistedModel):
    """Deterministic dependency and invalidation graph for compiled surfaces."""

    schema_version: Literal[SEMANTIC_SCHEMAS_VERSION] = SEMANTIC_SCHEMAS_VERSION
    semantic_contract_hash: str = Field(default="")
    manifest_hash: str = Field(default="")
    nodes: list[DependencyNode] = Field(default_factory=list)
    graph_hash: str = Field(default="")

    _check_semantic_contract_hash = field_validator(
        "semantic_contract_hash", mode="after"
    )(_check_hash)
    _check_manifest_hash = field_validator("manifest_hash", mode="after")(
        _check_hash
    )
    _check_graph_hash = field_validator("graph_hash", mode="after")(
        _check_hash
    )

    @model_validator(mode="after")
    def _validate_graph(self) -> "SemanticDependencyGraph":
        artifact_ids = [node.artifact_id for node in self.nodes]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError(
                "SemanticDependencyGraph contains duplicate artifact IDs."
            )
        known = set(artifact_ids)
        for node in self.nodes:
            unknown_dependencies = sorted(set(node.depends_on) - known)
            unknown_invalidations = sorted(set(node.invalidates) - known)
            if unknown_dependencies or unknown_invalidations:
                raise ValueError(
                    f"Dependency node '{node.artifact_id}' references unknown "
                    f"artifacts. dependencies={unknown_dependencies}; "
                    f"invalidations={unknown_invalidations}."
                )
        return self


# ---------------------------------------------------------------------------
# §7.4 – Persisted semantic projection receipt
# ---------------------------------------------------------------------------


class QueryReadiness(_StrictPersistedModel):
    """Query readiness evidence for one persisted deployment."""

    count_query_passed: bool = False
    typed_path_query_passed: bool = False
    nonzero_required_competencies: bool = False
    gql_node_count: int | None = None
    gql_edge_count: int | None = None
    canvas_visibility: Literal[
        "not_observed", "visible", "not_visible"
    ] = "not_observed"
    notes: list[str] = Field(default_factory=list)
    # Per-relationship row counts observed during GQL readiness validation (#13).
    # Key = semantic_id; value = observed row count.  Defaults to empty dict
    # so existing persisted receipts round-trip without modification.
    observed_relationship_rows: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_readiness_consistency(self) -> "QueryReadiness":
        if self.gql_node_count is not None and self.gql_node_count < 0:
            raise ValueError(
                f"gql_node_count must be nonnegative, got {self.gql_node_count}."
            )
        if self.gql_edge_count is not None and self.gql_edge_count < 0:
            raise ValueError(
                f"gql_edge_count must be nonnegative, got {self.gql_edge_count}."
            )
        if self.nonzero_required_competencies and not self.count_query_passed:
            raise ValueError(
                "nonzero_required_competencies=True requires count_query_passed=True."
            )
        return self


class PersistedProjectionReceipt(_StrictPersistedModel):
    """Persisted semantic projection receipt (SPEC-008A §7.4).

    Emitted after Ontology and Graph definition read-back and data-plane
    validation.  Downstream Data Agent compilation SHALL consume this receipt
    rather than assuming the submitted definition is the persisted definition.

    All identity, hash, and timestamp fields are required.  Use
    DraftProjectionReceipt for incremental in-progress construction.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    schema_version: Literal[SEMANTIC_SCHEMAS_VERSION] = SEMANTIC_SCHEMAS_VERSION
    semantic_model_manifest_hash: str
    ontology_item_id: str = Field(min_length=1)
    ontology_persisted_projection_hash: str
    graph_model_id: str = Field(min_length=1)
    graph_persisted_projection_hash: str
    ontology_definition_counts: dict[str, int]
    graph_definition_counts: dict[str, int]
    bound_table_counts: dict[str, int]
    query_readiness: QueryReadiness
    validated_at_utc: str = Field(min_length=1)

    _check_manifest_hash = field_validator(
        "semantic_model_manifest_hash", mode="after"
    )(_check_nonempty_hash)
    _check_ontology_hash = field_validator(
        "ontology_persisted_projection_hash", mode="after"
    )(_check_nonempty_hash)
    _check_graph_hash = field_validator(
        "graph_persisted_projection_hash", mode="after"
    )(_check_nonempty_hash)
    _check_validated_at = field_validator(
        "validated_at_utc", mode="after"
    )(_check_utc_timestamp)

    @model_validator(mode="after")
    def _validate_receipt_integrity(self) -> "PersistedProjectionReceipt":
        for counts_field, label in (
            (self.ontology_definition_counts, "ontology_definition_counts"),
            (self.graph_definition_counts, "graph_definition_counts"),
            (self.bound_table_counts, "bound_table_counts"),
        ):
            if not counts_field:
                raise ValueError(f"{label} must contain persisted read-back evidence.")
            for key, value in counts_field.items():
                if value < 0:
                    raise ValueError(
                        f"{label}['{key}'] must be nonnegative, got {value}."
                    )
        if not any(self.ontology_definition_counts.values()):
            raise ValueError(
                "ontology_definition_counts must prove at least one persisted "
                "semantic definition."
            )
        if not any(self.graph_definition_counts.values()):
            raise ValueError(
                "graph_definition_counts must prove at least one persisted "
                "semantic definition."
            )
        if not any(self.bound_table_counts.values()):
            raise ValueError(
                "bound_table_counts must prove at least one populated bound table."
            )
        readiness = self.query_readiness
        if not (
            readiness.count_query_passed
            and readiness.typed_path_query_passed
            and readiness.nonzero_required_competencies
        ):
            raise ValueError(
                "query_readiness must prove count-query, typed-path, and "
                "required-competency readiness."
            )
        if readiness.gql_node_count is None or readiness.gql_node_count <= 0:
            raise ValueError(
                "query_readiness.gql_node_count must be greater than zero."
            )
        if readiness.gql_edge_count is None or readiness.gql_edge_count < 0:
            raise ValueError(
                "query_readiness.gql_edge_count must be nonnegative and present."
            )
        return self


class DraftProjectionReceipt(StrictModel):
    """In-progress projection receipt for incremental deployment construction.

    All identity and hash fields default to empty; any provided hash must be
    a valid sha256.  Use PersistedProjectionReceipt for sealed, fully
    validated receipts that require all identity and hash evidence.
    """

    schema_version: Literal[SEMANTIC_SCHEMAS_VERSION] = SEMANTIC_SCHEMAS_VERSION
    semantic_model_manifest_hash: str = Field(default="")
    ontology_item_id: str = Field(default="")
    ontology_persisted_projection_hash: str = Field(default="")
    graph_model_id: str = Field(default="")
    graph_persisted_projection_hash: str = Field(default="")
    ontology_definition_counts: dict[str, int] = Field(default_factory=dict)
    graph_definition_counts: dict[str, int] = Field(default_factory=dict)
    bound_table_counts: dict[str, int] = Field(default_factory=dict)
    query_readiness: QueryReadiness = Field(default_factory=QueryReadiness)
    validated_at_utc: str = Field(default="")

    _check_manifest_hash = field_validator(
        "semantic_model_manifest_hash", mode="after"
    )(_check_hash)
    _check_ontology_hash = field_validator(
        "ontology_persisted_projection_hash", mode="after"
    )(_check_hash)
    _check_graph_hash = field_validator(
        "graph_persisted_projection_hash", mode="after"
    )(_check_hash)

    def seal(self) -> PersistedProjectionReceipt:
        """Attempt to seal this draft as a complete PersistedProjectionReceipt.

        Raises ValidationError if any required fields are missing or invalid.
        """
        return PersistedProjectionReceipt(**self.model_dump())


# ---------------------------------------------------------------------------
# §8 – Exact Data Agent target and publication receipt
# ---------------------------------------------------------------------------

DataAgentTargetMode = Literal["update", "create", "replace"]
DataAgentAction = Literal["update", "create", "replace", "publish"]

# Four-state classification for per-relationship example gating (#13).
# Derived at runtime from DataAvailability + required flag; never persisted
# as an enum member to preserve DataAvailabilityStatus round-trips.
CompetencyExampleStatus = Literal["pass", "published", "blocked", "skipped", "omitted"]

RelationshipAvailabilityClass = Literal[
    "schema_supported_unobserved",
    "optional_absent",
    "required_absent",
    "executable_nonempty",
]


class CompetencyExampleReceipt(_StrictPersistedModel):
    """Gating decision for one competency example (#13).

    Records whether a few-shot example can be published based on observed
    relationship row counts.  ``status="blocked"`` means a required example
    was suppressed because at least one required relationship has zero rows.
    ``status="skipped"`` means an optional example was silently dropped.
    ``status="pass"`` means the example is safe to publish.
    """

    competency_id: str = Field(min_length=1)
    required: bool = True
    required_relationship_ids: list[str] = Field(default_factory=list)
    observed_rows: dict[str, int] = Field(default_factory=dict)
    min_required_rows: int = Field(default=1, ge=0)
    status: CompetencyExampleStatus = "pass"
    remediation: str = ""
    published: bool = False


class AgentSelectedSource(_StrictPersistedModel):
    """One independently read-back Data Agent source selection."""

    source_type: str = Field(min_length=1)
    source_name: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    selected_element_count: int = Field(ge=1)
    property_child_count: int = Field(ge=0)


class AgentPublicationReceipt(_StrictPersistedModel):
    """Sealed proof that the exact intended Data Agent was published."""

    schema_version: Literal[SEMANTIC_SCHEMAS_VERSION] = SEMANTIC_SCHEMAS_VERSION
    semantic_model_manifest_hash: str
    persisted_projection_receipt_hash: str
    ontology_persisted_projection_hash: str
    graph_persisted_projection_hash: str
    workspace_name: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    data_agent_name: str = Field(min_length=1)
    data_agent_item_id: str = Field(min_length=1)
    configured_target_item_id: str | None = None
    target_mode: DataAgentTargetMode
    actions: list[DataAgentAction] = Field(min_length=2, max_length=2)
    selected_sources: list[AgentSelectedSource] = Field(min_length=1)
    package_instruction_hash: str
    compiled_instruction_hash: str
    draft_instruction_hash: str
    published_instruction_hash: str
    compiled_source_selection_hash: str
    draft_source_selection_hash: str
    published_source_selection_hash: str
    compiled_selected_element_hash: str
    published_selected_element_hash: str
    agent_schema_sidecar_hash: str
    property_child_coverage: float = Field(ge=0.0, le=1.0)
    publication_status: Literal["published"]
    validated_at_utc: str = Field(min_length=1)
    # Property assurance fields (#14) — counts at each stage and deterministic hashes.
    # All default to 0/"" so existing receipts round-trip without change.
    required_property_count: int = Field(default=0, ge=0)
    compiled_property_count: int = Field(default=0, ge=0)
    draft_property_count: int = Field(default=0, ge=0)
    published_property_count: int = Field(default=0, ge=0)
    compiled_property_selection_hash: str = Field(default="")
    published_property_selection_hash: str = Field(default="")
    # Grounding text counts (#12) — char counts for receipt audit trail.
    global_instruction_chars: int = Field(default=0, ge=0)
    instruction_chars: dict[str, int] = Field(default_factory=dict)
    description_chars: dict[str, int] = Field(default_factory=dict)

    _check_hash_fields = field_validator(
        "semantic_model_manifest_hash",
        "persisted_projection_receipt_hash",
        "ontology_persisted_projection_hash",
        "graph_persisted_projection_hash",
        "package_instruction_hash",
        "compiled_instruction_hash",
        "draft_instruction_hash",
        "published_instruction_hash",
        "compiled_source_selection_hash",
        "draft_source_selection_hash",
        "published_source_selection_hash",
        "compiled_selected_element_hash",
        "published_selected_element_hash",
        "agent_schema_sidecar_hash",
        mode="after",
    )(_check_nonempty_hash)
    # New optional hash fields use _check_hash (allows empty) for backward compat.
    _check_optional_hash_fields = field_validator(
        "compiled_property_selection_hash",
        "published_property_selection_hash",
        mode="after",
    )(_check_hash)
    _check_validated_at = field_validator(
        "validated_at_utc", mode="after"
    )(_check_utc_timestamp)

    @model_validator(mode="after")
    def _validate_publication_integrity(self) -> "AgentPublicationReceipt":
        expected_actions = [self.target_mode, "publish"]
        if self.actions != expected_actions:
            raise ValueError(
                f"actions must be {expected_actions!r}, got {self.actions!r}."
            )
        if self.target_mode == "create":
            if self.configured_target_item_id is not None:
                raise ValueError(
                    "create mode cannot carry configured_target_item_id."
                )
        elif not self.configured_target_item_id:
            raise ValueError(
                f"{self.target_mode} mode requires configured_target_item_id."
            )
        if (
            self.target_mode == "update"
            and self.configured_target_item_id != self.data_agent_item_id
        ):
            raise ValueError(
                "update mode must publish the configured Data Agent item ID."
            )
        if (
            self.target_mode == "replace"
            and self.configured_target_item_id == self.data_agent_item_id
        ):
            raise ValueError(
                "replace mode must publish a newly created Data Agent item ID."
            )
        instruction_hashes = {
            self.package_instruction_hash,
            self.compiled_instruction_hash,
            self.draft_instruction_hash,
            self.published_instruction_hash,
        }
        if len(instruction_hashes) != 1:
            raise ValueError(
                "Package, compiled, draft, and published instruction hashes "
                "must match."
            )
        selection_hashes = {
            self.compiled_source_selection_hash,
            self.draft_source_selection_hash,
            self.published_source_selection_hash,
        }
        if len(selection_hashes) != 1:
            raise ValueError(
                "Compiled, draft, and published source-selection hashes "
                "must match."
            )
        if (
            self.compiled_selected_element_hash
            != self.published_selected_element_hash
        ):
            raise ValueError(
                "Published selected elements must match the compiled selection."
            )
        if self.property_child_coverage != 1.0:
            raise ValueError(
                "Published agent-visible property-child coverage must equal 1.0."
            )
        return self


# ---------------------------------------------------------------------------
# §9 – Source-independent semantic query plan
# ---------------------------------------------------------------------------

QueryExecutionStatus = Literal[
    "no_match",
    "optional_data_absent",
    "invalid_semantic_plan",
    "invalid_physical_query",
    "authorization_failure",
    "platform_failure",
    "timeout",
    "concurrency_conflict",
    "partial_result",
    "success",
]


class ComplexityBudget(_StrictPersistedModel):
    """Query complexity budget (SPEC-008A §9.4).

    Default values represent the interactive limits from the spec.
    Competency contracts MAY define lower or higher reviewed limits.
    """

    max_hops: int = Field(default=4, ge=1)
    max_nodes: int = Field(default=6, ge=1)
    max_relationships: int = Field(default=5, ge=1)
    max_rows_per_subquery: int = Field(default=100, ge=1)
    max_subqueries: int = Field(default=4, ge=1)


class SemanticPathStep(_StrictPersistedModel):
    """One hop in a semantic path expression (AST element).

    Optional steps MUST NOT be emitted as mandatory discovery paths (§9.3).
    """

    step_id: str = Field(min_length=1)
    from_type_id: str = Field(min_length=1)
    via_relationship_id: str = Field(min_length=1)
    to_type_id: str = Field(min_length=1)
    direction: Literal["source_to_target", "target_to_source"] = "source_to_target"
    optional: bool = False
    max_depth: int = Field(default=1, ge=1)

    @field_validator("from_type_id", "to_type_id")
    @classmethod
    def _valid_type_id(cls, value: str) -> str:
        _check_semantic_type_id(value)
        return value

    @field_validator("via_relationship_id")
    @classmethod
    def _valid_rel_id(cls, value: str) -> str:
        _check_semantic_type_id(value)
        return value


class SemanticQueryPlan(_StrictPersistedModel):
    """Source-independent semantic query plan (SPEC-008A §9.1).

    Physical GQL, Ontology, Search, or composed queries SHALL be generated
    from this validated plan plus the persisted projection crosswalk.

    Invariants enforced:
    - required_relationships and optional_relationships are disjoint (§9.3)
    - required and optional relationship declarations exactly match path
      semantics, so a declared route cannot be omitted from validation
    - complexity budget defaults match SPEC-008A §9.4
    - set-like fields (required_types, required_relationships,
      optional_relationships, requested_properties, requested_concepts)
      are canonicalized (sorted, deduplicated) at construction time for
      deterministic hashing; path_steps ordering is preserved
    - required path depth, node references, and relationship references must
      not exceed the reviewed complexity budget
    """

    schema_version: Literal[SEMANTIC_SCHEMAS_VERSION] = SEMANTIC_SCHEMAS_VERSION
    plan_hash: str = Field(
        default="",
        description="sha256 hash of this plan excluding this field.",
    )
    manifest_hash: str = Field(default="")
    intent: str = Field(min_length=1)
    requested_concepts: list[str] = Field(default_factory=list)
    required_types: list[str] = Field(default_factory=list)
    required_relationships: list[str] = Field(default_factory=list)
    optional_relationships: list[str] = Field(default_factory=list)
    requested_properties: list[str] = Field(default_factory=list)
    evidence_required: bool = True
    path_steps: list[SemanticPathStep] = Field(default_factory=list)
    budget: ComplexityBudget = Field(default_factory=ComplexityBudget)

    _check_plan_hash = field_validator("plan_hash", mode="after")(_check_hash)
    _check_manifest_hash = field_validator("manifest_hash", mode="after")(
        _check_hash
    )

    @model_validator(mode="before")
    @classmethod
    def _canonicalize_set_fields(cls, data: Any) -> Any:
        """Sort and deduplicate set-like fields before construction."""
        if isinstance(data, dict):
            for field in (
                "requested_concepts",
                "required_types",
                "required_relationships",
                "optional_relationships",
                "requested_properties",
            ):
                if field in data:
                    data[field] = _canonicalize_string_list(
                        data[field], field_name=field
                    )
        return data

    @model_validator(mode="after")
    def _validate_plan_semantics(self) -> "SemanticQueryPlan":
        # §9.3: required and optional relationship sets must be disjoint
        req = set(self.required_relationships)
        opt = set(self.optional_relationships)
        overlap = req & opt
        if overlap:
            raise ValueError(
                "required_relationships and optional_relationships must be "
                f"disjoint. Overlapping IDs: {sorted(overlap)}"
            )

        # §9.3: optional path_steps must be marked optional=True
        req_step_rels = {
            step.via_relationship_id
            for step in self.path_steps
            if not step.optional
        }
        false_optional = opt & req_step_rels
        if false_optional:
            raise ValueError(
                "Path steps for optional relationships must have optional=True. "
                f"Violations: {sorted(false_optional)}"
            )
        optional_step_rels = {
            step.via_relationship_id
            for step in self.path_steps
            if step.optional
        }
        missing_optional_steps = opt - optional_step_rels
        if missing_optional_steps:
            raise ValueError(
                "Every optional_relationships entry requires an optional "
                "SemanticPathStep. Missing: "
                f"{sorted(missing_optional_steps)}"
            )
        undeclared_optional_steps = optional_step_rels - opt
        if undeclared_optional_steps:
            raise ValueError(
                "Optional SemanticPathStep relationships must be declared in "
                "optional_relationships. Undeclared: "
                f"{sorted(undeclared_optional_steps)}"
            )
        missing_required_steps = req - req_step_rels
        undeclared_required_steps = req_step_rels - req
        if missing_required_steps or undeclared_required_steps:
            raise ValueError(
                "Required relationship declarations must exactly match "
                "non-optional SemanticPathStep relationships. "
                f"Missing path steps: {sorted(missing_required_steps)}; "
                f"undeclared path steps: {sorted(undeclared_required_steps)}"
            )
        # §9.4: budget enforcement (shape-local invariants)
        required_hops = sum(
            step.max_depth for step in self.path_steps if not step.optional
        )
        if required_hops > self.budget.max_hops:
            raise ValueError(
                f"Plan has {required_hops} required bounded hop(s) but "
                f"budget.max_hops={self.budget.max_hops}. "
                "Plans exceeding budget must be decomposed before submission "
                "(SPEC-008A §9.4)."
            )
        node_references = set(self.required_types)
        for step in self.path_steps:
            node_references.add(step.from_type_id)
            node_references.add(step.to_type_id)
        if len(node_references) > self.budget.max_nodes:
            raise ValueError(
                f"Plan references {len(node_references)} node type(s) but "
                f"budget.max_nodes={self.budget.max_nodes}. "
                "Plans exceeding budget must be decomposed before submission "
                "(SPEC-008A §9.4)."
            )
        relationship_references = req | opt | {
            step.via_relationship_id for step in self.path_steps
        }
        total_rels = len(relationship_references)
        if total_rels > self.budget.max_relationships:
            raise ValueError(
                f"Plan declares {total_rels} relationship(s) but "
                f"budget.max_relationships={self.budget.max_relationships}. "
                "Plans exceeding budget must be decomposed before submission "
                "(SPEC-008A §9.4)."
            )

        return self


# ---------------------------------------------------------------------------
# S8A-QRY-002 – Persisted query schema (strict, deterministic, sealed)
# ---------------------------------------------------------------------------
#
# Derived deterministically from a sealed SemanticModelManifest (and,
# optionally, its SemanticCrosswalk) via ``build_persisted_query_schema``.
# Provides the ground truth that a SemanticQueryPlan and its generated
# physical GQL are validated against before execution: manifest identity,
# graph node labels, owner-scoped graph properties, and directed
# relationship labels/endpoints.


class PersistedQueryNodeSchema(_StrictPersistedModel):
    """One entity type's persisted graph identity for query validation.

    ``label`` is empty when the type has no physical graph projection
    (query plans referencing it must fail closed as not physically
    available, not merely as an unknown label).

    ``owner_properties`` maps canonical property ID (e.g. a
    ``ManifestPropertyEntry.property_id``) to its physical graph property
    key, preserving the canonical <-> physical association rather than a
    flat list of physical names.  A ``SemanticQueryPlan.requested_properties``
    entry resolves against this mapping by canonical ID (preferred) or,
    for backwards compatibility, by physical key.
    """

    semantic_id: str = Field(min_length=1)
    label: str = Field(default="")
    owner_properties: dict[str, str] = Field(default_factory=dict)

    @field_validator("semantic_id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        _check_semantic_type_id(value)
        return value

    @field_validator("owner_properties")
    @classmethod
    def _valid_owner_properties(cls, value: dict[str, str]) -> dict[str, str]:
        for property_id, physical_key in value.items():
            if not property_id or not property_id.strip():
                raise ValueError(
                    "owner_properties keys (canonical property IDs) must "
                    "not be empty."
                )
            if not physical_key or not physical_key.strip():
                raise ValueError(
                    f"owner_properties['{property_id}'] (physical graph "
                    "property key) must not be empty."
                )
        return value

    @property
    def physical_property_keys(self) -> frozenset[str]:
        """The set of physical graph property keys owned by this node."""
        return frozenset(self.owner_properties.values())


class PersistedQueryRelationshipSchema(_StrictPersistedModel):
    """One relationship type's persisted graph identity for query validation.

    ``source_type_id``/``target_type_id`` are canonical semantic IDs (used to
    validate SemanticQueryPlan path-step endpoints); ``source_label``/
    ``target_label``/``label`` are physical graph labels (used to validate
    generated GQL).  Any of the three labels may be empty when the
    relationship or one of its endpoints has no physical graph projection.
    """

    semantic_id: str = Field(min_length=1)
    label: str = Field(default="")
    source_type_id: str = Field(min_length=1)
    target_type_id: str = Field(min_length=1)
    source_label: str = Field(default="")
    target_label: str = Field(default="")
    direction: Literal["source_to_target"] = "source_to_target"

    @field_validator("semantic_id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        _check_semantic_type_id(value)
        return value

    @field_validator("source_type_id", "target_type_id")
    @classmethod
    def _valid_endpoint_id(cls, value: str) -> str:
        _check_semantic_type_id(value)
        return value


class PersistedQuerySchema(_StrictPersistedModel):
    """Strict, sealed persisted query schema (SPEC-008A §9.1, §9.2).

    Seal via ``compute_persisted_query_schema_hash`` (or use
    ``build_persisted_query_schema``, which seals automatically).  Every
    SemanticQueryPlan and every generated physical query SHALL be validated
    against exactly one sealed PersistedQuerySchema instance whose
    ``manifest_hash`` matches the plan's declared manifest identity.
    """

    schema_version: Literal[SEMANTIC_SCHEMAS_VERSION] = SEMANTIC_SCHEMAS_VERSION
    manifest_hash: str = Field(default="")
    nodes: list[PersistedQueryNodeSchema] = Field(default_factory=list)
    relationships: list[PersistedQueryRelationshipSchema] = Field(
        default_factory=list
    )
    schema_hash: str = Field(
        default="",
        description="sha256 hash of this schema excluding this field.",
    )

    _check_manifest_hash = field_validator("manifest_hash", mode="after")(
        _check_hash
    )
    _check_schema_hash = field_validator("schema_hash", mode="after")(
        _check_hash
    )

    @model_validator(mode="after")
    def _validate_schema_integrity(self) -> "PersistedQuerySchema":
        node_ids = [n.semantic_id for n in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError(
                "PersistedQuerySchema.nodes contains duplicate semantic IDs."
            )
        rel_ids = [r.semantic_id for r in self.relationships]
        if len(rel_ids) != len(set(rel_ids)):
            raise ValueError(
                "PersistedQuerySchema.relationships contains duplicate "
                "semantic IDs."
            )
        known_nodes = set(node_ids)
        for rel in self.relationships:
            if rel.source_type_id not in known_nodes:
                raise ValueError(
                    f"Relationship '{rel.semantic_id}' source_type_id "
                    f"'{rel.source_type_id}' is not in schema nodes."
                )
            if rel.target_type_id not in known_nodes:
                raise ValueError(
                    f"Relationship '{rel.semantic_id}' target_type_id "
                    f"'{rel.target_type_id}' is not in schema nodes."
                )
        return self


# ---------------------------------------------------------------------------
# §10.4 – Semantic diagnostic record
# ---------------------------------------------------------------------------

# Failure categories that constitute required-source execution failures (§10.1)
_SOURCE_FAILURE_STATUSES: frozenset[str] = frozenset({
    "invalid_semantic_plan",
    "invalid_physical_query",
    "authorization_failure",
    "platform_failure",
    "timeout",
})


class SemanticDiagnosticRecord(_StrictPersistedModel):
    """Per-run diagnostic record for one semantic query execution (§10.4).

    All SPEC-008A §10.4 envelope fields are required.  Status/failure
    consistency is enforced at construction:

    - required-source failure cannot be reported as semantic success
    - concurrency_conflict cannot be reported as semantic success

    Source text and credentials are redacted by default (not stored here).
    Use PartialDiagnosticExport for in-progress or incomplete ingestion.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    schema_version: Literal[SEMANTIC_SCHEMAS_VERSION] = SEMANTIC_SCHEMAS_VERSION
    # Required envelope fields (§10.4)
    export_freshness_watermark: str = Field(min_length=1)
    partial_snapshot: bool
    overlapping_snapshot: bool
    workspace_id: str = Field(min_length=1)
    target_item_id: str = Field(min_length=1)
    semantic_contract_hash: str
    manifest_hash: str
    ontology_projection_hash: str
    graph_projection_hash: str
    search_projection_hash: str
    instruction_hash: str
    source_selection_hash: str
    query_schema_hash: str
    selected_source: str = Field(min_length=1)
    semantic_plan: SemanticQueryPlan
    semantic_plan_hash: str
    physical_query_hash: str
    static_validation_passed: bool
    query_row_count: int = Field(ge=0)
    result_category: QueryExecutionStatus
    error_category: str | None
    request_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    operation_id: str = Field(min_length=1)
    latency_ms: float = Field(ge=0)
    retry_count: int = Field(ge=0)
    evidence_ids: list[str]
    final_semantic_status: QueryExecutionStatus
    notes: list[str] = Field(default_factory=list)

    _check_manifest_hash = field_validator("manifest_hash", mode="after")(
        _check_nonempty_hash
    )
    _check_plan_hash = field_validator("semantic_plan_hash", mode="after")(
        _check_nonempty_hash
    )
    _check_semantic_contract_hash = field_validator(
        "semantic_contract_hash", mode="after"
    )(_check_nonempty_hash)
    _check_ontology_hash = field_validator(
        "ontology_projection_hash", mode="after"
    )(_check_nonempty_hash)
    _check_graph_hash = field_validator("graph_projection_hash", mode="after")(
        _check_nonempty_hash
    )
    _check_search_hash = field_validator("search_projection_hash", mode="after")(
        _check_nonempty_hash
    )
    _check_instruction_hash = field_validator("instruction_hash", mode="after")(
        _check_nonempty_hash
    )
    _check_source_selection_hash = field_validator(
        "source_selection_hash", mode="after"
    )(
        _check_nonempty_hash
    )
    _check_query_schema_hash = field_validator(
        "query_schema_hash", mode="after"
    )(_check_nonempty_hash)
    _check_query_hash = field_validator("physical_query_hash", mode="after")(
        _check_nonempty_hash
    )
    _check_watermark = field_validator(
        "export_freshness_watermark", mode="after"
    )(_check_utc_timestamp)

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def _validate_evidence_ids(cls, value: Any) -> Any:
        return _canonicalize_string_list(value, field_name="evidence_ids")

    @model_validator(mode="after")
    def _validate_status_consistency(self) -> "SemanticDiagnosticRecord":
        computed_plan_hash = compute_query_plan_hash(self.semantic_plan)
        if self.semantic_plan_hash != computed_plan_hash:
            raise ValueError(
                "semantic_plan_hash does not match the embedded semantic_plan."
            )
        # §10.1: required-source failure cannot be reported as semantic success
        if (
            self.result_category in _SOURCE_FAILURE_STATUSES
            and self.final_semantic_status == "success"
        ):
            raise ValueError(
                f"final_semantic_status='success' with result_category="
                f"'{self.result_category}': required-source failure cannot be "
                "reported as semantic success (SPEC-008A §10.1 "
                "no-success-shaped-failure)."
            )
        # §10.2: concurrency conflict cannot be reported as semantic success
        if (
            self.result_category == "concurrency_conflict"
            and self.final_semantic_status == "success"
        ):
            raise ValueError(
                "result_category='concurrency_conflict' cannot yield "
                "final_semantic_status='success'. Concurrency conflicts must "
                "surface as concurrency_conflict or partial_result to preserve "
                "the first actionable failure (SPEC-008A §10.2)."
            )
        if self.final_semantic_status == "success" and self.result_category != "success":
            raise ValueError(
                "final_semantic_status='success' requires "
                "result_category='success'."
            )
        if self.final_semantic_status == "success" and not self.static_validation_passed:
            raise ValueError(
                "final_semantic_status='success' requires "
                "static_validation_passed=True."
            )
        if self.final_semantic_status == "success" and self.query_row_count <= 0:
            raise ValueError(
                "final_semantic_status='success' requires query_row_count > 0."
            )
        if (
            self.result_category in _SOURCE_FAILURE_STATUSES
            or self.result_category == "concurrency_conflict"
        ):
            if not self.error_category:
                raise ValueError(
                    f"result_category='{self.result_category}' requires "
                    "error_category."
                )
        elif self.error_category is not None:
            raise ValueError(
                "error_category must be null when result_category is not a "
                "source or concurrency failure."
            )
        if (
            self.partial_snapshot or self.overlapping_snapshot
        ) and self.final_semantic_status == "success":
            raise ValueError(
                "Partial or overlapping diagnostic snapshots cannot be sealed "
                "as semantic success."
            )
        # §10.3/§10.4: successful records must retain non-empty evidence IDs.
        if self.final_semantic_status == "success" and not self.evidence_ids:
            raise ValueError(
                "final_semantic_status='success' requires non-empty evidence_ids. "
                "Successful runs must retain evidence IDs per SPEC-008A §10.3/§10.4."
            )
        return self


class PartialDiagnosticExport(StrictModel):
    """Partial diagnostic record for in-progress or incomplete ingestion.

    All envelope fields are optional.  Use SemanticDiagnosticRecord for
    sealed, complete diagnostic envelopes that enforce §10.4 invariants.
    Partial exports are suitable for streaming ingestion before all fields
    are available, and for constructing test fixtures against checker
    functions.
    """

    schema_version: Literal[SEMANTIC_SCHEMAS_VERSION] = SEMANTIC_SCHEMAS_VERSION
    export_freshness_watermark: str = Field(default="")
    partial_snapshot: bool = False
    overlapping_snapshot: bool = False
    workspace_id: str = Field(default="")
    target_item_id: str = Field(default="")
    semantic_contract_hash: str = Field(default="")
    manifest_hash: str = Field(default="")
    ontology_projection_hash: str = Field(default="")
    graph_projection_hash: str = Field(default="")
    search_projection_hash: str = Field(default="")
    instruction_hash: str = Field(default="")
    source_selection_hash: str = Field(default="")
    query_schema_hash: str = Field(default="")
    selected_source: str | None = None
    semantic_plan: SemanticQueryPlan | None = None
    semantic_plan_hash: str = Field(default="")
    physical_query_hash: str = Field(default="")
    static_validation_passed: bool | None = None
    query_row_count: int | None = None
    result_category: QueryExecutionStatus | None = None
    error_category: str | None = None
    request_id: str | None = None
    correlation_id: str | None = None
    thread_id: str | None = None
    run_id: str | None = None
    operation_id: str | None = None
    latency_ms: float | None = None
    retry_count: int = Field(default=0, ge=0)
    evidence_ids: list[str] = Field(default_factory=list)
    final_semantic_status: QueryExecutionStatus | None = None
    notes: list[str] = Field(default_factory=list)

    _check_semantic_contract_hash = field_validator(
        "semantic_contract_hash", mode="after"
    )(_check_hash)
    _check_manifest_hash = field_validator("manifest_hash", mode="after")(
        _check_hash
    )
    _check_ontology_hash = field_validator(
        "ontology_projection_hash", mode="after"
    )(_check_hash)
    _check_graph_hash = field_validator("graph_projection_hash", mode="after")(
        _check_hash
    )
    _check_search_hash = field_validator("search_projection_hash", mode="after")(
        _check_hash
    )
    _check_instruction_hash = field_validator("instruction_hash", mode="after")(
        _check_hash
    )
    _check_source_selection_hash = field_validator(
        "source_selection_hash", mode="after"
    )(_check_hash)
    _check_plan_hash = field_validator("semantic_plan_hash", mode="after")(
        _check_hash
    )
    _check_query_hash = field_validator("physical_query_hash", mode="after")(
        _check_hash
    )


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------


def _canonical_json(obj: Any) -> str:
    """Deterministic, compact JSON suitable for hashing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def compute_manifest_hash(manifest: SemanticModelManifest) -> str:
    """Compute sha256 over the manifest, excluding the manifest_hash field.

    Set-like sub-fields (identifier_properties, published_properties, aliases
    on entity types) are canonicalized (sorted, deduplicated) before hashing
    so that equivalent semantic inputs produce identical hashes regardless of
    insertion order.

    Usage::

        manifest = SemanticModelManifest(...)
        sealed = manifest.model_copy(
            update={"manifest_hash": compute_manifest_hash(manifest)}
        )
    """
    payload = manifest.model_dump(mode="json")
    payload.pop("manifest_hash", None)
    # Manifest collections are semantic sets. Their serialized order must not
    # change model identity; path-like ordered collections are not present here.
    payload["entity_types"] = sorted(
        payload.get("entity_types", []),
        key=lambda entry: entry["semantic_id"],
    )
    payload["relationship_types"] = sorted(
        payload.get("relationship_types", []),
        key=lambda entry: entry["semantic_id"],
    )
    payload["property_definitions"] = sorted(
        payload.get("property_definitions", []),
        key=lambda entry: (entry["owner_type_id"], entry["property_id"]),
    )
    payload["competency_coverage"] = sorted(
        payload.get("competency_coverage", []),
        key=lambda entry: entry["competency_id"],
    )
    # Canonicalize set-like sub-fields in entity types.
    for entity in payload.get("entity_types", []):
        if isinstance(entity.get("identifier_properties"), list):
            entity["identifier_properties"] = sorted(
                set(entity["identifier_properties"])
            )
        if isinstance(entity.get("published_properties"), list):
            entity["published_properties"] = sorted(
                set(entity["published_properties"])
            )
        if isinstance(entity.get("aliases"), list):
            entity["aliases"] = sorted(set(entity["aliases"]))
    digest = hashlib.sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


def compute_query_plan_hash(plan: SemanticQueryPlan) -> str:
    """Compute sha256 over the plan, excluding the plan_hash field.

    Set-like fields (required_types, required_relationships,
    optional_relationships, requested_properties, requested_concepts) are
    already canonicalized (sorted, deduplicated) at construction time by the
    model validator.  path_steps ordering is preserved.
    """
    payload = plan.model_dump(mode="json")
    payload.pop("plan_hash", None)
    digest = hashlib.sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


def compute_model_quality_report_hash(
    report: SemanticModelQualityReport,
) -> str:
    """Compute sha256 over a model quality report excluding report_hash."""
    payload = report.model_dump(mode="json")
    payload.pop("report_hash", None)
    payload["findings"] = sorted(
        payload.get("findings", []),
        key=lambda finding: (
            finding["severity"],
            finding["code"],
            finding.get("semantic_id") or "",
            finding["message"],
        ),
    )
    digest = hashlib.sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


def compute_dependency_graph_hash(
    graph: SemanticDependencyGraph,
) -> str:
    """Compute sha256 over a dependency graph excluding graph_hash."""
    payload = graph.model_dump(mode="json")
    payload.pop("graph_hash", None)
    payload["nodes"] = sorted(
        payload.get("nodes", []),
        key=lambda node: node["artifact_id"],
    )
    digest = hashlib.sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


def compute_persisted_query_schema_hash(schema: PersistedQuerySchema) -> str:
    """Compute sha256 over a persisted query schema excluding schema_hash.

    Nodes and relationships are sorted by semantic_id before hashing so
    equivalent schemas produce identical hashes regardless of insertion
    order.  ``owner_properties`` (canonical property ID -> physical key) is
    a JSON object, so ``_canonical_json``'s ``sort_keys=True`` already makes
    its serialization order-independent.
    """
    payload = schema.model_dump(mode="json")
    payload.pop("schema_hash", None)
    payload["nodes"] = sorted(
        payload.get("nodes", []),
        key=lambda node: node["semantic_id"],
    )
    payload["relationships"] = sorted(
        payload.get("relationships", []),
        key=lambda rel: rel["semantic_id"],
    )
    digest = hashlib.sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"
