from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

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


@pytest.fixture(autouse=True)
def _stub_persisted_l5b_gate(monkeypatch):
    monkeypatch.setattr(
        l6,
        "_require_l6_evidence_publication",
        lambda *args, **kwargs: None,
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


def _operation_ref(seed, *, status="succeeded"):
    request_hash = canonical_sha256({"request": seed})
    response_hash = canonical_sha256({"response": seed})
    return l6.L6OpaqueOperationRef.from_hashes(
        request_hash=request_hash,
        response_hash=response_hash,
        status=status,
    )


class _GraphReceiptStore:
    def __init__(self):
        self.receipts = {}
        self.consumed = set()
        self.verify_calls = 0

    def issue(
        self,
        *,
        graph_query,
        graph_result,
        ontology_scope,
        retrieval_scope,
        budget,
    ):
        l6._validate_graph_query(
            graph_query,
            ontology_scope,
            retrieval_scope,
            budget,
        )
        graph_complete, _ = l6._validate_graph_result(
            graph_query,
            ontology_scope,
            graph_result,
        )
        if not graph_complete:
            raise ValueError("incomplete Graph result cannot mint a receipt")
        receipt_id = "gxr-sha256:" + canonical_sha256(
            {
                "request": graph_query.request_hash,
                "result": graph_result.response_hash,
                "scope": retrieval_scope.retrieval_scope_hash,
            }
        )
        values = {
            "graph_execution_receipt_id": receipt_id,
            "graph_request_id": graph_query.graph_request_id,
            "graph_request_hash": graph_query.request_hash,
            "graph_result_hash": graph_result.response_hash,
            "resolved_ontology_scope_id": (
                ontology_scope.resolved_ontology_scope_id
            ),
            "resolved_ontology_scope_hash": ontology_scope.resolved_scope_hash,
            "resolved_retrieval_scope_id": (
                retrieval_scope.resolved_retrieval_scope_id
            ),
            "resolved_retrieval_scope_hash": retrieval_scope.retrieval_scope_hash,
            "canonical_scope_id": ontology_scope.canonical_scope_id,
            "graph_model_hash": ontology_scope.graph_model_hash,
            "search_index_fingerprint": ontology_scope.search_index_fingerprint,
            "asserted_publication_hash": ontology_scope.asserted_publication_hash,
            "publication_crosswalk_hash": (
                ontology_scope.publication_crosswalk_hash
            ),
            "acl_scope_hash": ontology_scope.acl_scope_hash,
            "returned_canonical_ids": graph_result.returned_canonical_ids,
            "returned_assertion_ids": tuple(
                sorted(item.assertion_id for item in graph_result.assertions)
            ),
            "assertion_count": len(graph_result.assertions),
            "graph_complete": graph_complete,
            "accounting": graph_result.accounting,
            "execution_status": "succeeded",
        }
        receipt = l6.L6GraphExecutionReceipt(
            **values,
            receipt_hash=canonical_sha256(values),
        )
        self.receipts[receipt_id] = receipt
        return receipt

    def verify_and_consume(self, receipt_id, receipt_hash, expectation):
        self.verify_calls += 1
        receipt = self.receipts.get(receipt_id)
        if (
            receipt is None
            or receipt.receipt_hash != receipt_hash
            or receipt_id in self.consumed
            or not l6._receipt_matches_expectation(receipt, expectation)
        ):
            raise ValueError("invalid or replayed Graph execution receipt")
        self.consumed.add(receipt_id)
        return receipt


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


def _stable_presentations(evidence_result):
    by_id = {
        item.search_citation_envelope_id: item
        for item in evidence_result.citations
    }
    return tuple(
        l6.L6StableCitationPresentation.from_verified(
            presentation,
            by_id[presentation.search_citation_envelope_id],
        )
        for presentation in evidence_result.presentations
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
            operation_refs=(_operation_ref("graph-op:1"),),
            request_count=1,
            request_bytes=100,
            response_bytes=200,
            retry_count=0,
            retry_wait_milliseconds=0,
            duration_milliseconds=10,
            error_codes=("GRAPH_SOURCE_FAILURE",) if source_error else (),
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
    receipt_store = _GraphReceiptStore()
    orchestrator = l6.L6AgentOrchestrator(
        resolver=resolver,
        graph_host=graph,
        evidence_host=evidence,
        graph_receipt_authority=receipt_store,
        authorities=_authorities(),
    )
    return orchestrator, resolver, graph, evidence


def _standalone_evidence_boundary():
    evidence_result, context, budget, origin, origin_budget = _evidence()
    ontology = resolved_ontology_scope()
    retrieval = resolved_retrieval_scope()
    query = _graph_query(ontology)
    graph = _graph_result(ontology, query)
    store = _GraphReceiptStore()
    authorities = _authorities()
    receipt = store.issue(
        graph_query=query,
        graph_result=graph,
        ontology_scope=ontology,
        retrieval_scope=retrieval,
        budget=budget,
    )
    delegate = _EvidenceHost(evidence_result)
    tool = l6.L6VerifiedEvidenceTool(
        delegate=delegate,
        graph_receipt_authority=store,
        authorities=authorities,
    )
    request = l6.L6EvidenceToolInput(
        question="detail",
        resolved_retrieval_scope_id=retrieval.resolved_retrieval_scope_id,
        resolved_retrieval_scope_hash=retrieval.retrieval_scope_hash,
        request_context_id=context.request_context_id,
        request_context_hash=context.request_context_hash,
        graph_execution_receipt_id=receipt.graph_execution_receipt_id,
        graph_execution_receipt_hash=receipt.receipt_hash,
    )
    kwargs = {
        "ontology_scope": ontology,
        "retrieval_scope": retrieval,
        "context": context,
        "budget": budget,
        "publication": authorities.l5b,
        "originating_context": origin,
        "originating_budget": origin_budget,
    }
    return tool, delegate, store, request, kwargs


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
    assert result.operation_accounting.downstream_synthesis_calls == 0
    assert (
        result.operation_accounting.retrieval.delegated.double_counted_by_l6
        is False
    )
    assert "transient" not in str(
        result.citation_collection.model_dump(mode="json")
    ).casefold()
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
def test_standalone_retrieval_requires_graph_receipt_fields():
    with pytest.raises(ValidationError):
        l6.L6EvidenceToolInput(
            question="detail",
            resolved_retrieval_scope_id="scope",
            resolved_retrieval_scope_hash="a" * 64,
            request_context_id="context",
            request_context_hash="b" * 64,
        )


@pytest.mark.unit
@pytest.mark.parametrize("attack", ("missing", "forged", "cross_scope"))
def test_standalone_retrieval_rejects_invalid_graph_receipt_before_search(attack):
    tool, delegate, store, request, kwargs = _standalone_evidence_boundary()
    values = request.model_dump(mode="python")
    if attack == "missing":
        values["graph_execution_receipt_id"] = "gxr-sha256:" + "0" * 64
    elif attack == "forged":
        values["graph_execution_receipt_hash"] = "f" * 64
    else:
        values["resolved_retrieval_scope_hash"] = "e" * 64
    attacked = l6.L6EvidenceToolInput.model_validate(values)

    with pytest.raises(ValueError):
        tool.retrieve(attacked, **kwargs)

    assert delegate.calls == 0
    assert store.verify_calls == (0 if attack == "cross_scope" else 1)


@pytest.mark.unit
def test_standalone_retrieval_rejects_replayed_graph_receipt():
    tool, delegate, store, request, kwargs = _standalone_evidence_boundary()

    first = tool.retrieve(request, **kwargs)
    assert first.coverage_receipt.coverage_status == "complete"
    with pytest.raises(ValueError, match="replayed"):
        tool.retrieve(request, **kwargs)

    assert delegate.calls == 1
    assert store.verify_calls == 2


@pytest.mark.unit
def test_server_side_receipt_authority_issues_unique_single_use_receipts():
    ontology = resolved_ontology_scope()
    retrieval = resolved_retrieval_scope()
    query = _graph_query(ontology)
    graph = _graph_result(ontology, query)
    store = l6.L6InMemoryGraphReceiptAuthority()
    _, _, budget, _, _ = _evidence()
    receipt = store.issue(
        graph_query=query,
        graph_result=graph,
        ontology_scope=ontology,
        retrieval_scope=retrieval,
        budget=budget,
    )
    assert store.verify_and_consume(
        receipt.graph_execution_receipt_id,
        receipt.receipt_hash,
        l6._receipt_expectation(
            ontology,
            retrieval,
            _evidence()[1],
        ),
    ) == receipt
    second = store.issue(
        graph_query=query,
        graph_result=graph,
        ontology_scope=ontology,
        retrieval_scope=retrieval,
        budget=budget,
    )
    assert second != receipt
    assert second.graph_execution_receipt_id != receipt.graph_execution_receipt_id
    with pytest.raises(ValueError, match="replayed"):
        store.verify_and_consume(
            receipt.graph_execution_receipt_id,
            receipt.receipt_hash,
            l6._receipt_expectation(
                ontology,
                retrieval,
                _evidence()[1],
            ),
        )
    assert store.verify_and_consume(
        second.graph_execution_receipt_id,
        second.receipt_hash,
        l6._receipt_expectation(
            ontology,
            retrieval,
            _evidence()[1],
        ),
    ) == second


@pytest.mark.unit
def test_server_receipt_authority_rejects_narrowed_graph_query():
    ontology = resolved_ontology_scope()
    retrieval = resolved_retrieval_scope()
    _, _, budget, _, _ = _evidence()
    narrowed_ids = GENERIC_MEMBER_IDS[:-1]
    narrowed = _graph_query(ontology, required_ids=narrowed_ids)
    narrowed_graph = _graph_result(
        ontology,
        narrowed,
        returned_ids=narrowed_ids,
    )
    with pytest.raises(ValueError, match="exact resolved member"):
        l6.L6InMemoryGraphReceiptAuthority().issue(
            graph_query=narrowed,
            graph_result=narrowed_graph,
            ontology_scope=ontology,
            retrieval_scope=retrieval,
            budget=budget,
        )


@pytest.mark.unit
def test_standalone_retrieval_rejects_cross_index_context_before_consumption():
    tool, delegate, store, request, kwargs = _standalone_evidence_boundary()
    context = kwargs["context"]
    values = context.model_dump(
        mode="python",
        exclude={"request_context_hash"},
        round_trip=True,
    )
    values["search_index_fingerprint"] = "f" * 64
    forged_context = type(context)(
        **values,
        request_context_hash=canonical_sha256(values),
    )
    attacked_request = request.model_copy(
        update={
            "request_context_hash": forged_context.request_context_hash,
        }
    )
    kwargs["context"] = forged_context

    with pytest.raises(ValueError, match="Search index"):
        tool.retrieve(attacked_request, **kwargs)

    assert delegate.calls == 0
    assert store.verify_calls == 0


@pytest.mark.unit
def test_cross_scope_attack_does_not_consume_valid_receipt():
    tool, delegate, store, request, kwargs = _standalone_evidence_boundary()
    attacked = request.model_copy(
        update={"resolved_retrieval_scope_hash": "e" * 64}
    )
    with pytest.raises(ValueError):
        tool.retrieve(attacked, **kwargs)
    assert delegate.calls == 0

    valid = tool.retrieve(request, **kwargs)
    assert valid.coverage.coverage_status == "complete"
    assert delegate.calls == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "unsafe_id",
    (
        "https://provider.example/query",
        "https://x.test/?sig=secret",
        "principal:user@example.com",
        "user@example.com",
        "path:/workspace/query.gql",
        "op-sha256:" + "ａ" * 64,
        "op-sha256:" + ("a" * 63) + "\n",
    ),
)
def test_operation_refs_reject_provider_and_identity_metadata(unsafe_id):
    with pytest.raises(ValidationError):
        l6.L6OpaqueOperationRef(
            operation_id=unsafe_id,
            request_hash="a" * 64,
            response_hash="b" * 64,
            status="failed",
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "unsafe_code",
    (
        "provider error text",
        "https://provider.example",
        "USER@EXAMPLE.COM",
        "SIG=SECRET",
        "GRAPH_\nFAILURE",
        "ＧＲＡＰＨ_FAILURE",
        "SUPERSECRETAPIKEY123",
        "A" * 65,
    ),
)
def test_error_codes_reject_provider_text_secrets_and_confusables(unsafe_code):
    with pytest.raises(ValidationError):
        l6.L6OperationAccounting(
            operation_refs=(_operation_ref("safe", status="failed"),),
            request_count=1,
            request_bytes=1,
            response_bytes=1,
            retry_count=0,
            retry_wait_milliseconds=0,
            duration_milliseconds=1,
            error_codes=(unsafe_code,),
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

    assert result.status == "abstain"
    assert set(result.readiness.safe_missing_authority_ids) == {
        missing,
        ontology.members[-1].membership_assertion_ids[0],
    }
    assert result.readiness.failures[0].reason_code == "graph_incomplete"
    assert graph.calls == 1
    assert evidence.calls == 0


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
            operation_refs=(_operation_ref("graph-op:empty"),),
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
        graph_receipt_authority=_GraphReceiptStore(),
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
            "warning_codes": ("GRAPH_WARNING",),
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
    assert result.status == "abstain"
    assert result.readiness.graph_complete is False
    assert evidence.calls == 0


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
        graph_receipt_authority=_GraphReceiptStore(),
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
    accounting = result.operation_accounting.retrieval
    assert result.status == "abstain"
    assert accounting.attempted is True
    assert accounting.accounting_complete is False
    assert accounting.delegated is None
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
    assert result.citation_collection is None
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
        operation_refs=(
            _operation_ref("graph-op:1"),
            _operation_ref("graph-op:2"),
        ),
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
        graph_receipt_authority=_GraphReceiptStore(),
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
def test_sealed_output_is_deeply_immutable(monkeypatch):
    evidence_result, context, budget, _, _ = _evidence()
    ontology = resolved_ontology_scope()
    query = _graph_query(ontology)
    orchestrator, _, _, _ = _orchestrator(
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

    assert isinstance(result.coverage_receipt, AgenticRetrievalCoverageReceiptV1_1)
    assert isinstance(result.operation_accounting, l6.L6RunAccounting)
    assert isinstance(result.citation_collection.presentations, tuple)
    assert isinstance(
        result.operation_accounting.graph.operation.operation_refs,
        tuple,
    )
    with pytest.raises(ValidationError):
        result.coverage_receipt.coverage_status = "partial"
    with pytest.raises(ValidationError):
        result.operation_accounting.duration_milliseconds = 0
    with pytest.raises(TypeError):
        result.citation_collection.presentations[0] = (
            result.citation_collection.presentations[0]
        )
    stable = result.citation_collection.presentations[0]
    assert stable.__pydantic_private__ in (None, {})
    assert "url" not in str(stable.model_dump(mode="json")).casefold()


@pytest.mark.unit
def test_complete_evidence_output_rejects_empty_citations():
    evidence_result, _, _, _, _ = _evidence()
    values = {
        "graph_execution_receipt_id": "gxr-sha256:" + "a" * 64,
        "graph_execution_receipt_hash": "b" * 64,
        "citations": (),
        "presentations": (),
        "coverage_receipt": evidence_result.coverage,
    }
    with pytest.raises(ValidationError, match="non-empty verified citations"):
        l6.L6EvidenceToolOutput(
            **values,
            output_hash=canonical_sha256(values),
        )


@pytest.mark.unit
def test_transient_url_cannot_enter_stable_presentation():
    evidence_result, _, _, _, _ = _evidence()
    citation = evidence_result.citations[0]
    transient = evidence_result.presentations[0].with_transient_authorized_asset_url(
        "https://storage.example.test/file?sig=secret"
    )
    with pytest.raises(ValueError, match="transient citation URLs"):
        l6.L6StableCitationPresentation.from_verified(transient, citation)
    stable = l6.L6StableCitationPresentation.from_verified(
        evidence_result.presentations[0],
        citation,
    )
    payload = stable.model_dump(mode="json")
    payload["transient_authorized_asset_url"] = (
        "https://storage.example.test/file?sig=secret"
    )
    with pytest.raises(ValidationError):
        l6.L6StableCitationPresentation.model_validate(payload)
    forged_values = stable.model_dump(
        mode="python",
        exclude={"stable_presentation_hash"},
        round_trip=True,
    )
    forged_values["citation_presentation_id"] = (
        "https://private.example/file?sig=secret"
    )
    with pytest.raises(ValidationError, match="ID mismatch"):
        l6.L6StableCitationPresentation(
            **forged_values,
            stable_presentation_hash=canonical_sha256(forged_values),
        )


@pytest.mark.unit
def test_evidence_output_rejects_self_rehashed_forged_stable_presentation():
    evidence_result, _, _, _, _ = _evidence()
    stable = _stable_presentations(evidence_result)
    forged_values = stable[0].model_dump(
        mode="python",
        exclude={"stable_presentation_hash"},
        round_trip=True,
    )
    forged_values["original_document_name"] = "FORGED"
    forged = l6.L6StableCitationPresentation(
        **forged_values,
        stable_presentation_hash=canonical_sha256(forged_values),
    )
    values = {
        "graph_execution_receipt_id": "gxr-sha256:" + "a" * 64,
        "graph_execution_receipt_hash": "b" * 64,
        "citations": evidence_result.citations,
        "presentations": (forged, *stable[1:]),
        "coverage_receipt": evidence_result.coverage,
    }
    with pytest.raises(ValidationError, match="differs from citation authority"):
        l6.L6EvidenceToolOutput(
            **values,
            output_hash=canonical_sha256(values),
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "ids",
    (
        (),
        ("search-citation:1", "search-citation:1"),
        ("search-citation:2", "search-citation:1"),
    ),
)
def test_citation_input_requires_nonempty_sorted_unique_ids(ids):
    with pytest.raises(ValidationError):
        l6.L6CitationToolInput(
            coverage_receipt_id="coverage",
            coverage_receipt_hash="a" * 64,
            citation_envelope_ids=ids,
        )


@pytest.mark.unit
def test_citation_collection_exact_cardinality_and_hash():
    evidence_result, context, budget, origin, origin_budget = _evidence()
    retrieval = resolved_retrieval_scope()
    ids = tuple(
        sorted(item.search_citation_envelope_id for item in evidence_result.citations)
    )
    request = l6.L6CitationToolInput(
        coverage_receipt_id=evidence_result.coverage.coverage_receipt_id,
        coverage_receipt_hash=evidence_result.coverage.coverage_receipt_hash,
        citation_envelope_ids=ids,
    )
    collection = l6.assemble_l6_citation_collection(
        request,
        citations=evidence_result.citations,
        presentations=_stable_presentations(evidence_result),
        coverage=evidence_result.coverage,
        context=context,
        budget=budget,
        retrieval_scope=retrieval,
        originating_context=origin,
        originating_budget=origin_budget,
    )
    assert collection.citation_envelope_ids == ids
    assert len(collection.presentations) == len(ids)
    assert tuple(
        item.search_citation_envelope_id
        for item in collection.citation_envelope_hashes
    ) == ids
    assert collection.collection_hash == canonical_sha256(
        collection.model_dump(mode="json", exclude={"collection_hash"})
    )

    with pytest.raises(ValueError):
        l6.assemble_l6_citation_collection(
            request,
            citations=evidence_result.citations[:-1],
            presentations=_stable_presentations(evidence_result),
            coverage=evidence_result.coverage,
            context=context,
            budget=budget,
            retrieval_scope=retrieval,
            originating_context=origin,
            originating_budget=origin_budget,
        )
    stable_values = _stable_presentations(evidence_result)[0].model_dump(
        mode="python",
        exclude={"stable_presentation_hash"},
        round_trip=True,
    )
    stable_values["original_document_name"] = "WRONG DOCUMENT"
    wrong_stable = l6.L6StableCitationPresentation(
        **stable_values,
        stable_presentation_hash=canonical_sha256(stable_values),
    )
    with pytest.raises(ValueError, match="differs from citation authority"):
        l6.assemble_l6_citation_collection(
            request,
            citations=evidence_result.citations,
            presentations=(
                wrong_stable,
                *_stable_presentations(evidence_result)[1:],
            ),
            coverage=evidence_result.coverage,
            context=context,
            budget=budget,
            retrieval_scope=retrieval,
            originating_context=origin,
            originating_budget=origin_budget,
        )
    with pytest.raises(ValueError):
        l6.assemble_l6_citation_collection(
            request,
            citations=evidence_result.citations,
            presentations=(
                _stable_presentations(evidence_result)[0],
                _stable_presentations(evidence_result)[0],
                *_stable_presentations(evidence_result)[2:],
            ),
            coverage=evidence_result.coverage,
            context=context,
            budget=budget,
            retrieval_scope=retrieval,
            originating_context=origin,
            originating_budget=origin_budget,
        )
    unrelated_values = evidence_result.citations[0].model_dump(
        mode="python",
        exclude={"citation_hash"},
        round_trip=True,
    )
    unrelated_values["exact_authorized_quote"] = "Different sealed quote."
    unrelated_values["quote_hash"] = canonical_sha256(
        unrelated_values["exact_authorized_quote"]
    )
    unrelated = SearchCitationEnvelope(
        **unrelated_values,
        citation_hash=canonical_sha256(unrelated_values),
    )
    with pytest.raises(ValueError):
        l6.assemble_l6_citation_collection(
            request,
            citations=(unrelated, *evidence_result.citations[1:]),
            presentations=(
                l6.L6StableCitationPresentation.from_verified(
                    _presentation_for(unrelated),
                    unrelated,
                ),
                *_stable_presentations(evidence_result)[1:],
            ),
            coverage=evidence_result.coverage,
            context=context,
            budget=budget,
            retrieval_scope=retrieval,
            originating_context=origin,
            originating_budget=origin_budget,
        )


@pytest.mark.unit
def test_zero_source_abstention_accounting_is_representable():
    accounting = l6.L6DelegatedRetrievalAccounting(
        request_context_id="context",
        coverage_receipt_id="coverage:abstain",
        source_call_count=0,
        operation_refs=(),
        agentic_retrieval_invocations=1,
        agentic_source_calls=0,
        direct_search_requests=0,
        vector_search_requests=0,
        embedding_calls=0,
        embedding_items=0,
        retry_count=0,
        retry_wait_milliseconds=0,
        output_bytes=0,
        duration_milliseconds=1,
    )
    assert accounting.source_call_count == 0
    assert accounting.operation_refs == ()


@pytest.mark.unit
def test_standalone_scope_graph_and_readiness_tools_are_authority_bound():
    ontology = resolved_ontology_scope()
    retrieval = resolved_retrieval_scope()
    evidence_result, context, budget, origin, origin_budget = _evidence()
    resolver = _Resolver(ontology, retrieval)
    scopes = l6.L6VerifiedScopeTool(resolver).resolve(
        l6.L6ScopeResolutionInput(
            ontology_scope_envelope=ontology_scope()
        )
    )
    query = _graph_query(ontology)
    graph_host = _GraphHost(_graph_result(ontology, query))
    store = _GraphReceiptStore()
    graph_tool = l6.L6VerifiedGraphTool(
        delegate=graph_host,
        graph_receipt_authority=store,
    )
    graph_input = l6.L6GraphToolInput(
        resolved_ontology_scope_id=ontology.resolved_ontology_scope_id,
        resolved_ontology_scope_hash=ontology.resolved_scope_hash,
        graph_query=query,
    )
    graph_output = graph_tool.execute(
        graph_input,
        ontology_scope=scopes.ontology_scope,
        retrieval_scope=scopes.retrieval_scope,
        budget=budget,
    )
    assert graph_output.graph_execution_receipt.graph_result_hash == (
        graph_output.graph_result.response_hash
    )
    assert graph_host.calls == 1

    citation_request = l6.L6CitationToolInput(
        coverage_receipt_id=evidence_result.coverage.coverage_receipt_id,
        coverage_receipt_hash=evidence_result.coverage.coverage_receipt_hash,
        citation_envelope_ids=tuple(
            sorted(
                item.search_citation_envelope_id
                for item in evidence_result.citations
            )
        ),
    )
    citation_collection = l6.assemble_l6_citation_collection(
        citation_request,
        citations=evidence_result.citations,
        presentations=_stable_presentations(evidence_result),
        coverage=evidence_result.coverage,
        context=context,
        budget=budget,
        retrieval_scope=retrieval,
        originating_context=origin,
        originating_budget=origin_budget,
    )
    readiness_input = l6.L6ReadinessToolInput(
        graph_execution_receipt_id=(
            graph_output.graph_execution_receipt.graph_execution_receipt_id
        ),
        graph_execution_receipt_hash=(
            graph_output.graph_execution_receipt.receipt_hash
        ),
        coverage_receipt_id=evidence_result.coverage.coverage_receipt_id,
        coverage_receipt_hash=evidence_result.coverage.coverage_receipt_hash,
        citation_collection_hash=citation_collection.collection_hash,
    )
    evidence_output_values = {
        "graph_execution_receipt_id": (
            graph_output.graph_execution_receipt.graph_execution_receipt_id
        ),
        "graph_execution_receipt_hash": (
            graph_output.graph_execution_receipt.receipt_hash
        ),
        "citations": evidence_result.citations,
        "presentations": _stable_presentations(evidence_result),
        "coverage_receipt": evidence_result.coverage,
    }
    evidence_output = l6.L6EvidenceToolOutput(
        **evidence_output_values,
        output_hash=canonical_sha256(evidence_output_values),
    )
    report = l6.build_l6_readiness_report(
        readiness_input,
        graph_receipt=graph_output.graph_execution_receipt,
        evidence_output=evidence_output,
        citation_collection=citation_collection,
    )
    assert report.readiness.status == "complete"

    with pytest.raises(ValueError, match="Graph receipt mismatch"):
        l6.build_l6_readiness_report(
            readiness_input.model_copy(
                update={"graph_execution_receipt_hash": "f" * 64}
            ),
            graph_receipt=graph_output.graph_execution_receipt,
            evidence_output=evidence_output,
            citation_collection=citation_collection,
        )
    with pytest.raises(ValueError, match="citation collection"):
        l6.build_l6_readiness_report(
            readiness_input.model_copy(
                update={"citation_collection_hash": "e" * 64}
            ),
            graph_receipt=graph_output.graph_execution_receipt,
            evidence_output=evidence_output,
            citation_collection=citation_collection,
        )
    cross_values = graph_output.graph_execution_receipt.model_dump(
        mode="python",
        exclude={"receipt_hash"},
        round_trip=True,
    )
    cross_values["resolved_retrieval_scope_hash"] = "f" * 64
    cross_receipt = l6.L6GraphExecutionReceipt(
        **cross_values,
        receipt_hash=canonical_sha256(cross_values),
    )
    cross_input = readiness_input.model_copy(
        update={"graph_execution_receipt_hash": cross_receipt.receipt_hash}
    )
    with pytest.raises(ValueError, match="not authorized"):
        l6.build_l6_readiness_report(
            cross_input,
            graph_receipt=cross_receipt,
            evidence_output=evidence_output,
            citation_collection=citation_collection,
        )


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
    retrieval_input = definitions[2]["input_schema"]["properties"]
    assert "graph_execution_receipt_id" in retrieval_input
    assert "graph_execution_receipt_hash" in retrieval_input
    assert "graph_execution_receipt" in definitions[1]["output_schema"]["properties"]
    assert (
        definitions[3]["output_schema"]["title"]
        == "L6CitationPresentationCollection"
    )
    assert definitions[4]["output_schema"]["title"] == "L6ReadinessReport"
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
