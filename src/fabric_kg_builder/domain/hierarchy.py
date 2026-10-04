"""Deterministic schema-2 semantic hierarchy and identity-policy helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from fabric_kg_builder.contracts.base import canonical_sha256

from .models import (
    DomainEntityTypeV2,
    DomainRelationshipTypeV2,
    IdentityKeyPolicyV2,
    TypeHierarchyClosureV2,
)


def _index_unique(
    values: Iterable[Any],
    *,
    id_attribute: str,
    label: str,
) -> dict[str, Any]:
    indexed: dict[str, Any] = {}
    for value in values:
        item_id = str(getattr(value, id_attribute))
        if item_id in indexed:
            raise ValueError(f"duplicate {label}: {item_id}")
        indexed[item_id] = value
    return indexed


def _ancestor_ids(
    type_id: str,
    parent_by_type: Mapping[str, str | None],
) -> list[str]:
    ancestors: list[str] = []
    seen = {type_id}
    cursor = parent_by_type[type_id]
    while cursor is not None:
        if cursor not in parent_by_type:
            raise ValueError(f"unknown parent semantic type: {cursor}")
        if cursor in seen:
            raise ValueError("semantic type hierarchy contains a cycle")
        seen.add(cursor)
        ancestors.append(cursor)
        cursor = parent_by_type[cursor]
    return sorted(ancestors)


def _effective_ids(
    type_id: str,
    *,
    ancestors_by_type: Mapping[str, list[str]],
    values_by_type: Mapping[str, list[Any]],
    id_attribute: str,
    label: str,
) -> list[str]:
    effective: dict[str, Any] = {}
    lineage = sorted(ancestors_by_type[type_id]) + [type_id]
    for lineage_type_id in lineage:
        for value in values_by_type[lineage_type_id]:
            item_id = str(getattr(value, id_attribute))
            prior = effective.get(item_id)
            if prior is not None and prior != value:
                raise ValueError(
                    f"incompatible inherited {label} override for {item_id}"
                )
            effective[item_id] = value
    return sorted(effective)


def build_type_hierarchy_closure(
    entity_types: Iterable[DomainEntityTypeV2],
    relationship_types: Iterable[DomainRelationshipTypeV2],
) -> TypeHierarchyClosureV2:
    """Compute the complete sorted closure and seal its canonical hash."""
    entities = _index_unique(
        entity_types, id_attribute="type_id", label="semantic type ID"
    )
    relationships = _index_unique(
        relationship_types,
        id_attribute="relationship_type_id",
        label="relationship type ID",
    )
    parent_by_type = {
        type_id: entity.parent_type_id for type_id, entity in entities.items()
    }
    for type_id, parent_type_id in parent_by_type.items():
        if parent_type_id == type_id:
            raise ValueError("semantic type cannot be its own parent")
        if parent_type_id is not None and parent_type_id not in entities:
            raise ValueError(f"unknown parent semantic type: {parent_type_id}")

    ancestors_by_type = {
        type_id: _ancestor_ids(type_id, parent_by_type)
        for type_id in sorted(entities)
    }
    for type_id, entity in entities.items():
        roots = [
            candidate_id
            for candidate_id in [type_id, *ancestors_by_type[type_id]]
            if parent_by_type[candidate_id] is None
        ]
        if len(roots) != 1 or entity.identity_root_type_id != roots[0]:
            raise ValueError(
                f"identity_root_type_id for {type_id} must equal its transitive root"
            )
    descendants_by_type = {
        type_id: sorted(
            candidate_id
            for candidate_id, ancestors in ancestors_by_type.items()
            if type_id in ancestors
        )
        for type_id in sorted(entities)
    }

    properties_by_type = {
        type_id: entity.declared_properties
        for type_id, entity in entities.items()
    }
    constraints_by_type = {
        type_id: entity.declared_constraints
        for type_id, entity in entities.items()
    }
    effective_property_ids_by_type = {
        type_id: _effective_ids(
            type_id,
            ancestors_by_type=ancestors_by_type,
            values_by_type=properties_by_type,
            id_attribute="property_id",
            label="property",
        )
        for type_id in sorted(entities)
    }
    effective_constraint_ids_by_type = {
        type_id: _effective_ids(
            type_id,
            ancestors_by_type=ancestors_by_type,
            values_by_type=constraints_by_type,
            id_attribute="constraint_id",
            label="constraint",
        )
        for type_id in sorted(entities)
    }

    compatible_sources: dict[str, list[str]] = {}
    compatible_targets: dict[str, list[str]] = {}
    for relationship_id, relationship in sorted(relationships.items()):
        source_ids = set(relationship.source_type_ids)
        target_ids = set(relationship.target_type_ids)
        if relationship.endpoint_policy == "allow_subtypes":
            for type_id in relationship.source_type_ids:
                source_ids.update(descendants_by_type[type_id])
            for type_id in relationship.target_type_ids:
                target_ids.update(descendants_by_type[type_id])
        compatible_sources[relationship_id] = sorted(source_ids)
        compatible_targets[relationship_id] = sorted(target_ids)

    values = {
        "direct_parent_by_type": dict(sorted(parent_by_type.items())),
        "ancestors_by_type": ancestors_by_type,
        "descendants_by_type": descendants_by_type,
        "effective_property_ids_by_type": effective_property_ids_by_type,
        "effective_constraint_ids_by_type": effective_constraint_ids_by_type,
        "compatible_source_type_ids_by_relationship": compatible_sources,
        "compatible_target_type_ids_by_relationship": compatible_targets,
    }
    return TypeHierarchyClosureV2(
        **values,
        hierarchy_hash=canonical_sha256(values),
    )


def validate_relationship_endpoint_compatibility(
    relationships: Iterable[DomainRelationshipTypeV2],
    closure: TypeHierarchyClosureV2,
) -> None:
    """Prove all sealed endpoint compatibility sets are exact."""
    relationship_ids = {item.relationship_type_id for item in relationships}
    if set(closure.compatible_source_type_ids_by_relationship) != relationship_ids:
        raise ValueError("source endpoint closure does not match relationship set")
    if set(closure.compatible_target_type_ids_by_relationship) != relationship_ids:
        raise ValueError("target endpoint closure does not match relationship set")


def resolve_identity_root_policy(
    type_id: str,
    entity_types: Iterable[DomainEntityTypeV2],
) -> IdentityKeyPolicyV2:
    """Return the inherited root policy without permitting descendant overrides."""
    entities = _index_unique(
        entity_types, id_attribute="type_id", label="semantic type ID"
    )
    entity = entities.get(type_id)
    if entity is None:
        raise ValueError(f"unknown semantic type: {type_id}")
    root = entities.get(entity.identity_root_type_id)
    if root is None or root.parent_type_id is not None:
        raise ValueError("identity root is missing or is not a hierarchy root")
    if root.identity_key_policy is None:
        raise ValueError("identity root has no key policy")
    return root.identity_key_policy


def stable_entity_identity_inputs(
    *,
    project_id: str,
    policy: IdentityKeyPolicyV2,
    normalized_business_key: Mapping[str, str] | None = None,
    stable_source_identity: str | None = None,
) -> dict[str, Any]:
    """Return canonical authority inputs that intentionally exclude type labels."""
    if policy.key_mode == "business_key":
        if normalized_business_key is None or stable_source_identity is not None:
            raise ValueError("business-key policy requires only a normalized key")
        if set(normalized_business_key) != set(policy.business_key_fields):
            raise ValueError("normalized business-key fields do not match root policy")
        identity_value: Any = dict(sorted(normalized_business_key.items()))
    else:
        if stable_source_identity is None or normalized_business_key is not None:
            raise ValueError("source-identity policy requires only stable source identity")
        identity_value = stable_source_identity
    return {
        "project_id": project_id,
        "identity_authority": policy.authority,
        "identity_namespace": policy.namespace,
        "identity_value": identity_value,
        "normalization_version": policy.normalization_version,
    }


def stable_relationship_identity_inputs(
    *,
    predicate_id: str,
    source_entity_id: str,
    target_entity_id: str,
    governed_context: Mapping[str, Any] | str | None,
) -> dict[str, Any]:
    """Return relationship authority inputs independent of endpoint type labels."""
    return {
        "predicate_id": predicate_id,
        "source_entity_id": source_entity_id,
        "target_entity_id": target_entity_id,
        "governed_context": governed_context,
    }
