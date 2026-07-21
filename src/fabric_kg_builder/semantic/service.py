"""Load, normalize, hash, and validate SPEC-008 semantic contract bundles."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from .models import (
    PhysicalMappings,
    SemanticContract,
    StableIdLock,
    Vocabulary,
)


class SemanticContractError(Exception):
    """Base exception for semantic contract operations."""


class SemanticContractParseError(SemanticContractError):
    """Raised when a semantic YAML or JSON artifact cannot be parsed."""


class SemanticContractValidationError(SemanticContractError):
    """Raised when semantic artifacts violate their schema or cross-contract rules."""


class SemanticContractCompatibilityError(SemanticContractError):
    """Raised when a legacy artifact requires explicit migration."""


@dataclass(frozen=True)
class SemanticBundle:
    """Validated semantic contract and its supporting physical artifacts."""

    contract: SemanticContract
    mappings: PhysicalMappings
    vocabulary: Vocabulary
    ids: StableIdLock
    contract_hash: str


_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _load_mapping(path: Path | str) -> dict[str, Any]:
    artifact_path = Path(path)
    try:
        text = artifact_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SemanticContractError(
            f"Could not read semantic artifact '{artifact_path}': {exc}"
        ) from exc
    try:
        loaded = (
            json.loads(text)
            if artifact_path.suffix.lower() == ".json"
            else yaml.safe_load(text)
        )
    except (json.JSONDecodeError, yaml.MarkedYAMLError) as exc:
        raise SemanticContractParseError(
            f"Could not parse semantic artifact '{artifact_path}': {exc}"
        ) from exc
    if not isinstance(loaded, dict):
        raise SemanticContractValidationError(
            f"Semantic artifact '{artifact_path}' must contain an object."
        )
    return loaded


def _load_model(path: Path | str, model_type: type[_ModelT]) -> _ModelT:
    raw = _load_mapping(path)
    try:
        return model_type.model_validate(raw)
    except ValidationError as exc:
        raise SemanticContractValidationError(
            f"Semantic artifact '{Path(path)}' failed schema validation: {exc}"
        ) from exc


def load_semantic_contract(path: Path | str) -> SemanticContract:
    """Load one strict semantic contract YAML file."""
    return _load_model(path, SemanticContract)


def load_physical_mappings(path: Path | str) -> PhysicalMappings:
    """Load semantic-to-physical mappings."""
    return _load_model(path, PhysicalMappings)


def load_vocabulary(path: Path | str) -> Vocabulary:
    """Load the controlled vocabulary."""
    return _load_model(path, Vocabulary)


def load_stable_id_lock(path: Path | str) -> StableIdLock:
    """Load the stable ID lock and reject implicit legacy conversion."""
    raw = _load_mapping(path)
    if "entityTypes" in raw or "relationshipTypes" in raw:
        raise SemanticContractCompatibilityError(
            "Legacy ids.lock.json uses entityTypes/relationshipTypes numeric maps. "
            "Import it through the approved SPEC-008 migration workflow; it cannot "
            "be treated as a canonical semantic ID lock."
        )
    try:
        return StableIdLock.model_validate(raw)
    except ValidationError as exc:
        raise SemanticContractValidationError(
            f"Semantic ID lock '{Path(path)}' failed schema validation: {exc}"
        ) from exc


def normalize_semantic_contract(contract: SemanticContract) -> dict[str, Any]:
    """Return deterministic semantic JSON excluding approval metadata."""
    payload = contract.model_dump(mode="json", exclude={"approval"})
    entities = []
    for entity in sorted(payload["entity_types"], key=lambda item: item["id"]):
        normalized = dict(entity)
        normalized["aliases"] = sorted(set(normalized.get("aliases", [])))
        normalized["identifiers"] = sorted(set(normalized.get("identifiers", [])))
        normalized["lineage_properties"] = sorted(
            set(normalized.get("lineage_properties", []))
        )
        normalized["properties"] = sorted(
            normalized.get("properties", []), key=lambda item: item["name"]
        )
        for prop in normalized["properties"]:
            prop["aliases"] = sorted(set(prop.get("aliases", [])))
        entities.append(normalized)
    relationships = []
    for relationship in sorted(
        payload["relationship_types"], key=lambda item: item["id"]
    ):
        normalized = dict(relationship)
        policy = dict(normalized["assertion_policy"])
        policy["allowed_statuses"] = sorted(set(policy["allowed_statuses"]))
        normalized["assertion_policy"] = policy
        relationships.append(normalized)
    payload["entity_types"] = entities
    payload["relationship_types"] = relationships
    return payload


def compute_semantic_contract_hash(contract: SemanticContract) -> str:
    """Return a prefixed SHA-256 over normalized semantic JSON."""
    canonical = json.dumps(
        normalize_semantic_contract(contract),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def validate_approved_contract(contract: SemanticContract) -> str:
    """Validate approval state and return the verified semantic contract hash."""
    contract_hash = compute_semantic_contract_hash(contract)
    if contract.approval.status != "approved":
        raise SemanticContractValidationError(
            "Semantic contract must be approved before compilation."
        )
    if contract.approval.contract_hash != contract_hash:
        raise SemanticContractValidationError(
            "Semantic contract approval hash is missing or stale."
        )
    if not contract.approval.approved_by or not contract.approval.approved_at_utc:
        raise SemanticContractValidationError(
            "Approved semantic contract requires approver and approval timestamp."
        )
    return contract_hash


def validate_semantic_bundle(
    contract: SemanticContract,
    mappings: PhysicalMappings,
    vocabulary: Vocabulary,
    ids: StableIdLock,
    *,
    require_approval: bool = True,
) -> str:
    """Validate cross-file IDs, mappings, and approval invariants."""
    contract_hash = (
        validate_approved_contract(contract)
        if require_approval
        else compute_semantic_contract_hash(contract)
    )
    entities_by_name = {entity.name: entity for entity in contract.entity_types}
    relationships_by_name = {
        relationship.predicate: relationship
        for relationship in contract.relationship_types
    }
    if set(ids.entity_types) != set(entities_by_name):
        missing = sorted(set(entities_by_name) - set(ids.entity_types))
        extra = sorted(set(ids.entity_types) - set(entities_by_name))
        raise SemanticContractValidationError(
            f"Entity ID lock names differ from the contract; missing={missing}, "
            f"extra={extra}."
        )
    if set(ids.relationship_types) != set(relationships_by_name):
        missing = sorted(set(relationships_by_name) - set(ids.relationship_types))
        extra = sorted(set(ids.relationship_types) - set(relationships_by_name))
        raise SemanticContractValidationError(
            f"Relationship ID lock names differ from the contract; missing={missing}, "
            f"extra={extra}."
        )
    for name, entity in entities_by_name.items():
        if ids.entity_types[name].semantic_id != entity.id:
            raise SemanticContractValidationError(
                f"Entity ID lock remaps '{name}' from '{entity.id}' to "
                f"'{ids.entity_types[name].semantic_id}'."
            )
    for name, relationship in relationships_by_name.items():
        if ids.relationship_types[name].semantic_id != relationship.id:
            raise SemanticContractValidationError(
                f"Relationship ID lock remaps '{name}' from '{relationship.id}' to "
                f"'{ids.relationship_types[name].semantic_id}'."
            )

    entity_ids = {entity.id for entity in contract.entity_types}
    relationship_ids = {
        relationship.id for relationship in contract.relationship_types
    }
    mapped_entities = [mapping.semantic_id for mapping in mappings.entity_types]
    mapped_relationships = [
        mapping.semantic_id for mapping in mappings.relationship_types
    ]
    if len(mapped_entities) != len(set(mapped_entities)):
        raise SemanticContractValidationError("Duplicate entity physical mapping.")
    if len(mapped_relationships) != len(set(mapped_relationships)):
        raise SemanticContractValidationError(
            "Duplicate relationship physical mapping."
        )
    unknown_entities = sorted(set(mapped_entities) - entity_ids)
    unknown_relationships = sorted(set(mapped_relationships) - relationship_ids)
    if unknown_entities or unknown_relationships:
        raise SemanticContractValidationError(
            "Physical mappings reference unknown semantic IDs; "
            f"entities={unknown_entities}, relationships={unknown_relationships}."
        )
    required_entity_mappings = {
        entity.id
        for entity in contract.entity_types
        if entity.publication_status == "core" and not entity.abstract
    }
    required_relationship_mappings = {
        relationship.id
        for relationship in contract.relationship_types
        if relationship.publication_status == "core"
    }
    missing_entity_mappings = sorted(
        required_entity_mappings - set(mapped_entities)
    )
    missing_relationship_mappings = sorted(
        required_relationship_mappings - set(mapped_relationships)
    )
    if missing_entity_mappings or missing_relationship_mappings:
        raise SemanticContractValidationError(
            "Core concrete semantics require physical mappings; "
            f"entities={missing_entity_mappings}, "
            f"relationships={missing_relationship_mappings}."
        )
    entities_by_id = {entity.id: entity for entity in contract.entity_types}
    for mapping in mappings.entity_types:
        known_properties = {
            prop.name for prop in entities_by_id[mapping.semantic_id].properties
        }
        unknown_properties = sorted(
            set(mapping.property_columns) - known_properties
        )
        if unknown_properties:
            raise SemanticContractValidationError(
                f"Entity mapping '{mapping.semantic_id}' references unknown "
                f"properties: {unknown_properties}."
            )
    relationships_by_id = {
        relationship.id: relationship
        for relationship in contract.relationship_types
    }
    for mapping in mappings.relationship_types:
        relationship = relationships_by_id[mapping.semantic_id]
        if (
            relationship.evidence_policy == "required_for_asserted"
            and not mapping.evidence_id_column
        ):
            raise SemanticContractValidationError(
                f"Relationship mapping '{mapping.semantic_id}' requires an "
                "evidence_id_column."
            )

    vocabulary_ids = [term.id for term in vocabulary.terms]
    if len(vocabulary_ids) != len(set(vocabulary_ids)):
        raise SemanticContractValidationError(
            "Vocabulary contains duplicate term IDs."
        )
    return contract_hash


def load_semantic_bundle(
    *,
    contract_path: Path | str,
    mappings_path: Path | str,
    vocabulary_path: Path | str,
    ids_lock_path: Path | str,
    require_approval: bool = True,
) -> SemanticBundle:
    """Load and cross-validate the complete semantic contract bundle."""
    contract = load_semantic_contract(contract_path)
    mappings = load_physical_mappings(mappings_path)
    vocabulary = load_vocabulary(vocabulary_path)
    ids = load_stable_id_lock(ids_lock_path)
    contract_hash = validate_semantic_bundle(
        contract,
        mappings,
        vocabulary,
        ids,
        require_approval=require_approval,
    )
    return SemanticBundle(
        contract=contract,
        mappings=mappings,
        vocabulary=vocabulary,
        ids=ids,
        contract_hash=contract_hash,
    )
