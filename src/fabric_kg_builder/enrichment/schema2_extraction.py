"""L2 proposed-only schema-constrained extraction and C0 carrier sealing."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from fabric_kg_builder.contracts.base import (
    canonical_sha256,
    deterministic_contract_id,
    normalize_nfc,
)
from fabric_kg_builder.contracts.extraction import (
    ExtractionAuthorityReferences,
    ExtractionCandidateBatch,
    ExtractionCandidateReference,
    RequiredMemberOrderingPolicyV1_1,
    RequiredMemberReferenceV1_1,
    RequiredMemberSetProposalIdentityV1_1,
    RequiredMemberSetProposalV1_1,
)
from fabric_kg_builder.contracts.identity import CanonicalIdentityEnvelope
from fabric_kg_builder.contracts.lifecycle import (
    AssertionState,
    CandidateAccountingDisposition,
    CandidateLifecycleRecord,
)
from fabric_kg_builder.domain.hierarchy import (
    build_type_hierarchy_closure,
    resolve_identity_root_policy,
    stable_entity_identity_inputs,
    stable_relationship_identity_inputs,
)
from fabric_kg_builder.domain.models import (
    CompletenessRequirementV2,
    DomainContractV2,
    DomainEntityTypeV2,
    DomainPropertyV2,
    DomainRelationshipTypeV2,
)
from fabric_kg_builder.domain.service import compute_contract_hash

from .schema2_sources import L2StageError

L2_PROMPT_VERSION = "l2-schema-constrained/1.1.0"
L2_EXTRACTOR_VERSION = "1.1.0"
UNKNOWN_SEMANTIC_TYPE = {
    "entity": "unapproved-observation:entity",
    "relationship": "unapproved-observation:relationship",
    "property": "unapproved-observation:property",
}


class _StrictProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class ProposedAnchor(_StrictProposal):
    """Untrusted model-proposed SourceUnit-relative codepoint anchor."""

    span_start: int = Field(ge=0)
    span_end: int = Field(gt=0)
    quote: str = Field(min_length=1)
    model_authored_evidence_id: str | None = None


class RawEntityCandidate(_StrictProposal):
    candidate_kind: Literal["entity"]
    local_id: str = Field(min_length=1)
    observed_type: str = Field(min_length=1)
    label: str = Field(min_length=1)
    aliases: tuple[str, ...] = ()
    identity_key: dict[str, str] = Field(default_factory=dict)
    stable_source_identity: str | None = None
    anchors: tuple[ProposedAnchor, ...] = ()

    @field_validator("aliases", "anchors", mode="before")
    @classmethod
    def _json_arrays(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class RawRelationshipCandidate(_StrictProposal):
    candidate_kind: Literal["relationship"]
    source_local_id: str = Field(min_length=1)
    target_local_id: str = Field(min_length=1)
    observed_predicate: str = Field(min_length=1)
    direction: Literal["source_to_target", "reverse", "unknown"]
    governed_context: dict[str, Any] | str | None = None
    member_role_id: str | None = None
    member_order: int | None = Field(default=None, ge=0)
    anchor: ProposedAnchor | None = None


class RawPropertyCandidate(_StrictProposal):
    candidate_kind: Literal["property"]
    owner_local_id: str = Field(min_length=1)
    observed_property: str = Field(min_length=1)
    value: str | int | float | bool
    normalized_value: str | int | float | bool
    temporal_key: str | None = None
    anchor: ProposedAnchor | None = None


RawCandidate = Annotated[
    Union[RawEntityCandidate, RawRelationshipCandidate, RawPropertyCandidate],
    Field(discriminator="candidate_kind"),
]
_RAW_CANDIDATES = TypeAdapter(list[RawCandidate])


class RawCandidateResponse(_StrictProposal):
    candidates: tuple[RawCandidate, ...]

    @field_validator("candidates", mode="before")
    @classmethod
    def _json_candidates(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


def raw_candidate_response_schema() -> dict[str, Any]:
    """Return the exact L2 Foundry response envelope schema."""
    return RawCandidateResponse.model_json_schema()


@dataclass(frozen=True)
class ClosedVocabulary:
    contract_hash: str
    entities_by_alias: dict[str, DomainEntityTypeV2]
    entities_by_id: dict[str, DomainEntityTypeV2]
    relationships_by_alias: dict[str, DomainRelationshipTypeV2]
    properties_by_type_and_alias: dict[str, dict[str, DomainPropertyV2]]
    max_relations_per_work_unit: int
    prompt_payload: dict[str, Any]


@dataclass(frozen=True)
class ProposedCandidateRecord:
    input_candidate_id: str
    candidate_id: str
    candidate_version_id: str
    candidate_kind: str
    semantic_id: str
    approved_semantic_id: str | None
    observed_term: str
    source_unit_id: str
    work_unit_id: str
    local_reference: str | None
    classification_version_id: str | None
    proposed_anchor: ProposedAnchor | None
    payload_hash: str
    proposed_source_entity_id: str | None = None
    proposed_target_entity_id: str | None = None
    proposed_source_semantic_type_id: str | None = None
    proposed_target_semantic_type_id: str | None = None
    proposed_member_role_id: str | None = None
    proposed_member_order: int | None = None


@dataclass(frozen=True)
class ExtractionLeafResult:
    batch: ExtractionCandidateBatch
    proposed_candidates: tuple[ProposedCandidateRecord, ...]
    lifecycle_records: tuple[CandidateLifecycleRecord, ...]
    audit_reason_counts: tuple[tuple[str, int], ...]
    raw_candidate_count: int


@dataclass(frozen=True)
class CollectionMemberFragment:
    requirement_id: str
    aggregate_entity_id: str
    member_entity_id: str
    member_candidate_id: str
    member_semantic_type_id: str
    member_role_id: str | None
    member_order: int | None
    membership_relationship_candidate_id: str
    source_unit_id: str


@dataclass(frozen=True)
class ProposedRequiredMemberSetView:
    proposal: RequiredMemberSetProposalV1_1
    requirement_id: str
    aggregate_entity_id: str
    member_entity_ids: tuple[str, ...]
    contributing_source_unit_ids: tuple[str, ...]
    membership_relationship_candidate_ids: tuple[str, ...]
    unresolved_reasons: tuple[str, ...]
    view_hash: str


def extraction_leaf_to_dict(leaf: ExtractionLeafResult) -> dict[str, Any]:
    """Serialize one immutable leaf without adding a cross-layer contract."""

    return {
        "batch": leaf.batch.model_dump(mode="json"),
        "proposed_candidates": [
            {
                **{
                    key: value
                    for key, value in candidate.__dict__.items()
                    if key != "proposed_anchor"
                },
                "proposed_anchor": (
                    candidate.proposed_anchor.model_dump(mode="json")
                    if candidate.proposed_anchor is not None
                    else None
                ),
            }
            for candidate in leaf.proposed_candidates
        ],
        "lifecycle_records": [
            record.model_dump(mode="json") for record in leaf.lifecycle_records
        ],
        "audit_reason_counts": [list(item) for item in leaf.audit_reason_counts],
        "raw_candidate_count": leaf.raw_candidate_count,
    }


def extraction_leaf_from_dict(raw: dict[str, Any]) -> ExtractionLeafResult:
    """Rehydrate a checkpointed leaf through strict C0 validation."""

    try:
        candidates = tuple(
            ProposedCandidateRecord(
                **{
                    **candidate,
                    "proposed_anchor": (
                        ProposedAnchor.model_validate(candidate["proposed_anchor"])
                        if candidate.get("proposed_anchor") is not None
                        else None
                    ),
                }
            )
            for candidate in raw["proposed_candidates"]
        )
        return ExtractionLeafResult(
            batch=ExtractionCandidateBatch.model_validate_json(
                json.dumps(raw["batch"])
            ),
            proposed_candidates=candidates,
            lifecycle_records=tuple(
                CandidateLifecycleRecord.model_validate_json(json.dumps(record))
                for record in raw["lifecycle_records"]
            ),
            audit_reason_counts=tuple(
                (str(item[0]), int(item[1]))
                for item in raw["audit_reason_counts"]
            ),
            raw_candidate_count=int(raw["raw_candidate_count"]),
        )
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise L2StageError(
            "L2_CHECKPOINT_STALE",
            f"checkpointed extraction leaf is invalid: {exc}",
        ) from exc


def _add_unique_alias(
    index: dict[str, Any],
    *,
    alias: str,
    value: Any,
    value_id: str,
    label: str,
) -> None:
    key = normalize_nfc(alias).casefold()
    existing = index.get(key)
    if existing is not None:
        existing_id = getattr(
            existing,
            "type_id",
            getattr(existing, "relationship_type_id", getattr(existing, "property_id", "")),
        )
        if existing_id != value_id:
            raise L2StageError(
                "L2_APPROVED_CONCEPT_MISSING",
                f"ambiguous approved {label} alias {alias!r}",
            )
    index[key] = value


def compile_closed_vocabulary(contract: DomainContractV2) -> ClosedVocabulary:
    """Compile and hash-check the approved vocabulary once before model work."""

    if contract.extraction_policy.vocabulary_mode != "closed":
        raise L2StageError(
            "L2_DOMAIN_CONTRACT_INVALID",
            "L2 supports only a closed approved vocabulary",
        )
    try:
        closure = build_type_hierarchy_closure(
            contract.candidate_model.entity_types,
            contract.candidate_model.relationship_types,
        )
    except ValueError as exc:
        raise L2StageError("L2_HIERARCHY_INVALID", str(exc)) from exc
    if closure != contract.hierarchy_closure:
        raise L2StageError(
            "L2_HIERARCHY_HASH_MISMATCH",
            "sealed hierarchy closure does not recompute",
        )

    entities_by_alias: dict[str, DomainEntityTypeV2] = {}
    entities_by_id = {
        entity.type_id: entity for entity in contract.candidate_model.entity_types
    }
    for entity in contract.candidate_model.entity_types:
        for alias in (
            entity.type_id,
            entity.semantic_key,
            entity.display_name,
            *entity.aliases,
        ):
            _add_unique_alias(
                entities_by_alias,
                alias=alias,
                value=entity,
                value_id=entity.type_id,
                label="entity",
            )

    relationships_by_alias: dict[str, DomainRelationshipTypeV2] = {}
    for relationship in contract.candidate_model.relationship_types:
        for alias in (
            relationship.relationship_type_id,
            relationship.predicate_id,
            relationship.display_name,
        ):
            _add_unique_alias(
                relationships_by_alias,
                alias=alias,
                value=relationship,
                value_id=relationship.relationship_type_id,
                label="relationship",
            )

    declared_properties = {
        property_.property_id: property_
        for entity in contract.candidate_model.entity_types
        for property_ in entity.declared_properties
    }
    properties_by_type_and_alias: dict[str, dict[str, DomainPropertyV2]] = {}
    for type_id, property_ids in (
        contract.hierarchy_closure.effective_property_ids_by_type.items()
    ):
        aliases: dict[str, DomainPropertyV2] = {}
        for property_id in property_ids:
            property_ = declared_properties.get(property_id)
            if property_ is None:
                raise L2StageError(
                    "L2_APPROVED_CONCEPT_MISSING",
                    f"effective property {property_id} has no approved declaration",
                )
            for alias in (property_.property_id, property_.display_name):
                _add_unique_alias(
                    aliases,
                    alias=alias,
                    value=property_,
                    value_id=property_.property_id,
                    label="property",
                )
        properties_by_type_and_alias[type_id] = aliases

    contract_hash = compute_contract_hash(contract)
    prompt_payload = {
        "schema_version": "2.0",
        "domain_contract_hash": contract_hash,
        "hierarchy_hash": contract.hierarchy_closure.hierarchy_hash,
        "identity_policy_hash": contract.identity_policy_hash,
        "completeness_requirement_hash": contract.completeness_requirement_hash,
        "entity_types": [
            {
                "type_id": entity.type_id,
                "display_name": entity.display_name,
                "aliases": entity.aliases,
                "abstract": entity.abstract,
                "parent_type_id": entity.parent_type_id,
                "identity_root_type_id": entity.identity_root_type_id,
                "identity_key_policy": (
                    resolve_identity_root_policy(
                        entity.type_id,
                        contract.candidate_model.entity_types,
                    ).model_dump(mode="json")
                ),
                "effective_property_ids": (
                    contract.hierarchy_closure.effective_property_ids_by_type[
                        entity.type_id
                    ]
                ),
            }
            for entity in contract.candidate_model.entity_types
        ],
        "relationship_types": [
            {
                "relationship_type_id": relationship.relationship_type_id,
                "predicate_id": relationship.predicate_id,
                "display_name": relationship.display_name,
                "direction": relationship.direction,
                "endpoint_policy": relationship.endpoint_policy,
                "source_type_ids": relationship.source_type_ids,
                "target_type_ids": relationship.target_type_ids,
            }
            for relationship in contract.candidate_model.relationship_types
        ],
        "completeness_requirements": [
            requirement.model_dump(mode="json")
            for requirement in contract.completeness_requirements
        ],
        "rules": [
            "Return observations only; the runner owns every ID and lifecycle field.",
            "Use approved terms when supported and preserve unknown observed terms.",
            "Do not invent types, parents, predicates, properties, roles, counts, order, N, or K.",
            "Proposed source anchors use Unicode codepoint offsets and are not verified evidence.",
            "Do not return asserted state, verified evidence IDs, or publication fields.",
            "Do not truncate candidates.",
        ],
        "max_relations_per_work_unit": (
            contract.reasoning_policy.max_relations_per_work_unit
        ),
        "approved_relationship_type_count": (
            contract.reasoning_policy.relationship_type_count
        ),
        "approved_max_hops": contract.reasoning_policy.max_hops,
    }
    return ClosedVocabulary(
        contract_hash=contract_hash,
        entities_by_alias=entities_by_alias,
        entities_by_id=entities_by_id,
        relationships_by_alias=relationships_by_alias,
        properties_by_type_and_alias=properties_by_type_and_alias,
        max_relations_per_work_unit=(
            contract.reasoning_policy.max_relations_per_work_unit
        ),
        prompt_payload=prompt_payload,
    )


def render_extraction_prompt(
    vocabulary: ClosedVocabulary,
    *,
    source_unit_id: str,
    source_text_hash: str,
    source_text: str,
    slice_start: int,
    slice_end: int,
) -> str:
    payload = {
        **vocabulary.prompt_payload,
        "source_identity": {
            "source_unit_id": source_unit_id,
            "source_text_hash": source_text_hash,
            "slice_start": slice_start,
            "slice_end": slice_end,
        },
        "source_text": source_text,
        "source_offset_rule": (
            "Anchor offsets are absolute Unicode codepoint offsets in the complete "
            "SourceUnit; add slice_start to offsets within source_text."
        ),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_candidate_array(raw: object) -> tuple[RawCandidate, ...]:
    """Strictly parse the whole response; one malformed item fails the leaf."""

    try:
        return tuple(_RAW_CANDIDATES.validate_python(raw))
    except ValidationError as exc:
        raise L2StageError(
            "L2_CANDIDATE_SCHEMA_INVALID",
            f"candidate response is not wholly valid: {exc}",
        ) from exc


def _validated_identity(
    base: CanonicalIdentityEnvelope,
    *,
    contract_kind: str,
    source_bound: bool = False,
) -> CanonicalIdentityEnvelope:
    values = base.model_dump(mode="python")
    values["contract_kind"] = contract_kind
    values["immutable_locator"] = None
    if not source_bound:
        for field in (
            "asset_id",
            "asset_version_id",
            "source_file_id",
            "source_unit_id",
            "content_hash",
        ):
            values[field] = None
    return CanonicalIdentityEnvelope.model_validate(values)


def _normalized_identity_key(raw: RawEntityCandidate) -> dict[str, str]:
    return {
        key: " ".join(normalize_nfc(value).casefold().split())
        for key, value in sorted(raw.identity_key.items())
    }


def _entity_identity(
    raw: RawEntityCandidate,
    *,
    definition: DomainEntityTypeV2 | None,
    contract: DomainContractV2,
    project_id: str,
    source_unit_id: str,
) -> str:
    if definition is None:
        inputs = {
            "project_id": project_id,
            "identity_authority": "unapproved-observation",
            "identity_namespace": "domain-rereview",
            "identity_value": (
                _normalized_identity_key(raw)
                or {
                    "stable_source_identity": (
                        raw.stable_source_identity
                        or f"{source_unit_id}:{raw.local_id.casefold()}"
                    )
                }
            ),
            "normalization_version": "1",
        }
    else:
        try:
            policy = resolve_identity_root_policy(
                definition.type_id,
                contract.candidate_model.entity_types,
            )
            if policy.key_mode == "business_key":
                normalized = _normalized_identity_key(raw)
                inputs = stable_entity_identity_inputs(
                    project_id=project_id,
                    policy=policy,
                    normalized_business_key=normalized,
                )
            else:
                inputs = stable_entity_identity_inputs(
                    project_id=project_id,
                    policy=policy,
                    stable_source_identity=(
                        raw.stable_source_identity
                        or f"{source_unit_id}:{raw.local_id.casefold()}"
                    ),
                )
        except ValueError as exc:
            raise L2StageError("L2_IDENTITY_POLICY_INVALID", str(exc)) from exc
    return deterministic_contract_id("entity", inputs)


def _anchor_signature(anchor: ProposedAnchor | None) -> dict[str, Any] | None:
    if anchor is None:
        return None
    return {
        "span_start": anchor.span_start,
        "span_end": anchor.span_end,
        "quote_hash": canonical_sha256(anchor.quote),
    }


def _candidate_input_ids(
    candidates: tuple[RawCandidate, ...],
) -> tuple[tuple[str, RawCandidate], ...]:
    grouped: defaultdict[str, list[RawCandidate]] = defaultdict(list)
    for candidate in candidates:
        raw_hash = canonical_sha256(candidate.model_dump(mode="json"))
        grouped[raw_hash].append(candidate)
    indexed: list[tuple[str, RawCandidate]] = []
    for raw_hash, values in sorted(grouped.items()):
        for occurrence, candidate in enumerate(values):
            input_id = deterministic_contract_id(
                "input-candidate",
                {"raw_candidate_hash": raw_hash, "occurrence": occurrence},
            )
            indexed.append((input_id, candidate))
    return tuple(indexed)


def _classification_version_id(
    *,
    entity_id: str,
    approved_type_id: str | None,
    contract: DomainContractV2,
    classifier_version: str,
    raw: RawEntityCandidate,
) -> str:
    return deterministic_contract_id(
        "classification-version",
        {
            "entity_id": entity_id,
            "approved_type_id": approved_type_id,
            "hierarchy_hash": contract.hierarchy_closure.hierarchy_hash,
            "identity_policy_hash": contract.identity_policy_hash,
            "classifier_version": classifier_version,
            "classification_payload_hash": canonical_sha256(
                {
                    "observed_type": raw.observed_type,
                    "anchors": [
                        anchor.model_dump(mode="json") for anchor in raw.anchors
                    ],
                }
            ),
        },
    )


def _make_candidate_record(
    *,
    input_candidate_id: str,
    raw: RawCandidate,
    vocabulary: ClosedVocabulary,
    contract: DomainContractV2,
    project_id: str,
    source_unit_id: str,
    work_unit_id: str,
    local_entities: dict[str, tuple[str, str | None]],
    classifier_version: str,
    prompt_hash: str,
    model_hash: str,
    extractor_name: str,
    extractor_version: str,
) -> ProposedCandidateRecord:
    approved_id: str | None
    classification_version_id: str | None = None
    local_reference: str | None = None
    anchor: ProposedAnchor | None
    proposed_source_entity_id: str | None = None
    proposed_target_entity_id: str | None = None
    proposed_source_semantic_type_id: str | None = None
    proposed_target_semantic_type_id: str | None = None
    proposed_member_role_id: str | None = None
    proposed_member_order: int | None = None
    identity_policy_mismatch = False
    if isinstance(raw, RawEntityCandidate):
        definition = vocabulary.entities_by_alias.get(raw.observed_type.casefold())
        if definition is not None:
            policy = resolve_identity_root_policy(
                definition.type_id,
                contract.candidate_model.entity_types,
            )
            if policy.key_mode == "business_key":
                normalized_key = _normalized_identity_key(raw)
                identity_policy_mismatch = (
                    set(raw.identity_key) != set(policy.business_key_fields)
                    or any(not value for value in normalized_key.values())
                    or raw.stable_source_identity is not None
                )
            else:
                identity_policy_mismatch = (
                    bool(raw.identity_key)
                    or not raw.stable_source_identity
                )
            if identity_policy_mismatch:
                definition = None
        entity_id = _entity_identity(
            raw,
            definition=definition,
            contract=contract,
            project_id=project_id,
            source_unit_id=source_unit_id,
        )
        approved_id = definition.type_id if definition is not None else None
        semantic_id = entity_id
        observed_term = raw.observed_type
        local_reference = raw.local_id
        anchor = raw.anchors[0] if raw.anchors else None
        classification_version_id = _classification_version_id(
            entity_id=entity_id,
            approved_type_id=approved_id,
            contract=contract,
            classifier_version=classifier_version,
            raw=raw,
        )
        local_entities[raw.local_id.casefold()] = (entity_id, approved_id)
        occurrence_seed = {
            "entity_id": entity_id,
            "source_unit_id": source_unit_id,
            "anchor": _anchor_signature(anchor),
            "local_reference": raw.local_id.casefold(),
        }
    elif isinstance(raw, RawRelationshipCandidate):
        relationship = vocabulary.relationships_by_alias.get(
            raw.observed_predicate.casefold()
        )
        source = local_entities.get(raw.source_local_id.casefold())
        target = local_entities.get(raw.target_local_id.casefold())
        source_id = (
            source[0]
            if source is not None
            else deterministic_contract_id(
                "unresolved-entity",
                {"source_unit_id": source_unit_id, "local_id": raw.source_local_id.casefold()},
            )
        )
        target_id = (
            target[0]
            if target is not None
            else deterministic_contract_id(
                "unresolved-entity",
                {"source_unit_id": source_unit_id, "local_id": raw.target_local_id.casefold()},
            )
        )
        proposed_source_entity_id = source_id
        proposed_target_entity_id = target_id
        proposed_source_semantic_type_id = source[1] if source is not None else None
        proposed_target_semantic_type_id = target[1] if target is not None else None
        proposed_member_role_id = raw.member_role_id
        proposed_member_order = raw.member_order
        approved_id = (
            relationship.relationship_type_id if relationship is not None else None
        )
        predicate_id = (
            relationship.predicate_id
            if relationship is not None
            else f"unapproved-observation:{canonical_sha256(raw.observed_predicate)[:16]}"
        )
        relationship_inputs = stable_relationship_identity_inputs(
            predicate_id=predicate_id,
            source_entity_id=source_id,
            target_entity_id=target_id,
            governed_context={
                "approved_context": raw.governed_context,
                "direction": raw.direction,
            },
        )
        semantic_id = deterministic_contract_id("relationship", relationship_inputs)
        observed_term = raw.observed_predicate
        anchor = raw.anchor
        occurrence_seed = {
            "relationship_id": semantic_id,
            "source_unit_id": source_unit_id,
            "anchor": _anchor_signature(anchor),
            "fallback": input_candidate_id if anchor is None else None,
        }
    else:
        owner = local_entities.get(raw.owner_local_id.casefold())
        owner_id = (
            owner[0]
            if owner is not None
            else deterministic_contract_id(
                "unresolved-entity",
                {"source_unit_id": source_unit_id, "local_id": raw.owner_local_id.casefold()},
            )
        )
        owner_type_id = owner[1] if owner is not None else None
        property_ = (
            vocabulary.properties_by_type_and_alias.get(owner_type_id or "", {}).get(
                raw.observed_property.casefold()
            )
        )
        approved_id = property_.property_id if property_ is not None else None
        property_id = (
            approved_id
            or f"unapproved-observation:{canonical_sha256(raw.observed_property)[:16]}"
        )
        semantic_id = deterministic_contract_id(
            "property-observation",
            {
                "entity_id": owner_id,
                "property_id": property_id,
                "normalized_value": raw.normalized_value,
                "temporal_key": raw.temporal_key,
            },
        )
        observed_term = raw.observed_property
        anchor = raw.anchor
        occurrence_seed = {
            "property_observation_id": semantic_id,
            "source_unit_id": source_unit_id,
            "anchor": _anchor_signature(anchor),
            "fallback": input_candidate_id if anchor is None else None,
        }

    candidate_id = deterministic_contract_id(
        f"{raw.candidate_kind}-candidate",
        occurrence_seed,
    )
    payload_hash = canonical_sha256(
        {
            "candidate_id": candidate_id,
            "semantic_id": semantic_id,
            "approved_semantic_id": approved_id,
            "raw": raw.model_dump(mode="json"),
        }
    )
    candidate_version_id = deterministic_contract_id(
        "candidate-version",
        {
            "candidate_id": candidate_id,
            "domain_contract_hash": vocabulary.contract_hash,
            "prompt_hash": prompt_hash,
            "model_hash": model_hash,
            "extractor": [extractor_name, extractor_version],
            "payload_hash": payload_hash,
        },
    )
    return ProposedCandidateRecord(
        input_candidate_id=input_candidate_id,
        candidate_id=candidate_id,
        candidate_version_id=candidate_version_id,
        candidate_kind=raw.candidate_kind,
        semantic_id=semantic_id,
        approved_semantic_id=approved_id,
        observed_term=observed_term,
        source_unit_id=source_unit_id,
        work_unit_id=work_unit_id,
        local_reference=local_reference,
        classification_version_id=classification_version_id,
        proposed_anchor=anchor,
        payload_hash=payload_hash,
        proposed_source_entity_id=proposed_source_entity_id,
        proposed_target_entity_id=proposed_target_entity_id,
        proposed_source_semantic_type_id=proposed_source_semantic_type_id,
        proposed_target_semantic_type_id=proposed_target_semantic_type_id,
        proposed_member_role_id=proposed_member_role_id,
        proposed_member_order=proposed_member_order,
    )


def build_candidate_batch(
    raw_candidates: object,
    *,
    vocabulary: ClosedVocabulary,
    contract: DomainContractV2,
    authority: ExtractionAuthorityReferences,
    base_identity: CanonicalIdentityEnvelope,
    source_unit_id: str,
    work_unit_id: str,
    classifier_version: str,
    prompt_hash: str,
    model_hash: str,
    extractor_name: str,
    extractor_version: str,
    occurred_at_utc: datetime,
) -> ExtractionLeafResult:
    """Map observations without trust, deduplicate, and seal initial C0 events."""

    parsed = parse_candidate_array(raw_candidates)
    relationships = sum(
        isinstance(candidate, RawRelationshipCandidate) for candidate in parsed
    )
    if relationships > vocabulary.max_relations_per_work_unit:
        raise L2StageError(
            "L2_RELATION_BUDGET_EXCEEDED",
            "over-budget parent responses must be discarded and split",
        )

    indexed = _candidate_input_ids(parsed)
    local_entities: dict[str, tuple[str, str | None]] = {}
    records: list[ProposedCandidateRecord] = []
    # Entity identity must be available before relationship/property references.
    ordered = sorted(
        indexed,
        key=lambda item: (
            0 if isinstance(item[1], RawEntityCandidate) else 1,
            item[0],
        ),
    )
    for input_candidate_id, raw in ordered:
        records.append(
            _make_candidate_record(
                input_candidate_id=input_candidate_id,
                raw=raw,
                vocabulary=vocabulary,
                contract=contract,
                project_id=base_identity.project_id,
                source_unit_id=source_unit_id,
                work_unit_id=work_unit_id,
                local_entities=local_entities,
                classifier_version=classifier_version,
                prompt_hash=prompt_hash,
                model_hash=model_hash,
                extractor_name=extractor_name,
                extractor_version=extractor_version,
            )
        )

    by_candidate_id: dict[str, ProposedCandidateRecord] = {}
    dispositions: list[CandidateAccountingDisposition] = []
    lifecycle_records: list[CandidateLifecycleRecord] = []
    audit_reasons: Counter[str] = Counter()
    accounting_identity = _validated_identity(
        base_identity,
        contract_kind="c0.candidate_accounting_disposition",
    )
    lifecycle_identity = _validated_identity(
        base_identity,
        contract_kind="c0.candidate_lifecycle_record",
    )
    for record in sorted(
        records,
        key=lambda item: (
            item.candidate_id,
            item.payload_hash,
            item.input_candidate_id,
        ),
    ):
        retained = by_candidate_id.get(record.candidate_id)
        if retained is not None:
            conflict = retained.payload_hash != record.payload_hash
            if conflict:
                audit_reasons["CANDIDATE_PAYLOAD_CONFLICT"] += 1
            dispositions.append(
                CandidateAccountingDisposition(
                    identity=accounting_identity,
                    input_candidate_id=record.input_candidate_id,
                    disposition="deduplicated",
                    retained_candidate_id=None,
                    deduplicated_into_candidate_id=retained.candidate_id,
                    current_state=None,
                    reason_codes=(
                        ("CANDIDATE_PAYLOAD_CONFLICT",)
                        if conflict
                        else ()
                    ),
                )
            )
            continue
        by_candidate_id[record.candidate_id] = record
        if record.approved_semantic_id is None:
            audit_reasons["DOMAIN_REREVIEW_REQUESTED"] += 1
            if (
                record.candidate_kind == "entity"
                and record.observed_term.casefold()
                in vocabulary.entities_by_alias
            ):
                audit_reasons["IDENTITY_POLICY_MISMATCH"] += 1
            else:
                audit_reasons[
                    {
                        "entity": "UNKNOWN_ENTITY_TYPE",
                        "relationship": "UNKNOWN_RELATIONSHIP_TYPE",
                        "property": "UNKNOWN_PROPERTY",
                    }[record.candidate_kind]
                ] += 1
        dispositions.append(
            CandidateAccountingDisposition(
                identity=accounting_identity,
                input_candidate_id=record.input_candidate_id,
                disposition="retained",
                retained_candidate_id=record.candidate_id,
                deduplicated_into_candidate_id=None,
                current_state=AssertionState.PROPOSED,
                reason_codes=(),
            )
        )
        lifecycle_id = deterministic_contract_id(
            "candidate-lifecycle",
            {
                "candidate_id": record.candidate_id,
                "candidate_version_id": record.candidate_version_id,
                "sequence": 0,
            },
        )
        lifecycle_records.append(
            CandidateLifecycleRecord.seal(
                identity=lifecycle_identity,
                lifecycle_record_id=lifecycle_id,
                candidate_id=record.candidate_id,
                candidate_version_id=record.candidate_version_id,
                candidate_kind=record.candidate_kind,
                sequence=0,
                prior_lifecycle_record_id=None,
                from_state=None,
                to_state=AssertionState.PROPOSED,
                reason_codes=(),
                evidence_span_ids=(),
                governance_justification_id=None,
                resolved_source_entity_id=None,
                resolved_target_entity_id=None,
                source_inheritance_path=(),
                target_inheritance_path=(),
                validator_name="l2-proposal-only",
                validator_version=L2_EXTRACTOR_VERSION,
                occurred_at_utc=occurred_at_utc,
            )
        )

    lifecycle_by_candidate = {
        item.candidate_id: item for item in lifecycle_records
    }
    retained_records = tuple(sorted(by_candidate_id.values(), key=lambda item: item.candidate_id))
    references = tuple(
        ExtractionCandidateReference(
            candidate_id=record.candidate_id,
            candidate_version_id=record.candidate_version_id,
            candidate_kind=record.candidate_kind,
            semantic_type_id=(
                record.approved_semantic_id
                or UNKNOWN_SEMANTIC_TYPE[record.candidate_kind]
            ),
            lifecycle_record_id=(
                lifecycle_by_candidate[record.candidate_id].lifecycle_record_id
            ),
            evidence_span_ids=(),
        )
        for record in retained_records
    )
    batch_id = deterministic_contract_id(
        "extraction-candidate-batch",
        {
            "work_unit_id": work_unit_id,
            "source_unit_id": source_unit_id,
            "candidate_version_ids": [
                reference.candidate_version_id for reference in references
            ],
        },
    )
    batch = ExtractionCandidateBatch.seal(
        identity=_validated_identity(
            base_identity,
            contract_kind="c0.extraction_candidate_batch",
        ),
        extraction_candidate_batch_id=batch_id,
        authority=authority,
        input_candidate_count=len(dispositions),
        retained_candidate_count=len(references),
        deduplicated_input_count=sum(
            item.disposition == "deduplicated" for item in dispositions
        ),
        candidates=references,
        candidate_dispositions=tuple(dispositions),
    )
    batch.validate_core_references(
        lifecycle_records=lifecycle_records,
        evidence_spans=(),
    )
    return ExtractionLeafResult(
        batch=batch,
        proposed_candidates=retained_records,
        lifecycle_records=tuple(
            sorted(lifecycle_records, key=lambda item: item.lifecycle_record_id)
        ),
        audit_reason_counts=tuple(sorted(audit_reasons.items())),
        raw_candidate_count=len(parsed),
    )


def merge_candidate_batches(
    leaves: tuple[ExtractionLeafResult, ...],
    *,
    authority: ExtractionAuthorityReferences,
    base_identity: CanonicalIdentityEnvelope,
    merge_key: str,
) -> ExtractionCandidateBatch:
    """Create one deterministic C0 carrier for a cross-leaf governed collection."""

    references: dict[str, ExtractionCandidateReference] = {}
    conflicted_candidate_ids: set[str] = set()
    dispositions: list[CandidateAccountingDisposition] = []
    for leaf in leaves:
        for reference in leaf.batch.candidates:
            prior = references.get(reference.candidate_id)
            if prior is not None and prior != reference:
                conflicted_candidate_ids.add(reference.candidate_id)
                references[reference.candidate_id] = min(
                    (prior, reference),
                    key=canonical_sha256,
                )
            else:
                references[reference.candidate_id] = reference
        for disposition in leaf.batch.candidate_dispositions:
            target = (
                disposition.retained_candidate_id
                if disposition.disposition == "retained"
                else disposition.deduplicated_into_candidate_id
            )
            dispositions.append(
                disposition.model_copy(
                    update={
                        "input_candidate_id": deterministic_contract_id(
                            "merged-input-candidate",
                            {
                                "input_candidate_id": (
                                    disposition.input_candidate_id
                                ),
                                "target_candidate_id": target,
                            },
                        )
                    }
                )
            )
    # Input IDs are leaf-stable, but identical source overlap can repeat them.
    disposition_by_id: dict[str, CandidateAccountingDisposition] = {}
    for disposition in sorted(dispositions, key=lambda item: item.input_candidate_id):
        prior = disposition_by_id.get(disposition.input_candidate_id)
        if prior is not None and prior != disposition:
            raise L2StageError(
                "L2_ACCOUNTING_INCOMPLETE",
                "cross-leaf input candidate has conflicting dispositions",
            )
        disposition_by_id[disposition.input_candidate_id] = disposition
    retained_ids = set(references)
    normalized_dispositions: list[CandidateAccountingDisposition] = []
    accounted_retained: set[str] = set()
    identity = _validated_identity(
        base_identity,
        contract_kind="c0.candidate_accounting_disposition",
    )
    for disposition in disposition_by_id.values():
        target = (
            disposition.retained_candidate_id
            if disposition.disposition == "retained"
            else disposition.deduplicated_into_candidate_id
        )
        if target not in retained_ids:
            continue
        if target in accounted_retained:
            normalized_dispositions.append(
                CandidateAccountingDisposition(
                    identity=identity,
                    input_candidate_id=disposition.input_candidate_id,
                    disposition="deduplicated",
                    retained_candidate_id=None,
                    deduplicated_into_candidate_id=target,
                    current_state=None,
                    reason_codes=(
                        ("CANDIDATE_PAYLOAD_CONFLICT",)
                        if target in conflicted_candidate_ids
                        else ()
                    ),
                )
            )
        else:
            accounted_retained.add(target)
            normalized_dispositions.append(
                CandidateAccountingDisposition(
                    identity=identity,
                    input_candidate_id=disposition.input_candidate_id,
                    disposition="retained",
                    retained_candidate_id=target,
                    deduplicated_into_candidate_id=None,
                    current_state=AssertionState.PROPOSED,
                    reason_codes=(
                        ("CANDIDATE_PAYLOAD_CONFLICT",)
                        if target in conflicted_candidate_ids
                        else ()
                    ),
                )
            )
    if accounted_retained != retained_ids:
        raise L2StageError(
            "L2_ACCOUNTING_INCOMPLETE",
            "merged batch cannot account every retained candidate",
        )
    refs = tuple(sorted(references.values(), key=lambda item: item.candidate_id))
    return ExtractionCandidateBatch.seal(
        identity=_validated_identity(
            base_identity,
            contract_kind="c0.extraction_candidate_batch",
        ),
        extraction_candidate_batch_id=deterministic_contract_id(
            "extraction-candidate-batch",
            {"merge_key": merge_key, "candidate_ids": sorted(references)},
        ),
        authority=authority,
        input_candidate_count=len(normalized_dispositions),
        retained_candidate_count=len(refs),
        deduplicated_input_count=sum(
            item.disposition == "deduplicated"
            for item in normalized_dispositions
        ),
        candidates=refs,
        candidate_dispositions=tuple(normalized_dispositions),
    )


def _requirement_by_id(
    contract: DomainContractV2,
    requirement_id: str,
) -> CompletenessRequirementV2:
    matches = [
        item
        for item in contract.completeness_requirements
        if item.requirement_id == requirement_id
    ]
    if len(matches) != 1:
        raise L2StageError(
            "L2_REQUIRED_MEMBER_SET_INVALID",
            f"unknown or duplicate completeness requirement {requirement_id}",
        )
    requirement = matches[0]
    if requirement.structured_fact_set is None:
        raise L2StageError(
            "L2_REQUIRED_MEMBER_SET_INVALID",
            "required-member proposals require a structured_fact_set authority",
        )
    return requirement


def build_required_member_set_proposals(
    fragments: tuple[CollectionMemberFragment, ...],
    *,
    leaves: tuple[ExtractionLeafResult, ...],
    contract: DomainContractV2,
    authority_factory: Any,
    base_identity: CanonicalIdentityEnvelope,
) -> tuple[ProposedRequiredMemberSetView, ...]:
    """Merge full-manifest collection fragments without another model call."""

    grouped: defaultdict[
        tuple[str, str], list[CollectionMemberFragment]
    ] = defaultdict(list)
    for fragment in fragments:
        grouped[(fragment.requirement_id, fragment.aggregate_entity_id)].append(
            fragment
        )
    aggregate_ids_by_type: defaultdict[str, set[str]] = defaultdict(set)
    for leaf in leaves:
        for candidate in leaf.proposed_candidates:
            if (
                candidate.candidate_kind == "entity"
                and candidate.approved_semantic_id is not None
            ):
                aggregate_ids_by_type[candidate.approved_semantic_id].add(
                    candidate.semantic_id
                )
    for requirement in contract.completeness_requirements:
        fact_set = requirement.structured_fact_set
        if fact_set is None:
            continue
        cardinality = fact_set.cardinality
        empty_reasons: list[str] = []
        if cardinality is not None and cardinality.expected_count not in {None, 0}:
            empty_reasons.append("EXPECTED_MEMBER_COUNT_MISMATCH")
        if cardinality is not None and cardinality.minimum_count not in {None, 0}:
            empty_reasons.append("MINIMUM_MEMBERS_NOT_OBSERVED")
        if fact_set.member_role_ids:
            empty_reasons.append("L2_ORDER_ROLE_UNSPECIFIED")
        for aggregate_id in aggregate_ids_by_type[fact_set.aggregate_type_id]:
            if (
                (requirement.requirement_id, aggregate_id) not in grouped
                and empty_reasons
            ):
                raise L2StageError(
                    "L2_REQUIRED_MEMBER_SET_INVALID",
                    "observed aggregate has no required-member observations: "
                    + ", ".join(sorted(set(empty_reasons))),
                )
    candidate_ids = {
        candidate.candidate_id
        for leaf in leaves
        for candidate in leaf.batch.candidates
    }
    views: list[ProposedRequiredMemberSetView] = []
    for (requirement_id, aggregate_id), members in sorted(grouped.items()):
        requirement = _requirement_by_id(contract, requirement_id)
        fact_set = requirement.structured_fact_set
        assert fact_set is not None
        authority = authority_factory(requirement)
        merged_batch = merge_candidate_batches(
            leaves,
            authority=authority,
            base_identity=base_identity,
            merge_key=f"{requirement_id}:{aggregate_id}",
        )
        unresolved: set[str] = set()
        member_by_identity: dict[str, CollectionMemberFragment] = {}
        for member in sorted(
            members,
            key=lambda item: (
                item.member_order if item.member_order is not None else 2**31,
                item.member_entity_id,
            ),
        ):
            required_refs = {
                member.member_candidate_id,
                member.membership_relationship_candidate_id,
            }
            if not required_refs <= candidate_ids:
                unresolved.add("L2_REQUIRED_MEMBER_REFERENCE_INVALID")
            if member.member_semantic_type_id not in fact_set.allowed_member_type_ids:
                unresolved.add("L2_REQUIRED_MEMBER_REFERENCE_INVALID")
            if (
                member.member_role_id is not None
                and member.member_role_id not in fact_set.member_role_ids
            ):
                unresolved.add("L2_ORDER_ROLE_INVALID")
            if fact_set.member_role_ids and member.member_role_id is None:
                unresolved.add("L2_ORDER_ROLE_UNSPECIFIED")
            if fact_set.ordering_policy.mode == "ordered":
                if member.member_order is None:
                    unresolved.add("L2_ORDER_ROLE_INVALID")
            elif member.member_order is not None:
                unresolved.add("L2_ORDER_ROLE_INVALID")
            prior = member_by_identity.get(member.member_entity_id)
            if prior is not None and prior != member:
                unresolved.add("L2_REQUIRED_MEMBER_IDENTITY_CONFLICT")
            member_by_identity[member.member_entity_id] = member

        ordered_members = sorted(
            member_by_identity.values(),
            key=lambda item: (
                item.member_order if item.member_order is not None else 2**31,
                item.member_entity_id,
            ),
        )
        cardinality = fact_set.cardinality
        expected = cardinality.expected_count if cardinality else None
        minimum = cardinality.minimum_count if cardinality else None
        maximum = cardinality.maximum_count if cardinality else None
        if expected is not None and len(ordered_members) != expected:
            unresolved.add("EXPECTED_MEMBER_COUNT_MISMATCH")
        if minimum is not None and len(ordered_members) < minimum:
            unresolved.add("MINIMUM_MEMBERS_NOT_OBSERVED")
        if maximum is not None and len(ordered_members) > maximum:
            unresolved.add("MAXIMUM_MEMBERS_EXCEEDED")

        ordering = fact_set.ordering_policy
        if ordering.mode == "ordered":
            if ordering.unique_ordinals is not True or ordering.contiguous is not True:
                raise L2StageError(
                    "L2_ORDER_ROLE_INVALID",
                    "C0 1.1 requires approved unique contiguous ordered-member semantics",
                )
            observed_orders = [member.member_order for member in ordered_members]
            if observed_orders != list(range(len(ordered_members))):
                raise L2StageError(
                    "L2_ORDER_ROLE_INVALID",
                    "ordered members require observed unique contiguous zero-based positions",
                )
        if fact_set.member_role_ids:
            observed_roles = {member.member_role_id for member in ordered_members}
            if None in observed_roles or observed_roles != set(fact_set.member_role_ids):
                raise L2StageError(
                    "L2_ORDER_ROLE_INVALID",
                    "observed member roles do not exactly cover approved required roles",
                )
        structural_errors = {
            "L2_REQUIRED_MEMBER_REFERENCE_INVALID",
            "L2_ORDER_ROLE_INVALID",
            "L2_ORDER_ROLE_UNSPECIFIED",
            "L2_REQUIRED_MEMBER_IDENTITY_CONFLICT",
        }
        if unresolved & structural_errors:
            raise L2StageError(
                "L2_REQUIRED_MEMBER_SET_INVALID",
                "required-member proposal has structural violations: "
                + ", ".join(sorted(unresolved & structural_errors)),
            )

        c0_members = tuple(
            RequiredMemberReferenceV1_1.seal(
                member_canonical_id=member.member_entity_id,
                member_semantic_type_id=member.member_semantic_type_id,
                member_role_id=member.member_role_id,
                member_order=member.member_order,
                candidate_id=member.member_candidate_id,
                supporting_evidence_span_ids=(),
            )
            for member in ordered_members
        )
        if not c0_members:
            raise L2StageError(
                "L2_REQUIRED_MEMBER_SET_INVALID",
                "an observed aggregate proposal must contain at least one member",
            )
        proposal_id = deterministic_contract_id(
            "required-member-set-proposal",
            {
                "domain_contract_hash": compute_contract_hash(contract),
                "carrier_version": "1.1.0",
                "requirement_id": requirement_id,
                "aggregate_entity_id": aggregate_id,
                "member_entity_ids": [
                    member.member_entity_id for member in ordered_members
                ],
                "source_unit_ids": sorted(
                    {member.source_unit_id for member in ordered_members}
                ),
            },
        )
        ordering_policy = RequiredMemberOrderingPolicyV1_1(
            mode=ordering.mode,
            ordinal_property_id=ordering.ordinal_property_id,
            ordinal_value_type=ordering.ordinal_value_type,
            direction=ordering.direction,
            unique_ordinals=ordering.unique_ordinals,
            contiguous=ordering.contiguous,
            member_order_encoding=(
                "zero_based_contiguous" if ordering.mode == "ordered" else None
            ),
        )
        proposal_identity_values = _validated_identity(
            base_identity,
            contract_kind="c0.required_member_set_proposal",
        ).model_dump(mode="python")
        proposal_identity_values["contract_version"] = "1.1.0"
        proposal = RequiredMemberSetProposalV1_1.seal(
            identity=RequiredMemberSetProposalIdentityV1_1.model_validate(
                proposal_identity_values
            ),
            required_member_set_proposal_id=proposal_id,
            extraction_candidate_batch_id=merged_batch.extraction_candidate_batch_id,
            extraction_candidate_batch_hash=merged_batch.batch_hash,
            authority=authority,
            scope_canonical_id=aggregate_id,
            membership_semantic_relationship_id=(
                fact_set.membership_relationship_type_id
            ),
            ordering_policy=ordering_policy,
            expected_cardinality=expected,
            minimum_cardinality=minimum,
            maximum_cardinality=maximum,
            required_role_ids=tuple(fact_set.member_role_ids),
            members=c0_members,
        )
        proposal.validate_against_batch(merged_batch)
        view_values = {
            "proposal_hash": proposal.proposal_hash,
            "requirement_id": requirement_id,
            "aggregate_entity_id": aggregate_id,
            "member_entity_ids": tuple(
                member.member_entity_id for member in ordered_members
            ),
            "contributing_source_unit_ids": tuple(
                sorted({member.source_unit_id for member in ordered_members})
            ),
            "membership_relationship_candidate_ids": tuple(
                sorted(
                    {
                        member.membership_relationship_candidate_id
                        for member in ordered_members
                    }
                )
            ),
            "unresolved_reasons": tuple(sorted(unresolved)),
        }
        views.append(
            ProposedRequiredMemberSetView(
                proposal=proposal,
                **{key: value for key, value in view_values.items() if key != "proposal_hash"},
                view_hash=canonical_sha256(view_values),
            )
        )
    return tuple(views)


def derive_collection_member_fragments(
    leaves: tuple[ExtractionLeafResult, ...],
    *,
    contract: DomainContractV2,
) -> tuple[CollectionMemberFragment, ...]:
    """Derive only observed memberships governed by sealed fact-set authorities."""

    entities = {
        candidate.semantic_id: candidate
        for leaf in leaves
        for candidate in leaf.proposed_candidates
        if candidate.candidate_kind == "entity"
    }
    fragments: list[CollectionMemberFragment] = []
    for requirement in contract.completeness_requirements:
        fact_set = requirement.structured_fact_set
        if fact_set is None:
            continue
        for leaf in leaves:
            for relationship in leaf.proposed_candidates:
                if (
                    relationship.candidate_kind != "relationship"
                    or relationship.approved_semantic_id
                    != fact_set.membership_relationship_type_id
                ):
                    continue
                endpoint_options = (
                    (
                        relationship.proposed_source_entity_id,
                        relationship.proposed_source_semantic_type_id,
                        relationship.proposed_target_entity_id,
                        relationship.proposed_target_semantic_type_id,
                    ),
                    (
                        relationship.proposed_target_entity_id,
                        relationship.proposed_target_semantic_type_id,
                        relationship.proposed_source_entity_id,
                        relationship.proposed_source_semantic_type_id,
                    ),
                )
                for (
                    aggregate_id,
                    aggregate_type_id,
                    member_id,
                    member_type_id,
                ) in endpoint_options:
                    if (
                        aggregate_id is None
                        or member_id is None
                        or aggregate_type_id != fact_set.aggregate_type_id
                        or member_type_id not in fact_set.allowed_member_type_ids
                    ):
                        continue
                    member_candidate = entities.get(member_id)
                    if member_candidate is None:
                        continue
                    fragments.append(
                        CollectionMemberFragment(
                            requirement_id=requirement.requirement_id,
                            aggregate_entity_id=aggregate_id,
                            member_entity_id=member_id,
                            member_candidate_id=member_candidate.candidate_id,
                            member_semantic_type_id=member_type_id,
                            member_role_id=relationship.proposed_member_role_id,
                            member_order=relationship.proposed_member_order,
                            membership_relationship_candidate_id=(
                                relationship.candidate_id
                            ),
                            source_unit_id=relationship.source_unit_id,
                        )
                    )
                    break
    return tuple(
        sorted(
            fragments,
            key=lambda item: (
                item.requirement_id,
                item.aggregate_entity_id,
                item.member_entity_id,
                item.membership_relationship_candidate_id,
            ),
        )
    )
