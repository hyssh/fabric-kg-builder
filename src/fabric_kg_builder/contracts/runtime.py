"""Behavior-free C0.Runtime contracts for bounded evidence retrieval."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal, Mapping
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator

from .base import (
    ContractModel,
    RequiredText,
    SemVer,
    Sha256,
    canonical_sha256,
    reject_secret_text,
    sorted_unique,
)
from .identity import CanonicalIdentityEnvelope, ImmutableSourceLocator

if TYPE_CHECKING:
    from .extraction import RequiredMemberManifestV1_1

NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(ge=1)]
RelationshipK = Annotated[int, Field(ge=0, le=4)]
ScopeMode = Literal[
    "exact_type",
    "descendants",
    "ancestors_context",
    "explicit_member_set",
]
ScopeChange = Literal["exact", "narrow", "expand"]
HierarchyExpansionPolicy = Literal[
    "none",
    "sealed_descendants",
    "sealed_ancestors_context",
    "explicit_members",
]
RetrievalMode = Literal[
    "agentic_preview",
    "agentic_stable_without_dynamic_filter",
    "direct_hybrid_prefilter",
]
CoverageStatus = Literal["complete", "partial", "invalid"]
RemediationClass = Literal[
    "retry_same_scope",
    "new_scope_required",
    "explicit_policy_required",
    "operator_repair_required",
    "separate_domain_review_required",
    "downstream_abstention_required",
]
FailureReasonCode = Literal[
    "scope_invalid",
    "scope_unauthorized",
    "scope_key_missing",
    "scope_key_ambiguous",
    "scope_key_collision",
    "scope_hash_stale",
    "unknown_concept",
    "unknown_type",
    "unknown_predicate",
    "unknown_relationship",
    "schema_mutation_forbidden",
    "domain_review_required",
    "projection_hash_stale",
    "crosswalk_hash_stale",
    "acl_hash_stale",
    "type_hierarchy_hash_stale",
    "type_assertion_version_stale",
    "publication_unasserted",
    "search_orphan_key",
    "filter_unsafe",
    "filter_would_broaden",
    "capability_unavailable",
    "fallback_not_declared",
    "graph_request_limit",
    "search_request_limit",
    "graph_k_limit",
    "retrieval_budget_exhausted",
    "output_truncated",
    "source_failure",
    "activity_missing",
    "reference_missing",
    "citation_invalid",
    "citation_unauthorized",
    "exact_quote_missing",
    "required_member_missing",
    "required_role_missing",
    "duplicate_member",
    "unexpected_member",
    "group_mismatch",
    "sequence_mismatch",
    "adjacency_mismatch",
    "cardinality_mismatch",
    "collection_hash_mismatch",
    "hierarchy_mode_invalid",
    "hierarchy_scope_mismatch",
    "hierarchy_expansion_nondeterministic",
    "name_match_forbidden",
    "hierarchy_k_confusion",
]
REQUIRED_MEMBER_MANIFEST_V1_1_SCHEMA_HASH = (
    "e33003e128746f09c77ba44b4b4802510eadbdf000eb60430f16a4d2654a3c4c"
)
_URL_ALLOWED_TEXT_FIELDS = frozenset(
    {
        "exact_authorized_quote",
        "original_document_name",
        "relationship_k_4_justification",
        "scope_decision_reason_code",
    }
)


def _sorted_set(value: object, *, field_name: str) -> object:
    if isinstance(value, (list, tuple)):
        return sorted_unique(value, field_name=field_name)
    return value


def _secret_free(
    value: str,
    *,
    field_name: str,
    allow_url: bool = False,
) -> str:
    reject_secret_text(value, field_name=field_name)
    parsed = urlparse(value)
    if not allow_url and ("://" in value or parsed.netloc):
        raise ValueError(f"{field_name} must be a canonical ID, not a URL")
    return value


def _reject_secrets_and_urls(
    value: Any,
    *,
    path: str = "contract",
    field_name: str | None = None,
) -> None:
    if isinstance(value, str):
        _secret_free(
            value,
            field_name=path,
            allow_url=field_name in _URL_ALLOWED_TEXT_FIELDS,
        )
        return
    if isinstance(value, ContractModel):
        for name, item in value.__dict__.items():
            if name != "immutable_locator":
                _reject_secrets_and_urls(
                    item,
                    path=f"{path}.{name}",
                    field_name=name,
                )
        return
    if isinstance(value, Mapping):
        for name, item in value.items():
            if name == "immutable_locator":
                continue
            _reject_secrets_and_urls(name, path=f"{path}.key")
            _reject_secrets_and_urls(
                item,
                path=f"{path}.{name}",
                field_name=name,
            )
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_secrets_and_urls(item, path=f"{path}[{index}]")


def _validate_identity(
    identity: CanonicalIdentityEnvelope,
    *,
    kind: str,
    semantic_contract_hash: str | None = None,
) -> None:
    if identity.contract_kind != kind:
        raise ValueError(f"identity contract_kind must be {kind}")
    if identity.contract_version != "1.0.0":
        raise ValueError("C0.Runtime contract version must be 1.0.0")
    if (
        semantic_contract_hash is not None
        and identity.semantic_contract_hash != semantic_contract_hash
    ):
        raise ValueError("semantic contract hash differs from identity authority")


def _validate_hash(model: ContractModel, hash_field: str) -> None:
    expected = canonical_sha256(model.model_dump(mode="json", exclude={hash_field}))
    if getattr(model, hash_field) != expected:
        raise ValueError(f"{hash_field} does not match canonical contract content")


def _validate_citation_identity(
    identity: CanonicalIdentityEnvelope,
    *,
    source_file_id: str,
    source_unit_id: str,
    content_hash: str,
    immutable_locator: ImmutableSourceLocator,
    page: int | None,
    section_path: tuple[str, ...],
) -> None:
    checks = (
        ("source file ID", identity.source_file_id, source_file_id),
        ("source unit ID", identity.source_unit_id, source_unit_id),
        ("content hash", identity.content_hash, content_hash),
        ("immutable locator", identity.immutable_locator, immutable_locator),
        ("locator page", immutable_locator.page, page),
        ("locator section", immutable_locator.section_path, section_path),
    )
    for name, identity_value, citation_value in checks:
        if identity_value != citation_value:
            raise ValueError(f"citation {name} differs from canonical identity")


def _validate_hierarchy_configuration(
    *,
    mode: ScopeMode,
    policy: HierarchyExpansionPolicy,
    depth: int,
) -> None:
    expected_policy = {
        "exact_type": "none",
        "descendants": "sealed_descendants",
        "ancestors_context": "sealed_ancestors_context",
        "explicit_member_set": "explicit_members",
    }[mode]
    if policy != expected_policy:
        raise ValueError("hierarchy mode and expansion policy disagree")
    _validate_hierarchy_policy_depth(policy=policy, depth=depth)


def _validate_hierarchy_policy_depth(
    *,
    policy: HierarchyExpansionPolicy,
    depth: int,
) -> None:
    if policy in {"none", "explicit_members"} and depth != 0:
        raise ValueError("non-expanding hierarchy policy requires depth zero")
    if policy in {"sealed_descendants", "sealed_ancestors_context"} and depth == 0:
        raise ValueError("expanding hierarchy policy requires positive depth")


def _validate_scope_type_sets(
    *,
    mode: ScopeMode,
    requested_root_type_ids: tuple[str, ...],
    exact_type_ids: tuple[str, ...],
    ancestor_type_ids: tuple[str, ...],
    descendant_type_ids: tuple[str, ...],
) -> None:
    requested = set(requested_root_type_ids)
    exact = set(exact_type_ids)
    if not exact:
        raise ValueError("resolved scope requires exact semantic type IDs")
    if mode == "exact_type" and (
        exact != requested or ancestor_type_ids or descendant_type_ids
    ):
        raise ValueError("exact_type scope must contain exact assertions only")
    if mode == "descendants" and (
        not requested <= exact
        or not descendant_type_ids
        or ancestor_type_ids
        or not set(descendant_type_ids) <= exact
    ):
        raise ValueError(
            "descendant expansion has inconsistent resolved type sets"
        )
    if mode == "ancestors_context" and (
        not requested <= exact
        or not ancestor_type_ids
        or descendant_type_ids
        or not set(ancestor_type_ids) <= exact
    ):
        raise ValueError(
            "ancestors_context has inconsistent resolved type sets"
        )
    if mode == "explicit_member_set" and (
        requested_root_type_ids or ancestor_type_ids or descendant_type_ids
    ):
        raise ValueError("explicit_member_set cannot declare hierarchy expansion")


class RequiredMemberManifestReference(ContractModel):
    """Exact pointer to the sole L3 completeness authority."""

    required_member_manifest_id: RequiredText
    contract_kind: Literal["c0.required_member_manifest"] = (
        "c0.required_member_manifest"
    )
    contract_version: Literal["1.1.0"] = "1.1.0"
    schema_hash: Literal[
        "e33003e128746f09c77ba44b4b4802510eadbdf000eb60430f16a4d2654a3c4c"
    ] = REQUIRED_MEMBER_MANIFEST_V1_1_SCHEMA_HASH
    manifest_hash: Sha256
    authoritative_collection_hash: Sha256

    def validate_manifest(self, manifest: "RequiredMemberManifestV1_1") -> None:
        checks = (
            (
                "required member manifest ID",
                self.required_member_manifest_id,
                manifest.required_member_manifest_id,
            ),
            ("manifest hash", self.manifest_hash, manifest.manifest_hash),
            (
                "authoritative collection hash",
                self.authoritative_collection_hash,
                manifest.authoritative_collection_hash,
            ),
        )
        for name, referenced, authoritative in checks:
            if referenced != authoritative:
                raise ValueError(f"{name} differs from RequiredMemberManifest@1.1.0")


class AuthoritativeReceiptReference(ContractModel):
    receipt_id: RequiredText
    receipt_hash: Sha256


class TypeAssertionReference(ContractModel):
    canonical_entity_id: RequiredText
    canonical_semantic_type_id: RequiredText
    type_assertion_id: RequiredText
    type_assertion_version: PositiveInt
    type_assertion_hash: Sha256


class ScopeMemberReference(ContractModel):
    canonical_entity_id: RequiredText
    canonical_semantic_type_id: RequiredText
    type_assertion_id: RequiredText
    type_assertion_version: PositiveInt
    member_role_id: RequiredText | None = None
    membership_assertion_ids: tuple[str, ...]
    evidence_span_ids: tuple[str, ...]
    group_id: RequiredText | None = None
    sequence_position: NonNegativeInt | None = None
    member_hash: Sha256

    @field_validator("membership_assertion_ids", "evidence_span_ids", mode="before")
    @classmethod
    def _sets(cls, value: object, info: Any) -> object:
        return _sorted_set(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _invariants(self) -> "ScopeMemberReference":
        _reject_secrets_and_urls(self)
        if not self.membership_assertion_ids:
            raise ValueError("scope member requires a membership assertion")
        if not self.evidence_span_ids:
            raise ValueError("scope member requires exact evidence")
        _validate_hash(self, "member_hash")
        return self


class ScopeExpansionStep(ContractModel):
    ordinal: NonNegativeInt
    from_semantic_type_id: RequiredText
    to_semantic_type_id: RequiredText
    edge_kind: Literal["self", "child", "ancestor"]
    hierarchy_edge_assertion_id: RequiredText
    hierarchy_edge_hash: Sha256


class AdjacencyEdge(ContractModel):
    from_canonical_entity_id: RequiredText
    to_canonical_entity_id: RequiredText
    relationship_semantic_id: RequiredText
    relationship_assertion_id: RequiredText
    evidence_span_ids: tuple[str, ...]

    @field_validator("evidence_span_ids", mode="before")
    @classmethod
    def _evidence(cls, value: object) -> object:
        return _sorted_set(value, field_name="evidence_span_ids")


class RuntimeCollectionPolicy(ContractModel):
    ordering_mode: Literal["unordered", "ordered"]
    expected_cardinality: NonNegativeInt | None = None
    minimum_cardinality: NonNegativeInt | None = None
    maximum_cardinality: NonNegativeInt | None = None
    required_unique_member_count: NonNegativeInt | None = None
    required_role_ids: tuple[str, ...] = ()
    completeness_rule_ids: tuple[str, ...]
    cardinality_rule_ids: tuple[str, ...] = ()
    policy_hash: Sha256

    @field_validator(
        "required_role_ids",
        "completeness_rule_ids",
        "cardinality_rule_ids",
        mode="before",
    )
    @classmethod
    def _sets(cls, value: object, info: Any) -> object:
        return _sorted_set(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _invariants(self) -> "RuntimeCollectionPolicy":
        if not self.completeness_rule_ids:
            raise ValueError("collection policy must reference completeness authority")
        if (
            self.minimum_cardinality is not None
            and self.maximum_cardinality is not None
            and self.minimum_cardinality > self.maximum_cardinality
        ):
            raise ValueError("minimum cardinality cannot exceed maximum cardinality")
        if self.expected_cardinality is not None and (
            (
                self.minimum_cardinality is not None
                and self.expected_cardinality < self.minimum_cardinality
            )
            or (
                self.maximum_cardinality is not None
                and self.expected_cardinality > self.maximum_cardinality
            )
        ):
            raise ValueError("expected cardinality must be inside min/max bounds")
        _validate_hash(self, "policy_hash")
        return self


class SafeCanonicalFilterSpec(ContractModel):
    canonical_entity_ids: tuple[str, ...]
    exact_type_ids: tuple[str, ...] = ()
    ancestor_type_ids: tuple[str, ...] = ()
    canonical_relationship_ids: tuple[str, ...] = ()
    asserted_publication_only: Literal[True] = True
    filter_hash: Sha256

    @field_validator(
        "canonical_entity_ids",
        "exact_type_ids",
        "ancestor_type_ids",
        "canonical_relationship_ids",
        mode="before",
    )
    @classmethod
    def _sets(cls, value: object, info: Any) -> object:
        return _sorted_set(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _invariants(self) -> "SafeCanonicalFilterSpec":
        _reject_secrets_and_urls(self)
        if not self.canonical_entity_ids:
            raise ValueError("canonical filter requires canonical entity IDs")
        _validate_hash(self, "filter_hash")
        return self


class QueryBudget(ContractModel):
    """Agent-selected request ceilings; never retry or performance policy."""

    identity: CanonicalIdentityEnvelope
    query_budget_id: RequiredText
    max_ontology_graph_scope_requests: Annotated[int, Field(ge=0, le=1)]
    relationship_k: RelationshipK
    relationship_k_4_justification: RequiredText | None = None
    hierarchy_expansion_policy: HierarchyExpansionPolicy
    hierarchy_expansion_depth: NonNegativeInt
    retrieval_mode: RetrievalMode
    max_agentic_retrieval_invocations: Annotated[int, Field(ge=0, le=1)]
    max_agentic_internal_subqueries: NonNegativeInt
    max_agentic_source_calls: NonNegativeInt
    max_direct_search_requests: Annotated[int, Field(ge=0, le=1)]
    max_output_documents: PositiveInt
    max_output_tokens: PositiveInt
    max_output_bytes: PositiveInt
    max_runtime_milliseconds: PositiveInt
    max_graph_result_records: PositiveInt
    max_search_result_records: PositiveInt
    budget_hash: Sha256

    @model_validator(mode="after")
    def _invariants(self) -> "QueryBudget":
        _validate_identity(self.identity, kind="c0.query_budget")
        _reject_secrets_and_urls(self)
        if self.relationship_k == 4 and self.relationship_k_4_justification is None:
            raise ValueError("relationship K=4 requires reviewed justification")
        if self.relationship_k != 4 and self.relationship_k_4_justification is not None:
            raise ValueError("relationship K justification is permitted only for K=4")
        _validate_hierarchy_policy_depth(
            policy=self.hierarchy_expansion_policy,
            depth=self.hierarchy_expansion_depth,
        )
        if self.retrieval_mode.startswith("agentic_"):
            if (
                self.max_agentic_retrieval_invocations != 1
                or self.max_direct_search_requests != 0
            ):
                raise ValueError(
                    "agentic mode requires one agentic invocation and zero direct requests"
                )
        elif (
            self.max_agentic_retrieval_invocations != 0
            or self.max_direct_search_requests != 1
        ):
            raise ValueError(
                "direct mode requires zero agentic invocations and one direct request"
            )
        _validate_hash(self, "budget_hash")
        return self


class OntologyScopeEnvelope(ContractModel):
    """Agent-requested canonical scope; names and query text have no authority."""

    identity: CanonicalIdentityEnvelope
    ontology_scope_envelope_id: RequiredText
    parent_scope_id: RequiredText | None = None
    parent_scope_hash: Sha256 | None = None
    relative_change: ScopeChange
    hierarchy_scope_mode: ScopeMode
    canonical_root_semantic_type_ids: tuple[str, ...] = ()
    explicit_canonical_entity_ids: tuple[str, ...] = ()
    hierarchy_expansion_policy: HierarchyExpansionPolicy
    hierarchy_expansion_depth: NonNegativeInt
    aggregate_canonical_entity_id: RequiredText | None = None
    aggregate_semantic_type_id: RequiredText | None = None
    requested_member_semantic_type_ids: tuple[str, ...] = ()
    membership_relationship_semantic_id: RequiredText | None = None
    requested_member_role_ids: tuple[str, ...] = ()
    required_role_ids: tuple[str, ...] = ()
    approved_graph_path_ids: tuple[str, ...] = ()
    include_canonical_ids: tuple[str, ...] = ()
    exclude_canonical_ids: tuple[str, ...] = ()
    relationship_k: RelationshipK
    relationship_k_4_justification: RequiredText | None = None
    required_member_manifest: RequiredMemberManifestReference
    project_scope_id: RequiredText
    acl_scope_hash: Sha256
    asserted_publication_hash: Sha256
    semantic_contract_hash: Sha256
    type_hierarchy_id: RequiredText
    type_hierarchy_version: SemVer
    type_hierarchy_hash: Sha256
    type_closure_hash: Sha256
    semantic_projection_hash: Sha256
    graph_model_hash: Sha256
    search_index_fingerprint: Sha256
    publication_crosswalk_hash: Sha256
    agent_policy_id: RequiredText
    agent_policy_hash: Sha256
    scope_decision_reason_code: RequiredText
    scope_hash: Sha256

    @field_validator(
        "canonical_root_semantic_type_ids",
        "explicit_canonical_entity_ids",
        "requested_member_semantic_type_ids",
        "requested_member_role_ids",
        "required_role_ids",
        "approved_graph_path_ids",
        "include_canonical_ids",
        "exclude_canonical_ids",
        mode="before",
    )
    @classmethod
    def _sets(cls, value: object, info: Any) -> object:
        return _sorted_set(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _invariants(self) -> "OntologyScopeEnvelope":
        _validate_identity(
            self.identity,
            kind="c0.ontology_scope_envelope",
            semantic_contract_hash=self.semantic_contract_hash,
        )
        _reject_secrets_and_urls(self)
        if (self.parent_scope_id is None) != (self.parent_scope_hash is None):
            raise ValueError("parent scope ID and hash must be present together")
        if self.relative_change == "exact" and self.parent_scope_id is not None:
            raise ValueError("exact root scope cannot reference a parent")
        if self.relative_change != "exact" and self.parent_scope_id is None:
            raise ValueError("narrow and expand scopes require parent identity")
        if set(self.include_canonical_ids).intersection(self.exclude_canonical_ids):
            raise ValueError("include and exclude canonical IDs must be disjoint")
        if self.relationship_k == 4 and self.relationship_k_4_justification is None:
            raise ValueError("relationship K=4 requires reviewed justification")
        if self.relationship_k != 4 and self.relationship_k_4_justification is not None:
            raise ValueError("relationship K justification is permitted only for K=4")
        mode_requirements = {
            "exact_type": (True, False),
            "descendants": (True, False),
            "ancestors_context": (True, False),
            "explicit_member_set": (False, True),
        }
        needs_roots, needs_members = mode_requirements[self.hierarchy_scope_mode]
        if needs_roots != bool(self.canonical_root_semantic_type_ids):
            raise ValueError("hierarchy mode has invalid canonical root type IDs")
        if needs_members != bool(self.explicit_canonical_entity_ids):
            raise ValueError("hierarchy mode has invalid explicit member IDs")
        _validate_hierarchy_configuration(
            mode=self.hierarchy_scope_mode,
            policy=self.hierarchy_expansion_policy,
            depth=self.hierarchy_expansion_depth,
        )
        selected_ids = set(self.explicit_canonical_entity_ids).union(
            self.include_canonical_ids
        )
        if selected_ids.intersection(self.exclude_canonical_ids):
            raise ValueError("excluded canonical IDs cannot remain selected")
        _validate_hash(self, "scope_hash")
        return self

    def validate_relative_to(self, parent: "OntologyScopeEnvelope") -> None:
        if self.parent_scope_id != parent.ontology_scope_envelope_id:
            raise ValueError("parent scope ID mismatch")
        if self.parent_scope_hash != parent.scope_hash:
            raise ValueError("parent scope hash mismatch")
        invariant_fields = (
            "hierarchy_scope_mode",
            "hierarchy_expansion_policy",
            "aggregate_canonical_entity_id",
            "aggregate_semantic_type_id",
            "membership_relationship_semantic_id",
            "required_role_ids",
            "required_member_manifest",
            "project_scope_id",
            "acl_scope_hash",
            "asserted_publication_hash",
            "semantic_contract_hash",
            "type_hierarchy_id",
            "type_hierarchy_version",
            "type_hierarchy_hash",
            "type_closure_hash",
            "semantic_projection_hash",
            "graph_model_hash",
            "search_index_fingerprint",
            "publication_crosswalk_hash",
            "agent_policy_id",
            "agent_policy_hash",
        )
        if any(getattr(self, field) != getattr(parent, field) for field in invariant_fields):
            raise ValueError("relative scope cannot replace sealed authority or scope semantics")
        if (
            self.identity.project_id != parent.identity.project_id
            or self.identity.domain_contract_hash
            != parent.identity.domain_contract_hash
            or self.identity.canonical_schema_version
            != parent.identity.canonical_schema_version
        ):
            raise ValueError("relative scope identity authority differs from parent")

        set_fields = (
            "canonical_root_semantic_type_ids",
            "explicit_canonical_entity_ids",
            "requested_member_semantic_type_ids",
            "requested_member_role_ids",
            "approved_graph_path_ids",
            "include_canonical_ids",
        )
        if self.relative_change == "narrow":
            if any(
                not set(getattr(self, field)) <= set(getattr(parent, field))
                for field in set_fields
            ):
                raise ValueError("narrow scope cannot add canonical authority dimensions")
            if not set(parent.exclude_canonical_ids) <= set(
                self.exclude_canonical_ids
            ):
                raise ValueError("narrow scope cannot remove canonical exclusions")
            if (
                self.relationship_k > parent.relationship_k
                or self.hierarchy_expansion_depth
                > parent.hierarchy_expansion_depth
            ):
                raise ValueError("narrow scope cannot increase traversal bounds")
        elif self.relative_change == "expand":
            if any(
                not set(getattr(parent, field)) <= set(getattr(self, field))
                for field in set_fields
            ):
                raise ValueError("expanded scope cannot remove canonical authority dimensions")
            if not set(self.exclude_canonical_ids) <= set(
                parent.exclude_canonical_ids
            ):
                raise ValueError("expanded scope cannot add canonical exclusions")
            if (
                self.relationship_k < parent.relationship_k
                or self.hierarchy_expansion_depth
                < parent.hierarchy_expansion_depth
            ):
                raise ValueError("expanded scope cannot reduce traversal bounds")


class ResolvedOntologyScope(ContractModel):
    """Structured authoritative resolver output over sealed canonical keys."""

    identity: CanonicalIdentityEnvelope
    resolved_ontology_scope_id: RequiredText
    resolver_request_id: RequiredText
    resolver_request_hash: Sha256
    ontology_scope_envelope_id: RequiredText
    ontology_scope_envelope_hash: Sha256
    canonical_scope_id: RequiredText
    aggregate_canonical_entity_id: RequiredText
    aggregate_semantic_type_id: RequiredText
    collection_canonical_id: RequiredText
    membership_relationship_semantic_id: RequiredText
    hierarchy_scope_mode: ScopeMode
    requested_root_semantic_type_ids: tuple[str, ...] = ()
    resolved_exact_type_ids: tuple[str, ...]
    resolved_ancestor_type_ids: tuple[str, ...] = ()
    resolved_descendant_type_ids: tuple[str, ...] = ()
    expansion_trace: tuple[ScopeExpansionStep, ...] = ()
    type_hierarchy_id: RequiredText
    type_hierarchy_version: SemVer
    type_hierarchy_hash: Sha256
    type_closure_hash: Sha256
    hierarchy_expansion_policy: HierarchyExpansionPolicy
    hierarchy_expansion_depth: NonNegativeInt
    members: tuple[ScopeMemberReference, ...]
    type_assertions: tuple[TypeAssertionReference, ...]
    relationship_semantic_ids: tuple[str, ...]
    assertion_ids: tuple[str, ...]
    evidence_span_ids: tuple[str, ...]
    included_canonical_ids: tuple[str, ...]
    excluded_canonical_ids: tuple[str, ...] = ()
    adjacency_edges: tuple[AdjacencyEdge, ...] = ()
    collection_policy: RuntimeCollectionPolicy
    required_member_manifest: RequiredMemberManifestReference
    relationship_traversal_policy_id: RequiredText
    relationship_traversal_policy_hash: Sha256
    relationship_k: RelationshipK
    relationship_k_4_justification: RequiredText | None = None
    serving_projection_hash: Sha256
    publication_crosswalk_hash: Sha256
    graph_model_hash: Sha256
    search_index_fingerprint: Sha256
    asserted_publication_hash: Sha256
    acl_scope_hash: Sha256
    resolver_capability_id: RequiredText
    resolver_version: SemVer
    authoritative_receipts: tuple[AuthoritativeReceiptReference, ...]
    canonical_key_set_hash: Sha256
    resolved_scope_hash: Sha256

    @field_validator(
        "requested_root_semantic_type_ids",
        "resolved_exact_type_ids",
        "resolved_ancestor_type_ids",
        "resolved_descendant_type_ids",
        "relationship_semantic_ids",
        "assertion_ids",
        "evidence_span_ids",
        "included_canonical_ids",
        "excluded_canonical_ids",
        mode="before",
    )
    @classmethod
    def _sets(cls, value: object, info: Any) -> object:
        return _sorted_set(value, field_name=info.field_name)

    @field_validator("members", mode="before")
    @classmethod
    def _members(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return tuple(
                sorted(
                    value,
                    key=lambda item: (
                        item.canonical_entity_id
                        if isinstance(item, ScopeMemberReference)
                        else str(item.get("canonical_entity_id", ""))
                    ),
                )
            )
        return value

    @field_validator("type_assertions", mode="before")
    @classmethod
    def _assertions(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return tuple(
                sorted(
                    value,
                    key=lambda item: (
                        item.canonical_entity_id
                        if isinstance(item, TypeAssertionReference)
                        else str(item.get("canonical_entity_id", ""))
                    ),
                )
            )
        return value

    @field_validator("authoritative_receipts", mode="before")
    @classmethod
    def _receipts(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return tuple(
                sorted(
                    value,
                    key=lambda item: (
                        item.receipt_id
                        if isinstance(item, AuthoritativeReceiptReference)
                        else str(item.get("receipt_id", ""))
                    ),
                )
            )
        return value

    @model_validator(mode="after")
    def _invariants(self) -> "ResolvedOntologyScope":
        _validate_identity(self.identity, kind="c0.resolved_ontology_scope")
        _reject_secrets_and_urls(self)
        member_ids = [item.canonical_entity_id for item in self.members]
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("resolved scope member IDs must be unique")
        if set(self.included_canonical_ids) != set(member_ids):
            raise ValueError("included canonical IDs must equal resolved members")
        if set(member_ids).intersection(self.excluded_canonical_ids):
            raise ValueError("excluded canonical IDs cannot remain resolved members")
        assertion_by_entity = {
            item.canonical_entity_id: item for item in self.type_assertions
        }
        if set(assertion_by_entity) != set(member_ids):
            raise ValueError("every member requires exactly one current type assertion")
        for member in self.members:
            assertion = assertion_by_entity[member.canonical_entity_id]
            if (
                member.canonical_semantic_type_id
                != assertion.canonical_semantic_type_id
                or member.type_assertion_id != assertion.type_assertion_id
                or member.type_assertion_version != assertion.type_assertion_version
            ):
                raise ValueError("member and type assertion identity disagree")
        if self.membership_relationship_semantic_id not in self.relationship_semantic_ids:
            raise ValueError("membership relationship is absent from resolved relationships")
        if self.relationship_k == 4 and self.relationship_k_4_justification is None:
            raise ValueError("relationship K=4 requires reviewed justification")
        if self.relationship_k != 4 and self.relationship_k_4_justification is not None:
            raise ValueError("relationship K justification is permitted only for K=4")
        receipt_ids = [item.receipt_id for item in self.authoritative_receipts]
        if not receipt_ids or len(receipt_ids) != len(set(receipt_ids)):
            raise ValueError("authoritative receipt references must be nonempty and unique")
        if not set(
            assertion_id
            for member in self.members
            for assertion_id in member.membership_assertion_ids
        ).issubset(self.assertion_ids):
            raise ValueError("member assertion IDs are absent from scope assertions")
        if not set(
            evidence_id
            for member in self.members
            for evidence_id in member.evidence_span_ids
        ).issubset(self.evidence_span_ids):
            raise ValueError("member evidence IDs are absent from scope evidence")
        exact_types = set(self.resolved_exact_type_ids)
        member_types = {item.canonical_semantic_type_id for item in self.members}
        if not member_types <= exact_types:
            raise ValueError("member type is absent from resolved exact types")
        _validate_scope_type_sets(
            mode=self.hierarchy_scope_mode,
            requested_root_type_ids=self.requested_root_semantic_type_ids,
            exact_type_ids=self.resolved_exact_type_ids,
            ancestor_type_ids=self.resolved_ancestor_type_ids,
            descendant_type_ids=self.resolved_descendant_type_ids,
        )
        _validate_hierarchy_configuration(
            mode=self.hierarchy_scope_mode,
            policy=self.hierarchy_expansion_policy,
            depth=self.hierarchy_expansion_depth,
        )
        if [step.ordinal for step in self.expansion_trace] != list(
            range(len(self.expansion_trace))
        ):
            raise ValueError("expansion trace ordinals must be contiguous")
        self._validate_expansion_trace()
        if self.collection_policy.required_unique_member_count is not None and (
            len(member_ids) != self.collection_policy.required_unique_member_count
        ):
            raise ValueError("resolved unique member count differs from authority")
        if self.collection_policy.expected_cardinality is not None and (
            len(member_ids) != self.collection_policy.expected_cardinality
        ):
            raise ValueError("resolved member count differs from expected cardinality")
        if self.collection_policy.minimum_cardinality is not None and (
            len(member_ids) < self.collection_policy.minimum_cardinality
        ):
            raise ValueError("resolved member count is below minimum cardinality")
        if self.collection_policy.maximum_cardinality is not None and (
            len(member_ids) > self.collection_policy.maximum_cardinality
        ):
            raise ValueError("resolved member count exceeds maximum cardinality")
        expected_key_set_hash = canonical_sha256(
            {
                "aggregate_canonical_entity_id": self.aggregate_canonical_entity_id,
                "collection_canonical_id": self.collection_canonical_id,
                "membership_relationship_semantic_id": (
                    self.membership_relationship_semantic_id
                ),
                "members": [
                    {
                        "canonical_entity_id": member.canonical_entity_id,
                        "canonical_semantic_type_id": (
                            member.canonical_semantic_type_id
                        ),
                        "type_assertion_id": member.type_assertion_id,
                        "type_assertion_version": member.type_assertion_version,
                        "member_role_id": member.member_role_id,
                        "membership_assertion_ids": list(
                            member.membership_assertion_ids
                        ),
                        "evidence_span_ids": list(member.evidence_span_ids),
                    }
                    for member in self.members
                ],
            }
        )
        if self.canonical_key_set_hash != expected_key_set_hash:
            raise ValueError("canonical_key_set_hash does not match resolved keys")
        _validate_hash(self, "resolved_scope_hash")
        return self

    def _validate_expansion_trace(self) -> None:
        if self.hierarchy_scope_mode in {"exact_type", "explicit_member_set"}:
            if self.expansion_trace:
                raise ValueError("non-expanding scope cannot carry expansion trace")
            return
        if not self.expansion_trace:
            raise ValueError("expanding scope requires deterministic expansion trace")
        edge_kind = (
            "child"
            if self.hierarchy_scope_mode == "descendants"
            else "ancestor"
        )
        reachable = set(self.requested_root_semantic_type_ids)
        depths = {type_id: 0 for type_id in reachable}
        traversed: set[str] = set()
        for step in self.expansion_trace:
            if step.edge_kind != edge_kind:
                raise ValueError("expansion trace edge kind disagrees with scope mode")
            if step.from_semantic_type_id not in reachable:
                raise ValueError("expansion trace contains an unreachable source type")
            target_depth = depths[step.from_semantic_type_id] + 1
            if target_depth > self.hierarchy_expansion_depth:
                raise ValueError("expansion trace exceeds hierarchy expansion depth")
            prior_depth = depths.get(step.to_semantic_type_id)
            if prior_depth is None or target_depth < prior_depth:
                depths[step.to_semantic_type_id] = target_depth
            reachable.add(step.to_semantic_type_id)
            traversed.add(step.to_semantic_type_id)
        expected = (
            set(self.resolved_descendant_type_ids)
            if self.hierarchy_scope_mode == "descendants"
            else set(self.resolved_ancestor_type_ids)
        )
        if traversed != expected:
            raise ValueError("expansion trace does not equal resolved expanded type set")

    def validate_required_member_manifest(
        self,
        manifest: "RequiredMemberManifestV1_1",
    ) -> None:
        self.required_member_manifest.validate_manifest(manifest)
        manifest_members = {
            item.member_canonical_id: item for item in manifest.members
        }
        resolved_members = {
            item.canonical_entity_id: item for item in self.members
        }
        checks = (
            ("scope canonical ID", self.canonical_scope_id, manifest.scope_canonical_id),
            (
                "membership relationship",
                self.membership_relationship_semantic_id,
                manifest.membership_semantic_relationship_id,
            ),
            (
                "ordering mode",
                self.collection_policy.ordering_mode,
                manifest.ordering_policy.mode,
            ),
            (
                "expected cardinality",
                self.collection_policy.expected_cardinality,
                manifest.expected_cardinality,
            ),
            (
                "minimum cardinality",
                self.collection_policy.minimum_cardinality,
                manifest.minimum_cardinality,
            ),
            (
                "maximum cardinality",
                self.collection_policy.maximum_cardinality,
                manifest.maximum_cardinality,
            ),
            (
                "required role IDs",
                self.collection_policy.required_role_ids,
                manifest.required_role_ids,
            ),
            (
                "required unique member count",
                self.collection_policy.required_unique_member_count,
                len(manifest.members),
            ),
            (
                "completeness authority",
                self.collection_policy.completeness_rule_ids,
                (manifest.required_member_manifest_id,),
            ),
            (
                "canonical member IDs",
                tuple(sorted(resolved_members)),
                tuple(sorted(manifest_members)),
            ),
        )
        for name, resolved, authoritative in checks:
            if resolved != authoritative:
                raise ValueError(
                    f"{name} differs from RequiredMemberManifest@1.1.0"
                )
        for member_id, resolved in resolved_members.items():
            authoritative = manifest_members[member_id]
            member_checks = (
                (
                    "semantic type",
                    resolved.canonical_semantic_type_id,
                    authoritative.member_semantic_type_id,
                ),
                ("member role", resolved.member_role_id, authoritative.member_role_id),
                (
                    "member order",
                    resolved.sequence_position,
                    authoritative.member_order,
                ),
                (
                    "supporting evidence",
                    resolved.evidence_span_ids,
                    authoritative.supporting_evidence_span_ids,
                ),
            )
            for name, resolved_value, authoritative_value in member_checks:
                if resolved_value != authoritative_value:
                    raise ValueError(
                        f"member {name} differs from RequiredMemberManifest@1.1.0"
                    )

    def validate_envelope(self, envelope: "OntologyScopeEnvelope") -> None:
        checks = (
            (
                "scope envelope ID",
                self.ontology_scope_envelope_id,
                envelope.ontology_scope_envelope_id,
            ),
            (
                "scope envelope hash",
                self.ontology_scope_envelope_hash,
                envelope.scope_hash,
            ),
            ("hierarchy mode", self.hierarchy_scope_mode, envelope.hierarchy_scope_mode),
            (
                "requested root types",
                self.requested_root_semantic_type_ids,
                envelope.canonical_root_semantic_type_ids,
            ),
            ("hierarchy ID", self.type_hierarchy_id, envelope.type_hierarchy_id),
            (
                "hierarchy version",
                self.type_hierarchy_version,
                envelope.type_hierarchy_version,
            ),
            ("hierarchy hash", self.type_hierarchy_hash, envelope.type_hierarchy_hash),
            ("closure hash", self.type_closure_hash, envelope.type_closure_hash),
            (
                "hierarchy policy",
                self.hierarchy_expansion_policy,
                envelope.hierarchy_expansion_policy,
            ),
            (
                "hierarchy depth",
                self.hierarchy_expansion_depth,
                envelope.hierarchy_expansion_depth,
            ),
            ("relationship K", self.relationship_k, envelope.relationship_k),
            (
                "relationship K justification",
                self.relationship_k_4_justification,
                envelope.relationship_k_4_justification,
            ),
            (
                "required member manifest",
                self.required_member_manifest,
                envelope.required_member_manifest,
            ),
            (
                "membership relationship",
                self.membership_relationship_semantic_id,
                envelope.membership_relationship_semantic_id,
            ),
            ("ACL hash", self.acl_scope_hash, envelope.acl_scope_hash),
            (
                "asserted publication hash",
                self.asserted_publication_hash,
                envelope.asserted_publication_hash,
            ),
            (
                "semantic projection hash",
                self.serving_projection_hash,
                envelope.semantic_projection_hash,
            ),
            (
                "crosswalk hash",
                self.publication_crosswalk_hash,
                envelope.publication_crosswalk_hash,
            ),
            ("Graph model hash", self.graph_model_hash, envelope.graph_model_hash),
            (
                "Search index fingerprint",
                self.search_index_fingerprint,
                envelope.search_index_fingerprint,
            ),
        )
        for name, resolved, requested in checks:
            if resolved != requested:
                raise ValueError(f"{name} differs from scope envelope")
        if (
            envelope.aggregate_canonical_entity_id is not None
            and self.aggregate_canonical_entity_id
            != envelope.aggregate_canonical_entity_id
        ):
            raise ValueError("aggregate canonical ID differs from scope envelope")
        if (
            envelope.aggregate_semantic_type_id is not None
            and self.aggregate_semantic_type_id
            != envelope.aggregate_semantic_type_id
        ):
            raise ValueError("aggregate semantic type differs from scope envelope")
        if set(self.collection_policy.required_role_ids) != set(
            envelope.required_role_ids
        ):
            raise ValueError("required roles differ from scope envelope")
        if (
            self.identity.semantic_contract_hash
            != envelope.semantic_contract_hash
        ):
            raise ValueError("semantic contract hash differs from scope envelope")
        if envelope.hierarchy_scope_mode == "explicit_member_set" and set(
            self.included_canonical_ids
        ) != set(envelope.explicit_canonical_entity_ids):
            raise ValueError("explicit members differ from scope envelope")


class ScopeValidationFinding(ContractModel):
    reason_code: FailureReasonCode
    remediation: RemediationClass
    canonical_id: RequiredText | None = None
    authority_hash: Sha256 | None = None


class ResolvedRetrievalScope(ContractModel):
    """fabric-kg validation result without transport or retrieval behavior."""

    identity: CanonicalIdentityEnvelope
    resolved_retrieval_scope_id: RequiredText
    ontology_scope_envelope_id: RequiredText
    ontology_scope_envelope_hash: Sha256
    resolved_ontology_scope_id: RequiredText
    resolved_ontology_scope_hash: Sha256
    resolution_status: Literal["valid", "invalid"]
    findings: tuple[ScopeValidationFinding, ...] = ()
    canonical_scope_id: RequiredText
    aggregate_canonical_entity_id: RequiredText
    aggregate_semantic_type_id: RequiredText
    collection_canonical_id: RequiredText
    membership_relationship_semantic_id: RequiredText
    canonical_member_ids: tuple[str, ...]
    canonical_key_set_hash: Sha256
    hierarchy_scope_mode: ScopeMode
    requested_root_semantic_type_ids: tuple[str, ...] = ()
    resolved_exact_type_ids: tuple[str, ...]
    resolved_ancestor_type_ids: tuple[str, ...] = ()
    resolved_descendant_type_ids: tuple[str, ...] = ()
    expansion_trace_hash: Sha256
    type_hierarchy_version: SemVer
    type_hierarchy_hash: Sha256
    type_closure_hash: Sha256
    hierarchy_expansion_policy: HierarchyExpansionPolicy
    hierarchy_expansion_depth: NonNegativeInt
    relationship_k: RelationshipK
    relationship_k_4_justification: RequiredText | None = None
    type_assertion_set_hash: Sha256
    member_type_role_set_hash: Sha256
    required_role_ids: tuple[str, ...] = ()
    group_membership_hash: Sha256 | None = None
    sequence_hash: Sha256 | None = None
    adjacency_hash: Sha256 | None = None
    collection_policy_hash: Sha256
    required_member_manifest: RequiredMemberManifestReference
    include_canonical_ids: tuple[str, ...] = ()
    exclude_canonical_ids: tuple[str, ...] = ()
    acl_scope_hash: Sha256
    asserted_publication_hash: Sha256
    semantic_projection_hash: Sha256
    publication_crosswalk_hash: Sha256
    graph_model_hash: Sha256
    search_index_fingerprint: Sha256
    graph_scope_filter: SafeCanonicalFilterSpec
    collection_hash: Sha256
    parent_scope_change: ScopeChange
    retrieval_scope_hash: Sha256

    @field_validator(
        "canonical_member_ids",
        "requested_root_semantic_type_ids",
        "resolved_exact_type_ids",
        "resolved_ancestor_type_ids",
        "resolved_descendant_type_ids",
        "required_role_ids",
        "include_canonical_ids",
        "exclude_canonical_ids",
        mode="before",
    )
    @classmethod
    def _sets(cls, value: object, info: Any) -> object:
        return _sorted_set(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _invariants(self) -> "ResolvedRetrievalScope":
        _validate_identity(self.identity, kind="c0.resolved_retrieval_scope")
        _reject_secrets_and_urls(self)
        if self.resolution_status == "valid" and self.findings:
            raise ValueError("valid scope cannot contain validation findings")
        if self.resolution_status == "invalid" and not self.findings:
            raise ValueError("invalid scope requires validation findings")
        if set(self.include_canonical_ids).intersection(self.exclude_canonical_ids):
            raise ValueError("include and exclude canonical IDs must be disjoint")
        if set(self.canonical_member_ids).intersection(self.exclude_canonical_ids):
            raise ValueError("excluded canonical IDs cannot remain validated members")
        if tuple(self.graph_scope_filter.canonical_entity_ids) != tuple(
            self.canonical_member_ids
        ):
            raise ValueError("Graph filter canonical IDs must equal validated members")
        if tuple(self.graph_scope_filter.exact_type_ids) != tuple(
            self.resolved_exact_type_ids
        ):
            raise ValueError("Graph filter exact types must equal resolved exact types")
        if tuple(self.graph_scope_filter.ancestor_type_ids) != tuple(
            self.resolved_ancestor_type_ids
        ):
            raise ValueError(
                "Graph filter ancestor types must equal resolved ancestor types"
            )
        if (
            self.membership_relationship_semantic_id
            not in self.graph_scope_filter.canonical_relationship_ids
        ):
            raise ValueError("Graph filter must include the membership relationship")
        if self.graph_scope_filter.filter_hash == self.collection_hash:
            raise ValueError("filter and collection hashes have distinct semantics")
        if self.relationship_k == 4 and self.relationship_k_4_justification is None:
            raise ValueError("relationship K=4 requires reviewed justification")
        if self.relationship_k != 4 and self.relationship_k_4_justification is not None:
            raise ValueError("relationship K justification is permitted only for K=4")
        _validate_hierarchy_configuration(
            mode=self.hierarchy_scope_mode,
            policy=self.hierarchy_expansion_policy,
            depth=self.hierarchy_expansion_depth,
        )
        _validate_scope_type_sets(
            mode=self.hierarchy_scope_mode,
            requested_root_type_ids=self.requested_root_semantic_type_ids,
            exact_type_ids=self.resolved_exact_type_ids,
            ancestor_type_ids=self.resolved_ancestor_type_ids,
            descendant_type_ids=self.resolved_descendant_type_ids,
        )
        _validate_hash(self, "retrieval_scope_hash")
        return self

    def validate_authorities(
        self,
        *,
        canonical_key_set_hash: str,
        acl_scope_hash: str,
        asserted_publication_hash: str,
        semantic_projection_hash: str,
        publication_crosswalk_hash: str,
        type_hierarchy_hash: str,
        type_closure_hash: str,
        search_index_fingerprint: str,
    ) -> None:
        checks = (
            ("canonical key set", self.canonical_key_set_hash, canonical_key_set_hash),
            ("ACL", self.acl_scope_hash, acl_scope_hash),
            (
                "asserted publication",
                self.asserted_publication_hash,
                asserted_publication_hash,
            ),
            (
                "semantic projection",
                self.semantic_projection_hash,
                semantic_projection_hash,
            ),
            (
                "publication crosswalk",
                self.publication_crosswalk_hash,
                publication_crosswalk_hash,
            ),
            ("type hierarchy", self.type_hierarchy_hash, type_hierarchy_hash),
            ("type closure", self.type_closure_hash, type_closure_hash),
            ("Search index", self.search_index_fingerprint, search_index_fingerprint),
        )
        for name, sealed, authoritative in checks:
            if sealed != authoritative:
                raise ValueError(f"stale {name} hash")

    def validate_resolved_scope(self, scope: ResolvedOntologyScope) -> None:
        member_ids = tuple(item.canonical_entity_id for item in scope.members)
        type_assertion_hash = canonical_sha256(
            [item.model_dump(mode="json") for item in scope.type_assertions]
        )
        member_type_role_hash = canonical_sha256(
            sorted(
                (
                    item.canonical_entity_id,
                    item.canonical_semantic_type_id,
                    item.member_role_id,
                )
                for item in scope.members
            )
        )
        checks = (
            (
                "resolved Ontology scope ID",
                self.resolved_ontology_scope_id,
                scope.resolved_ontology_scope_id,
            ),
            (
                "resolved Ontology scope hash",
                self.resolved_ontology_scope_hash,
                scope.resolved_scope_hash,
            ),
            ("canonical scope ID", self.canonical_scope_id, scope.canonical_scope_id),
            (
                "aggregate canonical ID",
                self.aggregate_canonical_entity_id,
                scope.aggregate_canonical_entity_id,
            ),
            (
                "aggregate semantic type",
                self.aggregate_semantic_type_id,
                scope.aggregate_semantic_type_id,
            ),
            (
                "collection canonical ID",
                self.collection_canonical_id,
                scope.collection_canonical_id,
            ),
            (
                "membership relationship",
                self.membership_relationship_semantic_id,
                scope.membership_relationship_semantic_id,
            ),
            ("canonical members", self.canonical_member_ids, member_ids),
            (
                "canonical key-set hash",
                self.canonical_key_set_hash,
                scope.canonical_key_set_hash,
            ),
            ("hierarchy mode", self.hierarchy_scope_mode, scope.hierarchy_scope_mode),
            (
                "requested root types",
                self.requested_root_semantic_type_ids,
                scope.requested_root_semantic_type_ids,
            ),
            (
                "resolved exact types",
                self.resolved_exact_type_ids,
                scope.resolved_exact_type_ids,
            ),
            (
                "resolved ancestor types",
                self.resolved_ancestor_type_ids,
                scope.resolved_ancestor_type_ids,
            ),
            (
                "resolved descendant types",
                self.resolved_descendant_type_ids,
                scope.resolved_descendant_type_ids,
            ),
            (
                "expansion trace hash",
                self.expansion_trace_hash,
                canonical_sha256(
                    [item.model_dump(mode="json") for item in scope.expansion_trace]
                ),
            ),
            (
                "type hierarchy version",
                self.type_hierarchy_version,
                scope.type_hierarchy_version,
            ),
            ("type hierarchy hash", self.type_hierarchy_hash, scope.type_hierarchy_hash),
            ("type closure hash", self.type_closure_hash, scope.type_closure_hash),
            (
                "hierarchy policy",
                self.hierarchy_expansion_policy,
                scope.hierarchy_expansion_policy,
            ),
            (
                "hierarchy depth",
                self.hierarchy_expansion_depth,
                scope.hierarchy_expansion_depth,
            ),
            ("relationship K", self.relationship_k, scope.relationship_k),
            ("type assertions", self.type_assertion_set_hash, type_assertion_hash),
            (
                "member type/role set",
                self.member_type_role_set_hash,
                member_type_role_hash,
            ),
            (
                "required role IDs",
                self.required_role_ids,
                scope.collection_policy.required_role_ids,
            ),
            (
                "collection policy",
                self.collection_policy_hash,
                scope.collection_policy.policy_hash,
            ),
            (
                "required member manifest",
                self.required_member_manifest,
                scope.required_member_manifest,
            ),
            ("ACL hash", self.acl_scope_hash, scope.acl_scope_hash),
            (
                "asserted publication hash",
                self.asserted_publication_hash,
                scope.asserted_publication_hash,
            ),
            (
                "semantic projection hash",
                self.semantic_projection_hash,
                scope.serving_projection_hash,
            ),
            (
                "crosswalk hash",
                self.publication_crosswalk_hash,
                scope.publication_crosswalk_hash,
            ),
            ("Graph model hash", self.graph_model_hash, scope.graph_model_hash),
            (
                "Search index fingerprint",
                self.search_index_fingerprint,
                scope.search_index_fingerprint,
            ),
            (
                "collection hash",
                self.collection_hash,
                scope.required_member_manifest.authoritative_collection_hash,
            ),
        )
        for name, validated, authoritative in checks:
            if validated != authoritative:
                raise ValueError(f"{name} differs from resolved Ontology scope")


class RetrievalCapability(ContractModel):
    api_version: Literal["2026-04-01", "2026-05-01-preview"]
    capability_fingerprint: Sha256
    preview_feature_enabled: bool
    base_filter_supported: bool
    filter_add_on_supported: bool
    references_available: bool
    activity_available: bool


class AgenticRetrievalRequestContext(ContractModel):
    """Safe canonical request configuration for one selected retrieval mode."""

    identity: CanonicalIdentityEnvelope
    request_context_id: RequiredText
    resolved_retrieval_scope_id: RequiredText
    resolved_retrieval_scope_hash: Sha256
    knowledge_base_id: RequiredText
    knowledge_base_fingerprint: Sha256
    knowledge_source_id: RequiredText
    knowledge_source_fingerprint: Sha256
    search_index_id: RequiredText
    search_index_fingerprint: Sha256
    retrieval_mode: RetrievalMode
    capability: RetrievalCapability
    fallback_mode: Literal["direct_hybrid_prefilter", "fail_closed"]
    fallback_for_request_context_id: RequiredText | None = None
    fallback_for_request_context_hash: Sha256 | None = None
    static_base_policy_hash: Sha256
    acl_scope_hash: Sha256
    asserted_publication_hash: Sha256
    base_filter_hash: Sha256
    graph_scope_filter: SafeCanonicalFilterSpec
    filter_add_on: SafeCanonicalFilterSpec | None = None
    effective_filter_operator: Literal["AND", "BASE_ONLY"]
    narrowing_proof_hash: Sha256 | None = None
    vector_filter_mode: Literal["preFilter"] | None = None
    type_hierarchy_hash: Sha256
    hierarchy_scope_mode: ScopeMode
    exact_type_ids: tuple[str, ...]
    ancestor_type_ids: tuple[str, ...] = ()
    canonical_entity_ids: tuple[str, ...]
    required_role_ids: tuple[str, ...] = ()
    type_assertion_set_hash: Sha256
    filter_projection_hash: Sha256
    query_budget_id: RequiredText
    query_budget_hash: Sha256
    retrieval_reasoning_effort: Literal["minimal", "low", "medium"]
    request_references: Literal[True] = True
    request_source_data: Literal[True] = True
    request_activity: bool
    expected_canonical_key_set_hash: Sha256
    expected_member_collection_hash: Sha256
    request_context_hash: Sha256

    @field_validator(
        "exact_type_ids",
        "ancestor_type_ids",
        "canonical_entity_ids",
        "required_role_ids",
        mode="before",
    )
    @classmethod
    def _sets(cls, value: object, info: Any) -> object:
        return _sorted_set(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _invariants(self) -> "AgenticRetrievalRequestContext":
        _validate_identity(
            self.identity,
            kind="c0.agentic_retrieval_request_context",
        )
        _reject_secrets_and_urls(self)
        if (self.fallback_for_request_context_id is None) != (
            self.fallback_for_request_context_hash is None
        ):
            raise ValueError("fallback origin ID and hash must be present together")
        if (
            self.retrieval_mode != "direct_hybrid_prefilter"
            and self.fallback_for_request_context_id is not None
        ):
            raise ValueError("only direct preFilter context can reference fallback origin")
        if tuple(self.canonical_entity_ids) != tuple(
            self.graph_scope_filter.canonical_entity_ids
        ):
            raise ValueError("request canonical IDs differ from resolved Graph filter")
        if tuple(self.exact_type_ids) != tuple(self.graph_scope_filter.exact_type_ids):
            raise ValueError("request exact types differ from resolved Graph filter")
        if tuple(self.ancestor_type_ids) != tuple(
            self.graph_scope_filter.ancestor_type_ids
        ):
            raise ValueError("request ancestor types differ from resolved Graph filter")
        if self.retrieval_mode == "agentic_preview":
            if (
                self.capability.api_version != "2026-05-01-preview"
                or not self.capability.preview_feature_enabled
                or not self.capability.base_filter_supported
                or not self.capability.filter_add_on_supported
                or self.filter_add_on is None
                or self.effective_filter_operator != "AND"
                or self.narrowing_proof_hash is None
                or self.vector_filter_mode is not None
            ):
                raise ValueError("preview mode requires gated baseFilter AND filterAddOn")
            if not set(self.filter_add_on.canonical_entity_ids) <= set(
                self.graph_scope_filter.canonical_entity_ids
            ):
                raise ValueError("filterAddOn would broaden canonical entity scope")
            if not set(self.filter_add_on.exact_type_ids) <= set(
                self.graph_scope_filter.exact_type_ids
            ):
                raise ValueError("filterAddOn would broaden exact type scope")
            if not set(self.filter_add_on.ancestor_type_ids) <= set(
                self.graph_scope_filter.ancestor_type_ids
            ):
                raise ValueError("filterAddOn would broaden ancestor type scope")
            if not set(self.filter_add_on.canonical_relationship_ids) <= set(
                self.graph_scope_filter.canonical_relationship_ids
            ):
                raise ValueError("filterAddOn would broaden relationship scope")
            if not self.capability.references_available or not self.capability.activity_available:
                raise ValueError("preview mode requires references and activity capability")
        elif self.retrieval_mode == "direct_hybrid_prefilter":
            if (
                self.filter_add_on is not None
                or self.effective_filter_operator != "BASE_ONLY"
                or self.narrowing_proof_hash is not None
                or self.vector_filter_mode != "preFilter"
            ):
                raise ValueError("direct fallback requires exact preFilter scope")
        elif (
            self.filter_add_on is not None
            or self.effective_filter_operator != "BASE_ONLY"
            or self.narrowing_proof_hash is not None
            or self.vector_filter_mode is not None
        ):
            raise ValueError("stable agentic mode cannot declare dynamic filtering")
        _validate_hash(self, "request_context_hash")
        return self

    def validate_budget(self, budget: QueryBudget) -> None:
        if budget.query_budget_id != self.query_budget_id:
            raise ValueError("query budget ID mismatch")
        if budget.budget_hash != self.query_budget_hash:
            raise ValueError("query budget hash mismatch")
        if budget.retrieval_mode != self.retrieval_mode:
            raise ValueError("query budget retrieval mode mismatch")

    def validate_scope(self, scope: ResolvedRetrievalScope) -> None:
        if scope.resolution_status != "valid":
            raise ValueError("resolved retrieval scope is invalid and must fail closed")
        checks = (
            (
                "resolved retrieval scope ID",
                self.resolved_retrieval_scope_id,
                scope.resolved_retrieval_scope_id,
            ),
            (
                "resolved retrieval scope hash",
                self.resolved_retrieval_scope_hash,
                scope.retrieval_scope_hash,
            ),
            (
                "Search index fingerprint",
                self.search_index_fingerprint,
                scope.search_index_fingerprint,
            ),
            ("ACL hash", self.acl_scope_hash, scope.acl_scope_hash),
            (
                "asserted publication hash",
                self.asserted_publication_hash,
                scope.asserted_publication_hash,
            ),
            ("Graph scope filter", self.graph_scope_filter, scope.graph_scope_filter),
            ("hierarchy hash", self.type_hierarchy_hash, scope.type_hierarchy_hash),
            ("hierarchy mode", self.hierarchy_scope_mode, scope.hierarchy_scope_mode),
            ("exact type IDs", self.exact_type_ids, scope.resolved_exact_type_ids),
            (
                "ancestor type IDs",
                self.ancestor_type_ids,
                scope.resolved_ancestor_type_ids,
            ),
            ("canonical entity IDs", self.canonical_entity_ids, scope.canonical_member_ids),
            ("required role IDs", self.required_role_ids, scope.required_role_ids),
            (
                "type assertion set hash",
                self.type_assertion_set_hash,
                scope.type_assertion_set_hash,
            ),
            (
                "canonical key-set hash",
                self.expected_canonical_key_set_hash,
                scope.canonical_key_set_hash,
            ),
            (
                "member collection hash",
                self.expected_member_collection_hash,
                scope.collection_hash,
            ),
        )
        for name, requested, resolved in checks:
            if requested != resolved:
                raise ValueError(f"{name} differs from resolved retrieval scope")

    def validate_fallback_origin(
        self,
        origin: "AgenticRetrievalRequestContext",
    ) -> None:
        if self.retrieval_mode != "direct_hybrid_prefilter":
            raise ValueError("fallback execution context must use direct hybrid preFilter")
        if not origin.retrieval_mode.startswith("agentic_"):
            raise ValueError("fallback origin must be an agentic request context")
        if origin.fallback_mode != "direct_hybrid_prefilter":
            raise ValueError("fallback origin did not authorize direct hybrid preFilter")
        if (
            self.fallback_for_request_context_id != origin.request_context_id
            or self.fallback_for_request_context_hash != origin.request_context_hash
        ):
            raise ValueError("direct fallback context does not reference its origin")
        invariant_fields = (
            "resolved_retrieval_scope_id",
            "resolved_retrieval_scope_hash",
            "knowledge_base_id",
            "knowledge_base_fingerprint",
            "knowledge_source_id",
            "knowledge_source_fingerprint",
            "search_index_id",
            "search_index_fingerprint",
            "static_base_policy_hash",
            "acl_scope_hash",
            "asserted_publication_hash",
            "base_filter_hash",
            "graph_scope_filter",
            "type_hierarchy_hash",
            "hierarchy_scope_mode",
            "exact_type_ids",
            "ancestor_type_ids",
            "canonical_entity_ids",
            "required_role_ids",
            "type_assertion_set_hash",
            "filter_projection_hash",
            "retrieval_reasoning_effort",
            "request_references",
            "request_source_data",
            "expected_canonical_key_set_hash",
            "expected_member_collection_hash",
        )
        if any(getattr(self, field) != getattr(origin, field) for field in invariant_fields):
            raise ValueError("direct fallback context differs from originating scope")


class RetrievalFailure(ContractModel):
    reason_code: FailureReasonCode
    remediation: RemediationClass
    canonical_ids: tuple[str, ...] = ()

    @field_validator("canonical_ids", mode="before")
    @classmethod
    def _ids(cls, value: object) -> object:
        return _sorted_set(value, field_name="canonical_ids")


class PlannedSubqueryReceipt(ContractModel):
    subquery_id: RequiredText
    subquery_hash: Sha256
    executed: bool
    knowledge_source_ids: tuple[str, ...]
    returned_reference_count: NonNegativeInt

    @field_validator("knowledge_source_ids", mode="before")
    @classmethod
    def _sources(cls, value: object) -> object:
        return _sorted_set(value, field_name="knowledge_source_ids")


class ActivityReceipt(ContractModel):
    activity_id: RequiredText
    activity_kind: RequiredText
    activity_hash: Sha256
    warning_codes: tuple[str, ...] = ()
    truncated: bool

    @field_validator("warning_codes", mode="before")
    @classmethod
    def _warnings(cls, value: object) -> object:
        return _sorted_set(value, field_name="warning_codes")


class SourceCallReceipt(ContractModel):
    source_call_id: RequiredText
    knowledge_source_id: RequiredText
    request_hash: Sha256
    response_hash: Sha256 | None = None
    status: Literal["succeeded", "failed", "partial"]
    matched_count: NonNegativeInt | None = None
    returned_count: NonNegativeInt


class CoverageBudgetObservation(ContractModel):
    max_agentic_internal_subqueries: NonNegativeInt
    max_agentic_source_calls: NonNegativeInt
    max_output_documents: PositiveInt
    max_output_tokens: PositiveInt
    max_output_bytes: PositiveInt
    max_runtime_milliseconds: PositiveInt
    observed_output_documents: NonNegativeInt
    observed_output_tokens: NonNegativeInt
    observed_output_bytes: NonNegativeInt
    observed_runtime_milliseconds: NonNegativeInt
    budget_exhausted_dimensions: tuple[str, ...] = ()

    @field_validator("budget_exhausted_dimensions", mode="before")
    @classmethod
    def _dimensions(cls, value: object) -> object:
        return _sorted_set(value, field_name="budget_exhausted_dimensions")

    @model_validator(mode="after")
    def _invariants(self) -> "CoverageBudgetObservation":
        comparisons = (
            (
                "max_output_documents",
                self.observed_output_documents,
                self.max_output_documents,
            ),
            ("max_output_tokens", self.observed_output_tokens, self.max_output_tokens),
            ("max_output_bytes", self.observed_output_bytes, self.max_output_bytes),
            (
                "max_runtime_milliseconds",
                self.observed_runtime_milliseconds,
                self.max_runtime_milliseconds,
            ),
        )
        exhausted = set(self.budget_exhausted_dimensions)
        undeclared = {
            name for name, observed, ceiling in comparisons if observed > ceiling
        } - exhausted
        if undeclared:
            raise ValueError(
                "observed budget exceeds undeclared dimensions: "
                + ", ".join(sorted(undeclared))
            )
        return self


class CitationCanonicalMapping(ContractModel):
    canonical_entity_id: RequiredText
    search_reference_id: RequiredText
    search_citation_envelope_id: RequiredText


class AgenticRetrievalCoverageReceipt(ContractModel):
    """Bounded maximal structural coverage, never exhaustive discovery proof."""

    identity: CanonicalIdentityEnvelope
    coverage_receipt_id: RequiredText
    request_context_id: RequiredText
    request_context_hash: Sha256
    resolved_retrieval_scope_id: RequiredText
    resolved_retrieval_scope_hash: Sha256
    provider_request_id: RequiredText
    provider_correlation_id: RequiredText | None = None
    retrieval_mode: RetrievalMode
    api_version: Literal["2026-04-01", "2026-05-01-preview"]
    capability_fingerprint: Sha256
    fallback_used: bool
    fallback_reason_code: FailureReasonCode | None = None
    planned_subqueries: tuple[PlannedSubqueryReceipt, ...] = ()
    activity: tuple[ActivityReceipt, ...] = ()
    source_calls: tuple[SourceCallReceipt, ...]
    matched_document_count: NonNegativeInt
    returned_document_count: NonNegativeInt
    reference_count: NonNegativeInt
    unique_canonical_id_count: NonNegativeInt
    canonical_citation_count: NonNegativeInt
    required_canonical_ids: tuple[str, ...]
    returned_canonical_ids: tuple[str, ...]
    missing_canonical_ids: tuple[str, ...] = ()
    unexpected_canonical_ids: tuple[str, ...] = ()
    duplicate_canonical_ids: tuple[str, ...] = ()
    orphan_canonical_ids: tuple[str, ...] = ()
    required_canonical_id_set_hash: Sha256
    returned_canonical_id_set_hash: Sha256
    required_group_hash: Sha256 | None = None
    returned_group_hash: Sha256 | None = None
    required_sequence_hash: Sha256 | None = None
    returned_sequence_hash: Sha256 | None = None
    required_adjacency_hash: Sha256 | None = None
    returned_adjacency_hash: Sha256 | None = None
    required_role_ids: tuple[str, ...] = ()
    returned_role_ids: tuple[str, ...] = ()
    expected_cardinality: NonNegativeInt | None = None
    minimum_cardinality: NonNegativeInt | None = None
    maximum_cardinality: NonNegativeInt | None = None
    required_unique_member_count: NonNegativeInt | None = None
    returned_unique_member_count: NonNegativeInt
    required_collection_hash: Sha256
    returned_collection_hash: Sha256
    requested_exact_type_ids: tuple[str, ...]
    returned_exact_type_ids: tuple[str, ...]
    requested_ancestor_type_ids: tuple[str, ...] = ()
    returned_ancestor_type_ids: tuple[str, ...] = ()
    type_hierarchy_hash: Sha256
    hierarchy_scope_mode: ScopeMode
    type_assertion_set_hash: Sha256
    citation_mappings: tuple[CitationCanonicalMapping, ...]
    missing_reference_ids: tuple[str, ...] = ()
    warning_codes: tuple[str, ...] = ()
    source_failure_ids: tuple[str, ...] = ()
    output_truncated: bool
    partial_response: bool
    unsupported_capability_codes: tuple[str, ...] = ()
    budget: CoverageBudgetObservation
    retrieval_reasoning_effort: Literal["minimal", "low", "medium"]
    coverage_semantics: Literal["bounded_maximal"] = "bounded_maximal"
    coverage_status: CoverageStatus
    failures: tuple[RetrievalFailure, ...] = ()
    coverage_receipt_hash: Sha256

    @field_validator(
        "required_canonical_ids",
        "returned_canonical_ids",
        "missing_canonical_ids",
        "unexpected_canonical_ids",
        "duplicate_canonical_ids",
        "orphan_canonical_ids",
        "required_role_ids",
        "returned_role_ids",
        "requested_exact_type_ids",
        "returned_exact_type_ids",
        "requested_ancestor_type_ids",
        "returned_ancestor_type_ids",
        "missing_reference_ids",
        "warning_codes",
        "source_failure_ids",
        "unsupported_capability_codes",
        mode="before",
    )
    @classmethod
    def _sets(cls, value: object, info: Any) -> object:
        return _sorted_set(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _invariants(self) -> "AgenticRetrievalCoverageReceipt":
        _validate_identity(
            self.identity,
            kind="c0.agentic_retrieval_coverage_receipt",
        )
        _reject_secrets_and_urls(self)
        required = set(self.required_canonical_ids)
        returned = set(self.returned_canonical_ids)
        duplicates = set(self.duplicate_canonical_ids)
        if set(self.missing_canonical_ids) != required - returned:
            raise ValueError("missing canonical IDs do not match required minus returned")
        if set(self.unexpected_canonical_ids) != returned - required:
            raise ValueError("unexpected canonical IDs do not match returned minus required")
        if not duplicates <= returned:
            raise ValueError("duplicate canonical IDs must be returned IDs")
        if self.required_canonical_id_set_hash != canonical_sha256(
            sorted(required)
        ):
            raise ValueError("required canonical ID-set hash mismatch")
        if self.returned_canonical_id_set_hash != canonical_sha256(
            sorted(returned)
        ):
            raise ValueError("returned canonical ID-set hash mismatch")
        if self.unique_canonical_id_count != len(returned):
            raise ValueError("unique canonical ID count mismatch")
        if self.returned_unique_member_count != len(returned):
            raise ValueError("returned unique member count mismatch")
        if self.returned_document_count > self.matched_document_count:
            raise ValueError("returned document count cannot exceed matched count")
        if self.reference_count > self.returned_document_count:
            raise ValueError("reference count cannot exceed returned document count")
        if self.budget.observed_output_documents != self.returned_document_count:
            raise ValueError("observed output documents must equal returned documents")
        if (
            self.retrieval_mode.startswith("agentic_")
            and len(self.planned_subqueries)
            > self.budget.max_agentic_internal_subqueries
        ):
            raise ValueError("planned subqueries exceed declared request ceiling")
        if (
            self.retrieval_mode.startswith("agentic_")
            and len(self.source_calls) > self.budget.max_agentic_source_calls
        ):
            raise ValueError("source calls exceed declared request ceiling")
        mapped_ids = {item.canonical_entity_id for item in self.citation_mappings}
        mapped_reference_ids = {
            item.search_reference_id for item in self.citation_mappings
        }
        if self.canonical_citation_count != len(self.citation_mappings):
            raise ValueError("canonical citation count must equal citation mappings")
        if len(mapped_reference_ids) > self.reference_count:
            raise ValueError("citation mappings exceed recorded Search references")
        if sum(call.returned_count for call in self.source_calls) < (
            self.returned_document_count
        ):
            raise ValueError("source-call returns cannot be below returned documents")
        exact = (
            required == returned
            and not duplicates
            and not self.orphan_canonical_ids
            and mapped_ids >= required
            and set(self.required_role_ids) <= set(self.returned_role_ids)
            and self.required_collection_hash == self.returned_collection_hash
            and self.required_group_hash == self.returned_group_hash
            and self.required_sequence_hash == self.returned_sequence_hash
            and self.required_adjacency_hash == self.returned_adjacency_hash
            and set(self.requested_exact_type_ids) == set(self.returned_exact_type_ids)
            and set(self.requested_ancestor_type_ids)
            == set(self.returned_ancestor_type_ids)
            and not self.missing_reference_ids
            and not self.warning_codes
            and not self.source_failure_ids
            and not self.output_truncated
            and not self.partial_response
            and not self.unsupported_capability_codes
            and not self.budget.budget_exhausted_dimensions
            and all(call.status == "succeeded" for call in self.source_calls)
            and bool(self.source_calls)
            and all(
                not item.truncated and not item.warning_codes for item in self.activity
            )
            and all(item.executed for item in self.planned_subqueries)
            and (
                self.retrieval_mode != "agentic_preview"
                or bool(self.activity)
            )
            and (
                not required
                or (
                    self.returned_document_count > 0
                    and self.reference_count > 0
                )
            )
        )
        count = len(returned)
        exact = exact and (
            self.expected_cardinality is None or count == self.expected_cardinality
        )
        exact = exact and (
            self.minimum_cardinality is None or count >= self.minimum_cardinality
        )
        exact = exact and (
            self.maximum_cardinality is None or count <= self.maximum_cardinality
        )
        exact = exact and (
            self.required_unique_member_count is None
            or count == self.required_unique_member_count
        )
        if self.coverage_status == "complete" and (not exact or self.failures):
            raise ValueError("complete coverage requires exact bounded structural evidence")
        if self.coverage_status == "partial" and exact:
            raise ValueError("partial coverage requires an observed coverage gap")
        if self.coverage_status != "complete" and not self.failures:
            raise ValueError("partial or invalid coverage requires typed failures")
        if self.fallback_used != (self.fallback_reason_code is not None):
            raise ValueError("fallback use and reason must be present together")
        if self.fallback_used and self.retrieval_mode != "direct_hybrid_prefilter":
            raise ValueError("fallback receipt must use direct hybrid preFilter mode")
        _validate_hash(self, "coverage_receipt_hash")
        return self

    def validate_request_context(
        self,
        context: AgenticRetrievalRequestContext,
        budget: QueryBudget,
        *,
        originating_context: AgenticRetrievalRequestContext | None = None,
        originating_budget: QueryBudget | None = None,
    ) -> None:
        context.validate_budget(budget)
        if self.fallback_used:
            if originating_context is None or originating_budget is None:
                raise ValueError("fallback receipt requires its originating request context")
            originating_context.validate_budget(originating_budget)
            context.validate_fallback_origin(originating_context)
        elif originating_context is not None or originating_budget is not None:
            raise ValueError("non-fallback receipt cannot declare an originating context")
        elif context.fallback_for_request_context_id is not None:
            raise ValueError("non-fallback receipt cannot consume a fallback context")
        checks = (
            ("request context ID", self.request_context_id, context.request_context_id),
            (
                "request context hash",
                self.request_context_hash,
                context.request_context_hash,
            ),
            (
                "resolved retrieval scope ID",
                self.resolved_retrieval_scope_id,
                context.resolved_retrieval_scope_id,
            ),
            (
                "resolved retrieval scope hash",
                self.resolved_retrieval_scope_hash,
                context.resolved_retrieval_scope_hash,
            ),
            ("retrieval mode", self.retrieval_mode, context.retrieval_mode),
            (
                "capability fingerprint",
                self.capability_fingerprint,
                context.capability.capability_fingerprint,
            ),
            (
                "hierarchy mode",
                self.hierarchy_scope_mode,
                context.hierarchy_scope_mode,
            ),
            (
                "type hierarchy hash",
                self.type_hierarchy_hash,
                context.type_hierarchy_hash,
            ),
            (
                "type assertion set hash",
                self.type_assertion_set_hash,
                context.type_assertion_set_hash,
            ),
            (
                "reasoning effort",
                self.retrieval_reasoning_effort,
                context.retrieval_reasoning_effort,
            ),
            (
                "required canonical IDs",
                self.required_canonical_ids,
                context.canonical_entity_ids,
            ),
            (
                "requested exact types",
                self.requested_exact_type_ids,
                context.exact_type_ids,
            ),
            (
                "requested ancestor types",
                self.requested_ancestor_type_ids,
                context.ancestor_type_ids,
            ),
            (
                "required canonical ID-set hash",
                self.required_canonical_id_set_hash,
                canonical_sha256(sorted(context.canonical_entity_ids)),
            ),
            (
                "required collection hash",
                self.required_collection_hash,
                context.expected_member_collection_hash,
            ),
            (
                "required role IDs",
                self.required_role_ids,
                context.required_role_ids,
            ),
            (
                "max internal subqueries",
                self.budget.max_agentic_internal_subqueries,
                budget.max_agentic_internal_subqueries,
            ),
            (
                "max source calls",
                self.budget.max_agentic_source_calls,
                budget.max_agentic_source_calls,
            ),
            (
                "max output documents",
                self.budget.max_output_documents,
                budget.max_output_documents,
            ),
            (
                "max output tokens",
                self.budget.max_output_tokens,
                budget.max_output_tokens,
            ),
            (
                "max output bytes",
                self.budget.max_output_bytes,
                budget.max_output_bytes,
            ),
            (
                "max runtime",
                self.budget.max_runtime_milliseconds,
                budget.max_runtime_milliseconds,
            ),
            ("API version", self.api_version, context.capability.api_version),
        )
        for name, observed, authoritative in checks:
            if observed != authoritative:
                raise ValueError(f"{name} differs from request context or budget")


class SearchCitationEnvelope(ContractModel):
    """Normalized exact authorized Search grounding with canonical lineage."""

    identity: CanonicalIdentityEnvelope
    search_citation_envelope_id: RequiredText
    search_reference_id: RequiredText
    search_document_id: RequiredText
    original_document_name: RequiredText
    source_id: RequiredText
    source_file_id: RequiredText
    source_unit_id: RequiredText
    chunk_id: RequiredText
    evidence_span_ids: tuple[str, ...]
    canonical_scope_id: RequiredText
    canonical_entity_ids: tuple[str, ...]
    canonical_relationship_ids: tuple[str, ...] = ()
    canonical_assertion_ids: tuple[str, ...]
    exact_authorized_quote: RequiredText
    quote_hash: Sha256
    page: NonNegativeInt | None = None
    section_path: tuple[str, ...] = ()
    immutable_locator: ImmutableSourceLocator
    content_hash: Sha256
    asset_hash: Sha256
    access_policy_id: RequiredText
    access_policy_hash: Sha256
    governed_asset_reference_id: RequiredText | None = None
    governed_asset_reference_hash: Sha256 | None = None
    citation_hash: Sha256

    @field_validator(
        "evidence_span_ids",
        "canonical_entity_ids",
        "canonical_relationship_ids",
        "canonical_assertion_ids",
        mode="before",
    )
    @classmethod
    def _sets(cls, value: object, info: Any) -> object:
        return _sorted_set(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _invariants(self) -> "SearchCitationEnvelope":
        _validate_identity(self.identity, kind="c0.search_citation_envelope")
        _reject_secrets_and_urls(self)
        _validate_citation_identity(
            self.identity,
            source_file_id=self.source_file_id,
            source_unit_id=self.source_unit_id,
            content_hash=self.content_hash,
            immutable_locator=self.immutable_locator,
            page=self.page,
            section_path=self.section_path,
        )
        if not self.evidence_span_ids or not self.canonical_entity_ids:
            raise ValueError("citation requires evidence and canonical entity IDs")
        if not self.canonical_assertion_ids:
            raise ValueError("citation requires asserted canonical evidence")
        if (self.governed_asset_reference_id is None) != (
            self.governed_asset_reference_hash is None
        ):
            raise ValueError("governed asset ID and hash must be present together")
        if canonical_sha256(self.exact_authorized_quote) != self.quote_hash:
            raise ValueError("quote_hash does not match exact authorized quote")
        _validate_hash(self, "citation_hash")
        return self


class CitationPresentation(ContractModel):
    """User-displayable citation; transient asset URL is never persisted or hashed."""

    identity: CanonicalIdentityEnvelope
    citation_presentation_id: RequiredText
    search_citation_envelope_id: RequiredText
    search_citation_envelope_hash: Sha256
    original_document_name: RequiredText
    source_id: RequiredText
    source_file_id: RequiredText
    source_unit_id: RequiredText
    chunk_id: RequiredText
    evidence_span_ids: tuple[str, ...]
    exact_authorized_quote: RequiredText
    quote_hash: Sha256
    page: NonNegativeInt | None = None
    section_path: tuple[str, ...] = ()
    immutable_locator: ImmutableSourceLocator
    content_hash: Sha256
    asset_hash: Sha256
    governed_asset_reference_id: RequiredText | None = None
    governed_asset_reference_hash: Sha256 | None = None
    transient_authorized_asset_url: str | None = Field(
        default=None,
        exclude=True,
        repr=False,
    )
    presentation_hash: Sha256

    @field_validator("evidence_span_ids", mode="before")
    @classmethod
    def _evidence(cls, value: object) -> object:
        return _sorted_set(value, field_name="evidence_span_ids")

    @field_validator("transient_authorized_asset_url")
    @classmethod
    def _transient_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("transient asset URL must be HTTPS")
        return value

    @model_validator(mode="after")
    def _invariants(self) -> "CitationPresentation":
        _validate_identity(self.identity, kind="c0.citation_presentation")
        persisted = self.model_dump(
            mode="python",
            exclude={"transient_authorized_asset_url", "immutable_locator"},
        )
        _reject_secrets_and_urls(persisted)
        _validate_citation_identity(
            self.identity,
            source_file_id=self.source_file_id,
            source_unit_id=self.source_unit_id,
            content_hash=self.content_hash,
            immutable_locator=self.immutable_locator,
            page=self.page,
            section_path=self.section_path,
        )
        if (self.governed_asset_reference_id is None) != (
            self.governed_asset_reference_hash is None
        ):
            raise ValueError("governed asset ID and hash must be present together")
        if canonical_sha256(self.exact_authorized_quote) != self.quote_hash:
            raise ValueError("quote_hash does not match exact authorized quote")
        _validate_hash(self, "presentation_hash")
        return self

    def validate_citation(self, citation: SearchCitationEnvelope) -> None:
        if citation.search_citation_envelope_id != self.search_citation_envelope_id:
            raise ValueError("Search citation envelope ID mismatch")
        if citation.citation_hash != self.search_citation_envelope_hash:
            raise ValueError("Search citation envelope hash mismatch")
        checks = (
            ("original document name", self.original_document_name, citation.original_document_name),
            ("source ID", self.source_id, citation.source_id),
            ("source file ID", self.source_file_id, citation.source_file_id),
            ("source unit ID", self.source_unit_id, citation.source_unit_id),
            ("chunk ID", self.chunk_id, citation.chunk_id),
            ("evidence IDs", self.evidence_span_ids, citation.evidence_span_ids),
            ("exact quote", self.exact_authorized_quote, citation.exact_authorized_quote),
            ("quote hash", self.quote_hash, citation.quote_hash),
            ("page", self.page, citation.page),
            ("section", self.section_path, citation.section_path),
            ("immutable locator", self.immutable_locator, citation.immutable_locator),
            ("content hash", self.content_hash, citation.content_hash),
            ("asset hash", self.asset_hash, citation.asset_hash),
            (
                "governed asset ID",
                self.governed_asset_reference_id,
                citation.governed_asset_reference_id,
            ),
            (
                "governed asset hash",
                self.governed_asset_reference_hash,
                citation.governed_asset_reference_hash,
            ),
        )
        for name, presented, authoritative in checks:
            if presented != authoritative:
                raise ValueError(f"{name} differs from authorized citation")
