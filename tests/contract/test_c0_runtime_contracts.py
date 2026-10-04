"""C0.Runtime schema, authority, coverage, and citation contract gate."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from fabric_kg_builder.contracts import (
    ActivityReceipt,
    AdjacencyEdge,
    AgenticRetrievalCoverageReceipt,
    AgenticRetrievalCoverageReceiptIdentityV1_1,
    AgenticRetrievalCoverageReceiptV1_1,
    AgenticRetrievalRequestContext,
    AgenticRetrievalRequestContextIdentityV1_1,
    AgenticRetrievalRequestContextV1_1,
    AuthoritativeReceiptReference,
    CanonicalIdentityEnvelope,
    CitationCanonicalMapping,
    CitationPresentation,
    CoverageBudgetObservation,
    CoverageBudgetObservationV1_1,
    CoverageMemberReference,
    ExtractionAuthorityReferences,
    ImmutableSourceLocator,
    OntologyScopeEnvelope,
    PlannedSubqueryReceipt,
    QueryBudget,
    QueryBudgetIdentityV1_1,
    QueryBudgetV1_1,
    QUERY_BUDGET_V1_1_SCHEMA_HASH,
    RequiredMemberManifestReference,
    RequiredMemberOrderingPolicyV1_1,
    RequiredMemberReferenceV1_1,
    RequiredMemberSetProposalV1_1,
    ResolvedOntologyScope,
    ResolvedRetrievalScope,
    RetrievalCapability,
    RetrievalFailure,
    RuntimeCollectionPolicy,
    SafeCanonicalFilterSpec,
    ScopeExpansionStep,
    ScopeMemberReference,
    SearchCitationEnvelope,
    SourceCallReceipt,
    TypeAssertionReference,
    UnknownContractMajorError,
    REQUIRED_MEMBER_MANIFEST_V1_1_SCHEMA_HASH,
    RequiredMemberManifestV1_1,
    canonical_json,
    canonical_sha256,
    negotiate_contract,
    parse_contract,
    write_registered_schemas,
)
from fabric_kg_builder.contracts.extraction import (
    RequiredMemberManifestIdentityV1_1,
    RequiredMemberSetProposalIdentityV1_1,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "contracts"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
HASH_F = "f" * 64
GENERIC_MEMBER_IDS = tuple(f"entity:member-{index}" for index in range(10))
RUNTIME_KINDS = {
    "c0.query_budget",
    "c0.ontology_scope_envelope",
    "c0.resolved_ontology_scope",
    "c0.resolved_retrieval_scope",
    "c0.agentic_retrieval_request_context",
    "c0.agentic_retrieval_coverage_receipt",
    "c0.search_citation_envelope",
    "c0.citation_presentation",
}


def identity(
    kind: str,
    version: str = "1.0.0",
) -> (
    CanonicalIdentityEnvelope
    | RequiredMemberManifestIdentityV1_1
    | RequiredMemberSetProposalIdentityV1_1
):
    values = {
        "contract_kind": kind,
        "contract_version": version,
        "project_id": "project:generic",
        "asset_id": None,
        "asset_version_id": None,
        "run_id": "run:c0-runtime",
        "source_file_id": None,
        "source_unit_id": None,
        "content_hash": None,
        "domain_schema_version": "2.0",
        "domain_contract_hash": HASH_A,
        "semantic_contract_hash": HASH_B,
        "canonical_schema_version": "2.0",
        "prompt_version": None,
        "prompt_hash": None,
        "model_version": None,
        "model_hash": None,
        "extractor_name": None,
        "extractor_version": None,
        "parent_artifact_ids": ("artifact:authority",),
        "parent_record_ids": (),
        "immutable_locator": None,
    }
    if kind == "c0.required_member_set_proposal" and version == "1.1.0":
        return RequiredMemberSetProposalIdentityV1_1.model_validate(values)
    if kind == "c0.required_member_manifest" and version == "1.1.0":
        return RequiredMemberManifestIdentityV1_1.model_validate(values)
    return CanonicalIdentityEnvelope.model_validate(values)


def seal(model: type[Any], hash_field: str, values: dict[str, Any]) -> Any:
    return model(**values, **{hash_field: canonical_sha256(values)})


def required_member_manifest(
    entity_ids: tuple[str, ...] = GENERIC_MEMBER_IDS,
    *,
    assertion_version_offset: int = 0,
) -> RequiredMemberManifestV1_1:
    manifest_members = tuple(
        RequiredMemberReferenceV1_1.seal(
            member_canonical_id=entity_id,
            member_semantic_type_id="type:component",
            member_role_id="role:component",
            member_order=None,
            candidate_id=f"candidate:{ordinal}",
            supporting_evidence_span_ids=(
                f"evidence-span:{ordinal + assertion_version_offset:032x}",
            ),
        )
        for ordinal, entity_id in enumerate(entity_ids)
    )
    proposal = RequiredMemberSetProposalV1_1.seal(
        identity=identity("c0.required_member_set_proposal", "1.1.0"),
        required_member_set_proposal_id="proposal:runtime-required-members",
        extraction_candidate_batch_id="batch:runtime-required-members",
        extraction_candidate_batch_hash=HASH_A,
        authority=ExtractionAuthorityReferences(
            source_corpus_manifest_id="manifest:source-corpus",
            source_corpus_manifest_hash=HASH_A,
            source_unit_manifest_id="manifest:source-units",
            source_unit_manifest_hash=HASH_F,
            domain_contract_hash=HASH_A,
            completeness_requirement_id="completeness:required-members",
            completeness_requirement_hash=HASH_C,
            hierarchy_hash=HASH_D,
            identity_policy_hash=HASH_E,
        ),
        scope_canonical_id="scope:generic",
        membership_semantic_relationship_id="relationship:has-member",
        ordering_policy=RequiredMemberOrderingPolicyV1_1(mode="unordered"),
        expected_cardinality=len(entity_ids),
        minimum_cardinality=len(entity_ids),
        maximum_cardinality=len(entity_ids),
        required_role_ids=("role:component",),
        members=manifest_members,
    )
    return RequiredMemberManifestV1_1.seal_from_proposal(
        proposal,
        identity=identity("c0.required_member_manifest", "1.1.0"),
        required_member_manifest_id="manifest:required-members",
        validator_name="local-deterministic-validator",
        validator_version="1.1.0",
        sealed_at_utc=datetime(2026, 8, 24, 20, 0, tzinfo=timezone.utc),
    )


def manifest_reference(
    manifest: RequiredMemberManifestV1_1 | None = None,
) -> RequiredMemberManifestReference:
    manifest = manifest or required_member_manifest()
    return RequiredMemberManifestReference(
        required_member_manifest_id=manifest.required_member_manifest_id,
        contract_kind="c0.required_member_manifest",
        contract_version="1.1.0",
        schema_hash=REQUIRED_MEMBER_MANIFEST_V1_1_SCHEMA_HASH,
        manifest_hash=manifest.manifest_hash,
        authoritative_collection_hash=manifest.authoritative_collection_hash,
    )


def query_budget(
    mode: str = "agentic_preview",
    *,
    relationship_k: int = 3,
    justification: str | None = None,
    hierarchy_depth: int = 0,
) -> QueryBudget:
    values = {
        "identity": identity("c0.query_budget"),
        "query_budget_id": f"query-budget:{mode}",
        "max_ontology_graph_scope_requests": 1,
        "relationship_k": relationship_k,
        "relationship_k_4_justification": justification,
        "hierarchy_expansion_policy": (
            "none" if hierarchy_depth == 0 else "sealed_descendants"
        ),
        "hierarchy_expansion_depth": hierarchy_depth,
        "retrieval_mode": mode,
        "max_agentic_retrieval_invocations": (
            0 if mode == "direct_hybrid_prefilter" else 1
        ),
        "max_agentic_internal_subqueries": (
            0 if mode == "direct_hybrid_prefilter" else 4
        ),
        "max_agentic_source_calls": (
            0 if mode == "direct_hybrid_prefilter" else 4
        ),
        "max_direct_search_requests": (
            1 if mode == "direct_hybrid_prefilter" else 0
        ),
        "max_output_documents": 25,
        "max_output_tokens": 4096,
        "max_output_bytes": 131072,
        "max_runtime_milliseconds": 30000,
        "max_graph_result_records": 100,
        "max_search_result_records": 25,
    }
    return seal(QueryBudget, "budget_hash", values)


def ontology_scope(
    mode: str = "exact_type",
    *,
    relative_change: str = "exact",
    parent: OntologyScopeEnvelope | None = None,
    root_type_ids: tuple[str, ...] | None = None,
    member_ids: tuple[str, ...] | None = None,
    hierarchy_depth: int | None = None,
    manifest: RequiredMemberManifestV1_1 | None = None,
) -> OntologyScopeEnvelope:
    roots = (
        root_type_ids
        if root_type_ids is not None
        else (() if mode == "explicit_member_set" else ("type:component",))
    )
    members = (
        member_ids
        if member_ids is not None
        else (("entity:component-1",) if mode == "explicit_member_set" else ())
    )
    manifest = manifest or required_member_manifest(
        members if members else GENERIC_MEMBER_IDS
    )
    policy = {
        "exact_type": "none",
        "descendants": "sealed_descendants",
        "ancestors_context": "sealed_ancestors_context",
        "explicit_member_set": "explicit_members",
    }[mode]
    depth = (
        hierarchy_depth
        if hierarchy_depth is not None
        else (0 if mode in {"exact_type", "explicit_member_set"} else 2)
    )
    values = {
        "identity": identity("c0.ontology_scope_envelope"),
        "ontology_scope_envelope_id": f"ontology-scope:{mode}:{relative_change}",
        "parent_scope_id": (
            parent.ontology_scope_envelope_id if parent is not None else None
        ),
        "parent_scope_hash": parent.scope_hash if parent is not None else None,
        "relative_change": relative_change,
        "hierarchy_scope_mode": mode,
        "canonical_root_semantic_type_ids": roots,
        "explicit_canonical_entity_ids": members,
        "hierarchy_expansion_policy": policy,
        "hierarchy_expansion_depth": depth,
        "aggregate_canonical_entity_id": "entity:aggregate",
        "aggregate_semantic_type_id": "type:aggregate",
        "requested_member_semantic_type_ids": ("type:component",),
        "membership_relationship_semantic_id": "relationship:has-member",
        "approved_relationship_semantic_ids": ("relationship:has-member",),
        "requested_member_role_ids": ("role:component",),
        "required_role_ids": ("role:component",),
        "approved_graph_path_ids": ("graph-path:aggregate-members",),
        "include_canonical_ids": (),
        "exclude_canonical_ids": (),
        "relationship_k": 3,
        "relationship_k_4_justification": None,
        "required_member_manifest": manifest_reference(manifest),
        "project_scope_id": "project:generic",
        "acl_scope_hash": HASH_F,
        "asserted_publication_hash": HASH_A,
        "semantic_contract_hash": HASH_B,
        "type_hierarchy_id": "hierarchy:generic",
        "type_hierarchy_version": "1.0.0",
        "type_hierarchy_hash": HASH_C,
        "type_closure_hash": HASH_D,
        "semantic_projection_hash": HASH_E,
        "graph_model_hash": HASH_F,
        "search_index_fingerprint": HASH_A,
        "publication_crosswalk_hash": HASH_B,
        "agent_policy_id": "agent-policy:bounded-evidence",
        "agent_policy_hash": HASH_C,
        "scope_decision_reason_code": "requested_exact_authoritative_scope",
    }
    return seal(OntologyScopeEnvelope, "scope_hash", values)


def member(entity_id: str, ordinal: int) -> ScopeMemberReference:
    values = {
        "canonical_entity_id": entity_id,
        "canonical_semantic_type_id": "type:component",
        "type_assertion_id": f"type-assertion:{entity_id}:{ordinal + 1}",
        "type_assertion_version": ordinal + 1,
        "member_role_id": "role:component",
        "membership_assertion_ids": (f"assertion:membership:{ordinal}",),
        "evidence_span_ids": (f"evidence-span:{ordinal:032x}",),
        "group_id": None,
        "sequence_position": None,
    }
    return seal(ScopeMemberReference, "member_hash", values)


def collection_policy(
    count: int = 2,
    *,
    manifest_id: str = "manifest:required-members",
) -> RuntimeCollectionPolicy:
    values = {
        "ordering_mode": "unordered",
        "expected_cardinality": count,
        "minimum_cardinality": count,
        "maximum_cardinality": count,
        "required_unique_member_count": count,
        "required_role_ids": ("role:component",),
        "completeness_rule_ids": (manifest_id,),
        "cardinality_rule_ids": ("cardinality:exact",),
    }
    return seal(RuntimeCollectionPolicy, "policy_hash", values)


def resolved_ontology_scope(
    *,
    entity_ids: tuple[str, ...] = GENERIC_MEMBER_IDS,
    assertion_version_offset: int = 0,
    mode: str = "exact_type",
) -> ResolvedOntologyScope:
    authoritative_manifest = required_member_manifest(
        entity_ids,
        assertion_version_offset=assertion_version_offset,
    )
    envelope = ontology_scope(
        mode,
        member_ids=entity_ids if mode == "explicit_member_set" else None,
        manifest=authoritative_manifest,
    )
    members = tuple(
        member(entity_id, ordinal + assertion_version_offset)
        for ordinal, entity_id in enumerate(entity_ids)
    )
    expanded_type_id = (
        "type:subcomponent"
        if mode == "descendants"
        else "type:asset"
        if mode == "ancestors_context"
        else None
    )
    exact_type_ids = tuple(
        sorted(
            {"type:component"}
            | ({expanded_type_id} if expanded_type_id is not None else set())
        )
    )
    expansion_trace = (
        (
            ScopeExpansionStep(
                ordinal=0,
                from_semantic_type_id="type:component",
                to_semantic_type_id=expanded_type_id,
                edge_kind="child" if mode == "descendants" else "ancestor",
                hierarchy_edge_assertion_id=f"hierarchy-edge:{mode}",
                hierarchy_edge_hash=HASH_F,
            ),
        )
        if expanded_type_id is not None
        else ()
    )
    type_assertions = tuple(
        TypeAssertionReference(
            canonical_entity_id=item.canonical_entity_id,
            canonical_semantic_type_id=item.canonical_semantic_type_id,
            type_assertion_id=item.type_assertion_id,
            type_assertion_version=item.type_assertion_version,
            type_assertion_hash=canonical_sha256(
                {
                    "entity": item.canonical_entity_id,
                    "type": item.canonical_semantic_type_id,
                    "version": item.type_assertion_version,
                }
            ),
        )
        for item in members
    )
    key_values = {
        "aggregate_canonical_entity_id": "entity:aggregate",
        "collection_canonical_id": "collection:components",
        "membership_relationship_semantic_id": "relationship:has-member",
        "members": [
            {
                "canonical_entity_id": item.canonical_entity_id,
                "canonical_semantic_type_id": item.canonical_semantic_type_id,
                "type_assertion_id": item.type_assertion_id,
                "type_assertion_version": item.type_assertion_version,
                "member_role_id": item.member_role_id,
                "membership_assertion_ids": list(item.membership_assertion_ids),
                "evidence_span_ids": list(item.evidence_span_ids),
            }
            for item in members
        ],
    }
    values = {
        "identity": identity("c0.resolved_ontology_scope"),
        "resolved_ontology_scope_id": f"resolved-ontology-scope:{mode}",
        "resolver_request_id": "resolver-request:generic",
        "resolver_request_hash": HASH_A,
        "ontology_scope_envelope_id": envelope.ontology_scope_envelope_id,
        "ontology_scope_envelope_hash": envelope.scope_hash,
        "canonical_scope_id": "scope:generic",
        "aggregate_canonical_entity_id": "entity:aggregate",
        "aggregate_semantic_type_id": "type:aggregate",
        "collection_canonical_id": "collection:components",
        "membership_relationship_semantic_id": "relationship:has-member",
        "hierarchy_scope_mode": mode,
        "requested_root_semantic_type_ids": (
            envelope.canonical_root_semantic_type_ids
        ),
        "requested_member_semantic_type_ids": (
            envelope.requested_member_semantic_type_ids
        ),
        "approved_relationship_semantic_ids": (
            envelope.approved_relationship_semantic_ids
        ),
        "requested_member_role_ids": envelope.requested_member_role_ids,
        "approved_graph_path_ids": envelope.approved_graph_path_ids,
        "resolved_exact_type_ids": exact_type_ids,
        "resolved_ancestor_type_ids": (
            (expanded_type_id,) if mode == "ancestors_context" else ()
        ),
        "resolved_descendant_type_ids": (
            (expanded_type_id,) if mode == "descendants" else ()
        ),
        "expansion_trace": expansion_trace,
        "type_hierarchy_id": envelope.type_hierarchy_id,
        "type_hierarchy_version": envelope.type_hierarchy_version,
        "type_hierarchy_hash": envelope.type_hierarchy_hash,
        "type_closure_hash": envelope.type_closure_hash,
        "hierarchy_expansion_policy": envelope.hierarchy_expansion_policy,
        "hierarchy_expansion_depth": envelope.hierarchy_expansion_depth,
        "members": members,
        "type_assertions": type_assertions,
        "relationship_semantic_ids": ("relationship:has-member",),
        "assertion_ids": tuple(
            sorted(
                assertion_id
                for item in members
                for assertion_id in item.membership_assertion_ids
            )
        ),
        "evidence_span_ids": tuple(
            sorted(
                evidence_id
                for item in members
                for evidence_id in item.evidence_span_ids
            )
        ),
        "included_canonical_ids": entity_ids,
        "requested_include_canonical_ids": envelope.include_canonical_ids,
        "requested_exclude_canonical_ids": envelope.exclude_canonical_ids,
        "excluded_canonical_ids": (),
        "adjacency_edges": (),
        "collection_policy": collection_policy(
            len(entity_ids),
            manifest_id=authoritative_manifest.required_member_manifest_id,
        ),
        "required_member_manifest": manifest_reference(authoritative_manifest),
        "relationship_traversal_policy_id": "traversal:bounded",
        "relationship_traversal_policy_hash": HASH_E,
        "relationship_k": envelope.relationship_k,
        "relationship_k_4_justification": (
            envelope.relationship_k_4_justification
        ),
        "serving_projection_hash": envelope.semantic_projection_hash,
        "publication_crosswalk_hash": envelope.publication_crosswalk_hash,
        "graph_model_hash": envelope.graph_model_hash,
        "search_index_fingerprint": envelope.search_index_fingerprint,
        "asserted_publication_hash": envelope.asserted_publication_hash,
        "acl_scope_hash": envelope.acl_scope_hash,
        "project_scope_id": envelope.project_scope_id,
        "agent_policy_id": envelope.agent_policy_id,
        "agent_policy_hash": envelope.agent_policy_hash,
        "resolver_capability_id": "resolver:direct-gql",
        "resolver_version": "1.0.0",
        "authoritative_receipts": (
            AuthoritativeReceiptReference(
                receipt_id="receipt:graph",
                receipt_hash=HASH_E,
            ),
            AuthoritativeReceiptReference(
                receipt_id="receipt:manifest",
                receipt_hash=HASH_F,
            ),
        ),
        "canonical_key_set_hash": canonical_sha256(key_values),
    }
    resolved = seal(ResolvedOntologyScope, "resolved_scope_hash", values)
    resolved.validate_envelope(envelope)
    resolved.validate_required_member_manifest(authoritative_manifest)
    return resolved


def safe_filter(
    entity_ids: tuple[str, ...],
    *,
    exact_type_ids: tuple[str, ...] = ("type:component",),
    ancestor_type_ids: tuple[str, ...] = (),
) -> SafeCanonicalFilterSpec:
    values = {
        "canonical_entity_ids": entity_ids,
        "exact_type_ids": exact_type_ids,
        "ancestor_type_ids": ancestor_type_ids,
        "canonical_relationship_ids": ("relationship:has-member",),
        "asserted_publication_only": True,
    }
    return seal(SafeCanonicalFilterSpec, "filter_hash", values)


def resolved_retrieval_scope(
    *,
    entity_ids: tuple[str, ...] = GENERIC_MEMBER_IDS,
    mode: str = "exact_type",
) -> ResolvedRetrievalScope:
    resolved = resolved_ontology_scope(entity_ids=entity_ids, mode=mode)
    entity_ids = tuple(item.canonical_entity_id for item in resolved.members)
    values = {
        "identity": identity("c0.resolved_retrieval_scope"),
        "resolved_retrieval_scope_id": "resolved-retrieval-scope:generic",
        "ontology_scope_envelope_id": resolved.ontology_scope_envelope_id,
        "ontology_scope_envelope_hash": resolved.ontology_scope_envelope_hash,
        "resolved_ontology_scope_id": resolved.resolved_ontology_scope_id,
        "resolved_ontology_scope_hash": resolved.resolved_scope_hash,
        "resolution_status": "valid",
        "findings": (),
        "canonical_scope_id": resolved.canonical_scope_id,
        "aggregate_canonical_entity_id": resolved.aggregate_canonical_entity_id,
        "aggregate_semantic_type_id": resolved.aggregate_semantic_type_id,
        "collection_canonical_id": resolved.collection_canonical_id,
        "membership_relationship_semantic_id": (
            resolved.membership_relationship_semantic_id
        ),
        "relationship_semantic_ids": resolved.relationship_semantic_ids,
        "canonical_member_ids": entity_ids,
        "canonical_key_set_hash": resolved.canonical_key_set_hash,
        "hierarchy_scope_mode": resolved.hierarchy_scope_mode,
        "requested_root_semantic_type_ids": (
            resolved.requested_root_semantic_type_ids
        ),
        "resolved_exact_type_ids": resolved.resolved_exact_type_ids,
        "resolved_ancestor_type_ids": resolved.resolved_ancestor_type_ids,
        "resolved_descendant_type_ids": resolved.resolved_descendant_type_ids,
        "expansion_trace_hash": canonical_sha256([]),
        "type_hierarchy_version": resolved.type_hierarchy_version,
        "type_hierarchy_hash": resolved.type_hierarchy_hash,
        "type_closure_hash": resolved.type_closure_hash,
        "hierarchy_expansion_policy": resolved.hierarchy_expansion_policy,
        "hierarchy_expansion_depth": resolved.hierarchy_expansion_depth,
        "relationship_k": resolved.relationship_k,
        "relationship_k_4_justification": None,
        "type_assertion_set_hash": canonical_sha256(
            [item.model_dump(mode="json") for item in resolved.type_assertions]
        ),
        "member_type_role_set_hash": canonical_sha256(
            sorted(
                (
                    item.canonical_entity_id,
                    item.canonical_semantic_type_id,
                    item.member_role_id,
                )
                for item in resolved.members
            )
        ),
        "required_role_ids": resolved.collection_policy.required_role_ids,
        "group_membership_hash": None,
        "sequence_hash": None,
        "adjacency_hash": None,
        "collection_policy_hash": resolved.collection_policy.policy_hash,
        "required_member_manifest": resolved.required_member_manifest,
        "include_canonical_ids": (),
        "exclude_canonical_ids": (),
        "acl_scope_hash": resolved.acl_scope_hash,
        "asserted_publication_hash": resolved.asserted_publication_hash,
        "semantic_projection_hash": resolved.serving_projection_hash,
        "publication_crosswalk_hash": resolved.publication_crosswalk_hash,
        "graph_model_hash": resolved.graph_model_hash,
        "search_index_fingerprint": resolved.search_index_fingerprint,
        "graph_scope_filter": safe_filter(
            entity_ids,
            exact_type_ids=resolved.resolved_exact_type_ids,
            ancestor_type_ids=resolved.resolved_ancestor_type_ids,
        ),
        "collection_hash": resolved.required_member_manifest.authoritative_collection_hash,
        "parent_scope_change": "exact",
    }
    scope = seal(ResolvedRetrievalScope, "retrieval_scope_hash", values)
    scope.validate_resolved_scope(resolved)
    return scope


def request_context(
    mode: str = "agentic_preview",
    *,
    entity_ids: tuple[str, ...] = GENERIC_MEMBER_IDS,
    fallback_for: AgenticRetrievalRequestContext | None = None,
) -> tuple[AgenticRetrievalRequestContext, QueryBudget]:
    scope = resolved_retrieval_scope(entity_ids=entity_ids)
    budget = query_budget(mode)
    preview = mode == "agentic_preview"
    direct = mode == "direct_hybrid_prefilter"
    capability = RetrievalCapability(
        api_version="2026-05-01-preview" if preview else "2026-04-01",
        capability_fingerprint=HASH_A,
        preview_feature_enabled=preview,
        base_filter_supported=preview,
        filter_add_on_supported=preview,
        references_available=True,
        activity_available=mode != "agentic_stable_without_dynamic_filter",
    )
    add_on = (
        safe_filter(
            scope.canonical_member_ids[:1],
            exact_type_ids=scope.resolved_exact_type_ids,
            ancestor_type_ids=scope.resolved_ancestor_type_ids,
        )
        if preview
        else None
    )
    values = {
        "identity": identity("c0.agentic_retrieval_request_context"),
        "request_context_id": f"request-context:{mode}",
        "resolved_retrieval_scope_id": scope.resolved_retrieval_scope_id,
        "resolved_retrieval_scope_hash": scope.retrieval_scope_hash,
        "knowledge_base_id": "knowledge-base:generic",
        "knowledge_base_fingerprint": HASH_B,
        "knowledge_source_id": "knowledge-source:generic",
        "knowledge_source_fingerprint": HASH_C,
        "search_index_id": "search-index:generic",
        "search_index_fingerprint": scope.search_index_fingerprint,
        "retrieval_mode": mode,
        "capability": capability,
        "fallback_mode": "direct_hybrid_prefilter",
        "fallback_for_request_context_id": (
            fallback_for.request_context_id if fallback_for is not None else None
        ),
        "fallback_for_request_context_hash": (
            fallback_for.request_context_hash if fallback_for is not None else None
        ),
        "static_base_policy_hash": HASH_D,
        "acl_scope_hash": scope.acl_scope_hash,
        "asserted_publication_hash": scope.asserted_publication_hash,
        "base_filter_hash": HASH_E,
        "graph_scope_filter": scope.graph_scope_filter,
        "filter_add_on": add_on,
        "effective_filter_operator": "AND" if preview else "BASE_ONLY",
        "narrowing_proof_hash": HASH_F if preview else None,
        "vector_filter_mode": "preFilter" if direct else None,
        "type_hierarchy_hash": scope.type_hierarchy_hash,
        "hierarchy_scope_mode": scope.hierarchy_scope_mode,
        "exact_type_ids": scope.resolved_exact_type_ids,
        "ancestor_type_ids": scope.resolved_ancestor_type_ids,
        "canonical_entity_ids": scope.canonical_member_ids,
        "required_role_ids": scope.required_role_ids,
        "type_assertion_set_hash": scope.type_assertion_set_hash,
        "filter_projection_hash": HASH_A,
        "query_budget_id": budget.query_budget_id,
        "query_budget_hash": budget.budget_hash,
        "retrieval_reasoning_effort": "low",
        "request_references": True,
        "request_source_data": True,
        "request_activity": preview,
        "expected_canonical_key_set_hash": scope.canonical_key_set_hash,
        "expected_member_collection_hash": scope.collection_hash,
        "expected_member_type_role_set_hash": scope.member_type_role_set_hash,
        "expected_group_membership_hash": scope.group_membership_hash,
        "expected_sequence_hash": scope.sequence_hash,
        "expected_adjacency_hash": scope.adjacency_hash,
    }
    context = seal(
        AgenticRetrievalRequestContext,
        "request_context_hash",
        values,
    )
    context.validate_scope(scope)
    return context, budget


def coverage_receipt(
    *,
    count: int = 10,
    status: str = "complete",
    missing: int = 0,
    truncated: bool = False,
    fallback: bool = False,
) -> AgenticRetrievalCoverageReceipt:
    required = tuple(f"entity:member-{index}" for index in range(count))
    originating_context = None
    originating_budget = None
    if fallback:
        originating_context, originating_budget = request_context(
            entity_ids=required
        )
        context, query_budget_contract = request_context(
            "direct_hybrid_prefilter",
            entity_ids=required,
            fallback_for=originating_context,
        )
    else:
        context, query_budget_contract = request_context(entity_ids=required)
    returned = required[:-missing] if missing else required
    missing_ids = tuple(sorted(set(required) - set(returned)))
    collection_hash = context.expected_member_collection_hash
    citations = tuple(
        search_citation(entity_id=entity_id, index=index)
        for index, entity_id in enumerate(returned)
    )
    returned_members = tuple(
        seal(
            CoverageMemberReference,
            "member_hash",
            {
                "canonical_entity_id": entity_id,
                "canonical_semantic_type_id": "type:component",
                "member_role_id": "role:component",
                "group_id": None,
                "sequence_position": None,
                "search_reference_ids": (citations[index].search_reference_id,),
                "search_citation_envelope_ids": (
                    citations[index].search_citation_envelope_id,
                ),
            },
        )
        for index, entity_id in enumerate(returned)
    )
    returned_collection_hash = canonical_sha256(
        [item.model_dump(mode="json") for item in returned_members]
    )
    warning_codes = ("warning:output-truncated",) if truncated else ()
    failures = (
        (
            RetrievalFailure(
                reason_code=(
                    "required_member_missing" if missing else "output_truncated"
                ),
                remediation="downstream_abstention_required",
                canonical_ids=missing_ids,
            ),
        )
        if status != "complete"
        else ()
    )
    values = {
        "identity": identity("c0.agentic_retrieval_coverage_receipt"),
        "coverage_receipt_id": f"coverage:{status}:{count}:{missing}",
        "request_context_id": context.request_context_id,
        "request_context_hash": context.request_context_hash,
        "resolved_retrieval_scope_id": context.resolved_retrieval_scope_id,
        "resolved_retrieval_scope_hash": context.resolved_retrieval_scope_hash,
        "provider_request_id": "provider-request:generic",
        "provider_correlation_id": "provider-correlation:generic",
        "retrieval_mode": context.retrieval_mode,
        "api_version": context.capability.api_version,
        "capability_fingerprint": context.capability.capability_fingerprint,
        "fallback_used": fallback,
        "fallback_reason_code": "capability_unavailable" if fallback else None,
        "planned_subqueries": (),
        "activity": (
            ActivityReceipt(
                activity_id="activity:retrieve",
                activity_kind="retrieval",
                activity_hash=HASH_A,
                warning_codes=warning_codes,
                truncated=truncated,
            ),
        ),
        "source_calls": (
            SourceCallReceipt(
                source_call_id="source-call:search",
                knowledge_source_id=context.knowledge_source_id,
                request_hash=HASH_B,
                response_hash=HASH_C,
                status="succeeded",
                matched_count=count,
                returned_count=len(returned),
            ),
        ),
        "matched_document_count": count,
        "returned_document_count": len(returned),
        "reference_count": len(returned),
        "unique_canonical_id_count": len(returned),
        "canonical_citation_count": len(returned),
        "returned_members": returned_members,
        "returned_adjacency_edges": (),
        "required_canonical_ids": required,
        "returned_canonical_ids": returned,
        "missing_canonical_ids": missing_ids,
        "unexpected_canonical_ids": (),
        "duplicate_canonical_ids": (),
        "orphan_canonical_ids": (),
        "required_canonical_id_set_hash": canonical_sha256(sorted(required)),
        "returned_canonical_id_set_hash": canonical_sha256(sorted(returned)),
        "required_group_hash": context.expected_group_membership_hash,
        "returned_group_hash": context.expected_group_membership_hash,
        "required_sequence_hash": context.expected_sequence_hash,
        "returned_sequence_hash": context.expected_sequence_hash,
        "required_adjacency_hash": context.expected_adjacency_hash,
        "returned_adjacency_hash": context.expected_adjacency_hash,
        "required_role_ids": context.required_role_ids,
        "returned_role_ids": context.required_role_ids,
        "expected_cardinality": count,
        "minimum_cardinality": count,
        "maximum_cardinality": count,
        "required_unique_member_count": count,
        "returned_unique_member_count": len(returned),
        "required_collection_hash": collection_hash,
        "returned_collection_hash": returned_collection_hash,
        "requested_exact_type_ids": context.exact_type_ids,
        "returned_exact_type_ids": context.exact_type_ids,
        "requested_ancestor_type_ids": (),
        "returned_ancestor_type_ids": (),
        "type_hierarchy_hash": context.type_hierarchy_hash,
        "hierarchy_scope_mode": "exact_type",
        "type_assertion_set_hash": context.type_assertion_set_hash,
        "citation_mappings": tuple(
            CitationCanonicalMapping(
                canonical_entity_id=entity_id,
                search_reference_id=citations[index].search_reference_id,
                search_citation_envelope_id=(
                    citations[index].search_citation_envelope_id
                ),
                search_citation_envelope_hash=citations[index].citation_hash,
            )
            for index, entity_id in enumerate(returned)
        ),
        "missing_reference_ids": (),
        "warning_codes": warning_codes,
        "source_failure_ids": (),
        "output_truncated": truncated,
        "partial_response": missing > 0,
        "unsupported_capability_codes": (),
        "budget": CoverageBudgetObservation(
            max_ontology_graph_scope_requests=(
                query_budget_contract.max_ontology_graph_scope_requests
            ),
            max_agentic_retrieval_invocations=(
                query_budget_contract.max_agentic_retrieval_invocations
            ),
            max_agentic_internal_subqueries=(
                query_budget_contract.max_agentic_internal_subqueries
            ),
            max_agentic_source_calls=query_budget_contract.max_agentic_source_calls,
            max_direct_search_requests=(
                query_budget_contract.max_direct_search_requests
            ),
            max_output_documents=query_budget_contract.max_output_documents,
            max_output_tokens=query_budget_contract.max_output_tokens,
            max_output_bytes=query_budget_contract.max_output_bytes,
            max_runtime_milliseconds=(
                query_budget_contract.max_runtime_milliseconds
            ),
            max_graph_result_records=query_budget_contract.max_graph_result_records,
            max_search_result_records=(
                query_budget_contract.max_search_result_records
            ),
            observed_ontology_graph_scope_requests=1,
            observed_agentic_retrieval_invocations=(0 if fallback else 1),
            observed_agentic_internal_subqueries=0,
            observed_agentic_source_calls=(0 if fallback else 1),
            observed_direct_search_requests=(1 if fallback else 0),
            observed_output_documents=len(returned),
            observed_output_tokens=1024,
            observed_output_bytes=16384,
            observed_runtime_milliseconds=1000,
            observed_graph_result_records=count,
            observed_search_result_records=len(returned),
            budget_exhausted_dimensions=(
                ("max_output_documents",) if truncated else ()
            ),
        ),
        "retrieval_reasoning_effort": "low",
        "coverage_semantics": "bounded_maximal",
        "coverage_status": status,
        "failures": failures,
    }
    receipt = seal(
        AgenticRetrievalCoverageReceipt,
        "coverage_receipt_hash",
        values,
    )
    receipt.validate_request_context(
        context,
        query_budget_contract,
        originating_context=originating_context,
        originating_budget=originating_budget,
    )
    receipt.validate_citations(citations)
    return receipt


def request_context_v1_1(
    mode: str = "agentic_preview",
    *,
    entity_ids: tuple[str, ...] = GENERIC_MEMBER_IDS,
    vector_search_requests: int = 0,
    embedding_calls: int = 0,
    embedding_items: int = 0,
) -> tuple[
    AgenticRetrievalRequestContextV1_1,
    QueryBudgetV1_1,
    AgenticRetrievalRequestContextV1_1 | None,
    QueryBudgetV1_1 | None,
]:
    origin_context = None
    origin_budget = None
    old_origin = None
    if mode == "direct_hybrid_prefilter":
        old_origin, _ = request_context(entity_ids=entity_ids)
        old_context, old_budget = request_context(
            mode,
            entity_ids=entity_ids,
            fallback_for=old_origin,
        )
        origin_context, origin_budget, _, _ = request_context_v1_1(
            entity_ids=entity_ids
        )
    else:
        old_context, old_budget = request_context(mode, entity_ids=entity_ids)

    budget_values = old_budget.model_dump(
        mode="python",
        exclude={"budget_hash"},
        round_trip=True,
    )
    budget_values["identity"] = QueryBudgetIdentityV1_1.model_validate(
        {
            **budget_values["identity"],
            "contract_version": "1.1.0",
        }
    )
    budget_values.update(
        {
            "max_search_candidate_records": 50,
            "max_vector_search_requests": vector_search_requests,
            "max_embedding_calls": embedding_calls,
            "max_embedding_items": embedding_items,
            "max_retry_count": 2,
            "max_retry_wait_milliseconds": 1000,
        }
    )
    budget = seal(QueryBudgetV1_1, "budget_hash", budget_values)

    context_values = old_context.model_dump(
        mode="python",
        exclude={"request_context_hash"},
        round_trip=True,
    )
    context_values["identity"] = (
        AgenticRetrievalRequestContextIdentityV1_1.model_validate(
            {
                **context_values["identity"],
                "contract_version": "1.1.0",
            }
        )
    )
    context_values.update(
        {
            "query_budget_hash": budget.budget_hash,
            "query_budget_contract_version": "1.1.0",
            "query_budget_schema_hash": QUERY_BUDGET_V1_1_SCHEMA_HASH,
        }
    )
    if origin_context is not None:
        context_values.update(
            {
                "fallback_for_request_context_id": origin_context.request_context_id,
                "fallback_for_request_context_hash": origin_context.request_context_hash,
            }
        )
    context = seal(
        AgenticRetrievalRequestContextV1_1,
        "request_context_hash",
        context_values,
    )
    context.validate_budget(budget)
    return context, budget, origin_context, origin_budget


def coverage_receipt_v1_1(
    *,
    mode: str = "agentic_preview",
    status: str = "complete",
    observed: dict[str, int] | None = None,
    vector_search_requests: int = 0,
    embedding_calls: int = 0,
    embedding_items: int = 0,
) -> tuple[
    AgenticRetrievalCoverageReceiptV1_1,
    AgenticRetrievalRequestContextV1_1,
    QueryBudgetV1_1,
    AgenticRetrievalRequestContextV1_1 | None,
    QueryBudgetV1_1 | None,
]:
    fallback = mode == "direct_hybrid_prefilter"
    old_receipt = coverage_receipt(fallback=fallback)
    context, budget, origin_context, origin_budget = request_context_v1_1(
        mode,
        vector_search_requests=vector_search_requests,
        embedding_calls=embedding_calls,
        embedding_items=embedding_items,
    )
    receipt_values = old_receipt.model_dump(
        mode="python",
        exclude={"coverage_receipt_hash"},
        round_trip=True,
    )
    receipt_values["identity"] = (
        AgenticRetrievalCoverageReceiptIdentityV1_1.model_validate(
            {
                **receipt_values["identity"],
                "contract_version": "1.1.0",
            }
        )
    )
    receipt_values.update(
        {
            "request_context_id": context.request_context_id,
            "request_context_hash": context.request_context_hash,
            "coverage_status": status,
        }
    )
    observation_values = old_receipt.budget.model_dump(
        mode="python",
        round_trip=True,
    )
    for field_name in (
        "max_ontology_graph_scope_requests",
        "max_agentic_retrieval_invocations",
        "max_agentic_internal_subqueries",
        "max_agentic_source_calls",
        "max_direct_search_requests",
        "max_output_documents",
        "max_output_tokens",
        "max_output_bytes",
        "max_runtime_milliseconds",
        "max_graph_result_records",
        "max_search_result_records",
        "max_search_candidate_records",
        "max_vector_search_requests",
        "max_embedding_calls",
        "max_embedding_items",
        "max_retry_count",
        "max_retry_wait_milliseconds",
    ):
        observation_values[field_name] = getattr(budget, field_name)
    observation_values.update(
        {
            "observed_search_candidate_records": old_receipt.matched_document_count,
            "observed_vector_search_requests": 0,
            "observed_embedding_calls": 0,
            "observed_embedding_items": 0,
            "observed_retry_count": 0,
            "observed_retry_wait_milliseconds": 0,
        }
    )
    observation_values.update(observed or {})
    receipt_values["matched_document_count"] = observation_values[
        "observed_search_candidate_records"
    ]
    observed_to_ceiling = {
        "observed_ontology_graph_scope_requests": "max_ontology_graph_scope_requests",
        "observed_agentic_retrieval_invocations": "max_agentic_retrieval_invocations",
        "observed_agentic_internal_subqueries": "max_agentic_internal_subqueries",
        "observed_agentic_source_calls": "max_agentic_source_calls",
        "observed_direct_search_requests": "max_direct_search_requests",
        "observed_output_documents": "max_output_documents",
        "observed_output_tokens": "max_output_tokens",
        "observed_output_bytes": "max_output_bytes",
        "observed_runtime_milliseconds": "max_runtime_milliseconds",
        "observed_graph_result_records": "max_graph_result_records",
        "observed_search_result_records": "max_search_result_records",
        "observed_search_candidate_records": "max_search_candidate_records",
        "observed_vector_search_requests": "max_vector_search_requests",
        "observed_embedding_calls": "max_embedding_calls",
        "observed_embedding_items": "max_embedding_items",
        "observed_retry_count": "max_retry_count",
        "observed_retry_wait_milliseconds": "max_retry_wait_milliseconds",
    }
    exhausted = tuple(
        sorted(
            ceiling
            for observed_name, ceiling in observed_to_ceiling.items()
            if observation_values[observed_name] > observation_values[ceiling]
        )
    )
    observation_values["budget_exhausted_dimensions"] = exhausted

    if mode.startswith("agentic_"):
        planned_count = observation_values["observed_agentic_internal_subqueries"]
        receipt_values["planned_subqueries"] = tuple(
            PlannedSubqueryReceipt(
                subquery_id=f"subquery:{index}",
                subquery_hash=canonical_sha256({"subquery": index}),
                executed=True,
                knowledge_source_ids=(context.knowledge_source_id,),
                returned_reference_count=0,
            )
            for index in range(planned_count)
        )
        source_call_count = observation_values["observed_agentic_source_calls"]
    else:
        source_call_count = observation_values["observed_direct_search_requests"]
    existing_call = receipt_values["source_calls"][0]
    receipt_values["source_calls"] = tuple(
        SourceCallReceipt.model_validate(
            {
                **existing_call,
                "source_call_id": f"source-call:search:{index}",
                "request_hash": canonical_sha256({"source-call": index}),
            }
        )
        for index in range(source_call_count)
    )
    if exhausted:
        receipt_values.update(
            {
                "coverage_status": status,
                "failures": (
                    RetrievalFailure(
                        reason_code="retrieval_budget_exhausted",
                        remediation="downstream_abstention_required",
                    ),
                ),
            }
        )
    receipt_values["budget"] = CoverageBudgetObservationV1_1.model_validate(
        observation_values
    )
    receipt = seal(
        AgenticRetrievalCoverageReceiptV1_1,
        "coverage_receipt_hash",
        receipt_values,
    )
    receipt.validate_request_context(
        context,
        budget,
        originating_context=origin_context,
        originating_budget=origin_budget,
    )
    return receipt, context, budget, origin_context, origin_budget


def locator() -> ImmutableSourceLocator:
    values = {
        "locator_version": "1.0",
        "blob_uri": "https://storage.example.test/source/document.pdf",
        "blob_version_id": "version:document:1",
        "source_uri": None,
        "page": 4,
        "sheet": None,
        "slide": None,
        "section_path": ("section:maintenance",),
        "cell_range": None,
        "char_start": 10,
        "char_end": 42,
        "polygon": None,
        "sheet_zone": None,
        "tile_id": None,
        "coordinate_system": None,
        "transform": None,
        "native_layer_id": None,
        "native_object_id": None,
    }
    return ImmutableSourceLocator(**values, locator_hash=canonical_sha256(values))


def search_citation(
    *,
    entity_id: str = "entity:member-1",
    index: int = 1,
) -> SearchCitationEnvelope:
    quote = "Exact authorized evidence."
    source_locator = locator()
    source_identity = CanonicalIdentityEnvelope.model_validate(
        {
            **identity("c0.search_citation_envelope").model_dump(mode="python"),
            "asset_id": "asset:manual",
            "asset_version_id": "asset-version:manual:1",
            "source_file_id": "source-file:manual",
            "source_unit_id": "source-unit:paragraph-4",
            "content_hash": HASH_A,
            "immutable_locator": source_locator,
        }
    )
    values = {
        "identity": source_identity,
        "search_citation_envelope_id": f"search-citation:{index}",
        "search_reference_id": f"search-reference:{index}",
        "search_document_id": f"delivery-document:{index}",
        "original_document_name": "Original Service Manual.pdf",
        "source_id": "source:manual",
        "source_file_id": "source-file:manual",
        "source_unit_id": "source-unit:paragraph-4",
        "chunk_id": "chunk:paragraph-4",
        "evidence_span_ids": ("evidence:paragraph-4",),
        "canonical_scope_id": "scope:generic",
        "canonical_entity_ids": (entity_id,),
        "canonical_relationship_ids": ("relationship:has-member",),
        "canonical_assertion_ids": (f"assertion:{entity_id}",),
        "exact_authorized_quote": quote,
        "quote_hash": canonical_sha256(quote),
        "page": 4,
        "section_path": ("section:maintenance",),
        "immutable_locator": source_locator,
        "content_hash": HASH_A,
        "asset_hash": HASH_B,
        "access_policy_id": "access-policy:evidence",
        "access_policy_hash": HASH_C,
        "governed_asset_reference_id": "governed-asset:manual",
        "governed_asset_reference_hash": HASH_D,
    }
    return seal(SearchCitationEnvelope, "citation_hash", values)


def citation_presentation(
    *,
    transient_url: str | None = None,
) -> CitationPresentation:
    citation = search_citation()
    presentation_identity = CanonicalIdentityEnvelope.model_validate(
        {
            **citation.identity.model_dump(mode="python"),
            "contract_kind": "c0.citation_presentation",
        }
    )
    values = {
        "identity": presentation_identity,
        "citation_presentation_id": "citation-presentation:member-1",
        "search_citation_envelope_id": citation.search_citation_envelope_id,
        "search_citation_envelope_hash": citation.citation_hash,
        "original_document_name": citation.original_document_name,
        "source_id": citation.source_id,
        "source_file_id": citation.source_file_id,
        "source_unit_id": citation.source_unit_id,
        "chunk_id": citation.chunk_id,
        "evidence_span_ids": citation.evidence_span_ids,
        "exact_authorized_quote": citation.exact_authorized_quote,
        "quote_hash": citation.quote_hash,
        "page": citation.page,
        "section_path": citation.section_path,
        "immutable_locator": citation.immutable_locator,
        "content_hash": citation.content_hash,
        "asset_hash": citation.asset_hash,
        "governed_asset_reference_id": citation.governed_asset_reference_id,
        "governed_asset_reference_hash": citation.governed_asset_reference_hash,
    }
    presentation_hash = canonical_sha256(values)
    presentation = CitationPresentation(
        **values,
        presentation_hash=presentation_hash,
    )
    return (
        presentation.with_transient_authorized_asset_url(transient_url)
        if transient_url is not None
        else presentation
    )


@pytest.mark.contract
def test_exact_runtime_contract_registry_negotiation() -> None:
    expected = {
        "c0.query_budget": QueryBudget,
        "c0.ontology_scope_envelope": OntologyScopeEnvelope,
        "c0.resolved_ontology_scope": ResolvedOntologyScope,
        "c0.resolved_retrieval_scope": ResolvedRetrievalScope,
        "c0.agentic_retrieval_request_context": AgenticRetrievalRequestContext,
        "c0.agentic_retrieval_coverage_receipt": AgenticRetrievalCoverageReceipt,
        "c0.search_citation_envelope": SearchCitationEnvelope,
        "c0.citation_presentation": CitationPresentation,
    }
    assert set(expected) == RUNTIME_KINDS
    for kind, model in expected.items():
        assert negotiate_contract(kind, "1.0.0") is model
        with pytest.raises(ValueError, match="not registered"):
            negotiate_contract(kind, "1.0.1")
        with pytest.raises(UnknownContractMajorError):
            negotiate_contract(kind, "2.0.0")

    reference = manifest_reference()
    with pytest.raises(ValidationError):
        RequiredMemberManifestReference.model_validate(
            {**reference.model_dump(mode="json"), "schema_hash": HASH_C}
        )


@pytest.mark.contract
def test_runtime_contracts_are_strict_frozen_hash_bound_and_synthesis_free() -> None:
    contracts = (
        (query_budget(), "budget_hash"),
        (ontology_scope(), "scope_hash"),
        (resolved_ontology_scope(), "resolved_scope_hash"),
        (resolved_retrieval_scope(), "retrieval_scope_hash"),
        (request_context()[0], "request_context_hash"),
        (coverage_receipt(), "coverage_receipt_hash"),
        (search_citation(), "citation_hash"),
        (citation_presentation(), "presentation_hash"),
    )
    for contract, hash_field in contracts:
        payload = contract.model_dump(mode="json")
        payload["unknown_field"] = "rejected"
        with pytest.raises(ValidationError, match="Extra inputs"):
            type(contract).model_validate(payload)
        with pytest.raises(ValidationError):
            setattr(contract, hash_field, HASH_A)
        with pytest.raises(ValidationError, match=hash_field):
            contract.model_copy(update={hash_field: HASH_A})
        schema_text = json.dumps(type(contract).model_json_schema()).casefold()
        assert "synthesis" not in schema_text
        assert "answer_evidence" not in schema_text
        assert "claim_citation" not in schema_text
        assert "retry_policy" not in schema_text


@pytest.mark.contract
def test_scope_modes_exact_narrow_expand_and_explicit_members() -> None:
    exact = ontology_scope(root_type_ids=("type:component", "type:subcomponent"))
    narrow = ontology_scope(
        relative_change="narrow",
        parent=exact,
        root_type_ids=("type:subcomponent",),
    )
    narrow.validate_relative_to(exact)
    expanded = ontology_scope(
        relative_change="expand",
        parent=narrow,
        root_type_ids=("type:component", "type:subcomponent"),
    )
    expanded.validate_relative_to(narrow)
    explicit = ontology_scope(
        "explicit_member_set",
        member_ids=("entity:component-1", "entity:component-2"),
    )
    assert explicit.canonical_root_semantic_type_ids == ()
    assert explicit.hierarchy_expansion_depth == 0

    broadened_payload = narrow.model_dump(mode="python")
    broadened_payload["include_canonical_ids"] = ("entity:outside-parent",)
    broadened_payload["scope_hash"] = canonical_sha256(
        {
            key: value
            for key, value in broadened_payload.items()
            if key != "scope_hash"
        }
    )
    broadened = OntologyScopeEnvelope.model_validate(broadened_payload)
    with pytest.raises(ValueError, match="cannot add canonical authority"):
        broadened.validate_relative_to(exact)

    traversal_payload = narrow.model_dump(mode="python")
    traversal_payload["relationship_k"] = 4
    traversal_payload["relationship_k_4_justification"] = (
        "Reviewed bounded path required by authority."
    )
    traversal_payload["scope_hash"] = canonical_sha256(
        {
            key: value
            for key, value in traversal_payload.items()
            if key != "scope_hash"
        }
    )
    broader_traversal = OntologyScopeEnvelope.model_validate(traversal_payload)
    with pytest.raises(ValueError, match="cannot increase traversal"):
        broader_traversal.validate_relative_to(exact)

    invalid = exact.model_dump(mode="python")
    invalid["relative_change"] = "narrow"
    invalid["scope_hash"] = canonical_sha256(
        {key: value for key, value in invalid.items() if key != "scope_hash"}
    )
    with pytest.raises(ValidationError, match="require parent"):
        OntologyScopeEnvelope.model_validate(invalid)


@pytest.mark.contract
def test_scope_rejects_names_hierarchy_k_confusion_and_bad_k() -> None:
    scope = ontology_scope()
    payload = scope.model_dump(mode="python")
    payload["display_name"] = "Component"
    with pytest.raises(ValidationError, match="Extra inputs"):
        OntologyScopeEnvelope.model_validate(payload)

    with pytest.raises(ValidationError, match="depth zero"):
        ontology_scope("exact_type", hierarchy_depth=3)
    with pytest.raises(ValidationError, match="justification"):
        query_budget(relationship_k=4)
    justified = query_budget(
        relationship_k=4,
        justification="Reviewed bounded path required by authority.",
    )
    assert justified.relationship_k == 4
    with pytest.raises(ValidationError):
        query_budget(relationship_k=5)
    payload = query_budget().model_dump(mode="python")
    payload["hierarchy_expansion_depth"] = 9
    payload["budget_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "budget_hash"}
    )
    with pytest.raises(ValidationError, match="policy requires depth zero"):
        QueryBudget.model_validate(payload)


@pytest.mark.contract
def test_reclassification_keeps_entity_ids_and_changes_assertion_evidence() -> None:
    before = resolved_ontology_scope(assertion_version_offset=0)
    after = resolved_ontology_scope(assertion_version_offset=4)
    assert [item.canonical_entity_id for item in before.members] == [
        item.canonical_entity_id for item in after.members
    ]
    assert before.canonical_key_set_hash != after.canonical_key_set_hash
    assert before.resolved_scope_hash != after.resolved_scope_hash
    assert [item.type_assertion_version for item in before.members] != [
        item.type_assertion_version for item in after.members
    ]


@pytest.mark.contract
def test_descendant_and_ancestor_expansion_traces_are_deterministic() -> None:
    descendants = resolved_ontology_scope(mode="descendants")
    descendants.validate_envelope(ontology_scope("descendants"))
    assert descendants.resolved_descendant_type_ids == ("type:subcomponent",)
    assert descendants.resolved_ancestor_type_ids == ()
    assert descendants.expansion_trace[0].edge_kind == "child"

    ancestors = resolved_ontology_scope(mode="ancestors_context")
    ancestors.validate_envelope(ontology_scope("ancestors_context"))
    assert ancestors.resolved_ancestor_type_ids == ("type:asset",)
    assert ancestors.resolved_descendant_type_ids == ()
    assert ancestors.expansion_trace[0].edge_kind == "ancestor"

    payload = descendants.model_dump(mode="python")
    payload["expansion_trace"] = ()
    payload["resolved_scope_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "resolved_scope_hash"}
    )
    with pytest.raises(ValidationError, match="requires deterministic expansion trace"):
        ResolvedOntologyScope.model_validate(payload)

    payload = descendants.model_dump(mode="python")
    payload["resolved_exact_type_ids"] = (
        *descendants.resolved_exact_type_ids,
        "type:untraced",
    )
    payload["resolved_scope_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "resolved_scope_hash"}
    )
    with pytest.raises(ValidationError, match="inconsistent resolved type sets"):
        ResolvedOntologyScope.model_validate(payload)


@pytest.mark.contract
def test_scope_authority_staleness_collision_and_orphan_fail_closed() -> None:
    scope = resolved_retrieval_scope()
    scope.validate_authorities(
        canonical_key_set_hash=scope.canonical_key_set_hash,
        acl_scope_hash=scope.acl_scope_hash,
        asserted_publication_hash=scope.asserted_publication_hash,
        semantic_projection_hash=scope.semantic_projection_hash,
        publication_crosswalk_hash=scope.publication_crosswalk_hash,
        type_hierarchy_hash=scope.type_hierarchy_hash,
        type_closure_hash=scope.type_closure_hash,
        graph_model_hash=scope.graph_model_hash,
        search_index_fingerprint=scope.search_index_fingerprint,
    )
    with pytest.raises(ValueError, match="stale publication crosswalk"):
        scope.validate_authorities(
            canonical_key_set_hash=scope.canonical_key_set_hash,
            acl_scope_hash=scope.acl_scope_hash,
            asserted_publication_hash=scope.asserted_publication_hash,
            semantic_projection_hash=scope.semantic_projection_hash,
            publication_crosswalk_hash=HASH_F,
            type_hierarchy_hash=scope.type_hierarchy_hash,
            type_closure_hash=scope.type_closure_hash,
            graph_model_hash=scope.graph_model_hash,
            search_index_fingerprint=scope.search_index_fingerprint,
        )
    with pytest.raises(ValueError, match="stale Graph model"):
        scope.validate_authorities(
            canonical_key_set_hash=scope.canonical_key_set_hash,
            acl_scope_hash=scope.acl_scope_hash,
            asserted_publication_hash=scope.asserted_publication_hash,
            semantic_projection_hash=scope.semantic_projection_hash,
            publication_crosswalk_hash=scope.publication_crosswalk_hash,
            type_hierarchy_hash=scope.type_hierarchy_hash,
            type_closure_hash=scope.type_closure_hash,
            graph_model_hash=HASH_E,
            search_index_fingerprint=scope.search_index_fingerprint,
        )

    payload = scope.model_dump(mode="python")
    payload["canonical_member_ids"] = (
        scope.canonical_member_ids[0],
        scope.canonical_member_ids[0],
    )
    payload["retrieval_scope_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "retrieval_scope_hash"}
    )
    with pytest.raises(ValidationError):
        ResolvedRetrievalScope.model_validate(payload)


@pytest.mark.contract
def test_resolved_scope_receipts_hierarchy_and_exclusions_fail_closed() -> None:
    resolved = resolved_ontology_scope()
    payload = resolved.model_dump(mode="python")
    payload["authoritative_receipts"] = (
        resolved.authoritative_receipts[0],
        resolved.authoritative_receipts[0],
    )
    payload["resolved_scope_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "resolved_scope_hash"}
    )
    with pytest.raises(ValidationError, match="receipt references"):
        ResolvedOntologyScope.model_validate(payload)

    payload = resolved.model_dump(mode="python")
    payload["type_assertions"] = (
        *payload["type_assertions"],
        payload["type_assertions"][0],
    )
    payload["resolved_scope_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "resolved_scope_hash"}
    )
    with pytest.raises(ValidationError, match="assertion entity IDs must be unique"):
        ResolvedOntologyScope.model_validate(payload)

    envelope = ontology_scope()
    payload = resolved.model_dump(mode="python")
    payload["requested_member_role_ids"] = ()
    payload["resolved_scope_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "resolved_scope_hash"}
    )
    with pytest.raises(ValidationError, match="member role exceeds"):
        ResolvedOntologyScope.model_validate(payload)

    envelope_payload = envelope.model_dump(mode="python")
    envelope_payload["exclude_canonical_ids"] = (
        resolved.members[0].canonical_entity_id,
    )
    envelope_payload["scope_hash"] = canonical_sha256(
        {
            key: value
            for key, value in envelope_payload.items()
            if key != "scope_hash"
        }
    )
    excluding_envelope = OntologyScopeEnvelope.model_validate(envelope_payload)
    payload = resolved.model_dump(mode="python")
    payload["ontology_scope_envelope_hash"] = excluding_envelope.scope_hash
    payload["requested_exclude_canonical_ids"] = (
        resolved.members[0].canonical_entity_id,
    )
    payload["excluded_canonical_ids"] = (resolved.members[0].canonical_entity_id,)
    payload["resolved_scope_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "resolved_scope_hash"}
    )
    with pytest.raises(ValidationError, match="cannot remain resolved members"):
        ResolvedOntologyScope.model_validate(payload)

    payload = resolved.model_dump(mode="python")
    payload["adjacency_edges"] = (
        AdjacencyEdge(
            from_canonical_entity_id="entity:outside-scope",
            to_canonical_entity_id=resolved.members[0].canonical_entity_id,
            relationship_semantic_id=resolved.membership_relationship_semantic_id,
            relationship_assertion_id=resolved.assertion_ids[0],
            evidence_span_ids=(resolved.evidence_span_ids[0],),
        ),
    )
    payload["resolved_scope_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "resolved_scope_hash"}
    )
    with pytest.raises(ValidationError, match="endpoint is outside"):
        ResolvedOntologyScope.model_validate(payload)

    payload = resolved.model_dump(mode="python")
    payload["hierarchy_expansion_policy"] = "sealed_descendants"
    payload["hierarchy_expansion_depth"] = 2
    payload["resolved_scope_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "resolved_scope_hash"}
    )
    with pytest.raises(ValidationError, match="mode and expansion policy"):
        ResolvedOntologyScope.model_validate(payload)

    payload = resolved.model_dump(mode="python")
    policy = payload["collection_policy"]
    policy["expected_cardinality"] = None
    policy["required_unique_member_count"] = None
    policy["minimum_cardinality"] = len(resolved.members) + 1
    policy["maximum_cardinality"] = None
    policy["policy_hash"] = canonical_sha256(
        {key: value for key, value in policy.items() if key != "policy_hash"}
    )
    payload["resolved_scope_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "resolved_scope_hash"}
    )
    with pytest.raises(ValidationError, match="below minimum"):
        ResolvedOntologyScope.model_validate(payload)

    retrieval = resolved_retrieval_scope()
    payload = retrieval.model_dump(mode="python")
    payload["exclude_canonical_ids"] = (retrieval.canonical_member_ids[0],)
    payload["retrieval_scope_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "retrieval_scope_hash"}
    )
    with pytest.raises(ValidationError, match="cannot remain validated members"):
        ResolvedRetrievalScope.model_validate(payload)

    payload = retrieval.model_dump(mode="python")
    payload["hierarchy_expansion_policy"] = "invented-policy"
    payload["retrieval_scope_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "retrieval_scope_hash"}
    )
    with pytest.raises(ValidationError):
        ResolvedRetrievalScope.model_validate(payload)

    payload = retrieval.model_dump(mode="python")
    payload["graph_scope_filter"]["canonical_relationship_ids"] = (
        "relationship:has-member",
        "relationship:outside-authority",
    )
    payload["graph_scope_filter"]["filter_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload["graph_scope_filter"].items()
            if key != "filter_hash"
        }
    )
    payload["retrieval_scope_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "retrieval_scope_hash"}
    )
    with pytest.raises(ValidationError, match="relationships must equal"):
        ResolvedRetrievalScope.model_validate(payload)


@pytest.mark.contract
def test_resolved_scope_is_anchored_to_required_member_manifest_v1_1() -> None:
    manifest = required_member_manifest()
    resolved = resolved_ontology_scope()
    resolved.required_member_manifest.validate_manifest(manifest)
    resolved.validate_required_member_manifest(manifest)

    payload = resolved.model_dump(mode="python")
    payload["collection_policy"]["completeness_rule_ids"] = (
        "runtime:self-attested-completeness",
    )
    payload["collection_policy"]["policy_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload["collection_policy"].items()
            if key != "policy_hash"
        }
    )
    payload["resolved_scope_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "resolved_scope_hash"}
    )
    self_attested = ResolvedOntologyScope.model_validate(payload)
    with pytest.raises(ValueError, match="completeness authority"):
        self_attested.validate_required_member_manifest(manifest)

    unrelated_manifest = required_member_manifest(GENERIC_MEMBER_IDS[:-1])
    with pytest.raises(ValueError, match="manifest hash"):
        resolved.validate_required_member_manifest(unrelated_manifest)


@pytest.mark.contract
def test_preview_narrowing_and_direct_prefilter_contracts() -> None:
    preview, preview_budget = request_context("agentic_preview")
    preview.validate_budget(preview_budget)
    assert preview.capability.api_version == "2026-05-01-preview"
    assert preview.effective_filter_operator == "AND"
    assert set(preview.filter_add_on.canonical_entity_ids) < set(
        preview.graph_scope_filter.canonical_entity_ids
    )

    payload = preview.model_dump(mode="python")
    payload["filter_add_on"] = safe_filter(
        (*preview.canonical_entity_ids, "entity:outside-scope")
    )
    payload["request_context_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "request_context_hash"}
    )
    with pytest.raises(ValidationError, match="would broaden"):
        AgenticRetrievalRequestContext.model_validate(payload)

    payload = preview.model_dump(mode="python")
    payload["exact_type_ids"] = (
        *preview.exact_type_ids,
        "type:not-in-base-filter",
    )
    payload["filter_add_on"] = safe_filter(("entity:component-1",)).model_copy(
        update={
            "exact_type_ids": ("type:not-in-base-filter",),
            "filter_hash": canonical_sha256(
                {
                    "canonical_entity_ids": ["entity:component-1"],
                    "exact_type_ids": ["type:not-in-base-filter"],
                    "ancestor_type_ids": [],
                    "canonical_relationship_ids": ["relationship:has-member"],
                    "asserted_publication_only": True,
                }
            ),
        }
    )
    payload["request_context_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "request_context_hash"}
    )
    with pytest.raises(ValidationError, match="request exact types"):
        AgenticRetrievalRequestContext.model_validate(payload)

    direct, direct_budget = request_context("direct_hybrid_prefilter")
    direct.validate_budget(direct_budget)
    assert direct.vector_filter_mode == "preFilter"
    assert direct.filter_add_on is None
    assert direct.capability.preview_feature_enabled is False

    budget_payload = preview_budget.model_dump(mode="python")
    budget_payload["identity"]["project_id"] = "project:conflicting"
    budget_payload["budget_hash"] = canonical_sha256(
        {key: value for key, value in budget_payload.items() if key != "budget_hash"}
    )
    conflicting_budget = QueryBudget.model_validate(budget_payload)
    with pytest.raises(ValueError, match="identity authority"):
        preview.validate_budget(conflicting_budget)


@pytest.mark.contract
def test_preview_fallback_preserves_origin_and_uses_direct_budget() -> None:
    origin, origin_budget = request_context("agentic_preview")
    fallback_context, fallback_budget = request_context(
        "direct_hybrid_prefilter",
        fallback_for=origin,
    )
    fallback_context.validate_fallback_origin(origin)
    receipt = coverage_receipt(fallback=True)
    receipt.validate_request_context(
        fallback_context,
        fallback_budget,
        originating_context=origin,
        originating_budget=origin_budget,
    )
    assert receipt.fallback_used is True
    assert receipt.retrieval_mode == "direct_hybrid_prefilter"
    assert fallback_context.vector_filter_mode == "preFilter"

    with pytest.raises(ValueError, match="originating request context"):
        receipt.validate_request_context(fallback_context, fallback_budget)

    unrelated_origin, unrelated_budget = request_context(
        "agentic_preview",
        entity_ids=GENERIC_MEMBER_IDS[:-1],
    )
    with pytest.raises(ValueError, match="does not reference its origin"):
        receipt.validate_request_context(
            fallback_context,
            fallback_budget,
            originating_context=unrelated_origin,
            originating_budget=unrelated_budget,
        )


@pytest.mark.contract
def test_coverage_complete_generic_10_of_10_and_partial_conditions() -> None:
    complete = coverage_receipt(count=10)
    assert complete.coverage_status == "complete"
    assert len(complete.required_canonical_ids) == 10
    assert complete.required_canonical_ids == complete.returned_canonical_ids
    assert complete.coverage_semantics == "bounded_maximal"

    partial = coverage_receipt(count=10, status="partial", missing=1)
    assert len(partial.missing_canonical_ids) == 1
    assert partial.coverage_status == "partial"

    truncated = coverage_receipt(count=10, status="partial", truncated=True)
    assert truncated.output_truncated is True
    assert truncated.budget.budget_exhausted_dimensions

    payload = partial.model_dump(mode="python")
    payload["coverage_status"] = "complete"
    payload["coverage_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "coverage_receipt_hash"
        }
    )
    with pytest.raises(ValidationError, match="exact bounded structural"):
        AgenticRetrievalCoverageReceipt.model_validate(payload)


@pytest.mark.contract
def test_complete_coverage_rejects_hidden_activity_and_budget_gaps() -> None:
    complete = coverage_receipt()
    payload = complete.model_dump(mode="python")
    payload["activity"] = (
        ActivityReceipt(
            activity_id="activity:hidden-warning",
            activity_kind="retrieval",
            activity_hash=HASH_A,
            warning_codes=("warning:hidden-truncation",),
            truncated=True,
        ),
    )
    payload["coverage_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "coverage_receipt_hash"
        }
    )
    with pytest.raises(ValidationError, match="exact bounded structural"):
        AgenticRetrievalCoverageReceipt.model_validate(payload)

    payload = complete.model_dump(mode="python")
    payload["planned_subqueries"] = (
        PlannedSubqueryReceipt(
            subquery_id="subquery:not-executed",
            subquery_hash=HASH_B,
            executed=False,
            knowledge_source_ids=("knowledge-source:generic",),
            returned_reference_count=0,
        ),
    )
    payload["coverage_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "coverage_receipt_hash"
        }
    )
    with pytest.raises(ValidationError, match="observed internal subqueries"):
        AgenticRetrievalCoverageReceipt.model_validate(payload)

    budget_payload = complete.budget.model_dump(mode="python")
    budget_payload["observed_output_documents"] = (
        complete.budget.max_output_documents + 1
    )
    with pytest.raises(ValidationError, match="exceeds undeclared"):
        CoverageBudgetObservation.model_validate(budget_payload)

    budget_payload = complete.budget.model_dump(mode="python")
    budget_payload["observed_direct_search_requests"] = 1
    with pytest.raises(ValidationError, match="max_direct_search_requests"):
        CoverageBudgetObservation.model_validate(budget_payload)

    with pytest.raises(ValidationError, match="requires response hash"):
        SourceCallReceipt(
            source_call_id="source-call:unsealed",
            knowledge_source_id="knowledge-source:generic",
            request_hash=HASH_A,
            response_hash=None,
            status="succeeded",
            matched_count=1,
            returned_count=1,
        )


@pytest.mark.contract
def test_complete_coverage_is_anchored_to_scope_context_and_budget() -> None:
    envelope = ontology_scope()
    resolved = resolved_ontology_scope()
    retrieval = resolved_retrieval_scope()
    context, budget = request_context()
    receipt = coverage_receipt()
    resolved.validate_envelope(envelope)
    retrieval.validate_resolved_scope(resolved)
    context.validate_scope(retrieval)
    receipt.validate_request_context(context, budget)

    payload = context.model_dump(mode="python")
    payload["expected_member_collection_hash"] = HASH_F
    payload["request_context_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "request_context_hash"}
    )
    changed_context = AgenticRetrievalRequestContext.model_validate(payload)
    with pytest.raises(ValueError, match="request context hash"):
        receipt.validate_request_context(changed_context, budget)

    scope_payload = retrieval.model_dump(mode="python")
    scope_payload["resolution_status"] = "invalid"
    scope_payload["findings"] = (
        {
            "reason_code": "scope_unauthorized",
            "remediation": "downstream_abstention_required",
            "canonical_id": retrieval.canonical_scope_id,
            "authority_hash": retrieval.acl_scope_hash,
        },
    )
    scope_payload["retrieval_scope_hash"] = canonical_sha256(
        {
            key: value
            for key, value in scope_payload.items()
            if key != "retrieval_scope_hash"
        }
    )
    invalid_scope = ResolvedRetrievalScope.model_validate(scope_payload)
    with pytest.raises(ValueError, match="invalid and must fail closed"):
        context.validate_scope(invalid_scope)

    receipt_payload = receipt.model_dump(mode="python")
    receipt_payload["required_role_ids"] = ()
    receipt_payload["returned_role_ids"] = ()
    receipt_payload["coverage_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in receipt_payload.items()
            if key != "coverage_receipt_hash"
        }
    )
    with pytest.raises(ValidationError, match="returned role IDs"):
        AgenticRetrievalCoverageReceipt.model_validate(receipt_payload)

    receipt_payload = receipt.model_dump(mode="python")
    receipt_payload["required_group_hash"] = HASH_A
    receipt_payload["returned_group_hash"] = HASH_A
    receipt_payload["coverage_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in receipt_payload.items()
            if key != "coverage_receipt_hash"
        }
    )
    with pytest.raises(ValidationError, match="returned group hash"):
        AgenticRetrievalCoverageReceipt.model_validate(receipt_payload)

    receipt_payload = receipt.model_dump(mode="python")
    receipt_payload["returned_collection_hash"] = HASH_A
    receipt_payload["coverage_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in receipt_payload.items()
            if key != "coverage_receipt_hash"
        }
    )
    with pytest.raises(ValidationError, match="returned collection hash"):
        AgenticRetrievalCoverageReceipt.model_validate(receipt_payload)

    citations = tuple(
        search_citation(entity_id=entity_id, index=index)
        for index, entity_id in enumerate(receipt.returned_canonical_ids)
    )
    receipt_payload = receipt.model_dump(mode="python")
    receipt_payload["citation_mappings"][0][
        "search_citation_envelope_hash"
    ] = HASH_A
    receipt_payload["coverage_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in receipt_payload.items()
            if key != "coverage_receipt_hash"
        }
    )
    wrong_citation_hash = AgenticRetrievalCoverageReceipt.model_validate(
        receipt_payload
    )
    with pytest.raises(ValueError, match="citation hash"):
        wrong_citation_hash.validate_citations(citations)

    receipt_payload = receipt.model_dump(mode="python")
    receipt_payload["canonical_citation_count"] = 999
    receipt_payload["coverage_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in receipt_payload.items()
            if key != "coverage_receipt_hash"
        }
    )
    with pytest.raises(ValidationError, match="citation count"):
        AgenticRetrievalCoverageReceipt.model_validate(receipt_payload)

    receipt_payload = receipt.model_dump(mode="python")
    receipt_payload["source_calls"] = ()
    receipt_payload["coverage_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in receipt_payload.items()
            if key != "coverage_receipt_hash"
        }
    )
    with pytest.raises(ValidationError, match="observed retrieval request counts"):
        AgenticRetrievalCoverageReceipt.model_validate(receipt_payload)

    receipt_payload = receipt.model_dump(mode="python")
    receipt_payload["matched_document_count"] = 0
    receipt_payload["returned_document_count"] = 0
    receipt_payload["reference_count"] = 0
    receipt_payload["budget"]["observed_output_documents"] = 0
    receipt_payload["budget"]["observed_search_result_records"] = 0
    receipt_payload["source_calls"] = tuple(
        {
            **call,
            "matched_count": 0,
            "returned_count": 0,
        }
        for call in receipt_payload["source_calls"]
    )
    receipt_payload["coverage_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in receipt_payload.items()
            if key != "coverage_receipt_hash"
        }
    )
    with pytest.raises(ValidationError, match="citation mappings exceed"):
        AgenticRetrievalCoverageReceipt.model_validate(receipt_payload)


@pytest.mark.contract
def test_coverage_duplicate_orphan_and_invalid_are_typed() -> None:
    partial = coverage_receipt(status="partial", missing=1)
    payload = partial.model_dump(mode="python")
    payload["coverage_status"] = "invalid"
    payload["orphan_canonical_ids"] = ("entity:search-orphan",)
    payload["failures"] = (
        RetrievalFailure(
            reason_code="search_orphan_key",
            remediation="operator_repair_required",
            canonical_ids=("entity:search-orphan",),
        ),
    )
    payload["coverage_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "coverage_receipt_hash"
        }
    )
    invalid = AgenticRetrievalCoverageReceipt.model_validate(payload)
    assert invalid.coverage_status == "invalid"
    assert invalid.failures[0].reason_code == "search_orphan_key"

    payload = partial.model_dump(mode="python")
    payload["duplicate_canonical_ids"] = (partial.returned_canonical_ids[0],)
    payload["coverage_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "coverage_receipt_hash"
        }
    )
    duplicate = AgenticRetrievalCoverageReceipt.model_validate(payload)
    assert duplicate.duplicate_canonical_ids


@pytest.mark.contract
def test_citation_exactness_authorization_and_transient_url_exclusion() -> None:
    citation = search_citation()
    presentation = citation_presentation()
    presentation.validate_citation(citation)
    with_url = citation_presentation(
        transient_url="https://delivery.example.test/asset?sig=short-lived"
    )
    assert with_url.presentation_hash == presentation.presentation_hash
    assert canonical_json(with_url) == canonical_json(presentation)
    assert "transient_authorized_asset_url" not in with_url.model_dump(mode="json")
    assert (
        "transient_authorized_asset_url"
        not in CitationPresentation.model_json_schema()["properties"]
    )
    assert "short-lived" not in repr(with_url)
    persisted_payload = presentation.model_dump(mode="python")
    persisted_payload["transient_authorized_asset_url"] = (
        "https://delivery.example.test/persisted"
    )
    with pytest.raises(ValidationError, match="Extra inputs"):
        CitationPresentation.model_validate(persisted_payload)

    with pytest.raises(ValidationError, match="quote_hash"):
        presentation.model_copy(
            update={"exact_authorized_quote": "A different quote."}
        )

    payload = presentation.model_dump(mode="python")
    payload["asset_hash"] = HASH_F
    payload["presentation_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "presentation_hash"}
    )
    changed_asset = CitationPresentation.model_validate(payload)
    with pytest.raises(ValueError, match="asset hash"):
        changed_asset.validate_citation(citation)

    payload = presentation.model_dump(mode="python")
    payload["identity"]["project_id"] = "project:conflicting"
    payload["presentation_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "presentation_hash"}
    )
    conflicting_identity = CitationPresentation.model_validate(payload)
    with pytest.raises(ValueError, match="identity authority"):
        conflicting_identity.validate_citation(citation)

    payload = citation.model_dump(mode="python")
    payload["identity"]["source_file_id"] = "source-file:conflicting"
    payload["citation_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "citation_hash"}
    )
    with pytest.raises(ValidationError, match="source file ID"):
        SearchCitationEnvelope.model_validate(payload)

    payload = presentation.model_dump(mode="python")
    payload["identity"]["source_unit_id"] = "source-unit:conflicting"
    payload["presentation_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "presentation_hash"}
    )
    with pytest.raises(ValidationError, match="source unit ID"):
        CitationPresentation.model_validate(payload)


@pytest.mark.contract
def test_citation_accepts_source_derived_identity_locator() -> None:
    citation = search_citation()
    source_identity = citation.identity.model_copy(
        update={
            "asset_id": "asset:manual",
            "asset_version_id": "asset-version:manual:1",
            "source_file_id": citation.source_file_id,
            "source_unit_id": citation.source_unit_id,
            "content_hash": citation.content_hash,
            "immutable_locator": citation.immutable_locator,
        }
    )
    payload = citation.model_dump(mode="python")
    payload["identity"] = source_identity
    payload["citation_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "citation_hash"}
    )
    source_citation = SearchCitationEnvelope.model_validate(payload)

    presentation = citation_presentation()
    payload = presentation.model_dump(mode="python")
    payload["identity"] = source_identity.model_copy(
        update={"contract_kind": "c0.citation_presentation"}
    )
    payload["search_citation_envelope_hash"] = source_citation.citation_hash
    payload["presentation_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "presentation_hash"}
    )
    source_presentation = CitationPresentation.model_validate(payload)
    source_presentation.validate_citation(source_citation)


@pytest.mark.contract
def test_citation_preserves_legitimate_urls_but_rejects_secret_text() -> None:
    citation = search_citation()
    payload = citation.model_dump(mode="python")
    quote = "See https://contoso.example.test/policy for the maintenance window."
    payload["exact_authorized_quote"] = quote
    payload["quote_hash"] = canonical_sha256(quote)
    payload["citation_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "citation_hash"}
    )
    assert SearchCitationEnvelope.model_validate(payload).exact_authorized_quote == quote

    payload["exact_authorized_quote"] = "Use bearer secret-token-value."
    payload["quote_hash"] = canonical_sha256(payload["exact_authorized_quote"])
    payload["citation_hash"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "citation_hash"}
    )
    with pytest.raises(ValidationError, match="credentials"):
        SearchCitationEnvelope.model_validate(payload)

    budget = query_budget(
        relationship_k=4,
        justification="Approved at https://reviews.example.test/ticket/42.",
    )
    assert "https://" in budget.relationship_k_4_justification


@pytest.mark.contract
@pytest.mark.parametrize(
    ("factory", "field", "value"),
    [
        (ontology_scope, "agent_policy_id", "https://unsafe.example.test/policy"),
        (request_context, "knowledge_base_id", "knowledge-base?token=secret"),
        (search_citation, "search_document_id", "https://unsafe.example.test/doc"),
    ],
)
def test_runtime_contracts_reject_secrets_and_authority_urls(
    factory: Any,
    field: str,
    value: str,
) -> None:
    produced = factory()
    model = produced[0] if isinstance(produced, tuple) else produced
    payload = model.model_dump(mode="python")
    payload[field] = value
    hash_field = {
        OntologyScopeEnvelope: "scope_hash",
        AgenticRetrievalRequestContext: "request_context_hash",
        SearchCitationEnvelope: "citation_hash",
    }[type(model)]
    payload[hash_field] = canonical_sha256(
        {key: item for key, item in payload.items() if key != hash_field}
    )
    with pytest.raises(ValidationError, match="credentials|canonical ID"):
        type(model).model_validate(payload)


@pytest.mark.contract
def test_runtime_fixtures_round_trip_and_match_goldens() -> None:
    fixture_names = (
        "query-budget-generic",
        "ontology-scope-generic",
        "ontology-scope-descendants-generic",
        "ontology-scope-ancestors-generic",
        "resolved-ontology-scope-generic",
        "resolved-ontology-scope-descendants-generic",
        "resolved-ontology-scope-ancestors-generic",
        "resolved-retrieval-scope-generic",
        "agentic-request-context-generic",
        "agentic-coverage-receipt-generic",
        "search-citation-envelope-generic",
        "citation-presentation-generic",
    )
    for name in fixture_names:
        parsed = parse_contract(
            (FIXTURES / "valid" / f"{name}.json").read_text(encoding="utf-8")
        )
        expected_json = (
            FIXTURES / "golden" / f"{name}.canonical.json"
        ).read_text(encoding="utf-8").rstrip("\n")
        expected_hash = (
            FIXTURES / "golden" / f"{name}.sha256"
        ).read_text(encoding="utf-8").strip()
        assert canonical_json(parsed) == expected_json
        assert canonical_sha256(parsed) == expected_hash
        assert parse_contract(expected_json) == parsed


@pytest.mark.contract
def test_generated_runtime_schemas_preserve_every_prior_schema_hash(
    tmp_path: Path,
) -> None:
    schema_dir = (
        Path(__file__).parents[2]
        / "src"
        / "fabric_kg_builder"
        / "contracts"
        / "schemas"
    )
    previous = json.loads((schema_dir / "registry.json").read_text(encoding="utf-8"))
    previous_hashes = {
        (item["contract_kind"], item["contract_version"]): item["schema_hash"]
        for item in previous["schemas"]
        if item["contract_version"] == "1.0.0"
    }
    generated = write_registered_schemas(tmp_path)
    current = json.loads(
        (tmp_path / "registry.json").read_text(encoding="utf-8")
    )
    current_hashes = {
        (item["contract_kind"], item["contract_version"]): item["schema_hash"]
        for item in current["schemas"]
    }
    for key, digest in previous_hashes.items():
        assert current_hashes[key] == digest
        filename = f"{key[0].replace('.', '-')}-{key[1]}.schema.json"
        assert (tmp_path / filename).read_bytes() == (schema_dir / filename).read_bytes()
    assert current_hashes[
        ("c0.required_member_manifest", "1.1.0")
    ] == REQUIRED_MEMBER_MANIFEST_V1_1_SCHEMA_HASH
    assert {
        kind
        for kind, version in current_hashes
        if kind in RUNTIME_KINDS and version == "1.0.0"
    } == RUNTIME_KINDS
    assert generated["c0.query_budget"] == generated["c0.query_budget@1.0.0"]
    assert current_hashes[("c0.query_budget", "1.1.0")] == (
        "2d744838296209d78da2e2c8b7df7ab5f030af400d45a3d04d62b7d763f92b52"
    )
    assert current_hashes[
        ("c0.agentic_retrieval_request_context", "1.1.0")
    ] == "dfed8fe3449b824cffa1570c278d3e476712987cb8d2e8cb2c903ac480bd8868"
    assert current_hashes[
        ("c0.agentic_retrieval_coverage_receipt", "1.1.0")
    ] == "92d39c05d33a360bd542386af022a382ba18788efe4a1fe5b0728c42b5aec652"


@pytest.mark.contract
def test_runtime_1_1_versions_are_exact_and_cross_version_isolated() -> None:
    assert negotiate_contract("c0.query_budget", "1.1.0") is QueryBudgetV1_1
    assert (
        negotiate_contract("c0.agentic_retrieval_request_context", "1.1.0")
        is AgenticRetrievalRequestContextV1_1
    )
    assert (
        negotiate_contract("c0.agentic_retrieval_coverage_receipt", "1.1.0")
        is AgenticRetrievalCoverageReceiptV1_1
    )
    receipt, context, budget, _, _ = coverage_receipt_v1_1()
    old_context, old_budget = request_context()
    old_receipt = coverage_receipt()
    with pytest.raises(ValueError, match="1.0 request context"):
        old_context.validate_budget(budget)
    with pytest.raises(ValueError, match="1.1 request context"):
        context.validate_budget(old_budget)
    with pytest.raises(ValueError, match="1.0 coverage receipt"):
        old_receipt.validate_request_context(context, budget)
    with pytest.raises(ValueError, match="1.1 coverage receipt"):
        receipt.validate_request_context(old_context, old_budget)
    with pytest.raises(ValueError, match="not registered"):
        negotiate_contract("c0.query_budget", "1.2.0")


@pytest.mark.contract
def test_runtime_1_1_within_budget_complete_and_optional_paths_disabled() -> None:
    receipt, context, budget, _, _ = coverage_receipt_v1_1()
    assert receipt.coverage_status == "complete"
    assert receipt.budget.budget_exhausted_dimensions == ()
    assert budget.max_vector_search_requests == 0
    assert budget.max_embedding_calls == 0
    assert budget.max_embedding_items == 0
    assert receipt.budget.observed_vector_search_requests == 0
    assert receipt.budget.observed_embedding_calls == 0
    assert receipt.budget.observed_embedding_items == 0
    assert context.query_budget_contract_version == "1.1.0"
    assert context.query_budget_schema_hash == QUERY_BUDGET_V1_1_SCHEMA_HASH


@pytest.mark.contract
def test_runtime_1_1_records_agentic_provider_overexecution_without_clamping() -> None:
    receipt, _, budget, _, _ = coverage_receipt_v1_1(
        status="partial",
        observed={
            "observed_agentic_retrieval_invocations": 2,
            "observed_agentic_internal_subqueries": 5,
            "observed_agentic_source_calls": 5,
            "observed_search_candidate_records": 51,
        },
    )
    assert receipt.budget.observed_agentic_retrieval_invocations == 2
    assert len(receipt.planned_subqueries) == 5
    assert len(receipt.source_calls) == 5
    assert receipt.matched_document_count == 51
    assert receipt.returned_document_count == 10
    assert set(receipt.budget.budget_exhausted_dimensions) == {
        "max_agentic_retrieval_invocations",
        "max_agentic_internal_subqueries",
        "max_agentic_source_calls",
        "max_search_candidate_records",
    }
    assert budget.max_agentic_retrieval_invocations == 1
    assert budget.max_agentic_internal_subqueries == 4
    assert budget.max_agentic_source_calls == 4


@pytest.mark.contract
def test_runtime_1_1_graph_scope_and_result_overrun_can_abstain() -> None:
    receipt, _, _, _, _ = coverage_receipt_v1_1(
        status="abstain",
        observed={
            "observed_ontology_graph_scope_requests": 2,
            "observed_graph_result_records": 101,
        },
    )
    assert receipt.coverage_status == "abstain"
    assert receipt.budget.budget_exhausted_dimensions == (
        "max_graph_result_records",
        "max_ontology_graph_scope_requests",
    )


@pytest.mark.contract
def test_runtime_1_1_direct_vector_embedding_retry_overrun_is_exact() -> None:
    receipt, _, _, _, _ = coverage_receipt_v1_1(
        mode="direct_hybrid_prefilter",
        status="abstain",
        vector_search_requests=1,
        embedding_calls=1,
        embedding_items=1,
        observed={
            "observed_vector_search_requests": 2,
            "observed_embedding_calls": 2,
            "observed_embedding_items": 3,
            "observed_retry_count": 3,
            "observed_retry_wait_milliseconds": 1001,
        },
    )
    assert receipt.budget.budget_exhausted_dimensions == (
        "max_embedding_calls",
        "max_embedding_items",
        "max_retry_count",
        "max_retry_wait_milliseconds",
        "max_vector_search_requests",
    )
    assert receipt.budget.observed_direct_search_requests == 1
    assert receipt.budget.observed_agentic_retrieval_invocations == 0


@pytest.mark.contract
def test_runtime_1_1_direct_request_overrun_reconciles_exact_source_calls() -> None:
    receipt, _, _, _, _ = coverage_receipt_v1_1(
        mode="direct_hybrid_prefilter",
        status="partial",
        observed={"observed_direct_search_requests": 2},
    )
    assert receipt.budget.observed_direct_search_requests == 2
    assert len(receipt.source_calls) == 2
    assert len({call.source_call_id for call in receipt.source_calls}) == 2
    assert receipt.budget.budget_exhausted_dimensions == (
        "max_direct_search_requests",
    )

    payload = receipt.model_dump(mode="python")
    payload["budget"]["observed_direct_search_requests"] = 1
    payload["budget"]["budget_exhausted_dimensions"] = ()
    payload["coverage_status"] = "complete"
    payload["failures"] = ()
    payload["coverage_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "coverage_receipt_hash"
        }
    )
    with pytest.raises(ValidationError, match="request counts differ"):
        AgenticRetrievalCoverageReceiptV1_1.model_validate(payload)

    payload = receipt.model_dump(mode="python")
    payload["source_calls"][1]["source_call_id"] = payload["source_calls"][0][
        "source_call_id"
    ]
    payload["coverage_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "coverage_receipt_hash"
        }
    )
    with pytest.raises(ValidationError, match="source call IDs must be unique"):
        AgenticRetrievalCoverageReceiptV1_1.model_validate(payload)


@pytest.mark.contract
def test_runtime_1_1_retry_count_and_wait_coupling() -> None:
    _, _, budget, _, _ = coverage_receipt_v1_1()
    values = budget.model_dump(
        mode="python",
        exclude={"budget_hash"},
        round_trip=True,
    )
    values.update({"max_retry_count": 0, "max_retry_wait_milliseconds": 0})
    disabled = seal(QueryBudgetV1_1, "budget_hash", values)
    assert disabled.max_retry_count == disabled.max_retry_wait_milliseconds == 0

    values.update({"max_retry_count": 1, "max_retry_wait_milliseconds": 0})
    immediate = seal(QueryBudgetV1_1, "budget_hash", values)
    assert immediate.max_retry_count == 1
    assert immediate.max_retry_wait_milliseconds == 0

    values.update({"max_retry_count": 0, "max_retry_wait_milliseconds": 1})
    with pytest.raises(ValidationError, match="retry wait ceiling must be zero"):
        seal(QueryBudgetV1_1, "budget_hash", values)

    receipt, _, _, _, _ = coverage_receipt_v1_1(
        status="partial",
        observed={"observed_retry_count": 3},
    )
    observation = receipt.budget.model_dump(mode="python")
    observation.update(
        {
            "observed_retry_count": 0,
            "observed_retry_wait_milliseconds": 1,
            "budget_exhausted_dimensions": (),
        }
    )
    with pytest.raises(ValidationError, match="observed retry wait must be zero"):
        CoverageBudgetObservationV1_1.model_validate(observation)

    observation.update(
        {
            "observed_retry_count": 1,
            "observed_retry_wait_milliseconds": 0,
        }
    )
    assert (
        CoverageBudgetObservationV1_1.model_validate(
            observation
        ).observed_retry_wait_milliseconds
        == 0
    )


@pytest.mark.contract
@pytest.mark.parametrize(
    ("mutation", "match"),
    (
        (
            lambda payload: payload.update(
                {"budget_exhausted_dimensions": ()}
            ),
            "must be exact",
        ),
        (
            lambda payload: payload.update(
                {
                    "budget_exhausted_dimensions": (
                        "max_output_tokens",
                        "max_retry_count",
                    )
                }
            ),
            "must be exact",
        ),
        (
            lambda payload: payload.update(
                {
                    "budget_exhausted_dimensions": (
                        "max_retry_count",
                        "max_retry_count",
                    )
                }
            ),
            "must not contain duplicates",
        ),
    ),
)
def test_runtime_1_1_rejects_missing_extra_or_duplicate_exhaustion(
    mutation: Any,
    match: str,
) -> None:
    receipt, _, _, _, _ = coverage_receipt_v1_1(
        status="partial",
        observed={"observed_retry_count": 3},
    )
    payload = receipt.budget.model_dump(mode="python")
    mutation(payload)
    with pytest.raises(ValidationError, match=match):
        CoverageBudgetObservationV1_1.model_validate(payload)


@pytest.mark.contract
def test_runtime_1_1_rejects_complete_exhausted_clamped_and_mismatched_counts() -> None:
    receipt, _, _, _, _ = coverage_receipt_v1_1(
        status="partial",
        observed={"observed_retry_count": 3},
    )
    payload = receipt.model_dump(mode="python")
    payload["coverage_status"] = "complete"
    payload["coverage_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "coverage_receipt_hash"
        }
    )
    with pytest.raises(ValidationError, match="exact bounded structural"):
        AgenticRetrievalCoverageReceiptV1_1.model_validate(payload)

    observation = receipt.budget.model_dump(mode="python")
    observation["observed_retry_count"] = observation["max_retry_count"]
    with pytest.raises(ValidationError, match="must be exact"):
        CoverageBudgetObservationV1_1.model_validate(observation)

    payload = receipt.model_dump(mode="python")
    payload["failures"] = (
        RetrievalFailure(
            reason_code="output_truncated",
            remediation="downstream_abstention_required",
        ),
    )
    payload["coverage_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "coverage_receipt_hash"
        }
    )
    with pytest.raises(ValidationError, match="exactly one retrieval_budget_exhausted"):
        AgenticRetrievalCoverageReceiptV1_1.model_validate(payload)

    complete, _, _, _, _ = coverage_receipt_v1_1()
    payload = complete.model_dump(mode="python")
    payload["budget"]["observed_search_candidate_records"] = 9
    payload["coverage_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "coverage_receipt_hash"
        }
    )
    with pytest.raises(ValidationError, match="candidate records"):
        AgenticRetrievalCoverageReceiptV1_1.model_validate(payload)


@pytest.mark.contract
def test_runtime_1_1_rejects_mode_inapplicable_observations() -> None:
    complete, _, _, _, _ = coverage_receipt_v1_1()
    payload = complete.model_dump(mode="python")
    payload["budget"]["observed_vector_search_requests"] = 1
    payload["budget"]["budget_exhausted_dimensions"] = (
        "max_vector_search_requests",
    )
    payload["coverage_status"] = "partial"
    payload["failures"] = (
        RetrievalFailure(
            reason_code="retrieval_budget_exhausted",
            remediation="downstream_abstention_required",
        ),
    )
    payload["coverage_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "coverage_receipt_hash"
        }
    )
    with pytest.raises(ValidationError, match="mode-inapplicable"):
        AgenticRetrievalCoverageReceiptV1_1.model_validate(payload)


@pytest.mark.contract
def test_runtime_1_1_budget_failure_is_exactly_iff_exhaustion() -> None:
    receipt, _, _, _, _ = coverage_receipt_v1_1(
        status="partial",
        observed={"observed_retry_count": 3},
    )
    payload = receipt.model_dump(mode="python")
    payload["failures"] = (*payload["failures"], payload["failures"][0])
    payload["coverage_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "coverage_receipt_hash"
        }
    )
    with pytest.raises(ValidationError, match="retrieval failures must be unique"):
        AgenticRetrievalCoverageReceiptV1_1.model_validate(payload)

    complete, _, _, _, _ = coverage_receipt_v1_1()
    payload = complete.model_dump(mode="python")
    payload["coverage_status"] = "abstain"
    payload["failures"] = (
        RetrievalFailure(
            reason_code="retrieval_budget_exhausted",
            remediation="downstream_abstention_required",
        ),
    )
    payload["coverage_receipt_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload.items()
            if key != "coverage_receipt_hash"
        }
    )
    with pytest.raises(
        ValidationError,
        match="failure requires exhausted dimensions",
    ):
        AgenticRetrievalCoverageReceiptV1_1.model_validate(payload)


@pytest.mark.contract
def test_runtime_1_1_generic_fixtures_are_canonical_and_adversarial() -> None:
    valid_names = (
        "query-budget-v1.1-optional-disabled",
        "agentic-coverage-receipt-v1.1-complete",
        "agentic-coverage-receipt-v1.1-provider-overrun-partial",
        "agentic-coverage-receipt-v1.1-multi-overrun-abstain",
        "agentic-coverage-receipt-v1.1-graph-overrun-abstain",
        "agentic-coverage-receipt-v1.1-direct-request-overrun-partial",
        "query-budget-v1.1-immediate-retry",
    )
    for name in valid_names:
        parsed = parse_contract(
            (FIXTURES / "valid" / f"{name}.json").read_text(encoding="utf-8")
        )
        expected_json = (
            FIXTURES / "golden" / f"{name}.canonical.json"
        ).read_text(encoding="utf-8").rstrip("\n")
        expected_hash = (
            FIXTURES / "golden" / f"{name}.sha256"
        ).read_text(encoding="utf-8").strip()
        assert canonical_json(parsed) == expected_json
        assert canonical_sha256(parsed) == expected_hash

    invalid_names = (
        "agentic-coverage-receipt-v1.1-missing-exhausted",
        "agentic-coverage-receipt-v1.1-extra-exhausted",
        "agentic-coverage-receipt-v1.1-duplicate-exhausted",
        "agentic-coverage-receipt-v1.1-complete-exhausted",
        "agentic-coverage-receipt-v1.1-clamped-observation",
        "agentic-coverage-receipt-v1.1-mismatched-candidates",
        "agentic-coverage-receipt-v1.1-wrong-exhaustion-failure",
        "agentic-coverage-receipt-v1.1-direct-call-mismatch",
        "agentic-coverage-receipt-v1.1-duplicate-source-call-id",
        "query-budget-v1.1-retry-wait-without-count",
        "agentic-coverage-receipt-v1.1-retry-wait-without-retry",
        "agentic-coverage-receipt-v1.1-duplicate-budget-failure",
        "agentic-coverage-receipt-v1.1-false-budget-failure",
    )
    for name in invalid_names:
        with pytest.raises(ValidationError):
            parse_contract(
                (FIXTURES / "invalid" / f"{name}.json").read_text(encoding="utf-8")
            )


@pytest.mark.contract
def test_runtime_fixture_rejects_unknown_field_wrong_type_and_wrong_major() -> None:
    payload = json.loads(
        (FIXTURES / "valid" / "ontology-scope-generic.json").read_text(
            encoding="utf-8"
        )
    )
    unknown = copy.deepcopy(payload)
    unknown["display_name"] = "not authoritative"
    with pytest.raises(ValidationError, match="Extra inputs"):
        parse_contract(unknown)
    wrong_type = copy.deepcopy(payload)
    wrong_type["relationship_k"] = "3"
    with pytest.raises(ValidationError):
        parse_contract(wrong_type)
    wrong_major = copy.deepcopy(payload)
    wrong_major["identity"]["contract_version"] = "2.0.0"
    with pytest.raises(UnknownContractMajorError):
        parse_contract(wrong_major)
