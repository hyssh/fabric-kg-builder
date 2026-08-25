"""Isolated L3 evidence, hierarchy, identity, and completeness rule engine.

This module owns the deterministic local-only rules that L3 applies to L2
proposals. It performs no LLM, Foundry, Document Intelligence, embedding,
Search, Fabric, or other remote call, and it never mutates a sealed authority.

Every trust-making primitive here delegates to C0-owned contracts:

* evidence identity is minted only by ``EvidenceSpanV1_1.mint_verified`` and
  re-checked by ``EvidenceSpan.verify_against``;
* semantic identity is minted only by ``deterministic_contract_id`` over the
  sealed ``domain.hierarchy`` identity-input helpers;
* lifecycle transitions are sealed only by ``CandidateLifecycleRecord.seal``;
* required-member manifests are sealed only by
  ``RequiredMemberManifestV1_1.seal_from_proposal``.

``schema2_validation_stage`` wires these rules to persisted L2 artifacts.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from fabric_kg_builder.contracts.base import (
    canonical_sha256,
    deterministic_contract_id,
    normalize_nfc,
    utf8_sha256,
)
from fabric_kg_builder.contracts.evidence import (
    EvidenceSpan,
    EvidenceSpanV1_1,
    SourceUnit,
)
from fabric_kg_builder.contracts.extraction import (
    RequiredMemberOrderingPolicyV1_1,
    RequiredMemberReferenceV1_1,
    authoritative_collection_hash_v1_1,
)
from fabric_kg_builder.contracts.identity import CanonicalIdentityEnvelope
from fabric_kg_builder.contracts.lifecycle import (
    AssertionState,
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
    DomainConstraintV2,
    DomainContractV2,
    DomainEntityTypeV2,
    DomainPropertyV2,
    DomainRelationshipTypeV2,
    IdentityKeyPolicyV2,
)
from fabric_kg_builder.domain.service import compute_contract_hash

L3_STAGE_NAME = "Evidence Validation"
L3_STAGE_CONTRACT_VERSION = "1.0.0"
L3_VALIDATOR_NAME = "l3-evidence-validator"
L3_VALIDATOR_VERSION = "1.0.0"

# Verifier identity is purpose-scoped: L3 never reuses an L1 design verifier ID.
L3_EXTRACTION_VERIFIER_NAME = "fabric-kg.local-evidence-verifier/extraction_assertion"
L3_EXTRACTION_VERIFIER_VERSION = "1.0.0"
L3_EXTRACTION_PURPOSE = "extraction_assertion"
L3_EXTRACTION_PURPOSE_VERSION = "1.0.0"
L3_EVIDENCE_SPAN_VERSION = "1.1.0"

#: Modalities the approved local text verifier can prove by exact code points.
#: Derived visual/transcript renderings require a validator capability that the
#: approved local validator does not represent, so they become ``unsupported``.
#: The same ``unsupported`` state records every other validator-capability gap,
#: including facts the frozen L2 carrier does not persist for local re-proof.
L3_SUPPORTED_EVIDENCE_UNIT_KINDS = frozenset(
    {"heading", "paragraph", "table", "cell"}
)

UNKNOWN_TERM_REASON = {
    "entity": "UNKNOWN_ENTITY_TYPE",
    "relationship": "UNKNOWN_RELATIONSHIP_TYPE",
    "property": "UNKNOWN_PROPERTY",
}

REJECTION_REASONS = frozenset(
    {
        "ABSTRACT_TYPE_INSTANTIATION",
        "DIRECTION_MISMATCH",
        "ENDPOINT_EVIDENCE_UNGROUNDED",
        "EVIDENCE_QUOTE_MISMATCH",
        "EVIDENCE_SOURCE_MISMATCH",
        "EVIDENCE_SPAN_INVALID",
        "HIERARCHY_CONCEPT_MISSING",
        "IDENTITY_POLICY_VIOLATION",
        "INHERITED_CONSTRAINT_VIOLATION",
        "INHERITED_PROPERTY_INVALID",
        "PROPERTY_VALUE_INVALID",
        "SEMANTIC_ID_MISMATCH",
        "SOURCE_TYPE_MISMATCH",
        "SUBTYPE_HIERARCHY_CYCLE",
        "TARGET_TYPE_MISMATCH",
    }
)
UNSUPPORTED_REASONS = frozenset({"EVIDENCE_MODALITY_UNSUPPORTED"})
DISCOVERY_REASONS = frozenset(
    {"UNKNOWN_ENTITY_TYPE", "UNKNOWN_PROPERTY", "UNKNOWN_RELATIONSHIP_TYPE"}
)
UNRESOLVED_REASONS = frozenset(
    {
        "AMBIGUOUS_SIBLING_CLASSIFICATION",
        "ENDPOINT_UNRESOLVED",
        "EVIDENCE_MISSING",
        "IDENTITY_WITNESS_UNAVAILABLE",
    }
)
#: Recorded for audit only; they never change the deterministic target state.
INFORMATIONAL_REASONS = frozenset(
    {"DOMAIN_REREVIEW_REQUESTED", "MODEL_EVIDENCE_ID_IGNORED"}
)

COMPLETENESS_REASONS = frozenset(
    {
        "ADJACENCY_EDGE_INVALID",
        "ADJACENCY_EDGE_MISSING",
        "CARDINALITY_BOUND_VIOLATION",
        "CARDINALITY_EVIDENCE_INVALID",
        "COLLECTION_HASH_MISMATCH",
        "MEMBERSHIP_EVIDENCE_INVALID",
        "MEMBER_TYPE_OR_ROLE_MISMATCH",
        "ORDINAL_DUPLICATE",
        "ORDINAL_MISSING",
        "ORDINAL_POLICY_VIOLATION",
        "REQUIRED_MEMBER_MISSING",
        "REQUIRED_ROLE_MISSING",
    }
)

STABLE_REASON_CODES = frozenset(
    REJECTION_REASONS
    | UNSUPPORTED_REASONS
    | DISCOVERY_REASONS
    | UNRESOLVED_REASONS
    | INFORMATIONAL_REASONS
    | COMPLETENESS_REASONS
)

#: Identity seeds must never carry classification, ancestry, or hierarchy depth.
FORBIDDEN_IDENTITY_SEED_KEYS = frozenset(
    {
        "ancestor_path",
        "ancestors",
        "classification",
        "descendants",
        "endpoint_type_ids",
        "entity_type",
        "hierarchy_depth",
        "hierarchy_hash",
        "parent_type_id",
        "semantic_type_id",
        "source_semantic_type_id",
        "source_type_id",
        "source_type_ids",
        "target_semantic_type_id",
        "target_type_id",
        "target_type_ids",
        "type_id",
    }
)


class L3StageError(ValueError):
    """Fail-closed L3 error carrying one stable audit code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        reason_codes: Sequence[str] = (),
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.reason_codes = tuple(sorted(set(reason_codes)))


def sorted_reasons(reasons: Iterable[str]) -> tuple[str, ...]:
    """Return the deterministic sorted unique reason-code tuple."""

    return tuple(sorted({str(reason) for reason in reasons}))


# ---------------------------------------------------------------------------
# Exact evidence verification and minting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProposedOccurrenceAnchor:
    """Untrusted SourceUnit-relative code-point anchor proposed by L2."""

    span_start: int
    span_end: int
    quote: str
    model_authored_evidence_id: str | None = None


@dataclass(frozen=True)
class EvidenceOutcome:
    """Result of one exact local evidence verification attempt."""

    span: EvidenceSpanV1_1 | None
    reason_codes: tuple[str, ...]
    ignored_model_evidence_id: str | None = None

    @property
    def verified(self) -> bool:
        return self.span is not None


class SourceUnitIndex:
    """Preloaded SourceUnit partition index; L3 never issues per-candidate reads."""

    __slots__ = ("_units", "_id_set_hash", "_content_hash")

    def __init__(self, units: Iterable[SourceUnit]) -> None:
        indexed: dict[str, SourceUnit] = {}
        for unit in units:
            existing = indexed.get(unit.source_unit_id)
            if existing is not None and existing != unit:
                raise L3StageError(
                    "L3_SOURCE_UNIT_MISSING",
                    f"conflicting SourceUnit partitions for {unit.source_unit_id}",
                )
            indexed[unit.source_unit_id] = unit
        self._units = indexed
        self._id_set_hash = canonical_sha256(sorted(indexed))
        # The complete semantic artifact, not only IDs: unit kind, offset unit,
        # ordinal, locator, and identity all bind here.
        self._content_hash = canonical_sha256(
            [[key, canonical_sha256(indexed[key])] for key in sorted(indexed)]
        )

    def __len__(self) -> int:
        return len(self._units)

    @property
    def source_unit_id_set_hash(self) -> str:
        return self._id_set_hash

    @property
    def source_unit_content_hash(self) -> str:
        """Return the hash of every verified SourceUnit artifact, not only IDs."""

        return self._content_hash

    def semantic_hash(self, source_unit_id: str) -> str:
        """Return the exact canonical hash of one verified SourceUnit artifact."""

        return canonical_sha256(self.require(source_unit_id))

    @property
    def units(self) -> tuple[SourceUnit, ...]:
        """Return every preloaded SourceUnit in deterministic ID order."""

        return tuple(self._units[key] for key in sorted(self._units))

    def get(self, source_unit_id: str) -> SourceUnit | None:
        return self._units.get(source_unit_id)

    def require(self, source_unit_id: str) -> SourceUnit:
        unit = self._units.get(source_unit_id)
        if unit is None:
            raise L3StageError(
                "L3_SOURCE_UNIT_MISSING",
                f"candidate references unmaterialized SourceUnit {source_unit_id}",
            )
        return unit


def verify_and_mint_extraction_span(
    *,
    source_unit: SourceUnit,
    anchor: ProposedOccurrenceAnchor | None,
    verified_at_utc: datetime,
    expected_source_text_hash: str | None = None,
    verifier_name: str = L3_EXTRACTION_VERIFIER_NAME,
    verifier_version: str = L3_EXTRACTION_VERIFIER_VERSION,
    verifier_purpose_version: str = L3_EXTRACTION_PURPOSE_VERSION,
) -> EvidenceOutcome:
    """Verify an untrusted anchor exactly, then mint one C0 1.1 span.

    The anchor is never trusted for identity. Bounds are Unicode code points in
    the exact NFC SourceUnit text, the quote must equal the exact substring, and
    the SourceUnit text hash, locator, and source identity must all agree.
    """

    if anchor is None:
        return EvidenceOutcome(span=None, reason_codes=("EVIDENCE_MISSING",))
    ignored = anchor.model_authored_evidence_id or None
    reasons: set[str] = set()
    if ignored is not None:
        reasons.add("MODEL_EVIDENCE_ID_IGNORED")
    if (
        expected_source_text_hash is not None
        and expected_source_text_hash != source_unit.text_content_hash
    ):
        reasons.add("EVIDENCE_SOURCE_MISMATCH")
    if source_unit.offset_unit != "unicode_codepoint":
        reasons.add("EVIDENCE_SOURCE_MISMATCH")
    if source_unit.text_content_hash != utf8_sha256(source_unit.text):
        reasons.add("EVIDENCE_SOURCE_MISMATCH")
    if not (
        0 <= anchor.span_start < anchor.span_end <= source_unit.codepoint_count
    ):
        reasons.add("EVIDENCE_SPAN_INVALID")
        return EvidenceOutcome(
            span=None,
            reason_codes=sorted_reasons(reasons),
            ignored_model_evidence_id=ignored,
        )
    quote = normalize_nfc(anchor.quote)
    if not quote:
        reasons.add("EVIDENCE_SPAN_INVALID")
    elif source_unit.text[anchor.span_start : anchor.span_end] != quote:
        reasons.add("EVIDENCE_QUOTE_MISMATCH")
    if reasons - INFORMATIONAL_REASONS:
        return EvidenceOutcome(
            span=None,
            reason_codes=sorted_reasons(reasons),
            ignored_model_evidence_id=ignored,
        )
    span = EvidenceSpanV1_1.mint_verified(
        source_unit=source_unit,
        span_start=anchor.span_start,
        span_end=anchor.span_end,
        verifier_name=verifier_name,
        verifier_version=verifier_version,
        purpose=L3_EXTRACTION_PURPOSE,
        verifier_purpose_version=verifier_purpose_version,
        verified_at_utc=verified_at_utc,
    )
    try:
        span.verify_against(source_unit)
    except ValueError:
        # A locator or source-identity disagreement is exact source drift.
        return EvidenceOutcome(
            span=None,
            reason_codes=sorted_reasons(reasons | {"EVIDENCE_SOURCE_MISMATCH"}),
            ignored_model_evidence_id=ignored,
        )
    if span.quote_hash != utf8_sha256(quote):
        raise L3StageError(
            "L3_EVIDENCE_ID_COLLISION",
            "minted span quote hash differs from the verified quote",
        )
    return EvidenceOutcome(
        span=span,
        reason_codes=sorted_reasons(reasons),
        ignored_model_evidence_id=ignored,
    )


def require_extraction_evidence(span: EvidenceSpan) -> EvidenceSpanV1_1:
    """Accept only locally minted 1.1 ``extraction_assertion`` evidence."""

    if span.identity.contract_version != L3_EVIDENCE_SPAN_VERSION:
        raise L3StageError(
            "L3_CONTRACT_VERSION_UNSUPPORTED",
            "L3 extraction evidence requires c0.evidence_span@1.1.0",
        )
    if not isinstance(span, EvidenceSpanV1_1):
        raise L3StageError(
            "L3_CONTRACT_VERSION_UNSUPPORTED",
            "L3 extraction evidence requires the 1.1 purpose-bound carrier",
        )
    if span.purpose != L3_EXTRACTION_PURPOSE:
        raise L3StageError(
            "L3_EVIDENCE_PURPOSE_INVALID",
            f"{span.purpose!r} evidence cannot assert an extraction candidate",
        )
    if span.verifier_purpose_version != L3_EXTRACTION_PURPOSE_VERSION:
        raise L3StageError(
            "L3_EVIDENCE_PURPOSE_VERSION_UNSUPPORTED",
            "extraction verifier purpose-policy version is not accepted",
        )
    if span.verifier_name != L3_EXTRACTION_VERIFIER_NAME:
        raise L3StageError(
            "L3_EVIDENCE_PURPOSE_INVALID",
            "extraction evidence must carry the L3 extraction verifier name",
        )
    return span


# ---------------------------------------------------------------------------
# Endpoint occurrence grounding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EndpointGroundingRequest:
    """One endpoint that must be grounded inside a relationship span."""

    endpoint_id: str
    role: Literal["source", "target"]
    terms: tuple[str, ...] = ()
    anchor: ProposedOccurrenceAnchor | None = None


@dataclass(frozen=True)
class GroundedOccurrence:
    endpoint_id: str
    role: str
    span_start: int
    span_end: int


@dataclass(frozen=True)
class GroundingOutcome:
    occurrences: tuple[GroundedOccurrence, ...]
    reason_codes: tuple[str, ...]

    @property
    def grounded(self) -> bool:
        return not self.reason_codes


def exact_occurrences(text: str, term: str) -> tuple[tuple[int, int], ...]:
    """Return every exact (possibly overlapping) code-point range of ``term``."""

    normalized = normalize_nfc(term)
    if not normalized:
        return ()
    found: list[tuple[int, int]] = []
    cursor = text.find(normalized)
    while cursor != -1:
        found.append((cursor, cursor + len(normalized)))
        cursor = text.find(normalized, cursor + 1)
    return tuple(found)


def ground_endpoints(
    *,
    source_text: str,
    span_start: int,
    span_end: int,
    requests: Sequence[EndpointGroundingRequest],
) -> GroundingOutcome:
    """Ground every endpoint occurrence inside one relationship evidence span."""

    if not 0 <= span_start < span_end <= len(source_text):
        return GroundingOutcome(
            occurrences=(),
            reason_codes=("EVIDENCE_SPAN_INVALID",),
        )
    quote = source_text[span_start:span_end]
    reasons: set[str] = set()
    occurrences: list[GroundedOccurrence] = []
    for request in sorted(requests, key=lambda item: (item.role, item.endpoint_id)):
        anchor = request.anchor
        if anchor is not None:
            inside = span_start <= anchor.span_start < anchor.span_end <= span_end
            if inside and source_text[
                anchor.span_start : anchor.span_end
            ] == normalize_nfc(anchor.quote):
                occurrences.append(
                    GroundedOccurrence(
                        endpoint_id=request.endpoint_id,
                        role=request.role,
                        span_start=anchor.span_start,
                        span_end=anchor.span_end,
                    )
                )
                continue
            reasons.add("ENDPOINT_EVIDENCE_UNGROUNDED")
            continue
        matches: set[tuple[int, int]] = set()
        for term in request.terms:
            for start, end in exact_occurrences(quote, term):
                matches.add((span_start + start, span_start + end))
        if len(matches) != 1:
            # Zero matches are ungrounded; repeated occurrences require one
            # exact proposed occurrence anchor instead of a guess.
            reasons.add("ENDPOINT_EVIDENCE_UNGROUNDED")
            continue
        start, end = matches.pop()
        occurrences.append(
            GroundedOccurrence(
                endpoint_id=request.endpoint_id,
                role=request.role,
                span_start=start,
                span_end=end,
            )
        )
    ranges = [(item.span_start, item.span_end) for item in occurrences]
    if len(set(ranges)) != len(ranges):
        # One span cannot ground two distinct endpoints to one occurrence.
        reasons.add("ENDPOINT_EVIDENCE_UNGROUNDED")
    if len(occurrences) != len(requests):
        reasons.add("ENDPOINT_EVIDENCE_UNGROUNDED")
    return GroundingOutcome(
        occurrences=tuple(
            sorted(occurrences, key=lambda item: (item.role, item.endpoint_id))
        ),
        reason_codes=sorted_reasons(reasons),
    )


# ---------------------------------------------------------------------------
# Hierarchy closure, inheritance, and endpoint compatibility
# ---------------------------------------------------------------------------


def compile_parent_closure(
    parent_by_type: Mapping[str, str | None],
) -> dict[str, tuple[str, ...]]:
    """Return cycle-safe ancestor closures for an arbitrary parent map."""

    closure: dict[str, tuple[str, ...]] = {}
    for type_id in sorted(parent_by_type):
        ancestors: list[str] = []
        visited = {type_id}
        cursor = parent_by_type[type_id]
        while cursor is not None:
            if cursor not in parent_by_type:
                raise L3StageError(
                    "L3_HIERARCHY_INVALID",
                    f"unknown parent semantic type: {cursor}",
                    reason_codes=("HIERARCHY_CONCEPT_MISSING",),
                )
            if cursor in visited:
                raise L3StageError(
                    "L3_HIERARCHY_INVALID",
                    "semantic type hierarchy contains a cycle",
                    reason_codes=("SUBTYPE_HIERARCHY_CYCLE",),
                )
            visited.add(cursor)
            ancestors.append(cursor)
            cursor = parent_by_type[cursor]
        closure[type_id] = tuple(ancestors)
    return closure


def deterministic_ancestor_path(
    type_id: str,
    ancestor_id: str,
    parent_by_type: Mapping[str, str | None],
) -> tuple[str, ...]:
    """Return the unique child-to-ancestor path, or ``()`` when unreachable."""

    if type_id not in parent_by_type:
        return ()
    path = [type_id]
    visited = {type_id}
    cursor: str | None = type_id
    while cursor is not None:
        if cursor == ancestor_id:
            return tuple(path)
        cursor = parent_by_type.get(cursor)
        if cursor is None:
            return ()
        if cursor in visited:
            raise L3StageError(
                "L3_HIERARCHY_INVALID",
                "semantic type hierarchy contains a cycle",
                reason_codes=("SUBTYPE_HIERARCHY_CYCLE",),
            )
        visited.add(cursor)
        path.append(cursor)
    return ()


@dataclass(frozen=True)
class EndpointOutcome:
    compatible: bool
    inheritance_path: tuple[str, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class CompiledHierarchy:
    """One cycle-checked closure compiled per sealed authority, never per candidate."""

    domain_contract_hash: str
    hierarchy_hash: str
    identity_policy_hash: str
    completeness_requirement_hash: str
    external_reference_decision_hash: str
    parent_by_type: Mapping[str, str | None]
    ancestors_by_type: Mapping[str, tuple[str, ...]]
    descendants_by_type: Mapping[str, tuple[str, ...]]
    depth_by_type: Mapping[str, int]
    abstract_type_ids: frozenset[str]
    effective_property_ids_by_type: Mapping[str, tuple[str, ...]]
    effective_constraint_ids_by_type: Mapping[str, tuple[str, ...]]
    entity_by_id: Mapping[str, DomainEntityTypeV2]
    relationship_by_id: Mapping[str, DomainRelationshipTypeV2]
    property_by_id: Mapping[str, DomainPropertyV2]
    constraint_by_id: Mapping[str, DomainConstraintV2]
    identity_policy_by_type: Mapping[str, IdentityKeyPolicyV2]
    compatible_source_type_ids: Mapping[str, frozenset[str]]
    compatible_target_type_ids: Mapping[str, frozenset[str]]
    requirement_by_id: Mapping[str, CompletenessRequirementV2]

    @property
    def hierarchy_depth(self) -> int:
        """Report hierarchy depth independently; never derive it from K."""

        return max(self.depth_by_type.values(), default=-1) + 1

    def ancestor_path(self, type_id: str, ancestor_id: str) -> tuple[str, ...]:
        return deterministic_ancestor_path(type_id, ancestor_id, self.parent_by_type)

    def effective_property_ids(self, type_id: str) -> tuple[str, ...]:
        return self.effective_property_ids_by_type.get(type_id, ())

    def is_descendant(self, type_id: str, ancestor_id: str) -> bool:
        return ancestor_id in self.ancestors_by_type.get(type_id, ())

    def endpoint_outcome(
        self,
        relationship_type_id: str,
        type_id: str | None,
        *,
        role: Literal["source", "target"],
    ) -> EndpointOutcome:
        """Resolve endpoint compatibility through the sealed ancestor closure."""

        relationship = self.relationship_by_id.get(relationship_type_id)
        mismatch = "SOURCE_TYPE_MISMATCH" if role == "source" else "TARGET_TYPE_MISMATCH"
        if relationship is None:
            return EndpointOutcome(
                compatible=False,
                inheritance_path=(),
                reason_codes=("HIERARCHY_CONCEPT_MISSING",),
            )
        if type_id is None or type_id not in self.entity_by_id:
            return EndpointOutcome(
                compatible=False,
                inheritance_path=(),
                reason_codes=("ENDPOINT_UNRESOLVED",),
            )
        declared = (
            relationship.source_type_ids
            if role == "source"
            else relationship.target_type_ids
        )
        if type_id in declared:
            return EndpointOutcome(
                compatible=True,
                inheritance_path=(type_id,),
                reason_codes=(),
            )
        if relationship.endpoint_policy == "exact":
            # Exact policy accepts only the declared type; never infer subtypes.
            return EndpointOutcome(
                compatible=False,
                inheritance_path=(),
                reason_codes=(mismatch,),
            )
        for allowed in sorted(declared):
            path = self.ancestor_path(type_id, allowed)
            if path:
                return EndpointOutcome(
                    compatible=True,
                    inheritance_path=path,
                    reason_codes=(),
                )
        return EndpointOutcome(
            compatible=False,
            inheritance_path=(),
            reason_codes=(mismatch,),
        )


def compile_hierarchy(contract: DomainContractV2) -> CompiledHierarchy:
    """Compile and hash-check every sealed authority once before validation."""

    entities = contract.candidate_model.entity_types
    relationships = contract.candidate_model.relationship_types
    try:
        closure = build_type_hierarchy_closure(entities, relationships)
    except ValueError as exc:
        raise L3StageError(
            "L3_HIERARCHY_INVALID",
            str(exc),
            reason_codes=(
                ("SUBTYPE_HIERARCHY_CYCLE",)
                if "cycle" in str(exc).casefold()
                else ("HIERARCHY_CONCEPT_MISSING",)
            ),
        ) from exc
    if closure != contract.hierarchy_closure:
        raise L3StageError(
            "L3_HIERARCHY_HASH_MISMATCH",
            "sealed hierarchy closure does not recompute",
        )

    roots = [item for item in entities if item.parent_type_id is None]
    expected_identity_hash = canonical_sha256(
        {
            item.type_id: item.identity_key_policy.model_dump(mode="json")
            for item in roots
            if item.identity_key_policy is not None
        }
    )
    if contract.identity_policy_hash != expected_identity_hash:
        raise L3StageError(
            "L3_IDENTITY_POLICY_HASH_MISMATCH",
            "sealed root identity policy does not recompute",
        )
    expected_completeness_hash = canonical_sha256(
        [item.model_dump(mode="json") for item in contract.completeness_requirements]
    )
    if contract.completeness_requirement_hash != expected_completeness_hash:
        raise L3StageError(
            "L3_COMPLETENESS_HASH_MISMATCH",
            "sealed completeness requirements do not recompute",
        )
    expected_external_hash = canonical_sha256(
        [
            item.model_dump(mode="json")
            for item in contract.approved_external_references
        ]
    )
    if contract.external_reference_decision_hash != expected_external_hash:
        raise L3StageError(
            "L3_EXTERNAL_REFERENCE_DECISION_HASH_MISMATCH",
            "approved external-reference decisions do not recompute",
        )

    parent_by_type = {item.type_id: item.parent_type_id for item in entities}
    ancestors_by_type = compile_parent_closure(parent_by_type)
    entity_by_id = {item.type_id: item for item in entities}
    property_by_id: dict[str, DomainPropertyV2] = {}
    constraint_by_id: dict[str, DomainConstraintV2] = {}
    for entity in entities:
        for declared in entity.declared_properties:
            property_by_id.setdefault(declared.property_id, declared)
        for constraint in entity.declared_constraints:
            constraint_by_id.setdefault(constraint.constraint_id, constraint)

    for type_id, property_ids in closure.effective_property_ids_by_type.items():
        missing = [item for item in property_ids if item not in property_by_id]
        if missing:
            raise L3StageError(
                "L3_APPROVED_CONCEPT_MISSING",
                f"effective properties without declaration for {type_id}: {missing}",
                reason_codes=("HIERARCHY_CONCEPT_MISSING",),
            )
    for type_id, constraint_ids in closure.effective_constraint_ids_by_type.items():
        missing = [item for item in constraint_ids if item not in constraint_by_id]
        if missing:
            raise L3StageError(
                "L3_APPROVED_CONCEPT_MISSING",
                f"effective constraints without declaration for {type_id}: {missing}",
                reason_codes=("HIERARCHY_CONCEPT_MISSING",),
            )
    for relationship in relationships:
        unknown = sorted(
            set(relationship.source_type_ids + relationship.target_type_ids)
            - set(entity_by_id)
        )
        if unknown:
            raise L3StageError(
                "L3_APPROVED_CONCEPT_MISSING",
                f"relationship endpoints are not approved types: {unknown}",
                reason_codes=("HIERARCHY_CONCEPT_MISSING",),
            )
        if relationship.direction != "source_to_target":
            raise L3StageError(
                "L3_HIERARCHY_INVALID",
                "approved relationships must declare source_to_target direction",
                reason_codes=("DIRECTION_MISMATCH",),
            )

    identity_policy_by_type: dict[str, IdentityKeyPolicyV2] = {}
    for entity in entities:
        try:
            identity_policy_by_type[entity.type_id] = resolve_identity_root_policy(
                entity.type_id,
                entities,
            )
        except ValueError as exc:
            raise L3StageError(
                "L3_IDENTITY_POLICY_HASH_MISMATCH",
                f"identity root policy for {entity.type_id} is unusable: {exc}",
            ) from exc

    depth_by_type = {
        type_id: len(ancestors) for type_id, ancestors in ancestors_by_type.items()
    }
    requirement_by_id = {
        item.requirement_id: item for item in contract.completeness_requirements
    }
    if len(requirement_by_id) != len(contract.completeness_requirements):
        raise L3StageError(
            "L3_COMPLETENESS_HASH_MISMATCH",
            "duplicate approved completeness requirement IDs",
        )
    return CompiledHierarchy(
        domain_contract_hash=compute_contract_hash(contract),
        hierarchy_hash=closure.hierarchy_hash,
        identity_policy_hash=contract.identity_policy_hash,
        completeness_requirement_hash=contract.completeness_requirement_hash,
        external_reference_decision_hash=contract.external_reference_decision_hash,
        parent_by_type=parent_by_type,
        ancestors_by_type=ancestors_by_type,
        descendants_by_type={
            type_id: tuple(values)
            for type_id, values in closure.descendants_by_type.items()
        },
        depth_by_type=depth_by_type,
        abstract_type_ids=frozenset(
            item.type_id for item in entities if item.abstract
        ),
        effective_property_ids_by_type={
            type_id: tuple(values)
            for type_id, values in closure.effective_property_ids_by_type.items()
        },
        effective_constraint_ids_by_type={
            type_id: tuple(values)
            for type_id, values in closure.effective_constraint_ids_by_type.items()
        },
        entity_by_id=entity_by_id,
        relationship_by_id={
            item.relationship_type_id: item for item in relationships
        },
        property_by_id=property_by_id,
        constraint_by_id=constraint_by_id,
        identity_policy_by_type=identity_policy_by_type,
        compatible_source_type_ids={
            key: frozenset(values)
            for key, values in (
                closure.compatible_source_type_ids_by_relationship.items()
            )
        },
        compatible_target_type_ids={
            key: frozenset(values)
            for key, values in (
                closure.compatible_target_type_ids_by_relationship.items()
            )
        },
        requirement_by_id=requirement_by_id,
    )


@dataclass(frozen=True)
class ClassificationResolution:
    most_specific_type_id: str | None
    ambiguous: bool
    candidate_type_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]


def resolve_most_specific_classification(
    type_ids: Iterable[str],
    hierarchy: CompiledHierarchy,
) -> ClassificationResolution:
    """Keep one most-specific concrete type or mark competing siblings unresolved."""

    observed = tuple(sorted({item for item in type_ids if item}))
    if not observed:
        return ClassificationResolution(None, False, (), ())
    unknown = tuple(item for item in observed if item not in hierarchy.entity_by_id)
    if unknown:
        return ClassificationResolution(
            None,
            False,
            observed,
            ("HIERARCHY_CONCEPT_MISSING",),
        )
    if len(observed) == 1:
        return ClassificationResolution(observed[0], False, observed, ())
    winners = [
        candidate
        for candidate in observed
        if all(
            other == candidate or hierarchy.is_descendant(candidate, other)
            for other in observed
        )
    ]
    if len(winners) == 1:
        return ClassificationResolution(winners[0], False, observed, ())
    return ClassificationResolution(
        None,
        True,
        observed,
        ("AMBIGUOUS_SIBLING_CLASSIFICATION",),
    )


def evaluate_inherited_constraints(
    type_id: str,
    hierarchy: CompiledHierarchy,
    *,
    observed_property_ids: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """Enforce the root-to-leaf inherited conjunction as one deterministic rule.

    ``observed_property_ids`` is ``None`` when property-owner attribution is not
    available from the sealed L2 carrier; the required-property conjunction is
    then not evaluated instead of being guessed.
    """

    reasons: set[str] = set()
    if type_id not in hierarchy.entity_by_id:
        return ("HIERARCHY_CONCEPT_MISSING",)
    if type_id in hierarchy.abstract_type_ids:
        reasons.add("ABSTRACT_TYPE_INSTANTIATION")
    for constraint_id in hierarchy.effective_constraint_ids_by_type.get(type_id, ()):
        constraint = hierarchy.constraint_by_id.get(constraint_id)
        if constraint is None:
            reasons.add("HIERARCHY_CONCEPT_MISSING")
    if observed_property_ids is not None:
        observed = {str(item) for item in observed_property_ids}
        for property_id in hierarchy.effective_property_ids_by_type.get(type_id, ()):
            declaration = hierarchy.property_by_id.get(property_id)
            if declaration is None:
                reasons.add("HIERARCHY_CONCEPT_MISSING")
                continue
            if declaration.required and property_id not in observed:
                reasons.add("INHERITED_CONSTRAINT_VIOLATION")
        unknown = observed - set(
            hierarchy.effective_property_ids_by_type.get(type_id, ())
        )
        if unknown:
            reasons.add("INHERITED_PROPERTY_INVALID")
    return sorted_reasons(reasons)


_VALUE_TYPE_CHECKS = {
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float))
    and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "date": lambda value: isinstance(value, str),
    "datetime": lambda value: isinstance(value, str),
}


def validate_property_observation(
    *,
    hierarchy: CompiledHierarchy,
    owner_type_id: str | None,
    property_id: str | None,
    value: Any = None,
    value_available: bool = False,
    owner_classification_unresolved: bool = False,
) -> tuple[str, ...]:
    """Validate one observation against the sealed effective inherited property."""

    reasons: set[str] = set()
    if property_id is None:
        return ("UNKNOWN_PROPERTY",)
    declaration = hierarchy.property_by_id.get(property_id)
    if declaration is None:
        return ("HIERARCHY_CONCEPT_MISSING",)
    if owner_type_id is not None:
        effective = hierarchy.effective_property_ids_by_type.get(owner_type_id)
        if effective is None:
            reasons.add("HIERARCHY_CONCEPT_MISSING")
        elif property_id not in effective:
            reasons.add("INHERITED_PROPERTY_INVALID")
    if value_available:
        checker = _VALUE_TYPE_CHECKS.get(declaration.value_type)
        if checker is None or not checker(value):
            reasons.add("PROPERTY_VALUE_INVALID")
    if owner_classification_unresolved:
        # A property valid only for one unresolved sibling stays unresolved.
        reasons.add("AMBIGUOUS_SIBLING_CLASSIFICATION")
    return sorted_reasons(reasons)


def resolve_direction(direction: str | None) -> tuple[str, ...]:
    """Accept only the approved forward direction; never silently swap endpoints."""

    if direction == "source_to_target":
        return ()
    return ("DIRECTION_MISMATCH",)


# ---------------------------------------------------------------------------
# Stable identity recomputation
# ---------------------------------------------------------------------------


def assert_type_independent_identity_inputs(inputs: Mapping[str, Any]) -> None:
    """Reject any identity seed that carries classification or hierarchy facts."""

    def scan(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if str(key) in FORBIDDEN_IDENTITY_SEED_KEYS:
                    raise L3StageError(
                        "L3_IDENTITY_POLICY_HASH_MISMATCH",
                        f"identity seed must not contain {key!r}",
                        reason_codes=("IDENTITY_POLICY_VIOLATION",),
                    )
                scan(item)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                scan(item)

    scan(inputs)


def normalize_business_key(values: Mapping[str, str]) -> dict[str, str]:
    """Apply the sealed normalization used by the approved identity policy."""

    return {
        str(key): " ".join(normalize_nfc(str(value)).casefold().split())
        for key, value in sorted(values.items())
    }


def derived_stable_source_identity(
    *,
    source_unit_id: str,
    local_reference: str,
) -> str:
    """Return the deterministic source-scoped identity witness used by L2."""

    return f"{source_unit_id}:{normalize_nfc(local_reference).casefold()}"


def recompute_entity_id(
    *,
    project_id: str,
    policy: IdentityKeyPolicyV2,
    normalized_business_key: Mapping[str, str] | None = None,
    stable_source_identity: str | None = None,
) -> str:
    """Recompute one type-independent entity ID through the C0 ID primitive."""

    try:
        inputs = stable_entity_identity_inputs(
            project_id=project_id,
            policy=policy,
            normalized_business_key=normalized_business_key,
            stable_source_identity=stable_source_identity,
        )
    except ValueError as exc:
        raise L3StageError(
            "L3_IDENTITY_POLICY_HASH_MISMATCH",
            str(exc),
            reason_codes=("IDENTITY_POLICY_VIOLATION",),
        ) from exc
    assert_type_independent_identity_inputs(inputs)
    return deterministic_contract_id("entity", inputs)


def recompute_observation_entity_id(
    *,
    project_id: str,
    identity_value: Any,
) -> str:
    """Recompute the audit-only identity used for unapproved observations."""

    inputs = {
        "project_id": project_id,
        "identity_authority": "unapproved-observation",
        "identity_namespace": "domain-rereview",
        "identity_value": identity_value,
        "normalization_version": "1",
    }
    assert_type_independent_identity_inputs(inputs)
    return deterministic_contract_id("entity", inputs)


def recompute_relationship_id(
    *,
    predicate_id: str,
    source_entity_id: str,
    target_entity_id: str,
    governed_context: Mapping[str, Any] | str | None,
) -> str:
    """Recompute one relationship ID from predicate plus stable endpoint IDs."""

    inputs = stable_relationship_identity_inputs(
        predicate_id=predicate_id,
        source_entity_id=source_entity_id,
        target_entity_id=target_entity_id,
        governed_context=governed_context,
    )
    assert_type_independent_identity_inputs(inputs)
    return deterministic_contract_id("relationship", inputs)


def is_minted_contract_id(value: str, prefix: str) -> bool:
    """Return whether ``value`` has the exact C0 deterministic ID shape."""

    marker = f"{prefix}:"
    if not value.startswith(marker):
        return False
    digest = value[len(marker) :]
    return len(digest) == 32 and all(
        character in "0123456789abcdef" for character in digest
    )


#: Witness kinds whose stable ID L3 cannot recompute from the frozen L2 carrier.
#: None of them may assert; each carries an explicit non-asserting reason code.
NON_ASSERTABLE_WITNESS_KINDS = frozenset(
    {
        "business_key_witness_unavailable",
        "invalid_shape",
        "opaque_observation_identity",
        "opaque_source_identity",
        "policy_missing",
        "witness_unavailable",
    }
)


@dataclass(frozen=True)
class IdentityWitnessOutcome:
    """Result of one local stable-entity-identity recomputation attempt."""

    recomputed: bool
    witness_kind: str
    reason_codes: tuple[str, ...]


def resolve_identity_witness(
    *,
    semantic_id: str,
    approved_semantic_id: str | None,
    source_unit_id: str,
    local_reference: str | None,
    hierarchy: CompiledHierarchy,
    project_id: str,
) -> IdentityWitnessOutcome:
    """Recompute one stable entity ID, or fail closed when it is not provable.

    An entity may only assert when its persisted stable ID is reproduced exactly
    from the sealed identity policy and the persisted witness. A missing witness
    is ``unresolved`` (``IDENTITY_WITNESS_UNAVAILABLE``); a witness that does not
    reproduce the persisted ID is an opaque, model-controlled seed and is
    ``rejected`` (``IDENTITY_POLICY_VIOLATION``). Neither is ever asserted.
    """

    if not is_minted_contract_id(semantic_id, "entity"):
        return IdentityWitnessOutcome(
            recomputed=False,
            witness_kind="invalid_shape",
            reason_codes=("SEMANTIC_ID_MISMATCH",),
        )
    if local_reference is None:
        return IdentityWitnessOutcome(
            recomputed=False,
            witness_kind="witness_unavailable",
            reason_codes=("IDENTITY_WITNESS_UNAVAILABLE",),
        )
    derived = derived_stable_source_identity(
        source_unit_id=source_unit_id,
        local_reference=local_reference,
    )
    if approved_semantic_id is None:
        recomputed = recompute_observation_entity_id(
            project_id=project_id,
            identity_value={"stable_source_identity": derived},
        )
        if recomputed == semantic_id:
            return IdentityWitnessOutcome(True, "derived_observation_identity", ())
        return IdentityWitnessOutcome(
            recomputed=False,
            witness_kind="opaque_observation_identity",
            reason_codes=("IDENTITY_POLICY_VIOLATION",),
        )
    policy = hierarchy.identity_policy_by_type.get(approved_semantic_id)
    if policy is None:
        return IdentityWitnessOutcome(
            recomputed=False,
            witness_kind="policy_missing",
            reason_codes=("HIERARCHY_CONCEPT_MISSING",),
        )
    if policy.key_mode != "stable_source_identity":
        # The frozen carrier does not persist the normalized business key.
        return IdentityWitnessOutcome(
            recomputed=False,
            witness_kind="business_key_witness_unavailable",
            reason_codes=("IDENTITY_WITNESS_UNAVAILABLE",),
        )
    recomputed = recompute_entity_id(
        project_id=project_id,
        policy=policy,
        stable_source_identity=derived,
    )
    if recomputed == semantic_id:
        return IdentityWitnessOutcome(True, "derived_source_identity", ())
    return IdentityWitnessOutcome(
        recomputed=False,
        witness_kind="opaque_source_identity",
        reason_codes=("IDENTITY_POLICY_VIOLATION",),
    )


# ---------------------------------------------------------------------------
# Append-only lifecycle classification
# ---------------------------------------------------------------------------


def classify_state(reason_codes: Iterable[str]) -> AssertionState:
    """Map reason codes to one deterministic current state."""

    reasons = set(reason_codes)
    unknown = reasons - STABLE_REASON_CODES
    if unknown:
        raise L3StageError(
            "L3_VALIDATION_RESULT_INCOMPLETE",
            f"unstable reason codes are prohibited: {sorted(unknown)}",
        )
    if reasons & REJECTION_REASONS:
        return AssertionState.REJECTED
    if reasons & UNSUPPORTED_REASONS:
        return AssertionState.UNSUPPORTED
    if reasons & DISCOVERY_REASONS:
        return AssertionState.DISCOVERY
    if reasons & UNRESOLVED_REASONS:
        return AssertionState.UNRESOLVED
    return AssertionState.ASSERTED


def unprovable_assertion_reasons(
    blocking_reason_codes: Iterable[str],
) -> tuple[str, ...]:
    """Block assertion when the frozen L2 carrier cannot prove a required fact.

    The approved local validator can only assert what it re-proves from the
    persisted carrier. When a required proof — the model-proposed relationship
    direction token, or property owner attribution and value — is not persisted,
    the candidate is an explicit validator-capability gap rather than a proven
    assertion, so it becomes ``unsupported``. A more precise blocking reason that
    already applies keeps its own deterministic state and is never overwritten.
    """

    if classify_state(blocking_reason_codes) is AssertionState.ASSERTED:
        return ("EVIDENCE_MODALITY_UNSUPPORTED",)
    return ()


def relationship_direction_reasons(
    *,
    proposed_direction: str | None,
    direction_persisted: bool,
    blocking_reason_codes: Iterable[str] = (),
) -> tuple[str, ...]:
    """Reject a reverse/unknown direction; fail closed when none is persisted.

    ``direction_persisted`` is ``False`` for the frozen L2 proposed-candidate
    carrier, which folds the model-proposed direction token into the relationship
    identity seed without persisting it. Direction is then not provable locally,
    so the relationship can never be asserted as if the direction were proven.
    """

    if not direction_persisted:
        return unprovable_assertion_reasons(blocking_reason_codes)
    return resolve_direction(proposed_direction)


def property_attribution_reasons(
    *,
    owner_attribution_persisted: bool,
    value_persisted: bool,
    blocking_reason_codes: Iterable[str] = (),
) -> tuple[str, ...]:
    """Fail closed when property owner attribution or value is not persisted.

    Inherited-property validity and value conformance can only be claimed when
    the owner type and the observed value are both re-readable from the sealed
    carrier. The frozen L2 carrier persists neither, so a property observation is
    never asserted as if inheritance and value had been validated.
    """

    if owner_attribution_persisted and value_persisted:
        return ()
    return unprovable_assertion_reasons(blocking_reason_codes)


def append_current_transition(
    prior: CandidateLifecycleRecord,
    *,
    identity: CanonicalIdentityEnvelope,
    to_state: AssertionState,
    reason_codes: Iterable[str] = (),
    evidence_span_ids: Iterable[str] = (),
    resolved_source_entity_id: str | None = None,
    resolved_target_entity_id: str | None = None,
    source_inheritance_path: Iterable[str] = (),
    target_inheritance_path: Iterable[str] = (),
    occurred_at_utc: datetime,
    validator_name: str = L3_VALIDATOR_NAME,
    validator_version: str = L3_VALIDATOR_VERSION,
) -> CandidateLifecycleRecord:
    """Append exactly one current transition to a sequence-zero proposed event."""

    if prior.from_state is not None or prior.sequence != 0:
        raise L3StageError(
            "L3_LIFECYCLE_CHAIN_INVALID",
            "L3 appends only to the sequence-zero L2 proposed event",
        )
    if prior.to_state is not AssertionState.PROPOSED:
        raise L3StageError(
            "L3_LIFECYCLE_CHAIN_INVALID",
            f"terminal candidate {prior.candidate_id} cannot be mutated",
        )
    if to_state is AssertionState.PROPOSED:
        raise L3StageError(
            "L3_LIFECYCLE_CHAIN_INVALID",
            "L3 must record one non-proposed current state",
        )
    if to_state is AssertionState.ASSERTED and not tuple(evidence_span_ids):
        raise L3StageError(
            "L3_VALIDATION_RESULT_INCOMPLETE",
            f"asserted candidate {prior.candidate_id} requires verified evidence",
        )
    lifecycle_record_id = deterministic_contract_id(
        "candidate-lifecycle",
        {
            "candidate_id": prior.candidate_id,
            "candidate_version_id": prior.candidate_version_id,
            "sequence": prior.sequence + 1,
        },
    )
    if lifecycle_record_id == prior.lifecycle_record_id:
        raise L3StageError(
            "L3_LIFECYCLE_CHAIN_INVALID",
            "appended lifecycle record collides with its prior record",
        )
    return CandidateLifecycleRecord.seal(
        identity=identity,
        lifecycle_record_id=lifecycle_record_id,
        candidate_id=prior.candidate_id,
        candidate_version_id=prior.candidate_version_id,
        candidate_kind=prior.candidate_kind,
        sequence=prior.sequence + 1,
        prior_lifecycle_record_id=prior.lifecycle_record_id,
        from_state=prior.to_state,
        to_state=to_state,
        reason_codes=sorted_reasons(reason_codes),
        evidence_span_ids=tuple(sorted(set(evidence_span_ids))),
        governance_justification_id=None,
        resolved_source_entity_id=resolved_source_entity_id,
        resolved_target_entity_id=resolved_target_entity_id,
        source_inheritance_path=tuple(source_inheritance_path),
        target_inheritance_path=tuple(target_inheritance_path),
        validator_name=validator_name,
        validator_version=validator_version,
        occurred_at_utc=occurred_at_utc,
    )


# ---------------------------------------------------------------------------
# Structured fact-set completeness validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifiedMember:
    """One proposal member with its locally verified L3 result."""

    member_canonical_id: str
    member_semantic_type_id: str
    member_role_id: str | None
    member_order: int | None
    candidate_id: str
    member_state: AssertionState
    membership_state: AssertionState
    membership_evidence_span_ids: tuple[str, ...] = ()
    member_evidence_span_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdjacencyPolicy:
    """Adjacency obligations, used only when the Domain separately approves one."""

    relationship_type_id: str
    edges: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class CompletenessOutcome:
    required_member_set_proposal_id: str
    scope_canonical_id: str
    requirement_id: str
    completeness_state: Literal["complete", "unresolved"]
    reason_codes: tuple[str, ...]
    verified_member_ids: tuple[str, ...]
    verified_member_count: int
    specified_expected_count: int | None
    specified_minimum_count: int | None
    specified_maximum_count: int | None
    role_coverage: tuple[tuple[str, int], ...]
    membership_evidence_span_ids: tuple[str, ...]
    recomputed_collection_hash: str
    proposal_collection_hash: str


def validate_adjacency_edges(
    *,
    policy: AdjacencyPolicy | None,
    ordered_member_ids: Sequence[str],
    verified_edges: Iterable[tuple[str, str]],
) -> tuple[str, ...]:
    """Verify approved adjacency edges only; ordinal order never invents them."""

    if policy is None:
        return ()
    observed = list(verified_edges)
    unique = set(observed)
    reasons: set[str] = set()
    if len(observed) != len(unique):
        reasons.add("ADJACENCY_EDGE_INVALID")
    required = set(policy.edges)
    if not required:
        required = {
            (ordered_member_ids[index], ordered_member_ids[index + 1])
            for index in range(len(ordered_member_ids) - 1)
        }
    missing = required - unique
    if missing:
        reasons.add("ADJACENCY_EDGE_MISSING")
    extra = unique - required
    if extra:
        reasons.add("ADJACENCY_EDGE_INVALID")
    members = set(ordered_member_ids)
    if any(
        source not in members or target not in members for source, target in unique
    ):
        reasons.add("ADJACENCY_EDGE_INVALID")
    outgoing: dict[str, int] = {}
    for source, _target in unique:
        outgoing[source] = outgoing.get(source, 0) + 1
    if any(count > 1 for count in outgoing.values()):
        reasons.add("ADJACENCY_EDGE_INVALID")
    if _has_cycle(unique):
        reasons.add("ADJACENCY_EDGE_INVALID")
    return sorted_reasons(reasons)


def _has_cycle(edges: Iterable[tuple[str, str]]) -> bool:
    successors: dict[str, list[str]] = {}
    for source, target in edges:
        successors.setdefault(source, []).append(target)
    color: dict[str, int] = {}
    for start in sorted(successors):
        if color.get(start, 0) != 0:
            continue
        stack: list[tuple[str, int]] = [(start, 0)]
        color[start] = 1
        while stack:
            node, index = stack.pop()
            children = sorted(successors.get(node, ()))
            if index < len(children):
                stack.append((node, index + 1))
                child = children[index]
                state = color.get(child, 0)
                if state == 1:
                    return True
                if state == 0:
                    color[child] = 1
                    stack.append((child, 0))
            else:
                color[node] = 2
    return False


def validate_required_member_proposal(
    *,
    proposal_id: str,
    requirement: CompletenessRequirementV2,
    scope_canonical_id: str,
    ordering_policy: RequiredMemberOrderingPolicyV1_1,
    required_role_ids: Sequence[str],
    expected_cardinality: int | None,
    minimum_cardinality: int | None,
    maximum_cardinality: int | None,
    proposal_members: Sequence[RequiredMemberReferenceV1_1],
    verified_members: Sequence[VerifiedMember],
    hierarchy: CompiledHierarchy,
    authority: Any,
    membership_semantic_relationship_id: str,
    proposal_collection_hash: str,
    approved_cardinality_evidence_ids: Iterable[str] = (),
    prohibited_cardinality_evidence_ids: Iterable[str] = (),
    adjacency_policy: AdjacencyPolicy | None = None,
    verified_adjacency_edges: Iterable[tuple[str, str]] = (),
) -> CompletenessOutcome:
    """Validate one generic proposal against its sealed completeness requirement."""

    fact_set = requirement.structured_fact_set
    if fact_set is None:
        raise L3StageError(
            "L3_COMPLETENESS_HASH_MISMATCH",
            f"{requirement.requirement_id} does not govern a structured fact set",
        )
    reasons: set[str] = set()
    verified_by_id = {item.member_canonical_id: item for item in verified_members}
    if len(verified_by_id) != len(verified_members):
        reasons.add("REQUIRED_MEMBER_MISSING")

    retained: list[VerifiedMember] = []
    for member in proposal_members:
        verified = verified_by_id.get(member.member_canonical_id)
        if verified is None:
            reasons.add("REQUIRED_MEMBER_MISSING")
            continue
        if verified.member_state is not AssertionState.ASSERTED:
            reasons.add("REQUIRED_MEMBER_MISSING")
            continue
        if verified.membership_state is not AssertionState.ASSERTED:
            reasons.add("MEMBERSHIP_EVIDENCE_INVALID")
            continue
        if not verified.membership_evidence_span_ids:
            reasons.add("MEMBERSHIP_EVIDENCE_INVALID")
            continue
        allowed = _member_type_allowed(
            verified.member_semantic_type_id,
            fact_set.allowed_member_type_ids,
            hierarchy,
        )
        role_allowed = (
            verified.member_role_id in set(fact_set.member_role_ids)
            if fact_set.member_role_ids
            else verified.member_role_id is None
        )
        if not allowed or not role_allowed:
            reasons.add("MEMBER_TYPE_OR_ROLE_MISMATCH")
            continue
        if (
            verified.member_semantic_type_id != member.member_semantic_type_id
            or verified.member_role_id != member.member_role_id
            or verified.member_order != member.member_order
            or verified.candidate_id != member.candidate_id
        ):
            reasons.add("MEMBER_TYPE_OR_ROLE_MISMATCH")
            continue
        retained.append(verified)

    retained_ids = {item.member_canonical_id for item in retained}
    canonical_members = tuple(
        member
        for member in proposal_members
        if member.member_canonical_id in retained_ids
    )

    if fact_set.member_role_ids:
        covered = {
            item.member_role_id for item in retained if item.member_role_id is not None
        }
        if not set(fact_set.member_role_ids) <= covered:
            reasons.add("REQUIRED_ROLE_MISSING")
    role_coverage = tuple(
        sorted(
            (
                role_id,
                sum(1 for item in retained if item.member_role_id == role_id),
            )
            for role_id in fact_set.member_role_ids
        )
    )

    reasons.update(
        _ordinal_reasons(
            ordering_policy=ordering_policy,
            members=canonical_members,
        )
    )

    ordered_member_ids = tuple(
        member.member_canonical_id for member in canonical_members
    )
    reasons.update(
        validate_adjacency_edges(
            policy=adjacency_policy,
            ordered_member_ids=ordered_member_ids,
            verified_edges=verified_adjacency_edges,
        )
    )

    distinct_count = len(retained_ids)
    cardinality = fact_set.cardinality
    if cardinality is not None:
        if cardinality.source_kind == "source_evidence":
            claimed = set(cardinality.source_evidence_span_ids)
            approved = set(approved_cardinality_evidence_ids)
            prohibited = set(prohibited_cardinality_evidence_ids)
            # Only locally minted extraction evidence can prove an observed
            # count; bounded design-sample evidence never proves extraction.
            if not claimed <= approved or claimed & prohibited or not claimed:
                reasons.add("CARDINALITY_EVIDENCE_INVALID")
        if (
            expected_cardinality is not None
            and distinct_count != expected_cardinality
        ) or (
            minimum_cardinality is not None and distinct_count < minimum_cardinality
        ) or (
            maximum_cardinality is not None and distinct_count > maximum_cardinality
        ):
            reasons.add("CARDINALITY_BOUND_VIOLATION")
    elif (
        expected_cardinality is not None
        or minimum_cardinality is not None
        or maximum_cardinality is not None
    ):
        # Counts are never inferred when the Domain contract does not specify one.
        reasons.add("CARDINALITY_EVIDENCE_INVALID")

    recomputed_hash = authoritative_collection_hash_v1_1(
        authority=authority,
        scope_canonical_id=scope_canonical_id,
        membership_semantic_relationship_id=membership_semantic_relationship_id,
        ordering_policy=ordering_policy,
        expected_cardinality=expected_cardinality,
        minimum_cardinality=minimum_cardinality,
        maximum_cardinality=maximum_cardinality,
        required_role_ids=tuple(required_role_ids),
        members=canonical_members,
    )
    if recomputed_hash != proposal_collection_hash:
        reasons.add("COLLECTION_HASH_MISMATCH")

    membership_evidence = tuple(
        sorted(
            {
                evidence_id
                for item in retained
                for evidence_id in item.membership_evidence_span_ids
            }
        )
    )
    reason_codes = sorted_reasons(reasons)
    return CompletenessOutcome(
        required_member_set_proposal_id=proposal_id,
        scope_canonical_id=scope_canonical_id,
        requirement_id=requirement.requirement_id,
        completeness_state="complete" if not reason_codes else "unresolved",
        reason_codes=reason_codes,
        verified_member_ids=tuple(sorted(retained_ids)),
        verified_member_count=distinct_count,
        specified_expected_count=expected_cardinality,
        specified_minimum_count=minimum_cardinality,
        specified_maximum_count=maximum_cardinality,
        role_coverage=role_coverage,
        membership_evidence_span_ids=membership_evidence,
        recomputed_collection_hash=recomputed_hash,
        proposal_collection_hash=proposal_collection_hash,
    )


def _member_type_allowed(
    type_id: str,
    allowed_type_ids: Sequence[str],
    hierarchy: CompiledHierarchy,
) -> bool:
    if type_id in set(allowed_type_ids):
        return True
    return any(
        hierarchy.is_descendant(type_id, allowed) for allowed in allowed_type_ids
    )


def _ordinal_reasons(
    *,
    ordering_policy: RequiredMemberOrderingPolicyV1_1,
    members: Sequence[RequiredMemberReferenceV1_1],
) -> tuple[str, ...]:
    reasons: set[str] = set()
    orders = [member.member_order for member in members]
    if ordering_policy.mode == "unordered":
        if any(order is not None for order in orders):
            reasons.add("ORDINAL_POLICY_VIOLATION")
        return sorted_reasons(reasons)
    if any(order is None for order in orders):
        reasons.add("ORDINAL_MISSING")
        return sorted_reasons(reasons)
    if ordering_policy.ordinal_value_type != "integer" or any(
        isinstance(order, bool) or not isinstance(order, int) for order in orders
    ):
        reasons.add("ORDINAL_POLICY_VIOLATION")
    present = [order for order in orders if order is not None]
    if len(set(present)) != len(present):
        reasons.add("ORDINAL_DUPLICATE")
    if ordering_policy.contiguous and sorted(present) != list(range(len(present))):
        reasons.add("ORDINAL_MISSING")
    if ordering_policy.direction == "ascending" and present != sorted(present):
        reasons.add("ORDINAL_POLICY_VIOLATION")
    if ordering_policy.direction == "descending" and present != sorted(
        present, reverse=True
    ):
        reasons.add("ORDINAL_POLICY_VIOLATION")
    return sorted_reasons(reasons)
