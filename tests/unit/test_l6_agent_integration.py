from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import hashlib
import hmac
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from fabric_kg_builder.agent import l6_integration as l6
from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
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
    monkeypatch.setattr(
        l6,
        "require_l5a_publication_receipt",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        l6,
        "require_l5b_publication_receipt",
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


class _GraphReceiptStore(l6.L6InMemoryGraphReceiptAuthority):
    def __init__(self):
        super().__init__()
        self.verify_calls = 0

    def verify_and_consume(
        self,
        receipt_id,
        receipt_hash,
        expectation,
        retrieval_claim_hash,
    ):
        self.verify_calls += 1
        return super().verify_and_consume(
            receipt_id,
            receipt_hash,
            expectation,
            retrieval_claim_hash,
        )


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
                "access_policy_id": "policy:l6",
                "access_policy_hash": "f" * 64,
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


def _graph_query(scope, *, required_ids=GENERIC_MEMBER_IDS, run_seed="unit-l6"):
    run_id = "l6r-sha256:" + canonical_sha256({"run": run_seed})
    return l6.L6GraphQuery.seal(
        l6_run_id=run_id,
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


def _issue_graph_receipt(
    store,
    query,
    graph,
    ontology,
    retrieval,
    budget,
    *,
    access=None,
    authorities=None,
):
    access = access or _access()
    authorities = authorities or _authorities()
    completed = store.execute_graph_once(
        l6_run_id=query.l6_run_id,
        graph_query=query,
        ontology_scope=ontology,
        retrieval_scope=retrieval,
        budget=budget,
        access=access,
        authorities=authorities,
        execute=lambda: graph,
    )
    return store.issue(
        graph_query=query,
        graph_result=completed,
        ontology_scope=ontology,
        retrieval_scope=retrieval,
        budget=budget,
        access=access,
        authorities=authorities,
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


def _principal_scope():
    values = {
        "principal_type": "managed_identity",
        "principal_id": "principal:l6",
        "resource_scope_ids": ("resource:l6",),
    }
    return SimpleNamespace(
        **values,
        model_dump=lambda **kwargs: values,
    )


def _access():
    principal_scope = _principal_scope()
    return l6.L6AccessContext(
        principal_type=principal_scope.principal_type,
        principal_id=principal_scope.principal_id,
        principal_scope_hash=canonical_sha256(
            principal_scope.model_dump(mode="json")
        ),
        access_policy_id="policy:l6",
        access_policy_hash="f" * 64,
        project_scope_id="project:generic",
    )


def _authorities():
    policy = SimpleNamespace(
        access_policy_id="policy:l6",
        policy_hash="f" * 64,
        allowed_operations=("content", "metadata"),
        principal_scopes=(_principal_scope(),),
    )
    asset = SimpleNamespace(
        governed_asset_reference_id="governed-asset:manual",
        asset_reference_hash=HASH_D,
        source_file_id="source-file:manual",
        content_hash="b" * 64,
        access_policy_id=policy.access_policy_id,
        access_policy_hash=policy.policy_hash,
        validate_access_policy=lambda candidate: None,
    )
    l5a = SimpleNamespace(
        compiled=SimpleNamespace(
            source=SimpleNamespace(),
            fingerprint="1" * 64,
            crosswalks=(),
            access_policy=policy,
        ),
        receipt=SimpleNamespace(receipt_hash="2" * 64),
        output_manifest=SimpleNamespace(manifest_hash="3" * 64),
    )
    l5b = SimpleNamespace(
        compiled=SimpleNamespace(
            fingerprint="4" * 64,
            index_fingerprint="a" * 64,
            access_policy=policy,
            governed_assets=(asset,),
        ),
        receipt=SimpleNamespace(receipt_hash="6" * 64),
        output_manifest=SimpleNamespace(manifest_hash="7" * 64),
    )
    return l6.L6Authorities(
        l5a=l5a,
        l5b=l5b,
        access_policy=policy,
        governed_assets=(asset,),
    )


def _orchestrator(monkeypatch, graph_result, evidence_result):
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
    receipt = _issue_graph_receipt(
        store, query, graph, ontology, retrieval, budget
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


def _claimed_evidence_capability():
    tool, _, store, request, kwargs = _standalone_evidence_boundary()
    output = tool.retrieve(request, **kwargs)
    collection = l6.assemble_l6_citation_collection(
        l6.L6CitationToolInput(
            coverage_receipt_id=output.coverage.coverage_receipt_id,
            coverage_receipt_hash=output.coverage.coverage_receipt_hash,
            citation_envelope_ids=tuple(
                sorted(
                    item.search_citation_envelope_id
                    for item in output.citations
                )
            ),
        ),
        citations=output.citations,
        presentations=output.presentations,
        coverage=output.coverage,
        context=kwargs["context"],
        budget=kwargs["budget"],
        retrieval_scope=kwargs["retrieval_scope"],
        originating_context=kwargs["originating_context"],
        originating_budget=kwargs["originating_budget"],
    )
    graph_receipt = store._receipts[request.graph_execution_receipt_id]
    return store, graph_receipt, output, collection


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
    authority = orchestrator._graph_receipt_authority
    result.validate_trusted(
        receipt_authority=authority,
    )
    with pytest.raises(ValueError, match="replayed"):
        result.validate_trusted(receipt_authority=authority)
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
def test_orchestrator_single_use_is_atomic_under_concurrency(monkeypatch):
    evidence_result, context, budget, _, _ = _evidence()
    ontology = resolved_ontology_scope()
    query = _graph_query(ontology)
    orchestrator, _, graph, evidence = _orchestrator(
        monkeypatch,
        _graph_result(ontology, query),
        evidence_result,
    )
    request = l6.L6RunRequest(
        question="detail",
        ontology_scope_envelope=ontology_scope(),
        graph_query=query,
        request_context=context,
        query_budget=budget,
        access=_access(),
    )
    start = threading.Barrier(3)

    def invoke():
        start.wait()
        try:
            return orchestrator.run(request)
        except RuntimeError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(invoke) for _ in range(2)]
        start.wait()
        outcomes = [future.result() for future in futures]

    assert sum(isinstance(item, l6.L6SynthesisInput) for item in outcomes) == 1
    assert sum(isinstance(item, RuntimeError) for item in outcomes) == 1
    assert graph.calls == 1
    assert evidence.calls == 1


@pytest.mark.unit
def test_evidence_receipt_is_authenticated_bound_and_single_use(monkeypatch):
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
    receipt = result.evidence_execution_receipt
    authority = orchestrator._graph_receipt_authority
    authority.verify_and_consume_evidence(receipt)
    with pytest.raises(ValueError, match="replayed"):
        authority.verify_and_consume_evidence(receipt)

    values = receipt.model_dump(
        mode="python",
        exclude={"receipt_hash"},
        round_trip=True,
    )
    values["graph_execution_receipt_hash"] = "f" * 64
    forged = l6.L6EvidenceExecutionReceipt(
        **values,
        receipt_hash=canonical_sha256(values),
    )
    with pytest.raises(ValueError, match="authentication failed"):
        l6._verify_evidence_receipt_trust(
            forged,
            keyring_provider=authority.keyring_provider,
            now_milliseconds=authority._clock_milliseconds(),
        )


@pytest.mark.unit
def test_one_idempotent_evidence_capability_per_graph_receipt():
    store, graph_receipt, output, collection = _claimed_evidence_capability()
    first = store.issue_evidence(
        graph_receipt=graph_receipt,
        evidence_output=output,
        citation_collection=collection,
    )
    second = store.issue_evidence(
        graph_receipt=graph_receipt,
        evidence_output=output,
        citation_collection=collection,
    )
    assert first == second
    store.verify_and_consume_evidence(first)
    with pytest.raises(ValueError):
        store.issue_evidence(
            graph_receipt=graph_receipt,
            evidence_output=output,
            citation_collection=collection,
        )


@pytest.mark.unit
def test_evidence_capability_rejects_wrong_claim_and_unconsumed_graph():
    store, graph_receipt, output, collection = _claimed_evidence_capability()
    values = output.model_dump(
        mode="python",
        exclude={"output_hash"},
        round_trip=True,
    )
    values["retrieval_claim_hash"] = "f" * 64
    wrong_claim = l6.L6EvidenceToolOutput(
        **values,
        output_hash=canonical_sha256(values),
    )
    with pytest.raises(ValueError, match="retrieval claim"):
        store.issue_evidence(
            graph_receipt=graph_receipt,
            evidence_output=wrong_claim,
            citation_collection=collection,
        )

    ontology = resolved_ontology_scope()
    retrieval = resolved_retrieval_scope()
    query = _graph_query(ontology, run_seed="unconsumed-evidence")
    _, _, budget, _, _ = _evidence()
    graph = _graph_result(ontology, query)
    unconsumed = _issue_graph_receipt(
        store, query, graph, ontology, retrieval, budget
    )
    with pytest.raises(ValueError):
        store.issue_evidence(
            graph_receipt=unconsumed,
            evidence_output=output,
            citation_collection=collection,
        )


@pytest.mark.unit
def test_concurrent_evidence_issuers_receive_one_capability():
    store, graph_receipt, output, collection = _claimed_evidence_capability()
    barrier = threading.Barrier(5)

    def issue():
        barrier.wait()
        return store.issue_evidence(
            graph_receipt=graph_receipt,
            evidence_output=output,
            citation_collection=collection,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(issue) for _ in range(4)]
        barrier.wait()
        receipts = [future.result() for future in futures]
    assert len({item.evidence_execution_receipt_id for item in receipts}) == 1


@pytest.mark.unit
def test_graph_revocation_invalidates_evidence_signed_by_new_active_key():
    store, graph_receipt, output, collection = _claimed_evidence_capability()
    now = store._clock_milliseconds()
    new_key = b"evidence-rotation-key".ljust(32, b"\0")
    new_authenticator = l6._L6HmacGraphReceiptAuthenticator(new_key)
    new_metadata = l6.L6AuthorityKeyMetadata(
        authority_id="gxra-sha256:" + canonical_sha256(new_key.hex()),
        authority_version=2,
        algorithm="HMAC-SHA256",
        not_before_milliseconds=now - 1,
        not_after_milliseconds=now + 100_000,
        state="active",
    )
    old_key = store.keyring_provider.snapshot().keys[0]
    store.keyring_provider.replace(
        l6.L6AuthorityKeyringSnapshot(
            snapshot_version=2,
            keys=(
                old_key,
                l6.L6TrustedAuthorityKey(
                    metadata=new_metadata,
                    authenticator=new_authenticator,
                ),
            ),
        )
    )
    evidence_receipt = store.issue_evidence(
        graph_receipt=graph_receipt,
        evidence_output=output,
        citation_collection=collection,
    )
    assert evidence_receipt.authority_version == 2
    store.keyring_provider.replace(
        l6.L6AuthorityKeyringSnapshot(
            snapshot_version=3,
            keys=(
                l6.L6TrustedAuthorityKey(
                    metadata=old_key.metadata.model_copy(
                        update={"state": "revoked"}
                    ),
                    authenticator=old_key.authenticator,
                ),
                l6.L6TrustedAuthorityKey(
                    metadata=new_metadata,
                    authenticator=new_authenticator,
                ),
            ),
        )
    )
    with pytest.raises(ValueError, match="Graph receipt authentication"):
        store.verify_and_consume_evidence(evidence_receipt)


@pytest.mark.unit
def test_evidence_issue_uses_one_keyring_snapshot():
    store, graph_receipt, output, collection = _claimed_evidence_capability()
    original = store.keyring_provider

    class _CountingProvider:
        def __init__(self):
            self.calls = 0

        def snapshot(self):
            self.calls += 1
            return original.snapshot()

    counting = _CountingProvider()
    store.keyring_provider = counting
    store.issue_evidence(
        graph_receipt=graph_receipt,
        evidence_output=output,
        citation_collection=collection,
    )
    assert counting.calls == 1


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
def test_server_side_receipt_authority_reuses_one_run_receipt():
    ontology = resolved_ontology_scope()
    retrieval = resolved_retrieval_scope()
    query = _graph_query(ontology)
    graph = _graph_result(ontology, query)
    store = l6.L6InMemoryGraphReceiptAuthority()
    _, _, budget, _, _ = _evidence()
    receipt = _issue_graph_receipt(
        store, query, graph, ontology, retrieval, budget
    )
    assert store.verify_and_consume(
        receipt.graph_execution_receipt_id,
        receipt.receipt_hash,
        l6._receipt_expectation(
            ontology,
            retrieval,
            _evidence()[1],
        ),
        "a" * 64,
    ) == receipt
    second = store.issue(
        graph_query=query,
        graph_result=graph,
        ontology_scope=ontology,
        retrieval_scope=retrieval,
        budget=budget,
        access=_access(),
        authorities=_authorities(),
    )
    assert second == receipt
    with pytest.raises(ValueError, match="replayed"):
        store.verify_and_consume(
            receipt.graph_execution_receipt_id,
            receipt.receipt_hash,
            l6._receipt_expectation(
                ontology,
                retrieval,
                _evidence()[1],
            ),
            "a" * 64,
        )


@pytest.mark.unit
def test_external_receipt_authenticator_supports_multi_process_verification():
    ontology = resolved_ontology_scope()
    retrieval = resolved_retrieval_scope()
    query = _graph_query(ontology)
    graph = _graph_result(ontology, query)
    _, _, budget, _, _ = _evidence()
    store = l6.L6InMemoryGraphReceiptAuthority()
    receipt = _issue_graph_receipt(
        store, query, graph, ontology, retrieval, budget
    )
    key = b"durable-verifier-key".ljust(32, b"\0")
    authority_id = "gxra-sha256:" + canonical_sha256(key.hex())

    class _DurableVerifier:
        def verify(self, payload, authentication_tag):
            expected = hmac.new(
                key,
                canonical_json(payload).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            return hmac.compare_digest(authentication_tag, expected)

    values = receipt.model_dump(
        mode="python",
        exclude={"authentication_tag", "receipt_hash"},
        round_trip=True,
    )
    values["authority_id"] = authority_id
    values["authority_version"] = 7
    values["issued_at_milliseconds"] = 1_000
    authentication_tag = hmac.new(
        key,
        canonical_json(values).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    sealed = {**values, "authentication_tag": authentication_tag}
    restored = l6.L6GraphExecutionReceipt(
        **sealed,
        receipt_hash=canonical_sha256(sealed),
    )
    assert restored.authority_id == authority_id
    snapshot = l6.L6AuthorityKeyringSnapshot(
        snapshot_version=1,
        keys=(
            l6.L6TrustedAuthorityKey(
                metadata=l6.L6AuthorityKeyMetadata(
                    authority_id=authority_id,
                    authority_version=7,
                    algorithm="HMAC-SHA256",
                    not_before_milliseconds=500,
                    not_after_milliseconds=2_000,
                    state="active",
                ),
                authenticator=_DurableVerifier(),
            ),
        ),
    )
    assert snapshot.verify(
        authority_id=restored.authority_id,
        authority_version=restored.authority_version,
        algorithm=restored.authentication_algorithm,
        issued_at_milliseconds=restored.issued_at_milliseconds,
        payload=l6._graph_receipt_auth_payload(restored.model_dump(mode="json")),
        authentication_tag=restored.authentication_tag,
        now_milliseconds=1_500,
    )


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
        _issue_graph_receipt(
            l6.L6InMemoryGraphReceiptAuthority(),
            narrowed,
            narrowed_graph,
            ontology,
            retrieval,
            budget,
        )


@pytest.mark.unit
@pytest.mark.parametrize("state", ("revoked", "disabled"))
def test_keyring_revocation_and_disable_fail_closed(state):
    clock = [1_000]
    store = l6.L6InMemoryGraphReceiptAuthority(
        clock_milliseconds=lambda: clock[0],
        validity_milliseconds=1_000,
    )
    ontology = resolved_ontology_scope()
    retrieval = resolved_retrieval_scope()
    query = _graph_query(ontology)
    _, _, budget, _, _ = _evidence()
    graph = _graph_result(ontology, query)
    receipt = _issue_graph_receipt(
        store, query, graph, ontology, retrieval, budget
    )
    metadata = store._metadata.model_copy(update={"state": state})
    store.keyring_provider.replace(
        l6.L6AuthorityKeyringSnapshot(
            snapshot_version=2,
            keys=(
                l6.L6TrustedAuthorityKey(
                    metadata=metadata,
                    authenticator=store._authenticator,
                ),
            ),
        )
    )
    with pytest.raises(ValueError, match="authentication failed"):
        l6._verify_graph_receipt_trust(
            receipt,
            keyring_provider=store.keyring_provider,
            now_milliseconds=clock[0],
        )


@pytest.mark.unit
def test_keyring_expiry_not_yet_valid_and_rotation():
    clock = [1_000]
    store = l6.L6InMemoryGraphReceiptAuthority(
        clock_milliseconds=lambda: clock[0],
        validity_milliseconds=100,
    )
    ontology = resolved_ontology_scope()
    retrieval = resolved_retrieval_scope()
    query = _graph_query(ontology)
    _, _, budget, _, _ = _evidence()
    graph = _graph_result(ontology, query)
    receipt = _issue_graph_receipt(
        store, query, graph, ontology, retrieval, budget
    )
    clock[0] = 1_101
    with pytest.raises(ValueError, match="authentication failed"):
        l6._verify_graph_receipt_trust(
            receipt,
            keyring_provider=store.keyring_provider,
            now_milliseconds=clock[0],
        )
    future_metadata = store._metadata.model_copy(
        update={
            "not_before_milliseconds": 2_000,
            "not_after_milliseconds": 3_000,
        }
    )
    store.keyring_provider.replace(
        l6.L6AuthorityKeyringSnapshot(
            snapshot_version=2,
            keys=(
                l6.L6TrustedAuthorityKey(
                    metadata=future_metadata,
                    authenticator=store._authenticator,
                ),
            ),
        )
    )
    with pytest.raises(ValueError, match="authentication failed"):
        l6._verify_graph_receipt_trust(
            receipt,
            keyring_provider=store.keyring_provider,
            now_milliseconds=1_500,
        )
    new_key = b"rotated-signing-key".ljust(32, b"\0")
    new_authenticator = l6._L6HmacGraphReceiptAuthenticator(new_key)
    new_authority_id = "gxra-sha256:" + canonical_sha256(new_key.hex())
    rotated_metadata = l6.L6AuthorityKeyMetadata(
        authority_id=new_authority_id,
        authority_version=2,
        algorithm="HMAC-SHA256",
        not_before_milliseconds=1_100,
        not_after_milliseconds=3_000,
        state="active",
    )
    store.keyring_provider.replace(
        l6.L6AuthorityKeyringSnapshot(
            snapshot_version=3,
            keys=(
                l6.L6TrustedAuthorityKey(
                    metadata=store._metadata.model_copy(
                        update={"state": "revoked"}
                    ),
                    authenticator=store._authenticator,
                ),
                l6.L6TrustedAuthorityKey(
                    metadata=rotated_metadata,
                    authenticator=new_authenticator,
                ),
            ),
        )
    )
    clock[0] = 1_500
    rotated_query = _graph_query(ontology, run_seed="rotated-key")
    rotated_graph = _graph_result(ontology, rotated_query)
    rotated = _issue_graph_receipt(
        store,
        rotated_query,
        rotated_graph,
        ontology,
        retrieval,
        budget,
    )
    assert rotated.authority_id == new_authority_id
    assert rotated.authority_version == 2
    l6._verify_graph_receipt_trust(
        rotated,
        keyring_provider=store.keyring_provider,
        now_milliseconds=clock[0],
    )


@pytest.mark.unit
def test_keyring_snapshot_replacement_is_atomic_under_concurrency():
    store = l6.L6InMemoryGraphReceiptAuthority()
    provider = store.keyring_provider
    errors = []

    def reader():
        for _ in range(500):
            snapshot = provider.snapshot()
            if not isinstance(snapshot.keys, tuple) or not snapshot.keys:
                errors.append("partial snapshot")

    def writer():
        for version in range(2, 30):
            prior = provider.snapshot()
            provider.replace(
                l6.L6AuthorityKeyringSnapshot(
                    snapshot_version=version,
                    keys=prior.keys,
                )
            )

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(reader) for _ in range(4)]
        futures.append(pool.submit(writer))
        for future in futures:
            future.result()
    assert errors == []
    assert provider.snapshot().snapshot_version == 29


@pytest.mark.unit
def test_keyring_snapshot_normalizes_mutable_input_to_tuple():
    store = l6.L6InMemoryGraphReceiptAuthority()
    mutable_keys = list(store.keyring_provider.snapshot().keys)
    snapshot = l6.L6AuthorityKeyringSnapshot(
        snapshot_version=2,
        keys=mutable_keys,
    )
    store.keyring_provider.replace(snapshot)
    mutable_keys.clear()
    assert isinstance(store.keyring_provider.snapshot().keys, tuple)
    assert len(store.keyring_provider.snapshot().keys) == 1


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
    citation_values["access_policy_hash"] = "8" * 64
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
def test_partial_package_requires_exact_verified_subset_evidence(monkeypatch):
    evidence_result, context, budget, _, _ = _evidence(
        status="partial",
        observed={"observed_search_candidate_records": 51},
    )
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
    assert result.status == "partial"
    assert result.synthesis_call_limit == 1
    assert result.coverage_receipt.coverage_status == "partial"
    assert result.search_citations
    assert result.citation_collection is not None

    missing_collection = result.model_dump(
        mode="python",
        exclude={"package_hash"},
        round_trip=True,
    )
    missing_collection["citation_collection"] = None
    with pytest.raises(ValidationError, match="non-abstain"):
        l6.L6SynthesisInput(
            **missing_collection,
            package_hash=canonical_sha256(missing_collection),
        )

    forged_values = result.search_citations[0].model_dump(
        mode="python",
        exclude={"citation_hash"},
        round_trip=True,
    )
    forged_values["original_document_name"] = "FORGED"
    forged = SearchCitationEnvelope(
        **forged_values,
        citation_hash=canonical_sha256(forged_values),
    )
    forged_package = result.model_dump(
        mode="python",
        exclude={"package_hash"},
        round_trip=True,
    )
    forged_package["search_citations"] = (
        forged,
        *result.search_citations[1:],
    )
    with pytest.raises(ValidationError):
        l6.L6SynthesisInput(
            **forged_package,
            package_hash=canonical_sha256(forged_package),
        )


@pytest.mark.unit
def test_abstain_package_rejects_injected_citation(monkeypatch):
    evidence_result, context, budget, _, _ = _evidence()
    ontology = resolved_ontology_scope()
    query = _graph_query(ontology)
    empty_graph = l6.L6GraphResult.seal(
        graph_request_id=query.graph_request_id,
        graph_request_hash=query.request_hash,
        canonical_scope_id=ontology.canonical_scope_id,
        assertions=(),
        returned_canonical_ids=(),
        warning_codes=(),
        truncated=False,
        source_error=False,
        accounting=l6.L6OperationAccounting(
            operation_refs=(_operation_ref("abstain-empty"),),
            request_count=1,
            request_bytes=1,
            response_bytes=1,
            retry_count=0,
            retry_wait_milliseconds=0,
            duration_milliseconds=1,
        ),
    )
    orchestrator, _, _, _ = _orchestrator(
        monkeypatch,
        empty_graph,
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
    assert result.synthesis_call_limit == 0
    values = result.model_dump(
        mode="python",
        exclude={"package_hash"},
        round_trip=True,
    )
    values["search_citations"] = (evidence_result.citations[0],)
    with pytest.raises(ValidationError, match="cannot expose synthesis evidence"):
        l6.L6SynthesisInput(
            **values,
            package_hash=canonical_sha256(values),
        )


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
    run_id = "l6r-sha256:" + canonical_sha256({"run": "over-budget"})
    query = l6.L6GraphQuery.seal(
        l6_run_id=run_id,
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
def test_synthesis_input_rejects_rehashed_citation_not_in_coverage(monkeypatch):
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
    citation_values = result.search_citations[0].model_dump(
        mode="python",
        exclude={"citation_hash"},
        round_trip=True,
    )
    citation_values["original_document_name"] = "FORGED"
    forged = SearchCitationEnvelope(
        **citation_values,
        citation_hash=canonical_sha256(citation_values),
    )
    values = result.model_dump(
        mode="python",
        exclude={"package_hash"},
        round_trip=True,
    )
    values["search_citations"] = (forged, *result.search_citations[1:])
    with pytest.raises(ValidationError):
        l6.L6SynthesisInput(
            **values,
            package_hash=canonical_sha256(values),
        )

    assertion_values = result.graph_assertions[0].model_dump(
        mode="python",
        exclude={"assertion_hash"},
    )
    assertion_values["graph_path_id"] = "graph-path:forged"
    forged_assertion = l6.L6GraphAssertion(
        **assertion_values,
        assertion_hash=canonical_sha256(assertion_values),
    )
    graph_values = result.model_dump(
        mode="python",
        exclude={"package_hash"},
        round_trip=True,
    )
    graph_values["graph_assertions"] = (
        forged_assertion,
        *result.graph_assertions[1:],
    )
    with pytest.raises(ValidationError):
        l6.L6SynthesisInput(
            **graph_values,
            package_hash=canonical_sha256(graph_values),
        )

    collection_values = result.citation_collection.model_dump(
        mode="python",
        exclude={"collection_hash"},
        round_trip=True,
    )
    collection_values["source_response_hashes"] = ("f" * 64,)
    forged_collection = l6.L6CitationPresentationCollection(
        **collection_values,
        collection_hash=canonical_sha256(collection_values),
    )
    response_values = result.model_dump(
        mode="python",
        exclude={"package_hash"},
        round_trip=True,
    )
    response_values["citation_collection"] = forged_collection
    with pytest.raises(ValidationError):
        l6.L6SynthesisInput(
            **response_values,
            package_hash=canonical_sha256(response_values),
        )
    unsafe_collection_values = result.citation_collection.model_dump(
        mode="python",
        exclude={"collection_hash"},
        round_trip=True,
    )
    unsafe_collection_values["source_response_hashes"] = (
        "https://private.example/x?sig=secret",
    )
    with pytest.raises(ValidationError, match="SHA-256"):
        l6.L6CitationPresentationCollection(
            **unsafe_collection_values,
            collection_hash=canonical_sha256(unsafe_collection_values),
        )
    for field_name, forged_value in (
        ("canonical_scope_id", "scope:forged"),
        ("resolved_ontology_scope_hash", "f" * 64),
        ("resolved_retrieval_scope_id", "retrieval:forged"),
        ("graph_request_hash", "e" * 64),
        ("graph_response_hash", "d" * 64),
    ):
        identity_values = result.model_dump(
            mode="python",
            exclude={"package_hash"},
            round_trip=True,
        )
        identity_values[field_name] = forged_value
        with pytest.raises(ValidationError):
            l6.L6SynthesisInput(
                **identity_values,
                package_hash=canonical_sha256(identity_values),
            )


@pytest.mark.unit
def test_complete_evidence_output_rejects_empty_citations():
    evidence_result, _, _, _, _ = _evidence()
    values = {
        "graph_execution_receipt_id": "gxr-sha256:" + "a" * 64,
        "graph_execution_receipt_hash": "b" * 64,
        "retrieval_claim_hash": "c" * 64,
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
    with pytest.raises(ValidationError, match="unsafe stable text"):
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
        "retrieval_claim_hash": "c" * 64,
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
    ("field_name", "unsafe_value"),
    (
        ("original_document_name", "https://private.example/file?sig=secret"),
        ("exact_authorized_quote", "See file://private/path"),
        ("source_id", "principal:user@example.com"),
        ("chunk_id", "provider:internal-backend"),
        (
            "original_document_name",
            "%68%74%74%70%73%3A%2F%2Fprivate.example%2Ffile%3Fsig%3Dsecret",
        ),
        ("exact_authorized_quote", "ＡＰＩ＿ＫＥＹ=secret"),
        ("source_id", "prіncipal:user"),
        ("source_id", "principaӏ:user"),
        ("exact_authorized_quote", "tоken=secret"),
        ("exact_authorized_quote", "toкen=secret"),
        ("exact_authorized_quote", "user@例子.公司"),
        ("exact_authorized_quote", "user@xn--fsqu00a.xn--55qx5d"),
        ("exact_authorized_quote", "用户@例子.公司"),
        ("exact_authorized_quote", "用户@例子。公司"),
        ("exact_authorized_quote", "用户@例子．公司"),
        ("exact_authorized_quote", "用户@例子｡公司"),
        ("exact_authorized_quote", "user＠例子.公司"),
        ("exact_authorized_quote", "Contact:user@例子.公司"),
        ("exact_authorized_quote", "(user@例子.公司)"),
        ("exact_authorized_quote", "user@उदाहरण.भारत"),
        ("exact_authorized_quote", "user@example.com/"),
        ("exact_authorized_quote", "user@example.com#"),
        ("exact_authorized_quote", "Contact/user@例子.公司"),
        ("exact_authorized_quote", "customer!@example.com"),
        ("exact_authorized_quote", '"customer"@example.com'),
    ),
)
def test_direct_stable_constructor_rejects_unsafe_strings(
    field_name,
    unsafe_value,
):
    evidence_result, _, _, _, _ = _evidence()
    stable = _stable_presentations(evidence_result)[0]
    values = stable.model_dump(
        mode="python",
        exclude={"stable_presentation_hash"},
        round_trip=True,
    )
    values[field_name] = unsafe_value
    with pytest.raises(ValidationError, match="unsafe stable text"):
        l6.L6StableCitationPresentation(
            **values,
            stable_presentation_hash=canonical_sha256(values),
        )


@pytest.mark.unit
def test_direct_stable_constructor_rejects_unsafe_section_and_allows_unicode():
    evidence_result, _, _, _, _ = _evidence()
    stable = _stable_presentations(evidence_result)[0]
    values = stable.model_dump(
        mode="python",
        exclude={"stable_presentation_hash"},
        round_trip=True,
    )
    values["section_path"] = ("section:维修 café", "https://private.example")
    with pytest.raises(ValidationError, match="unsafe stable text"):
        l6.L6StableCitationPresentation(
            **values,
            stable_presentation_hash=canonical_sha256(values),
        )

    values["section_path"] = ("section:维修 café",)
    values["original_document_name"] = "维修手册 café.pdf"
    safe = l6.L6StableCitationPresentation(
        **values,
        stable_presentation_hash=canonical_sha256(values),
    )
    assert safe.original_document_name == "维修手册 café.pdf"
    assert safe.immutable_locator.model_dump(mode="json").get("blob_uri") is None

    for safe_text in (
        "Use @ marker for emphasis",
        "Model α: performance",
        "English Русский: summary",
    ):
        multilingual_values = dict(values)
        multilingual_values["exact_authorized_quote"] = safe_text
        multilingual_values["quote_hash"] = canonical_sha256(safe_text)
        constructed = l6.L6StableCitationPresentation(
            **multilingual_values,
            stable_presentation_hash=canonical_sha256(multilingual_values),
        )
        assert constructed.exact_authorized_quote == safe_text


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
    collection_values = collection.model_dump(
        mode="python",
        exclude={"collection_hash"},
        round_trip=True,
    )
    bindings = list(collection.presentation_source_bindings)
    binding_values = bindings[0].model_dump(mode="python")
    binding_values["source_citation_envelope_hash"] = "f" * 64
    bindings[0] = l6.L6PresentationSourceBinding.model_validate(binding_values)
    collection_values["presentation_source_bindings"] = tuple(bindings)
    with pytest.raises(ValidationError, match="source binding"):
        l6.L6CitationPresentationCollection(
            **collection_values,
            collection_hash=canonical_sha256(collection_values),
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
        authorities=_authorities(),
    )
    graph_input = l6.L6GraphToolInput(
        l6_run_id=query.l6_run_id,
        resolved_ontology_scope_id=ontology.resolved_ontology_scope_id,
        resolved_ontology_scope_hash=ontology.resolved_scope_hash,
        graph_query=query,
    )
    graph_output = graph_tool.execute(
        graph_input,
        ontology_scope=scopes.ontology_scope,
        retrieval_scope=scopes.retrieval_scope,
        budget=budget,
        access=_access(),
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
    evidence_output_values = {
        "graph_execution_receipt_id": (
            graph_output.graph_execution_receipt.graph_execution_receipt_id
        ),
        "graph_execution_receipt_hash": (
            graph_output.graph_execution_receipt.receipt_hash
        ),
        "retrieval_claim_hash": "c" * 64,
        "citations": evidence_result.citations,
        "presentations": _stable_presentations(evidence_result),
        "coverage_receipt": evidence_result.coverage,
    }
    evidence_output = l6.L6EvidenceToolOutput(
        **evidence_output_values,
        output_hash=canonical_sha256(evidence_output_values),
    )
    store.verify_and_consume(
        graph_output.graph_execution_receipt.graph_execution_receipt_id,
        graph_output.graph_execution_receipt.receipt_hash,
        l6._receipt_expectation(ontology, retrieval, context),
        evidence_output.retrieval_claim_hash,
    )
    evidence_receipt = store.issue_evidence(
        graph_receipt=graph_output.graph_execution_receipt,
        evidence_output=evidence_output,
        citation_collection=citation_collection,
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
        evidence_execution_receipt_id=(
            evidence_receipt.evidence_execution_receipt_id
        ),
        evidence_execution_receipt_hash=evidence_receipt.receipt_hash,
    )
    report = l6.build_l6_readiness_report(
        readiness_input,
        graph_receipt=graph_output.graph_execution_receipt,
        evidence_output=evidence_output,
        citation_collection=citation_collection,
        evidence_receipt=evidence_receipt,
        keyring_provider=store.keyring_provider,
        now_milliseconds=store._clock_milliseconds(),
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
            evidence_receipt=evidence_receipt,
            keyring_provider=store.keyring_provider,
            now_milliseconds=store._clock_milliseconds(),
        )
    with pytest.raises(ValueError, match="citation collection"):
        l6.build_l6_readiness_report(
            readiness_input.model_copy(
                update={"citation_collection_hash": "e" * 64}
            ),
            graph_receipt=graph_output.graph_execution_receipt,
            evidence_output=evidence_output,
            citation_collection=citation_collection,
            evidence_receipt=evidence_receipt,
            keyring_provider=store.keyring_provider,
            now_milliseconds=store._clock_milliseconds(),
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
    with pytest.raises(ValueError, match="authentication failed"):
        l6._verify_graph_receipt_trust(
            cross_receipt,
            keyring_provider=store.keyring_provider,
            now_milliseconds=store._clock_milliseconds(),
        )


def _standalone_graph_tool(query, graph, store=None, host=None):
    ontology = resolved_ontology_scope()
    retrieval = resolved_retrieval_scope()
    _, _, budget, _, _ = _evidence()
    authority = store or l6.L6InMemoryGraphReceiptAuthority()
    delegate = host or _GraphHost(graph)
    tool = l6.L6VerifiedGraphTool(
        delegate=delegate,
        graph_receipt_authority=authority,
        authorities=_authorities(),
    )
    request = l6.L6GraphToolInput(
        l6_run_id=query.l6_run_id,
        resolved_ontology_scope_id=ontology.resolved_ontology_scope_id,
        resolved_ontology_scope_hash=ontology.resolved_scope_hash,
        graph_query=query,
    )
    kwargs = {
        "ontology_scope": ontology,
        "retrieval_scope": retrieval,
        "budget": budget,
        "access": _access(),
    }
    return tool, delegate, authority, request, kwargs


@pytest.mark.unit
def test_standalone_graph_retry_reuses_receipt_without_provider_call():
    ontology = resolved_ontology_scope()
    query = _graph_query(ontology, run_seed="sequential-duplicate")
    graph = _graph_result(ontology, query)
    tool, host, _, request, kwargs = _standalone_graph_tool(query, graph)

    first = tool.execute(request, **kwargs)
    second = tool.execute(request, **kwargs)

    assert second == first
    assert host.calls == 1


@pytest.mark.unit
def test_two_graph_tool_instances_share_one_run_authority():
    ontology = resolved_ontology_scope()
    query = _graph_query(ontology, run_seed="two-tool-instances")
    graph = _graph_result(ontology, query)
    store = l6.L6InMemoryGraphReceiptAuthority()
    host = _GraphHost(graph)
    first, _, _, request, kwargs = _standalone_graph_tool(
        query, graph, store=store, host=host
    )
    second, _, _, _, _ = _standalone_graph_tool(
        query, graph, store=store, host=host
    )

    outputs = (first.execute(request, **kwargs), second.execute(request, **kwargs))

    assert outputs[0] == outputs[1]
    assert host.calls == 1


@pytest.mark.unit
def test_concurrent_standalone_graph_duplicate_has_one_provider_call():
    ontology = resolved_ontology_scope()
    query = _graph_query(ontology, run_seed="concurrent-duplicate")
    graph = _graph_result(ontology, query)

    class _BlockingGraphHost(_GraphHost):
        def __init__(self, result):
            super().__init__(result)
            self.entered = threading.Event()
            self.release = threading.Event()

        def execute(self, request, *, scope):
            self.calls += 1
            self.entered.set()
            assert self.release.wait(timeout=5)
            return self.result

    host = _BlockingGraphHost(graph)
    tool, _, _, request, kwargs = _standalone_graph_tool(
        query, graph, host=host
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(tool.execute, request, **kwargs)
        assert host.entered.wait(timeout=5)
        second = pool.submit(tool.execute, request, **kwargs)
        host.release.set()
        outputs = (first.result(), second.result())

    assert outputs[0] == outputs[1]
    assert host.calls == 1


@pytest.mark.unit
def test_same_run_rejects_different_graph_request_before_provider_call():
    ontology = resolved_ontology_scope()
    query = _graph_query(ontology, run_seed="different-request")
    graph = _graph_result(ontology, query)
    tool, host, store, request, kwargs = _standalone_graph_tool(query, graph)
    tool.execute(request, **kwargs)
    changed_values = query.model_dump(
        mode="python",
        exclude={"graph_request_id", "request_hash"},
        round_trip=True,
    )
    changed_values["max_result_records"] -= 1
    changed = l6.L6GraphQuery.seal(**changed_values)
    changed_request = request.model_copy(
        update={"graph_query": changed}
    )
    changed_tool = l6.L6VerifiedGraphTool(
        delegate=host,
        graph_receipt_authority=store,
        authorities=_authorities(),
    )

    with pytest.raises(ValueError, match="different Graph execution authority"):
        changed_tool.execute(changed_request, **kwargs)
    assert host.calls == 1


@pytest.mark.unit
def test_failed_graph_run_is_consumed_and_identical_retry_is_closed():
    ontology = resolved_ontology_scope()
    query = _graph_query(ontology, run_seed="failed-run")
    graph = _graph_result(ontology, query)

    class _FailingGraphHost(_GraphHost):
        def execute(self, request, *, scope):
            self.calls += 1
            raise RuntimeError("provider detail must not escape")

    host = _FailingGraphHost(graph)
    tool, _, _, request, kwargs = _standalone_graph_tool(
        query, graph, host=host
    )
    with pytest.raises(RuntimeError, match="provider detail"):
        tool.execute(request, **kwargs)
    with pytest.raises(ValueError, match="previously failed"):
        tool.execute(request, **kwargs)
    assert host.calls == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "dimension",
    (
        "ontology_scope",
        "retrieval_scope",
        "acl",
        "publication",
        "graph_model",
        "l5a_readback",
        "budget",
    ),
)
def test_same_run_rejects_changed_execution_authority_fingerprint(dimension):
    ontology = resolved_ontology_scope()
    retrieval = resolved_retrieval_scope()
    query = _graph_query(ontology, run_seed=f"fingerprint-{dimension}")
    graph = _graph_result(ontology, query)
    _, _, budget, _, _ = _evidence()
    access = _access()
    authorities = _authorities()
    store = l6.L6InMemoryGraphReceiptAuthority()
    calls = [0]

    def execute():
        calls[0] += 1
        return graph

    first = {
        "l6_run_id": query.l6_run_id,
        "graph_query": query,
        "ontology_scope": ontology,
        "retrieval_scope": retrieval,
        "budget": budget,
        "access": access,
        "authorities": authorities,
        "execute": execute,
    }
    assert store.execute_graph_once(**first) == graph
    changed = dict(first)
    if dimension == "ontology_scope":
        values = ontology.model_dump(
            mode="python",
            exclude={"resolved_scope_hash"},
            round_trip=True,
        )
        values["resolved_ontology_scope_id"] += "-other"
        changed["ontology_scope"] = seal(
            type(ontology),
            "resolved_scope_hash",
            values,
        )
    elif dimension == "retrieval_scope":
        values = retrieval.model_dump(
            mode="python",
            exclude={"retrieval_scope_hash"},
            round_trip=True,
        )
        values["resolved_retrieval_scope_id"] += "-other"
        changed["retrieval_scope"] = seal(
            type(retrieval),
            "retrieval_scope_hash",
            values,
        )
    elif dimension == "acl":
        changed["access"] = access.model_copy(
            update={"access_policy_hash": "8" * 64}
        )
    elif dimension == "publication":
        values = ontology.model_dump(
            mode="python",
            exclude={"resolved_scope_hash"},
            round_trip=True,
        )
        values["asserted_publication_hash"] = "8" * 64
        changed["ontology_scope"] = seal(
            type(ontology),
            "resolved_scope_hash",
            values,
        )
    elif dimension == "graph_model":
        values = ontology.model_dump(
            mode="python",
            exclude={"resolved_scope_hash"},
            round_trip=True,
        )
        values["graph_model_hash"] = "8" * 64
        changed["ontology_scope"] = seal(
            type(ontology),
            "resolved_scope_hash",
            values,
        )
    elif dimension == "l5a_readback":
        changed["authorities"] = l6.L6Authorities(
            l5a=SimpleNamespace(
                compiled=authorities.l5a.compiled,
                receipt=SimpleNamespace(receipt_hash="8" * 64),
                output_manifest=authorities.l5a.output_manifest,
            ),
            l5b=authorities.l5b,
            access_policy=authorities.access_policy,
            governed_assets=authorities.governed_assets,
        )
    else:
        values = budget.model_dump(
            mode="python",
            exclude={"budget_hash"},
            round_trip=True,
        )
        values["max_output_tokens"] += 1
        changed["budget"] = seal(type(budget), "budget_hash", values)

    with pytest.raises(ValueError, match="different Graph execution authority"):
        store.execute_graph_once(**changed)
    assert calls == [1]


@pytest.mark.unit
def test_graph_receipt_binds_execution_authority_fingerprint():
    ontology = resolved_ontology_scope()
    retrieval = resolved_retrieval_scope()
    query = _graph_query(ontology, run_seed="receipt-fingerprint")
    graph = _graph_result(ontology, query)
    _, _, budget, _, _ = _evidence()
    access = _access()
    authorities = _authorities()
    store = l6.L6InMemoryGraphReceiptAuthority()

    receipt = _issue_graph_receipt(
        store,
        query,
        graph,
        ontology,
        retrieval,
        budget,
        access=access,
        authorities=authorities,
    )

    assert receipt.graph_execution_fingerprint == l6._graph_execution_fingerprint(
        graph_query=query,
        ontology_scope=ontology,
        retrieval_scope=retrieval,
        budget=budget,
        access=access,
        authorities=authorities,
    )


@pytest.mark.unit
def test_provider_baseexception_wakes_concurrent_waiter_and_consumes_run():
    class _ProviderAbort(BaseException):
        pass

    ontology = resolved_ontology_scope()
    retrieval = resolved_retrieval_scope()
    query = _graph_query(ontology, run_seed="baseexception")
    _, _, budget, _, _ = _evidence()
    store = l6.L6InMemoryGraphReceiptAuthority()
    entered = threading.Event()
    release = threading.Event()
    calls = [0]
    kwargs = {
        "l6_run_id": query.l6_run_id,
        "graph_query": query,
        "ontology_scope": ontology,
        "retrieval_scope": retrieval,
        "budget": budget,
        "access": _access(),
        "authorities": _authorities(),
    }

    def abort():
        calls[0] += 1
        entered.set()
        assert release.wait(timeout=5)
        raise _ProviderAbort()

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(store.execute_graph_once, **kwargs, execute=abort)
        assert entered.wait(timeout=5)
        waiter = pool.submit(
            store.execute_graph_once,
            **kwargs,
            execute=lambda: pytest.fail("waiter called provider"),
        )
        release.set()
        with pytest.raises(_ProviderAbort):
            owner.result(timeout=2)
        with pytest.raises(ValueError, match="previously failed"):
            waiter.result(timeout=2)
    assert calls == [1]


@pytest.mark.unit
def test_duplicate_waiter_times_out_under_budget_after_spurious_notify():
    ontology = resolved_ontology_scope()
    retrieval = resolved_retrieval_scope()
    query = _graph_query(ontology, run_seed="wait-timeout")
    graph = _graph_result(ontology, query)
    _, _, budget, _, _ = _evidence()
    values = budget.model_dump(
        mode="python",
        exclude={"budget_hash"},
        round_trip=True,
    )
    values["max_runtime_milliseconds"] = 50
    budget = seal(type(budget), "budget_hash", values)
    store = l6.L6InMemoryGraphReceiptAuthority()
    entered = threading.Event()
    release = threading.Event()
    calls = [0]
    kwargs = {
        "l6_run_id": query.l6_run_id,
        "graph_query": query,
        "ontology_scope": ontology,
        "retrieval_scope": retrieval,
        "budget": budget,
        "access": _access(),
        "authorities": _authorities(),
    }

    def hanging_owner():
        calls[0] += 1
        entered.set()
        assert release.wait(timeout=5)
        return graph

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(
            store.execute_graph_once,
            **kwargs,
            execute=hanging_owner,
        )
        assert entered.wait(timeout=5)
        waiter = pool.submit(
            store.execute_graph_once,
            **kwargs,
            execute=lambda: pytest.fail("waiter called provider"),
        )
        time.sleep(0.01)
        with store._run_condition:
            store._run_condition.notify_all()
        with pytest.raises(TimeoutError, match="sealed runtime budget"):
            waiter.result(timeout=2)
        assert not owner.done()
        release.set()
        assert owner.result(timeout=2) == graph
    assert calls == [1]


@pytest.mark.unit
def test_result_validation_failure_wakes_waiter_and_consumes_run():
    ontology = resolved_ontology_scope()
    retrieval = resolved_retrieval_scope()
    query = _graph_query(ontology, run_seed="invalid-result")
    graph = _graph_result(ontology, query).model_copy(
        update={"graph_request_hash": "8" * 64}
    )
    _, _, budget, _, _ = _evidence()
    store = l6.L6InMemoryGraphReceiptAuthority()
    entered = threading.Event()
    release = threading.Event()
    kwargs = {
        "l6_run_id": query.l6_run_id,
        "graph_query": query,
        "ontology_scope": ontology,
        "retrieval_scope": retrieval,
        "budget": budget,
        "access": _access(),
        "authorities": _authorities(),
    }

    def invalid_result():
        entered.set()
        assert release.wait(timeout=5)
        return graph

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(
            store.execute_graph_once,
            **kwargs,
            execute=invalid_result,
        )
        assert entered.wait(timeout=5)
        waiter = pool.submit(
            store.execute_graph_once,
            **kwargs,
            execute=lambda: pytest.fail("waiter called provider"),
        )
        release.set()
        with pytest.raises(ValueError, match="accounting or request binding"):
            owner.result(timeout=2)
        with pytest.raises(ValueError, match="previously failed"):
            waiter.result(timeout=2)


@pytest.mark.unit
@pytest.mark.parametrize(
    "unsafe_id",
    (
        "graph-request:l6",
        "https://provider.example/query",
        "/path/request",
        "request?sig=secret",
        "request#fragment",
        "client_secret=secret",
        "Bearer token",
        "principal:user@example.com",
        "user@example.com",
        "grq-sha256:%30",
        "grq-sha256:" + "a" * 64,
        "grq-sha256:" + "a" * 65,
        "grq-sha256:" + "a" * 63 + "\u202e",
    ),
)
def test_graph_request_id_rejects_nonopaque_metadata(unsafe_id):
    ontology = resolved_ontology_scope()
    query = _graph_query(ontology)
    values = query.model_dump(
        mode="python",
        round_trip=True,
    )
    values["graph_request_id"] = unsafe_id
    with pytest.raises(ValidationError, match="Graph request ID"):
        l6.L6GraphQuery.model_validate(values)


@pytest.mark.unit
def test_graph_request_id_is_exact_canonical_payload_hash():
    ontology = resolved_ontology_scope()
    query = _graph_query(ontology, run_seed="canonical-request-id")
    payload = query.model_dump(
        mode="json",
        exclude={"graph_request_id", "request_hash"},
    )
    assert query.graph_request_id == f"grq-sha256:{canonical_sha256(payload)}"
    assert query.request_hash == canonical_sha256(
        query.model_dump(mode="json", exclude={"request_hash"})
    )

    mutated = query.model_dump(mode="python", round_trip=True)
    mutated["max_result_records"] -= 1
    with pytest.raises(ValidationError, match="canonical request payload"):
        l6.L6GraphQuery.model_validate(mutated)

    collision = dict(mutated)
    collision["graph_request_id"] = "grq-sha256:" + "a" * 64
    with pytest.raises(ValidationError, match="canonical request payload"):
        l6.L6GraphQuery.model_validate(collision)

    resealed = l6.L6GraphQuery.seal(
        **{
            key: value
            for key, value in mutated.items()
            if key not in {"graph_request_id", "request_hash"}
        }
    )
    assert resealed.graph_request_id != query.graph_request_id


@pytest.mark.unit
def test_real_authority_validation_accepts_valid_authorities():
    _, context, budget, _, _ = _evidence()
    scopes = l6.L6ResolvedScopes(
        ontology_scope=resolved_ontology_scope(),
        retrieval_scope=resolved_retrieval_scope(),
    )
    l6._validate_authorities(
        _authorities(),
        _access(),
        scopes,
        context,
        budget,
        None,
        None,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("access_update", "expected"),
    (
        ({"access_policy_hash": "8" * 64}, "access policy authority mismatch"),
        ({"principal_scope_hash": "8" * 64}, "principal scope"),
        ({"project_scope_id": "project:other"}, "project scope"),
    ),
)
def test_real_authority_validation_rejects_typed_access_mismatch(
    access_update, expected
):
    _, context, budget, _, _ = _evidence()
    scopes = l6.L6ResolvedScopes(
        ontology_scope=resolved_ontology_scope(),
        retrieval_scope=resolved_retrieval_scope(),
    )
    with pytest.raises(ValueError, match=expected):
        l6._validate_authorities(
            _authorities(),
            _access().model_copy(update=access_update),
            scopes,
            context,
            budget,
            None,
            None,
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


@pytest.mark.unit
@pytest.mark.parametrize(
    ("fabric_id", "remote_id"),
    (
        (
            "/subscriptions/00000000-0000-4000-8000-000000000001/"
            "resourceGroups/kg-rg/providers/Microsoft.Fabric/capacities/kg-cap",
            "connection:remote-tool",
        ),
        (
            "fabric:workspace/00000000-0000-4000-8000-000000000001/"
            "item/00000000-0000-4000-8000-000000000002",
            "00000000-0000-4000-8000-000000000003",
        ),
    ),
)
def test_agent_definition_accepts_stable_arm_and_fabric_connections(
    fabric_id, remote_id
):
    definition = l6.build_l6_agent_definition(
        agent_name="KG evidence agent",
        fabric_data_agent_connection_id=fabric_id,
        foundry_remote_tool_connection_id=remote_id,
    )
    assert definition["connections"]["fabric_data_agent"][
        "project_connection_id"
    ] == fabric_id


@pytest.mark.unit
@pytest.mark.parametrize(
    "unsafe_value",
    (
        "https://storage.example/file?sig=secret",
        "connection:tool?client_secret=secret",
        "Bearer token",
        "principal:user@example.com",
        "user@example.com",
        "connection:tool\u202e",
        "connection%3Atool",
        "/subscriptions//resourceGroups/rg/providers/Microsoft.X/type/name",
        "/subscriptions/sub/resourceGroups/../providers/Microsoft.X/type/name",
    ),
)
def test_agent_definition_rejects_unsafe_connection_metadata(unsafe_value):
    with pytest.raises(ValueError, match="unsafe stable identity"):
        l6.build_l6_agent_definition(
            agent_name="KG evidence agent",
            fabric_data_agent_connection_id=unsafe_value,
            foundry_remote_tool_connection_id="connection:remote-tool",
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "unsafe_name",
    (
        "Agent client_secret=secret",
        "Agent principal:user@example.com",
        "Agent user@example.com",
        "Agent\u202e",
        "https://provider.example/agent",
        "api.example.com/agent",
        "localhost:8080/agent",
        "team/agent",
        "team\\agent",
        "agent?mode=test",
        "agent#fragment",
        "agent..name",
        "user@host/agent",
        "api%2Eexample%2Ecom%2Fagent",
    ),
)
def test_agent_definition_rejects_unsafe_display_name(unsafe_name):
    with pytest.raises(ValueError, match="unsafe"):
        l6.build_l6_agent_definition(
            agent_name=unsafe_name,
            fabric_data_agent_connection_id="connection:fabric",
            foundry_remote_tool_connection_id="connection:remote-tool",
        )


@pytest.mark.unit
def test_persistence_recursively_rejects_tampered_definition_text(tmp_path: Path):
    definition = l6.build_l6_agent_definition(
        agent_name="KG evidence agent",
        fabric_data_agent_connection_id="connection:fabric",
        foundry_remote_tool_connection_id="connection:remote-tool",
    )
    tampered_tools = [dict(item) for item in definition["tools"]]
    tampered_tools[0]["description"] = "client_secret=do-not-store"
    tampered = {**definition, "tools": tuple(tampered_tools)}
    tampered["definition_hash"] = canonical_sha256(
        {key: value for key, value in tampered.items() if key != "definition_hash"}
    )
    with pytest.raises(ValueError, match="unsafe stable text"):
        l6.persist_l6_agent_definition(tmp_path / "agent.json", tampered)

    endpoint_tools = [dict(item) for item in definition["tools"]]
    endpoint_tools[0]["description"] = "Read api.example.com/private from here"
    endpoint = {**definition, "tools": tuple(endpoint_tools)}
    endpoint["definition_hash"] = canonical_sha256(
        {key: value for key, value in endpoint.items() if key != "definition_hash"}
    )
    with pytest.raises(ValueError, match="unsafe stable text"):
        l6.persist_l6_agent_definition(tmp_path / "agent.json", endpoint)

    renamed_tools = [dict(item) for item in definition["tools"]]
    renamed_tools[0]["name"] = "fabric_kg_unapproved_tool"
    renamed = {**definition, "tools": tuple(renamed_tools)}
    renamed["definition_hash"] = canonical_sha256(
        {key: value for key, value in renamed.items() if key != "definition_hash"}
    )
    with pytest.raises(ValueError, match="closed toolset"):
        l6.persist_l6_agent_definition(tmp_path / "agent.json", renamed)
