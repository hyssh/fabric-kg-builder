"""Strict models for the SPEC-008 canonical semantic contract."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SEMANTIC_CONTRACT_SCHEMA_VERSION = "1.0"
_SEMANTIC_ID_RE = re.compile(
    r"^(?:entity-type|relationship-type):[a-z0-9][a-z0-9._-]*$"
)
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 _.-]*$")
_PROPERTY_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

PublicationStatus = Literal["core", "optional", "experimental", "excluded"]
PropertyType = Literal[
    "string",
    "integer",
    "number",
    "boolean",
    "datetime",
    "date",
    "uri",
    "json",
]
EvidencePolicy = Literal["required_for_asserted", "optional", "none"]
AssertionStatus = Literal[
    "asserted",
    "normalized",
    "derived",
    "inferred",
    "unresolved",
    "rejected",
]
TemporalPolicy = Literal["required", "optional", "not_applicable"]


def _unique_strings(values: list[str]) -> list[str]:
    """Return non-empty strings in first-seen order."""
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class StrictModel(BaseModel):
    """Base model that rejects unknown fields and trims strings."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ApprovalMetadata(StrictModel):
    """Approval state attached to one semantic contract version."""

    status: Literal["draft", "needs_review", "approved"] = "draft"
    approved_by: str | None = None
    approved_at_utc: str | None = None
    contract_hash: str | None = None
    notes: list[str] = Field(default_factory=list)

    _dedupe_notes = field_validator("notes")(_unique_strings)


class PropertyDefinition(StrictModel):
    """One business property on an entity type."""

    name: str = Field(min_length=1)
    type: PropertyType
    required: bool = False
    description: str = ""
    aliases: list[str] = Field(default_factory=list)

    _dedupe_aliases = field_validator("aliases")(_unique_strings)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not _PROPERTY_NAME_RE.fullmatch(value):
            raise ValueError(
                "Property names must start with a letter or underscore and contain "
                "only letters, numbers, and underscores."
            )
        return value


class EntityTypeDefinition(StrictModel):
    """One authoritative or reviewable semantic entity type."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    business_name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    abstract: bool = False
    parent: str | None = None
    identifiers: list[str] = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    properties: list[PropertyDefinition] = Field(min_length=1)
    lineage_properties: list[str] = Field(default_factory=list)
    publication_status: PublicationStatus = "core"

    _dedupe_identifiers = field_validator("identifiers")(_unique_strings)
    _dedupe_aliases = field_validator("aliases")(_unique_strings)
    _dedupe_lineage = field_validator("lineage_properties")(_unique_strings)

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not value.startswith("entity-type:") or not _SEMANTIC_ID_RE.fullmatch(value):
            raise ValueError("Entity type IDs must use 'entity-type:<slug>'.")
        return value

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not _NAME_RE.fullmatch(value):
            raise ValueError("Entity type names contain unsupported characters.")
        return value

    @model_validator(mode="after")
    def _validate_properties(self) -> "EntityTypeDefinition":
        names = [prop.name for prop in self.properties]
        if len(names) != len(set(names)):
            raise ValueError(f"Entity type '{self.name}' has duplicate properties.")
        missing = sorted(set(self.identifiers) - set(names))
        if missing:
            raise ValueError(
                f"Entity type '{self.name}' identifiers reference unknown "
                f"properties: {', '.join(missing)}"
            )
        return self


class InverseDefinition(StrictModel):
    """Business inverse of one relationship."""

    predicate: str = Field(min_length=1)
    materialization: Literal["virtual", "materialized", "none"] = "virtual"


class Cardinality(StrictModel):
    """Source and target relationship cardinality."""

    source: Literal["one", "many"] = "many"
    target: Literal["one", "many"] = "many"


class AssertionPolicy(StrictModel):
    """Allowed assertion states and the default extracted state."""

    allowed_statuses: list[AssertionStatus] = Field(
        default_factory=lambda: ["asserted", "unresolved"]
    )
    default_status: AssertionStatus = "unresolved"

    @model_validator(mode="after")
    def _validate_default(self) -> "AssertionPolicy":
        self.allowed_statuses = list(dict.fromkeys(self.allowed_statuses))
        if self.default_status not in self.allowed_statuses:
            raise ValueError("default_status must be present in allowed_statuses.")
        return self


class RelationshipTypeDefinition(StrictModel):
    """One directed business relationship definition."""

    id: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    business_name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    source_type: str = Field(min_length=1)
    target_type: str = Field(min_length=1)
    direction: Literal["source_to_target"] = "source_to_target"
    inverse: InverseDefinition | None = None
    cardinality: Cardinality = Field(default_factory=Cardinality)
    evidence_policy: EvidencePolicy = "required_for_asserted"
    assertion_policy: AssertionPolicy = Field(default_factory=AssertionPolicy)
    temporal: TemporalPolicy = "optional"
    publication_status: PublicationStatus = "core"

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not value.startswith("relationship-type:") or not _SEMANTIC_ID_RE.fullmatch(
            value
        ):
            raise ValueError(
                "Relationship type IDs must use 'relationship-type:<slug>'."
            )
        return value

    @field_validator("predicate")
    @classmethod
    def _valid_predicate(cls, value: str) -> str:
        if not _PROPERTY_NAME_RE.fullmatch(value):
            raise ValueError(
                "Relationship predicates must use letters, numbers, and underscores."
            )
        return value


class SemanticContract(StrictModel):
    """Approved source of semantic meaning for all compiled artifacts."""

    schema_version: Literal[SEMANTIC_CONTRACT_SCHEMA_VERSION] = (
        SEMANTIC_CONTRACT_SCHEMA_VERSION
    )
    contract_version: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    entity_types: list[EntityTypeDefinition] = Field(min_length=1)
    relationship_types: list[RelationshipTypeDefinition] = Field(default_factory=list)
    approval: ApprovalMetadata = Field(default_factory=ApprovalMetadata)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_semantics(self) -> "SemanticContract":
        entity_ids = [entity.id for entity in self.entity_types]
        entity_names = [entity.name for entity in self.entity_types]
        relationship_ids = [relationship.id for relationship in self.relationship_types]
        predicates = [
            relationship.predicate for relationship in self.relationship_types
        ]
        for label, values in (
            ("entity type ID", entity_ids),
            ("entity type name", entity_names),
            ("relationship type ID", relationship_ids),
            ("relationship predicate", predicates),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"Duplicate {label} in semantic contract.")

        known_ids = set(entity_ids)
        for entity in self.entity_types:
            if entity.parent and entity.parent not in known_ids:
                raise ValueError(
                    f"Entity type '{entity.name}' references unknown parent "
                    f"'{entity.parent}'."
                )
            if entity.parent == entity.id:
                raise ValueError(f"Entity type '{entity.name}' cannot parent itself.")

        parent_by_id = {entity.id: entity.parent for entity in self.entity_types}
        for entity_id in entity_ids:
            seen: set[str] = set()
            cursor: str | None = entity_id
            while cursor:
                if cursor in seen:
                    raise ValueError("Entity inheritance contains a cycle.")
                seen.add(cursor)
                cursor = parent_by_id.get(cursor)

        status_by_id = {
            entity.id: entity.publication_status for entity in self.entity_types
        }
        for relationship in self.relationship_types:
            if relationship.source_type not in known_ids:
                raise ValueError(
                    f"Relationship '{relationship.predicate}' references unknown "
                    f"source type '{relationship.source_type}'."
                )
            if relationship.target_type not in known_ids:
                raise ValueError(
                    f"Relationship '{relationship.predicate}' references unknown "
                    f"target type '{relationship.target_type}'."
                )
            if relationship.publication_status == "core" and (
                status_by_id[relationship.source_type] == "excluded"
                or status_by_id[relationship.target_type] == "excluded"
            ):
                raise ValueError(
                    f"Core relationship '{relationship.predicate}' cannot target "
                    "an excluded entity type."
                )
            if (
                relationship.inverse
                and relationship.inverse.materialization == "materialized"
                and relationship.inverse.predicate not in predicates
            ):
                raise ValueError(
                    f"Relationship '{relationship.predicate}' materializes inverse "
                    f"'{relationship.inverse.predicate}', but that predicate is not "
                    "defined in the contract."
                )
        return self


class StableIdBinding(StrictModel):
    """Stable semantic ID and optional existing Fabric physical ID."""

    semantic_id: str = Field(min_length=1)
    fabric_id: str | None = None

    @field_validator("semantic_id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not _SEMANTIC_ID_RE.fullmatch(value):
            raise ValueError("Stable semantic ID uses an unsupported format.")
        return value


class StableIdLock(StrictModel):
    """Versioned lock mapping business names to stable semantic IDs."""

    schema_version: Literal[SEMANTIC_CONTRACT_SCHEMA_VERSION] = (
        SEMANTIC_CONTRACT_SCHEMA_VERSION
    )
    entity_types: dict[str, StableIdBinding]
    relationship_types: dict[str, StableIdBinding] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_unique_ids(self) -> "StableIdLock":
        bindings = list(self.entity_types.values()) + list(
            self.relationship_types.values()
        )
        semantic_ids = [binding.semantic_id for binding in bindings]
        if len(semantic_ids) != len(set(semantic_ids)):
            raise ValueError("Stable ID lock contains duplicate semantic IDs.")
        fabric_ids = [
            binding.fabric_id for binding in bindings if binding.fabric_id is not None
        ]
        if len(fabric_ids) != len(set(fabric_ids)):
            raise ValueError("Stable ID lock contains duplicate Fabric IDs.")
        return self


class EntityMapping(StrictModel):
    """Physical table binding for one semantic entity type."""

    semantic_id: str
    table: str = Field(min_length=1)
    entity_id_column: str = Field(min_length=1)
    display_name_column: str = Field(min_length=1)
    property_columns: dict[str, str] = Field(default_factory=dict)
    type_filter_column: str | None = None
    type_filter_value: str | None = None

    @model_validator(mode="after")
    def _validate_filter(self) -> "EntityMapping":
        if bool(self.type_filter_column) != (self.type_filter_value is not None):
            raise ValueError(
                "Entity type filter column and value must be provided together."
            )
        return self


class RelationshipMapping(StrictModel):
    """Physical table binding for one semantic relationship type."""

    semantic_id: str
    table: str = Field(min_length=1)
    relationship_id_column: str = Field(min_length=1)
    source_entity_id_column: str = Field(min_length=1)
    target_entity_id_column: str = Field(min_length=1)
    evidence_id_column: str | None = None
    type_filter_column: str | None = None
    type_filter_value: str | None = None

    @model_validator(mode="after")
    def _validate_filter(self) -> "RelationshipMapping":
        if bool(self.type_filter_column) != (self.type_filter_value is not None):
            raise ValueError(
                "Relationship type filter column and value must be provided together."
            )
        return self


class PhysicalMappings(StrictModel):
    """Physical source/destination bindings kept outside semantic meaning."""

    schema_version: Literal[SEMANTIC_CONTRACT_SCHEMA_VERSION] = (
        SEMANTIC_CONTRACT_SCHEMA_VERSION
    )
    entity_types: list[EntityMapping]
    relationship_types: list[RelationshipMapping] = Field(default_factory=list)


class VocabularyTerm(StrictModel):
    """One controlled term and aliases."""

    id: str = Field(min_length=1)
    preferred_label: str = Field(min_length=1)
    definition: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)

    _dedupe_aliases = field_validator("aliases")(_unique_strings)


class Vocabulary(StrictModel):
    """Controlled vocabulary associated with a semantic contract."""

    schema_version: Literal[SEMANTIC_CONTRACT_SCHEMA_VERSION] = (
        SEMANTIC_CONTRACT_SCHEMA_VERSION
    )
    terms: list[VocabularyTerm] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_terms(self) -> "Vocabulary":
        ids = [term.id for term in self.terms]
        labels = [term.preferred_label.lower() for term in self.terms]
        if len(ids) != len(set(ids)):
            raise ValueError("Vocabulary contains duplicate term IDs.")
        if len(labels) != len(set(labels)):
            raise ValueError("Vocabulary contains duplicate preferred labels.")
        return self
