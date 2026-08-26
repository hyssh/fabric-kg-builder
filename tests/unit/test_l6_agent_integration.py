from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from fabric_kg_builder.agent import l6_integration as l6
from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.contracts.identity import CanonicalIdentityEnvelope
from fabric_kg_builder.contracts.runtime import (
    AgenticRetrievalCoverageReceiptV1_1,
    CitationPresentation,
    SearchCitationEnvelope,
)
from fabric_kg_builder.serving.evidence_retrieval import L5bRetrievalResult
from tests.contract.test_c0_runtime_contracts import (
    GENERIC_MEMBER_IDS,
    HASH_C,
    HASH_D,
    citation_presentation,
    coverage_receipt_v1_1,
    ontology_scope,
    resolved_ontology_scope,
    resolved_retrieval_scope,
    seal,
    search_citation,
)


class _Resolver:
    def __init__(self, ontology, retrieval):
        self.ontology = ontology
        self.retrieval = retrieval
        self.calls = 0

    def resolve(self, request):
        self.calls += 1
        assert request.ontology_scope_envelope.scope_hash == self.ontology.ontology_scope_envelope_hash
        return l6.L6ResolvedScopes(
            ontology_scope=self.ontology,
            retrieval_scope=self.retrieval,
        )


class _GraphHost:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def execute(self, request, *, scope):
        self.calls += 1
        assert request.resolved_ontology_scope_hash == scope.resolved_scope_hash
        return self.result


class _EvidenceHost:
    def __init__(self, result):
        self.result = result
        self.calls = 0
        self.questions = []

    def retrieve(self, request, **kwargs):
        self.calls += 1
        self.questions.append(request.question)
        assert request.request_context_hash == kwargs["context"].request_context_hash
        return self.result


def _presentation_for(citation):
    base = citation_presentation().model_dump(
        mode="python",
        exclude={"presentation_hash"},
        round_trip=True,
    )
    identity = CanonicalIdentityEnvelope.model_validate(
        {
            **citation.identity.model_dump(mode="python"),
            "contract_kind": "c0.citation_presentation",
        }
    )
    values = {
        **base,
        "identity": identity,
        "citation_presentation_id": (
            f"citation-presentation:{citation.search_citation_envelope_id}"
        ),
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
    return CitationPresentation(
        **values,
        presentation_hash=canonical_sha256(values),
    )


def _evidence(mode="agentic_preview", *, status="complete", observed=None):
    receipt, context, budget, origin_context, origin_budget = coverage_receipt_v1_1(
        mode=mode,
        status=status,
        observed=observed,
    )
    ontology = resolved_ontology_scope()
    citations = []
    for index, entity_id in enumerate(receipt.returned_canonical_ids):
        original = search_citation(entity_id=entity_id, index=index)
        values = original.model_dump(
            mode="python",
            exclude={"citation_hash"},
            round_trip=True,
        )
        values.update(
            {
                "canonical_scope_id": context.resolved_retrieval_scope_id,
                "canonical_assertion_ids": (
                    ontology.members[index].membership_assertion_ids[0],
                ),
                "evidence_span_ids": ontology.members[index].evidence_span_ids,
            }
        )
        citations.append(
            SearchCitationEnvelope(
                **values,
                citation_hash=canonical_sha256(values),
            )
        )
    citations = tuple(citations)
    receipt_values = receipt.model_dump(
        mode="python",
        exclude={"coverage_receipt_hash"},
        round_trip=True,
    )
    citation_hashes = {
        item.search_citation_envelope_id: item.citation_hash
        for item in citations
    }
    receipt_values["citation_mappings"] = tuple(
        mapping.model_copy(
            update={
                "search_citation_envelope_hash": citation_hashes[
                    mapping.search_citation_envelope_id
                ]
            }
        )
        for mapping in receipt.citation_mappings
    )
    receipt = seal(
        AgenticRetrievalCoverageReceiptV1_1,
        "coverage_receipt_hash",
        receipt_values,
    )
    presentations = tuple(_presentation_for(item) for item in citations)
    return (
        L5bRetrievalResult(
            citations=citations,
            presentations=presentations,
            coverage=receipt,
        ),
        context,
        budget,
        origin_context,
        origin_budget,
    )


def _graph_query(scope, *, required_ids=GENERIC_MEMBER_IDS):
    return l6.L6GraphQuery.seal(
        graph_request_id="graph-request:l6",
        canonical_scope_id=scope.canonical_scope_id,
        approved_graph_path_ids=("graph-path:aggregate-members",),
        relationship_semantic_ids=("relationship:has-member",),
        required_canonical_ids=tuple(required_ids),
        required_assertion_ids=tuple(
            assertion_id
            for member in scope.members
            if member.canonical_entity_id in required_ids
            for assertion_id in member.membership_assertion_ids
        ),
        relationship_k=3,
        max_result_records=100,
    )


def _graph_result(scope, query, *, returned_ids=GENERIC_MEMBER_IDS, source_error=False):
    assertions = tuple(
        l6.L6GraphAssertion.seal(
            assertion_id=scope.members[index].membership_assertion_ids[0],
            source_canonical_id=scope.aggregate_canonical_entity_id,
            relationship_semantic_id="relationship:has-member",
            target_canonical_id=entity_id,
            graph_path_id="graph-path:aggregate-members",
            evidence_span_ids=scope.members[index].evidence_span_ids,
        )
        for index, entity_id in enumerate(returned_ids)
    )
    return l6.L6GraphResult.seal(
        graph_request_id=query.graph_request_id,
        graph_request_hash=query.request_hash,
        canonical_scope_id=scope.canonical_scope_id,
        assertions=assertions,
        returned_canonical_ids=tuple(returned_ids),
        warning_codes=(),
        truncated=False,
        source_error=source_error,
        accounting=l6.L6OperationAccounting(
            operation_refs=("graph-op:1",),
            request_count=1,
            request_bytes=100,
            response_bytes=200,
            retry_count=0,
            retry_wait_milliseconds=0,
            duration_milliseconds=10,
            error_codes=("graph_source_failure",) if source_error else (),
        ),
    )


def _access():
    return l6.L6AccessContext(
        principal_type="managed_identity",
        principal_id="principal:l6",
        principal_scope_hash="a" * 64,
        access_policy_id="policy:l6",
        access_policy_hash="b" * 64,
        project_scope_id="project:generic",
    )


def _authorities():
    policy = SimpleNamespace(
        access_policy_id="access-policy:evidence",
        policy_hash=HASH_C,
    )
    asset = SimpleNamespace(
        governed_asset_reference_id="governed-asset:manual",
        asset_reference_hash=HASH_D,
        source_file_id="source-file:manual",
        content_hash="b" * 64,
        access_policy_id=policy.access_policy_id,
        access_policy_hash=policy.policy_hash,
    )
    return l6.L6Authorities(
        l5a=SimpleNamespace(),
        l5b=SimpleNamespace(),
        access_policy=policy,
        governed_assets=(asset,),
    )


def _orchestrator(monkeypatch, graph_result, evidence_result):
    monkeypatch.setattr(l6, "_validate_authorities", lambda *args, **kwargs: None)
    ontology = resolved_ontology_scope()
    retrieval = resolved_retrieval_scope()
    resolver = _Resolver(ontology, retrieval)
    graph = _GraphHost(graph_result)
    evidence = _EvidenceHost(evidence_result)
    orchestrator = l6.L6AgentOrchestrator(
        resolver=resolver,
        graph_host=graph,
        evidence_host=evidence,
        authorities=_authorities(),
    )
    return orchestrator, resolver, graph, evidence


@pytest.mark.unit
def test_complete_path_is_zero_synthesis_and_one_graph_one_search(monkeypatch):
    evidence_result, context, budget, _, _ = _evidence()
    ontology = resolved_ontology_scope()
    query = _graph_query(ontology)
    graph_result = _graph_result(ontology, query)
    orchestrator, resolver, graph, evidence = _orchestrator(
        monkeypatch, graph_result, evidence_result
    )

    result = orchestrator.run(
        l6.L6RunRequest(
            question="What does the source say about café equipment?",
            ontology_scope_envelope=ontology_scope(),
            graph_query=query,
            request_context=context,
            query_budget=budget,
            access=_access(),
        )
    )

    assert result.status == "complete"
    assert result.zero_synthesis is True
    assert result.synthesis_call_limit == 1
    assert resolver.calls == graph.calls == evidence.calls == 1
    assert evidence.questions == ["What does the source say about café equipment?"]
    assert result.operation_accounting["downstream_synthesis_calls"] == 0
    assert result.operation_accounting["l5b_delegated"]["double_counted_by_l6"] is False
    assert all(
        presentation.transient_authorized_asset_url is None
        for presentation in result.citation_presentations
    )
    with pytest.raises(RuntimeError, match="exactly one run"):
        orchestrator.run(
            l6.L6RunRequest(
                question="repeat",
                ontology_scope_envelope=ontology_scope(),
                graph_query=query,
                request_context=context,
                query_budget=budget,
                access=_access(),
            )
        )


@pytest.mark.unit
def test_missing_graph_member_returns_partial_with_safe_exact_id(monkeypatch):
    evidence_result, context, budget, _, _ = _evidence()
    ontology = resolved_ontology_scope()
    query = _graph_query(ontology)
    missing = query.required_canonical_ids[-1]
    graph_result = _graph_result(
        ontology, query, returned_ids=query.required_canonical_ids[:-1]
    )
    orchestrator, _, graph, evidence = _orchestrator(
        monkeypatch, graph_result, evidence_result
    )

    result = orchestrator.run(
        l6.L6RunRequest(
            question="detail",
            ontology_scope_envelope=ontology_scope(),
            graph_query=query,
            request_context=context,
            query_budget=budget,
            access=_access(),
        )
    )

    assert result.status == "partial"
    assert set(result.readiness.safe_missing_authority_ids) == {
        missing,
        ontology.members[-1].membership_assertion_ids[0],
    }
    assert result.readiness.failures[0].reason_code == "graph_incomplete"
    assert graph.calls == evidence.calls == 1


@pytest.mark.unit
def test_empty_graph_abstains_without_search(monkeypatch):
    evidence_result, context, budget, _, _ = _evidence()
    ontology = resolved_ontology_scope()
    query = _graph_query(ontology)
    graph_result = l6.L6GraphResult.seal(
        graph_request_id=query.graph_request_id,
        graph_request_hash=query.request_hash,
        canonical_scope_id=ontology.canonical_scope_id,
        assertions=(),
        returned_canonical_ids=(),
        warning_codes=(),
        truncated=False,
        source_error=False,
        accounting=l6.L6OperationAccounting(
            operation_refs=("graph-op:empty",),
            request_count=1,
            request_bytes=10,
            response_bytes=10,
            retry_count=0,
            retry_wait_milliseconds=0,
            duration_milliseconds=2,
        ),
    )
    orchestrator, _, graph, evidence = _orchestrator(
        monkeypatch, graph_result, evidence_result
    )
    result = orchestrator.run(
        l6.L6RunRequest(
            question="detail",
            ontology_scope_envelope=ontology_scope(),
            graph_query=query,
            request_context=context,
            query_budget=budget,
            access=_access(),
        )
    )
    assert result.status == "abstain"
    assert result.readiness.failures[0].reason_code == "graph_empty"
    assert graph.calls == 1
    assert evidence.calls == 0


@pytest.mark.unit
def test_graph_source_error_is_sanitized_and_never_falls_back(monkeypatch):
    evidence_result, context, budget, _, _ = _evidence()
    ontology = resolved_ontology_scope()
    query = _graph_query(ontology)

    class _SecretGraphHost:
        calls = 0

        def execute(self, request, *, scope):
            self.calls += 1
            raise RuntimeError("Bearer token-secret at https://provider.example")

    monkeypatch.setattr(l6, "_validate_authorities", lambda *args, **kwargs: None)
    evidence = _EvidenceHost(evidence_result)
    graph = _SecretGraphHost()
    orchestrator = l6.L6AgentOrchestrator(
        resolver=_Resolver(ontology, resolved_retrieval_scope()),
        graph_host=graph,
        evidence_host=evidence,
        authorities=_authorities(),
    )
    result = orchestrator.run(
        l6.L6RunRequest(
            question="detail",
            ontology_scope_envelope=ontology_scope(),
            graph_query=query,
            request_context=context,
            query_budget=budget,
            access=_access(),
        )
    )
    detail = result.readiness.failures[0].detail
    assert result.status == "abstain"
    assert "token-secret" not in detail
    assert "provider.example" not in detail
    assert evidence.calls == 0


@pytest.mark.unit
def test_out_of_scope_graph_result_abstains_without_search(monkeypatch):
    evidence_result, context, budget, _, _ = _evidence()
    ontology = resolved_ontology_scope()
    query = _graph_query(ontology)
    graph_result = _graph_result(ontology, query).model_copy(
        update={"returned_canonical_ids": (*GENERIC_MEMBER_IDS, "entity:outside")}
    )
    orchestrator, _, _, evidence = _orchestrator(
        monkeypatch, graph_result, evidence_result
    )
    result = orchestrator.run(
        l6.L6RunRequest(
            question="detail",
            ontology_scope_envelope=ontology_scope(),
            graph_query=query,
            request_context=context,
            query_budget=budget,
            access=_access(),
        )
    )
    assert result.status == "abstain"
    assert result.readiness.failures[0].reason_code == "graph_out_of_scope"
    assert evidence.calls == 0


@pytest.mark.unit
def test_graph_ids_without_assertions_cannot_prove_complete(monkeypatch):
    evidence_result, context, budget, _, _ = _evidence()
    ontology = resolved_ontology_scope()
    query = _graph_query(ontology)
    complete = _graph_result(ontology, query)
    values = complete.model_dump(
        mode="python",
        exclude={"response_hash"},
        round_trip=True,
    )
    values["assertions"] = complete.assertions[:1]
    values["accounting"] = complete.accounting
    graph_result = l6.L6GraphResult.seal(**values)
    orchestrator, _, _, evidence = _orchestrator(
        monkeypatch, graph_result, evidence_result
    )
    result = orchestrator.run(
        l6.L6RunRequest(
            question="detail",
            ontology_scope_envelope=ontology_scope(),
            graph_query=query,
            request_context=context,
            query_budget=budget,
            access=_access(),
        )
    )
    assert result.status == "abstain"
    assert result.readiness.failures[0].reason_code == "graph_out_of_scope"
    assert evidence.calls == 0


@pytest.mark.unit
def test_graph_warning_or_truncation_can_never_prove_complete(monkeypatch):
    evidence_result, context, budget, _, _ = _evidence()
    ontology = resolved_ontology_scope()
    query = _graph_query(ontology)
    complete = _graph_result(ontology, query)
    values = complete.model_dump(
        mode="python",
        exclude={"response_hash"},
        round_trip=True,
    )
    values.update(
        {
            "assertions": complete.assertions,
            "accounting": complete.accounting,
            "warning_codes": ("graph_warning",),
            "truncated": True,
        }
    )
    graph_result = l6.L6GraphResult.seal(**values)
    orchestrator, _, _, evidence = _orchestrator(
        monkeypatch, graph_result, evidence_result
    )
    result = orchestrator.run(
        l6.L6RunRequest(
            question="detail",
            ontology_scope_envelope=ontology_scope(),
            graph_query=query,
            request_context=context,
            query_budget=budget,
            access=_access(),
        )
    )
    assert result.status == "partial"
    assert result.readiness.graph_complete is False
    assert evidence.calls == 1


@pytest.mark.unit
def test_citation_duplicate_or_mismatch_abstains(monkeypatch):
    evidence_result, context, budget, _, _ = _evidence()
    ontology = resolved_ontology_scope()
    query = _graph_query(ontology)
    graph_result = _graph_result(ontology, query)
    spoofed = L5bRetrievalResult(
        citations=evidence_result.citations,
        presentations=(
            evidence_result.presentations[0],
            evidence_result.presentations[0],
            *evidence_result.presentations[2:],
        ),
        coverage=evidence_result.coverage,
    )
    orchestrator, _, _, evidence = _orchestrator(
        monkeypatch, graph_result, spoofed
    )
    result = orchestrator.run(
        l6.L6RunRequest(
            question="detail",
            ontology_scope_envelope=ontology_scope(),
            graph_query=query,
            request_context=context,
            query_budget=budget,
            access=_access(),
        )
    )
    assert result.status == "abstain"
    assert result.search_citations == ()
    assert result.readiness.failures[0].reason_code == "citation_invalid"
    assert evidence.calls == 1


@pytest.mark.unit
def test_citation_policy_or_asset_mismatch_abstains(monkeypatch):
    evidence_result, context, budget, _, _ = _evidence()
    ontology = resolved_ontology_scope()
    query = _graph_query(ontology)
    graph_result = _graph_result(ontology, query)
    citation_values = evidence_result.citations[0].model_dump(
        mode="python",
        exclude={"citation_hash"},
        round_trip=True,
    )
    citation_values["access_policy_hash"] = "f" * 64
    wrong = SearchCitationEnvelope(
        **citation_values,
        citation_hash=canonical_sha256(citation_values),
    )
    coverage_values = evidence_result.coverage.model_dump(
        mode="python",
        exclude={"coverage_receipt_hash"},
        round_trip=True,
    )
    coverage_values["citation_mappings"] = tuple(
        mapping.model_copy(
            update={"search_citation_envelope_hash": wrong.citation_hash}
        )
        if mapping.search_citation_envelope_id
        == wrong.search_citation_envelope_id
        else mapping
        for mapping in evidence_result.coverage.citation_mappings
    )
    coverage = seal(
        AgenticRetrievalCoverageReceiptV1_1,
        "coverage_receipt_hash",
        coverage_values,
    )
    spoofed = L5bRetrievalResult(
        citations=(wrong, *evidence_result.citations[1:]),
        presentations=(
            _presentation_for(wrong),
            *evidence_result.presentations[1:],
        ),
        coverage=coverage,
    )
    orchestrator, _, _, evidence = _orchestrator(
        monkeypatch, graph_result, spoofed
    )
    result = orchestrator.run(
        l6.L6RunRequest(
            question="detail",
            ontology_scope_envelope=ontology_scope(),
            graph_query=query,
            request_context=context,
            query_budget=budget,
            access=_access(),
        )
    )
    assert result.status == "abstain"
    assert result.readiness.failures[0].reason_code == "citation_invalid"
    assert evidence.calls == 1


@pytest.mark.unit
def test_retrieval_exception_records_attempt_with_incomplete_accounting(monkeypatch):
    evidence_result, context, budget, _, _ = _evidence()
    ontology = resolved_ontology_scope()
    query = _graph_query(ontology)
    graph_result = _graph_result(ontology, query)

    class _FailingEvidenceHost:
        calls = 0

        def retrieve(self, request, **kwargs):
            self.calls += 1
            raise RuntimeError("api-key=must-not-leak")

    monkeypatch.setattr(l6, "_validate_authorities", lambda *args, **kwargs: None)
    evidence = _FailingEvidenceHost()
    orchestrator = l6.L6AgentOrchestrator(
        resolver=_Resolver(ontology, resolved_retrieval_scope()),
        graph_host=_GraphHost(graph_result),
        evidence_host=evidence,
        authorities=_authorities(),
    )
    result = orchestrator.run(
        l6.L6RunRequest(
            question="detail",
            ontology_scope_envelope=ontology_scope(),
            graph_query=query,
            request_context=context,
            query_budget=budget,
            access=_access(),
        )
    )
    accounting = result.operation_accounting["l5b_delegated"]
    assert result.status == "abstain"
    assert accounting["attempted"] is True
    assert accounting["accounting_complete"] is False
    assert accounting["request_count"] is None
    assert "api-key" not in result.readiness.failures[0].detail


@pytest.mark.unit
def test_invalid_runtime_receipt_abstains_and_suppresses_citations(monkeypatch):
    evidence_result, context, budget, _, _ = _evidence(
        status="abstain",
        observed={"observed_ontology_graph_scope_requests": 2},
    )
    ontology = resolved_ontology_scope()
    query = _graph_query(ontology)
    orchestrator, _, _, evidence = _orchestrator(
        monkeypatch,
        _graph_result(ontology, query),
        evidence_result,
    )
    result = orchestrator.run(
        l6.L6RunRequest(
            question="detail",
            ontology_scope_envelope=ontology_scope(),
            graph_query=query,
            request_context=context,
            query_budget=budget,
            access=_access(),
        )
    )
    assert result.status == "abstain"
    assert result.search_citations == ()
    assert result.citation_presentations == ()
    assert evidence.calls == 1


@pytest.mark.unit
def test_graph_overexecution_is_rejected_without_search(monkeypatch):
    evidence_result, context, budget, _, _ = _evidence()
    ontology = resolved_ontology_scope()
    query = _graph_query(ontology)
    complete = _graph_result(ontology, query)
    values = complete.model_dump(
        mode="python",
        exclude={"response_hash"},
        round_trip=True,
    )
    values["assertions"] = complete.assertions
    values["accounting"] = l6.L6OperationAccounting(
        operation_refs=("graph-op:1", "graph-op:2"),
        request_count=2,
        request_bytes=200,
        response_bytes=400,
        retry_count=1,
        retry_wait_milliseconds=10,
        duration_milliseconds=20,
    )
    graph_result = l6.L6GraphResult.seal(**values)
    orchestrator, _, _, evidence = _orchestrator(
        monkeypatch, graph_result, evidence_result
    )
    result = orchestrator.run(
        l6.L6RunRequest(
            question="detail",
            ontology_scope_envelope=ontology_scope(),
            graph_query=query,
            request_context=context,
            query_budget=budget,
            access=_access(),
        )
    )
    assert result.status == "abstain"
    assert evidence.calls == 0


@pytest.mark.unit
def test_direct_fallback_requires_and_preserves_exact_origin(monkeypatch):
    evidence_result, context, budget, origin, origin_budget = _evidence(
        "direct_hybrid_prefilter"
    )
    ontology = resolved_ontology_scope()
    query = _graph_query(ontology)
    graph_result = _graph_result(ontology, query)
    orchestrator, _, graph, evidence = _orchestrator(
        monkeypatch, graph_result, evidence_result
    )
    result = orchestrator.run(
        l6.L6RunRequest(
            question="detail",
            ontology_scope_envelope=ontology_scope(),
            graph_query=query,
            request_context=context,
            query_budget=budget,
            originating_request_context=origin,
            originating_query_budget=origin_budget,
            access=_access(),
        )
    )
    assert result.status == "complete"
    assert graph.calls == evidence.calls == 1

    with pytest.raises(ValueError, match="present together"):
        l6.L6RunRequest(
            question="detail",
            ontology_scope_envelope=ontology_scope(),
            graph_query=query,
            request_context=context,
            query_budget=budget,
            originating_request_context=origin,
            access=_access(),
        )


@pytest.mark.unit
def test_graph_budget_and_k_are_enforced_before_host(monkeypatch):
    evidence_result, context, budget, _, _ = _evidence()
    ontology = resolved_ontology_scope()
    query = l6.L6GraphQuery.seal(
        graph_request_id="graph-request:l6-over-budget",
        canonical_scope_id=ontology.canonical_scope_id,
        approved_graph_path_ids=("graph-path:aggregate-members",),
        relationship_semantic_ids=("relationship:has-member",),
        required_canonical_ids=GENERIC_MEMBER_IDS,
        required_assertion_ids=tuple(
            assertion_id
            for member in ontology.members
            for assertion_id in member.membership_assertion_ids
        ),
        relationship_k=3,
        max_result_records=101,
    )
    graph_result = _graph_result(ontology, _graph_query(ontology))
    orchestrator, _, graph, evidence = _orchestrator(
        monkeypatch, graph_result, evidence_result
    )
    result = orchestrator.run(
        l6.L6RunRequest(
            question="detail",
            ontology_scope_envelope=ontology_scope(),
            graph_query=query,
            request_context=context,
            query_budget=budget,
            access=_access(),
        )
    )
    assert result.status == "abstain"
    assert result.readiness.failures[0].reason_code == "budget_exhausted"
    assert graph.calls == evidence.calls == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    (
        (ValueError("stale ACL and access policy hash"), "policy_mismatch"),
        (RuntimeError("L5B_PUBLICATION_RECEIPT_INVALID"), "authority_invalid"),
    ),
)
def test_stale_policy_authority_abstains_before_graph(
    monkeypatch,
    failure,
    expected_reason,
):
    evidence_result, context, budget, _, _ = _evidence()
    ontology = resolved_ontology_scope()
    query = _graph_query(ontology)
    graph_result = _graph_result(ontology, query)
    monkeypatch.setattr(
        l6,
        "_validate_authorities",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            failure
        ),
    )
    graph = _GraphHost(graph_result)
    evidence = _EvidenceHost(evidence_result)
    orchestrator = l6.L6AgentOrchestrator(
        resolver=_Resolver(ontology, resolved_retrieval_scope()),
        graph_host=graph,
        evidence_host=evidence,
        authorities=_authorities(),
    )
    result = orchestrator.run(
        l6.L6RunRequest(
            question="detail",
            ontology_scope_envelope=ontology_scope(),
            graph_query=query,
            request_context=context,
            query_budget=budget,
            access=_access(),
        )
    )
    assert result.status == "abstain"
    assert result.readiness.failures[0].reason_code == expected_reason
    assert graph.calls == evidence.calls == 0


@pytest.mark.unit
def test_tool_schemas_instructions_and_definition_readback(tmp_path: Path):
    definitions = l6.build_l6_tool_definitions()
    assert [item["name"] for item in definitions] == [
        l6.L6_TOOL_RESOLVE_SCOPE,
        l6.L6_TOOL_EXECUTE_GRAPH,
        l6.L6_TOOL_RETRIEVE_EVIDENCE,
        l6.L6_TOOL_ASSEMBLE_CITATIONS,
        l6.L6_TOOL_REPORT_READINESS,
    ]
    assert all(
        item["input_schema"]["additionalProperties"] is False
        for item in definitions
    )
    assert all(
        "answer" not in item["output_schema"].get("properties", {})
        and "summary" not in item["output_schema"].get("properties", {})
        for item in definitions
    )
    retrieval_output = definitions[2]["output_schema"]
    coverage_ref = retrieval_output["properties"]["coverage_receipt"]["$ref"]
    assert "AgenticRetrievalCoverageReceiptV1_1" in coverage_ref
    assert "raw gql" in definitions[1]["description"].casefold()
    instructions = l6.build_l6_agent_instructions()
    assert "exact order" in instructions
    assert "abstain" in instructions
    assert "at most once" in instructions

    definition = l6.build_l6_agent_definition(
        agent_name="kg-l6",
        fabric_data_agent_connection_id="connection:fabric",
        foundry_remote_tool_connection_id="connection:remote-tool",
    )
    path = tmp_path / "l6-agent-definition.json"
    definition_hash = l6.persist_l6_agent_definition(path, definition)
    assert definition_hash == definition["definition_hash"]
    assert "0.2.4" not in path.read_text("utf-8")
    assert "rdf" not in path.read_text("utf-8").casefold()

    drifted = {**definition, "agent_name": "drifted"}
    with pytest.raises(ValueError, match="hash mismatch"):
        l6.persist_l6_agent_definition(path, drifted)
