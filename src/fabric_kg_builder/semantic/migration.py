"""Explicit migration helpers for legacy ontology identity artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import SemanticContract, StableIdBinding, StableIdLock
from .service import (
    SemanticContractCompatibilityError,
    SemanticContractParseError,
    SemanticContractValidationError,
)


@dataclass(frozen=True)
class LegacyIdImportResult:
    """Stable ID proposal plus legacy names requiring an explicit disposition."""

    ids: StableIdLock
    unmapped_legacy_entity_types: tuple[str, ...]
    unmapped_legacy_relationship_types: tuple[str, ...]
    new_entity_types: tuple[str, ...]
    new_relationship_types: tuple[str, ...]


def _load_legacy_lock(path: Path | str) -> dict[str, Any]:
    artifact_path = Path(path)
    try:
        raw = json.loads(artifact_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SemanticContractCompatibilityError(
            f"Could not read legacy ID lock '{artifact_path}': {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise SemanticContractParseError(
            f"Could not parse legacy ID lock '{artifact_path}': {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise SemanticContractValidationError(
            "Legacy ID lock must contain a JSON object."
        )
    return raw


def import_legacy_id_lock(
    path: Path | str,
    contract: SemanticContract,
    *,
    allow_unmapped_legacy: bool = False,
) -> LegacyIdImportResult:
    """Preserve matching Fabric IDs without silently accepting old semantics."""
    raw = _load_legacy_lock(path)
    legacy_entities = raw.get("entityTypes")
    legacy_relationships = raw.get("relationshipTypes")
    if not isinstance(legacy_entities, dict) or not isinstance(
        legacy_relationships, dict
    ):
        raise SemanticContractCompatibilityError(
            "Expected legacy entityTypes and relationshipTypes maps."
        )

    contract_entities = {entity.name: entity for entity in contract.entity_types}
    contract_relationships = {
        relationship.predicate: relationship
        for relationship in contract.relationship_types
    }
    unmapped_entities = tuple(
        sorted(set(legacy_entities) - set(contract_entities))
    )
    unmapped_relationships = tuple(
        sorted(set(legacy_relationships) - set(contract_relationships))
    )
    if (unmapped_entities or unmapped_relationships) and not allow_unmapped_legacy:
        raise SemanticContractCompatibilityError(
            "Legacy ID lock contains semantics absent from the canonical contract. "
            "Classify them as retained, migrated, or excluded before import; "
            f"entityTypes={list(unmapped_entities)}, "
            f"relationshipTypes={list(unmapped_relationships)}."
        )

    entity_bindings = {
        name: StableIdBinding(
            semantic_id=entity.id,
            fabric_id=(
                str(legacy_entities[name])
                if name in legacy_entities
                else None
            ),
        )
        for name, entity in contract_entities.items()
    }
    relationship_bindings = {
        name: StableIdBinding(
            semantic_id=relationship.id,
            fabric_id=(
                str(legacy_relationships[name])
                if name in legacy_relationships
                else None
            ),
        )
        for name, relationship in contract_relationships.items()
    }
    return LegacyIdImportResult(
        ids=StableIdLock(
            entity_types=entity_bindings,
            relationship_types=relationship_bindings,
        ),
        unmapped_legacy_entity_types=unmapped_entities,
        unmapped_legacy_relationship_types=unmapped_relationships,
        new_entity_types=tuple(
            sorted(set(contract_entities) - set(legacy_entities))
        ),
        new_relationship_types=tuple(
            sorted(set(contract_relationships) - set(legacy_relationships))
        ),
    )
