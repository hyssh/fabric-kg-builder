"""Compatibility classification for semantic contract version changes."""

from __future__ import annotations

import sys

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    from enum import Enum

    class StrEnum(str, Enum):  # type: ignore[no-redef]
        """Minimal StrEnum backport for Python 3.10."""

from pydantic import Field

from .models import SemanticContract, StrictModel


class CompatibilityLevel(StrEnum):
    """Deployment impact of a semantic contract change."""

    COMPATIBLE = "compatible"
    CONDITIONAL = "conditional"
    BREAKING = "breaking"


_LEVEL_RANK = {
    CompatibilityLevel.COMPATIBLE: 0,
    CompatibilityLevel.CONDITIONAL: 1,
    CompatibilityLevel.BREAKING: 2,
}


class CompatibilityChange(StrictModel):
    """One classified semantic change."""

    level: CompatibilityLevel
    code: str
    path: str
    message: str


class CompatibilityReport(StrictModel):
    """Aggregate result for a previous/current contract comparison."""

    level: CompatibilityLevel
    previous_version: str
    current_version: str
    changes: list[CompatibilityChange] = Field(default_factory=list)


def _append(
    changes: list[CompatibilityChange],
    level: CompatibilityLevel,
    code: str,
    path: str,
    message: str,
) -> None:
    changes.append(
        CompatibilityChange(
            level=level,
            code=code,
            path=path,
            message=message,
        )
    )


def classify_contract_change(
    previous: SemanticContract,
    current: SemanticContract,
) -> CompatibilityReport:
    """Classify semantic changes as compatible, conditional, or breaking."""
    changes: list[CompatibilityChange] = []
    previous_entities = {entity.id: entity for entity in previous.entity_types}
    current_entities = {entity.id: entity for entity in current.entity_types}
    previous_relationships = {
        relationship.id: relationship for relationship in previous.relationship_types
    }
    current_relationships = {
        relationship.id: relationship for relationship in current.relationship_types
    }

    for semantic_id, entity in previous_entities.items():
        path = f"entity_types[{semantic_id}]"
        replacement = current_entities.get(semantic_id)
        if replacement is None:
            _append(
                changes,
                CompatibilityLevel.BREAKING,
                "ENTITY_REMOVED",
                path,
                f"Entity type '{entity.name}' was removed.",
            )
            continue
        if entity.name != replacement.name:
            _append(
                changes,
                CompatibilityLevel.CONDITIONAL,
                "ENTITY_RENAMED",
                f"{path}.name",
                f"Entity type '{entity.name}' was renamed to '{replacement.name}' "
                "while retaining its stable semantic ID.",
            )
        if entity.parent != replacement.parent:
            _append(
                changes,
                CompatibilityLevel.CONDITIONAL,
                "ENTITY_PARENT_CHANGED",
                f"{path}.parent",
                "Entity inheritance changed and requires compiler/runtime review.",
            )
        if set(entity.identifiers) != set(replacement.identifiers):
            _append(
                changes,
                CompatibilityLevel.BREAKING,
                "ENTITY_IDENTIFIERS_CHANGED",
                f"{path}.identifiers",
                "Entity identity properties changed.",
            )
        if (
            entity.publication_status != "excluded"
            and replacement.publication_status == "excluded"
        ):
            _append(
                changes,
                CompatibilityLevel.BREAKING,
                "ENTITY_EXCLUDED",
                f"{path}.publication_status",
                "A previously published entity type is now excluded.",
            )

        previous_properties = {prop.name: prop for prop in entity.properties}
        current_properties = {prop.name: prop for prop in replacement.properties}
        for property_name, prop in previous_properties.items():
            property_path = f"{path}.properties[{property_name}]"
            new_prop = current_properties.get(property_name)
            if new_prop is None:
                _append(
                    changes,
                    CompatibilityLevel.BREAKING,
                    "PROPERTY_REMOVED",
                    property_path,
                    f"Property '{property_name}' was removed.",
                )
                continue
            if prop.type != new_prop.type:
                _append(
                    changes,
                    CompatibilityLevel.BREAKING,
                    "PROPERTY_TYPE_CHANGED",
                    f"{property_path}.type",
                    f"Property type changed from '{prop.type}' to '{new_prop.type}'.",
                )
            if not prop.required and new_prop.required:
                _append(
                    changes,
                    CompatibilityLevel.BREAKING,
                    "PROPERTY_BECAME_REQUIRED",
                    f"{property_path}.required",
                    f"Property '{property_name}' became required.",
                )
        for property_name, prop in current_properties.items():
            if property_name not in previous_properties:
                _append(
                    changes,
                    CompatibilityLevel.CONDITIONAL
                    if prop.required
                    else CompatibilityLevel.COMPATIBLE,
                    "PROPERTY_ADDED",
                    f"{path}.properties[{property_name}]",
                    f"{'Required' if prop.required else 'Optional'} property "
                    f"'{property_name}' was added.",
                )

    for semantic_id, entity in current_entities.items():
        if semantic_id not in previous_entities:
            _append(
                changes,
                CompatibilityLevel.CONDITIONAL
                if entity.publication_status == "core"
                else CompatibilityLevel.COMPATIBLE,
                "ENTITY_ADDED",
                f"entity_types[{semantic_id}]",
                f"Entity type '{entity.name}' was added.",
            )

    for semantic_id, relationship in previous_relationships.items():
        path = f"relationship_types[{semantic_id}]"
        replacement = current_relationships.get(semantic_id)
        if replacement is None:
            _append(
                changes,
                CompatibilityLevel.BREAKING,
                "RELATIONSHIP_REMOVED",
                path,
                f"Relationship '{relationship.predicate}' was removed.",
            )
            continue
        if relationship.predicate != replacement.predicate:
            _append(
                changes,
                CompatibilityLevel.BREAKING,
                "RELATIONSHIP_PREDICATE_CHANGED",
                f"{path}.predicate",
                "The Graph-visible relationship predicate changed.",
            )
        for field_name in ("source_type", "target_type", "direction"):
            if getattr(relationship, field_name) != getattr(replacement, field_name):
                _append(
                    changes,
                    CompatibilityLevel.BREAKING,
                    f"RELATIONSHIP_{field_name.upper()}_CHANGED",
                    f"{path}.{field_name}",
                    f"Relationship {field_name.replace('_', ' ')} changed.",
                )
        if (
            relationship.evidence_policy != "required_for_asserted"
            and replacement.evidence_policy == "required_for_asserted"
        ):
            _append(
                changes,
                CompatibilityLevel.CONDITIONAL,
                "RELATIONSHIP_EVIDENCE_TIGHTENED",
                f"{path}.evidence_policy",
                "Relationship evidence requirements became stricter.",
            )
        if (
            relationship.temporal != "required"
            and replacement.temporal == "required"
        ):
            _append(
                changes,
                CompatibilityLevel.BREAKING,
                "RELATIONSHIP_TEMPORAL_REQUIRED",
                f"{path}.temporal",
                "Relationship time bounds became mandatory.",
            )
        if (
            relationship.publication_status != "excluded"
            and replacement.publication_status == "excluded"
        ):
            _append(
                changes,
                CompatibilityLevel.BREAKING,
                "RELATIONSHIP_EXCLUDED",
                f"{path}.publication_status",
                "A previously published relationship is now excluded.",
            )

    for semantic_id, relationship in current_relationships.items():
        if semantic_id not in previous_relationships:
            _append(
                changes,
                CompatibilityLevel.CONDITIONAL
                if relationship.publication_status == "core"
                else CompatibilityLevel.COMPATIBLE,
                "RELATIONSHIP_ADDED",
                f"relationship_types[{semantic_id}]",
                f"Relationship '{relationship.predicate}' was added.",
            )

    level = max(
        (change.level for change in changes),
        key=lambda item: _LEVEL_RANK[item],
        default=CompatibilityLevel.COMPATIBLE,
    )
    return CompatibilityReport(
        level=level,
        previous_version=previous.contract_version,
        current_version=current.contract_version,
        changes=changes,
    )
