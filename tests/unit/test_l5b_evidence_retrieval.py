from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.contracts.base import canonical_json
from fabric_kg_builder.contracts.publication import AccessPolicy, PrincipalScope
from fabric_kg_builder.serving.evidence_retrieval import (
    L5B_AGENTIC_API_VERSION,
    L5B_MAX_BATCH_SIZE,
    L5bMutationOperation,
    L5bPublicationError,
    L5bRemoteAccounting,
    L5bStateOperation,
    L5bTargetState,
    build_agentic_retrieve_payload,
    build_direct_search_payload,
    canonical_scope_filter,
    compile_l5b_publication,
    interpret_retrieval_response,
    require_l5b_publication_receipt,
    run_l5b,
    _scope_keys,
)
from fabric_kg_builder.serving.lifecycle_projection import run_l4
from fabric_kg_builder.serving.structured_publication import run_l5a
from tests.contract.test_c0_runtime_contracts import (
    HASH_A,
    HASH_B,
    locator,
    request_context,
    resolved_ontology_scope,
    resolved_retrieval_scope,
)
from tests.unit.test_l5a_structured_publication import (
    _FakeClient as _L5aClient,
    _assets,
    _crosswalk,
    _policy,
)
from tests.unit.test_schema2_projection_stage import _l3_with_sealed_manifest


class _SearchClient:
    def __init__(self, policy_hash: str) -> None:
        self.policy_hash = policy_hash
        self.state: L5bTargetState | None = None
        self.calls: list[str] = []
        self.sequence = 0
        self.tamper_read_back = False
        self.fail_after_publish = False

    def _accounting(self, verb: str) -> L5bRemoteAccounting:
        self.sequence += 1
        return L5bRemoteAccounting(
            operation_refs=(f"search-op:{self.sequence}:{verb}",),
            request_bytes=17,
            response_bytes=19,
            retry_count=1 if verb == "publish" else 0,
            retry_wait_ms=3 if verb == "publish" else 0,
            latency_ms=5,
        )

    def inspect(self, target_id: str) -> L5bStateOperation:
        self.calls.append("inspect")
        return L5bStateOperation(self.state, self._accounting("inspect"))

    def publish(self, target_id: str, **kwargs) -> L5bMutationOperation:
        self.calls.append("publish")
        assert kwargs["batch_size"] == L5B_MAX_BATCH_SIZE
        documents = json.loads(kwargs["documents_path"].read_text("utf-8"))
        prior = self.state
        assert prior == kwargs["expected_state"]
        self.state = L5bTargetState(
            target_id=target_id,
            target_version="1.0.0",
            index_definition=json.loads(
                kwargs["index_definition_path"].read_text("utf-8")
            ),
            knowledge_source_definition=json.loads(
                kwargs["knowledge_source_definition_path"].read_text("utf-8")
            ),
            knowledge_base_definition=json.loads(
                kwargs["knowledge_base_definition_path"].read_text("utf-8")
            ),
            document_ids=tuple(item["id"] for item in documents),
            document_hashes=tuple(
                (item["id"], item["document_hash"]) for item in documents
            ),
            vector_state_hash=canonical_sha256([
                (item["id"], item["vector_state"], item["vector"])
                for item in documents
            ]),
            access_policy_id="access-policy:l5a",
            access_policy_hash=self.policy_hash,
            publication_token=kwargs["publication_token"],
        )
        if self.fail_after_publish:
            raise TimeoutError("publish response lost")
        return L5bMutationOperation(
            target_id=target_id,
            created=prior is None,
            applied=True,
            publication_token=kwargs["publication_token"],
            accounting=self._accounting("publish"),
        )

    def read_back(self, target_id: str) -> L5bStateOperation:
        self.calls.append("read_back")
        state = self.state
        if state is not None and self.tamper_read_back:
            self.tamper_read_back = False
            state = dataclasses.replace(state, vector_state_hash="f" * 64)
        return L5bStateOperation(state, self._accounting("read-back"))

    def cleanup(self, target_id: str, **kwargs) -> L5bMutationOperation:
        self.calls.append("cleanup")
        applied = (
            self.state is not None
            and self.state.publication_token == kwargs["publication_token"]
        )
        if applied:
            self.state = None
        return L5bMutationOperation(
            target_id=target_id,
            created=False,
            applied=applied,
            publication_token=kwargs["publication_token"],
            accounting=self._accounting("cleanup"),
        )

    def restore(self, target_id: str, **kwargs) -> L5bMutationOperation:
        self.calls.append("restore")
        applied = (
            self.state is not None
            and self.state.publication_token == kwargs["publication_token"]
        )
        if applied:
            self.state = kwargs["prior_state"]
        return L5bMutationOperation(
            target_id=target_id,
            created=False,
            applied=applied,
            publication_token=kwargs["publication_token"],
            accounting=self._accounting("restore"),
        )


class _MissingAccountingClient(_SearchClient):
    def inspect(self, target_id: str):  # type: ignore[override]
        self.calls.append("inspect")
        return object()


def _inputs(tmp_path: Path):
    properties = {
        "semantic-type:manufacturing.record": ({
            "property_id": "property:record:canonical-id",
            "display_name": "Record ID",
            "value_type": "string",
            "required": True,
        },),
        "semantic-type:manufacturing.subject": ({
            "property_id": "property:subject:canonical-id",
            "display_name": "Subject ID",
            "value_type": "string",
            "required": True,
        },),
    }
    identity_keys = {
        "semantic-type:manufacturing.record": (
            "property:record:canonical-id",
        ),
        "semantic-type:manufacturing.subject": (
            "property:subject:canonical-id",
        ),
    }
    l3 = _l3_with_sealed_manifest(
        tmp_path,
        type_properties=properties,
        identity_business_keys=identity_keys,
        inject_identity_keys=True,
    )
    l4 = run_l4(l3, state_root=tmp_path / ".fkg" / "l4")
    source = l4.sealed_source()
    policy = _policy(source)
    crosswalk = _crosswalk(source)
    target_ids = {
        "parquet": "target:lakehouse",
        "semantic_model": "target:semantic-model",
        "ontology": "target:ontology",
        "graph": "target:graph",
    }
    l5a = run_l5a(
        source,
        crosswalks=(crosswalk,),
        access_policy=policy,
        governed_assets=_assets(
            source,
            crosswalk,
            policy,
            target_ids,
        ),
        target_ids=target_ids,
        client=_L5aClient(),
        state_root=tmp_path / ".fkg" / "l5a",
    )
    kwargs = {
        "evidence_partitions": {
            f"{leaf.extraction_candidate_batch_id}:evidence": leaf.evidence_spans
            for leaf in l3.leaves
        },
        "source_unit_manifest": l3.inputs.source_unit_manifest,
        "source_units": l3.inputs.source_units.units,
        "source_file_names": {
            unit.source_file_id: "manufacturing-source.txt"
            for unit in l3.inputs.source_units.units
        },
        "access_policy": policy,
        "target_id": "target:search-evidence",
        "index_name": "search-evidence-index",
        "knowledge_source_name": "search-evidence-source",
        "knowledge_base_name": "search-evidence-base",
    }
    return source, l5a, kwargs


@pytest.mark.unit
def test_l5b_compiles_exact_search_resources_from_sealed_authority(
    tmp_path: Path,
) -> None:
    source, l5a, kwargs = _inputs(tmp_path)
    compiled = compile_l5b_publication(source, l5a, **kwargs)

    assert compiled.index_definition["name"] == "search-evidence-index"
    assert compiled.knowledge_source_definition["searchIndexParameters"][
        "baseFilter"
    ].startswith("access_policy_hash eq ")
    assert compiled.knowledge_base_definition["outputMode"] == "extractiveData"
    assert compiled.knowledge_base_definition["models"] == []
    fields = {
        item["name"]: item for item in compiled.index_definition["fields"]
    }
    assert fields["canonical_entity_ids"]["searchable"] is True
    assert fields["canonical_assertion_ids"]["searchable"] is True
    assert compiled.documents == ()
    assert compiled.vector_state_hash == canonical_sha256([])
    assert L5B_AGENTIC_API_VERSION == "2026-05-01-preview"


@pytest.mark.unit
def test_l5b_rejects_l3_evidence_partition_tamper(tmp_path: Path) -> None:
    source, l5a, kwargs = _inputs(tmp_path)
    partition_id, spans = next(iter(kwargs["evidence_partitions"].items()))
    kwargs["evidence_partitions"] = {partition_id: spans[:-1]}

    with pytest.raises(
        L5bPublicationError,
        match="L5B_L3_EVIDENCE_PARTITION_TAMPERED",
    ):
        compile_l5b_publication(source, l5a, **kwargs)


@pytest.mark.unit
def test_l5b_rejects_source_unit_semantic_tamper(tmp_path: Path) -> None:
    source, l5a, kwargs = _inputs(tmp_path)
    unit = kwargs["source_units"][0]
    kwargs["source_units"] = (
        unit.model_copy(update={"unit_kind": "heading"}),
    )

    with pytest.raises(L5bPublicationError, match="L5B_SOURCE_UNIT_TAMPERED"):
        compile_l5b_publication(source, l5a, **kwargs)


@pytest.mark.unit
def test_l5b_rejects_stale_policy_and_acl_scope_collision(tmp_path: Path) -> None:
    source, l5a, kwargs = _inputs(tmp_path)
    stale_values = kwargs["access_policy"].model_dump(
        mode="python",
        exclude={"policy_hash"},
    )
    stale_values["retention_class"] = "retention:stale"
    stale = AccessPolicy(
        **stale_values,
        policy_hash=canonical_sha256(stale_values),
    )
    with pytest.raises(L5bPublicationError, match="L5B_ACCESS_POLICY_STALE"):
        compile_l5b_publication(
            source,
            l5a,
            **{**kwargs, "access_policy": stale},
        )

    policy = kwargs["access_policy"]
    values = policy.model_dump(mode="python", exclude={"policy_hash"})
    values["principal_scopes"] = (
        PrincipalScope(
            principal_type="group",
            principal_id="principal:two",
            resource_scope_ids=("resource:shared",),
        ),
        PrincipalScope(
            principal_type="managed_identity",
            principal_id="principal:one",
            resource_scope_ids=("resource:shared",),
        ),
    )
    colliding = AccessPolicy(**values, policy_hash=canonical_sha256(values))
    with pytest.raises(L5bPublicationError, match="L5B_ACL_SCOPE_COLLISION"):
        _scope_keys(colliding)


@pytest.mark.unit
def test_l5b_publishes_reads_back_and_reuses_hash_keyed_state(
    tmp_path: Path,
) -> None:
    source, l5a, kwargs = _inputs(tmp_path)
    client = _SearchClient(kwargs["access_policy"].policy_hash)
    state_root = tmp_path / ".fkg" / "l5b"

    first = run_l5b(
        source,
        l5a,
        **kwargs,
        client=client,
        state_root=state_root,
    )
    require_l5b_publication_receipt(source, l5a, first)
    client.calls.clear()
    second = run_l5b(
        source,
        l5a,
        **kwargs,
        client=client,
        state_root=state_root,
    )

    assert first.receipt.status == "succeeded"
    assert first.metrics.search_calls == 3
    assert second.receipt.status == "skipped"
    assert second.reused
    assert client.calls == ["read_back"]


@pytest.mark.unit
def test_l5b_stale_reuse_repairs_within_four_call_success_bound(
    tmp_path: Path,
) -> None:
    source, l5a, kwargs = _inputs(tmp_path)
    client = _SearchClient(kwargs["access_policy"].policy_hash)
    state_root = tmp_path / ".fkg" / "l5b"
    run_l5b(
        source,
        l5a,
        **kwargs,
        client=client,
        state_root=state_root,
    )
    client.calls.clear()
    client.tamper_read_back = True

    repaired = run_l5b(
        source,
        l5a,
        **kwargs,
        client=client,
        state_root=state_root,
    )

    assert not repaired.reused
    assert repaired.receipt.status == "succeeded"
    assert repaired.metrics.search_calls == 4
    assert client.calls == ["read_back", "inspect", "publish", "read_back"]


@pytest.mark.unit
def test_l5b_missing_remote_accounting_fails_closed(tmp_path: Path) -> None:
    source, l5a, kwargs = _inputs(tmp_path)
    client = _MissingAccountingClient(kwargs["access_policy"].policy_hash)

    with pytest.raises(
        L5bPublicationError,
        match="L5B_REMOTE_ACCOUNTING_MISSING",
    ) as raised:
        run_l5b(
            source,
            l5a,
            **kwargs,
            client=client,
            state_root=tmp_path / ".fkg" / "l5b",
        )

    assert raised.value.metrics is not None
    assert raised.value.metrics.search_calls == 1


@pytest.mark.unit
def test_l5b_read_back_tamper_fails_and_cas_cleanup_runs(tmp_path: Path) -> None:
    source, l5a, kwargs = _inputs(tmp_path)
    client = _SearchClient(kwargs["access_policy"].policy_hash)
    client.tamper_read_back = True

    with pytest.raises(L5bPublicationError, match="L5B_READ_BACK_MISMATCH"):
        run_l5b(
            source,
            l5a,
            **kwargs,
            client=client,
            state_root=tmp_path / ".fkg" / "l5b",
        )

    assert client.state is None
    assert client.calls[-1] == "cleanup"


@pytest.mark.unit
def test_l5b_ambiguous_publish_is_prebudgeted_and_recovered(
    tmp_path: Path,
) -> None:
    source, l5a, kwargs = _inputs(tmp_path)
    client = _SearchClient(kwargs["access_policy"].policy_hash)
    client.fail_after_publish = True

    with pytest.raises(L5bPublicationError) as raised:
        run_l5b(
            source,
            l5a,
            **kwargs,
            client=client,
            state_root=tmp_path / ".fkg" / "l5b",
        )

    assert raised.value.metrics is not None
    assert raised.value.metrics.search_calls == 4
    assert client.calls == ["inspect", "publish", "inspect", "cleanup"]
    assert client.state is None
    assert "L5B_REMOTE_PUBLISH_AMBIGUOUS" in raised.value.receipt.error_codes


@pytest.mark.unit
def test_canonical_filters_escape_injection_and_preview_is_exact_and_narrow() -> None:
    scope = resolved_retrieval_scope()
    context, budget = request_context()
    filter_text = canonical_scope_filter(
        canonical_entity_ids=context.filter_add_on.canonical_entity_ids,
        canonical_type_ids=context.filter_add_on.exact_type_ids,
        canonical_relationship_ids=(
            context.filter_add_on.canonical_relationship_ids
        ),
        access_policy_hash=context.acl_scope_hash,
        asserted_publication_hash=context.asserted_publication_hash,
    )
    payload = build_agentic_retrieve_payload(
        context,
        budget,
        scope,
        query_text="return exact evidence",
        filter_add_on=filter_text,
    )

    assert payload["outputMode"] == "extractiveData"
    assert payload["knowledgeSourceParams"][0]["filterAddOn"] == filter_text
    assert payload["knowledgeSourceParams"][0]["failOnError"] is True
    assert "maxSubQueries" not in payload["knowledgeSourceParams"][0]
    assert payload["maxOutputSize"] == budget.max_output_tokens
    escaped = canonical_scope_filter(
        canonical_entity_ids=("entity:x' or true or 'y",),
        canonical_type_ids=(),
        canonical_relationship_ids=(),
        access_policy_hash="a" * 64,
        asserted_publication_hash="b" * 64,
    )
    assert "entity:x'' or true or ''y" in escaped


@pytest.mark.unit
def test_direct_fallback_is_prefiltered_and_vector_degradation_is_explicit() -> None:
    scope = resolved_retrieval_scope()
    context, budget = request_context("direct_hybrid_prefilter")

    vector_payload = build_direct_search_payload(
        context,
        budget,
        scope,
        query_text="evidence",
        vector=(0.0,) * 1536,
        vector_available=True,
    )
    degraded_payload = build_direct_search_payload(
        context,
        budget,
        scope,
        query_text="evidence",
        vector=None,
    )

    assert vector_payload.payload["vectorFilterMode"] == "preFilter"
    assert "canonical_entity_ids/any" in vector_payload.payload["filter"]
    assert vector_payload.degradation_code is None
    assert degraded_payload.degradation_code == (
        "vector_unavailable_keyword_semantic_filtered"
    )
    assert degraded_payload.payload["filter"] == vector_payload.payload["filter"]
    assert "vectorDegradation" not in degraded_payload.payload

    with pytest.raises(ValueError, match="1536 finite"):
        build_direct_search_payload(
            context,
            budget,
            scope,
            query_text="evidence",
            vector=(0.0, 1.0),
            vector_available=True,
        )


def _reference_document(context, entity_id: str, index: int) -> dict:
    source_locator = locator()
    values = {
        "id": f"delivery-document:{index}",
        "assertion_kind": "entity",
        "canonical_entity_ids": [entity_id],
        "canonical_relationship_ids": ["relationship:has-member"],
        "canonical_property_ids": [],
        "canonical_type_ids": ["type:component"],
        "canonical_assertion_ids": [f"assertion:{entity_id}"],
        "required_member_manifest_ids": ["manifest:required-members"],
        "source_id": "source:manual",
        "original_document_name": "Original Service Manual.pdf",
        "source_file_id": "source-file:manual",
        "asset_id": "asset:manual",
        "asset_version_id": "asset-version:manual:1",
        "asset_hash": HASH_B,
        "source_unit_id": f"source-unit:paragraph-{index}",
        "source_unit_hash": HASH_A,
        "source_unit_kind": "paragraph",
        "source_text_content_hash": HASH_A,
        "evidence_span_ids": [f"evidence:paragraph-{index}"],
        "evidence_span_hashes": [HASH_A],
        "evidence_purposes": ["extraction_assertion"],
        "content": "Exact authorized evidence.",
        "source_quote": "Exact authorized evidence.",
        "source_quote_is_verbatim": True,
        "quote_hash": canonical_sha256("Exact authorized evidence."),
        "immutable_locator_json": canonical_json(source_locator),
        "immutable_locator_hash": source_locator.locator_hash,
        "page": source_locator.page,
        "section_path": list(source_locator.section_path or ()),
        "access_policy_id": "access-policy:evidence",
        "access_policy_hash": context.acl_scope_hash,
        "acl_principal_keys": ["managed_identity:principal:reader"],
        "acl_scope_keys": ["scope:generic"],
        "authorization_resource_id": "authorization-resource:evidence",
        "l3_artifact_manifest_id": "manifest:l3",
        "l3_artifact_manifest_hash": HASH_A,
        "l4_projection_hash": HASH_A,
        "l4_receipt_hash": HASH_A,
        "l5a_publication_fingerprint": HASH_A,
        "l5a_receipt_hash": HASH_A,
        "publication_crosswalk_hashes": [HASH_A],
        "asserted_publication_hash": context.asserted_publication_hash,
        "lifecycle_state": "asserted",
        "governed_asset_reference_id": None,
        "governed_asset_reference_hash": None,
        "vector": None,
        "vector_state": "unavailable",
    }
    values["document_hash"] = canonical_sha256(values)
    return values


@pytest.mark.unit
@pytest.mark.parametrize(
    ("returned_count", "warning", "output_tokens", "expected_status"),
    (
        (10, False, 0, "complete"),
        (9, True, 0, "partial"),
        (10, False, None, "partial"),
    ),
)
def test_retrieval_interop_returns_evidence_coverage_without_synthesis(
    returned_count: int,
    warning: bool,
    output_tokens: int | None,
    expected_status: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology_scope = resolved_ontology_scope()
    retrieval_scope = resolved_retrieval_scope()
    context, budget = request_context()
    references = [
        {
            "id": f"search-reference:{index}",
            "activitySource": 1,
            "sourceData": _reference_document(context, entity_id, index),
        }
        for index, entity_id in enumerate(
            retrieval_scope.canonical_member_ids[:returned_count]
        )
    ]
    response = {
        "requestId": "provider-request:bounded",
        "references": references,
        "activity": [{
            "id": 1,
            "type": "searchIndex",
            "knowledgeSourceName": context.knowledge_source_id,
            "count": 10,
            "elapsedMs": 25,
            "searchIndexArguments": {
                "search": "exact evidence",
                "filter": "sealed-filter",
            },
            "warning": "warning:source" if warning else None,
            "warningCode": "outputTruncated" if warning else None,
        }],
        "outputTokens": 0,
    }
    accounting = L5bRemoteAccounting(
        operation_refs=("retrieve:1",),
        request_bytes=100,
        response_bytes=1000,
        retry_count=0,
        retry_wait_ms=0,
        latency_ms=25,
        candidate_count=10,
        warning_codes=("warning:source",) if warning else (),
        output_tokens=output_tokens,
    )
    publication = SimpleNamespace(
        compiled=SimpleNamespace(
            source=None,
            l5a_result=None,
            index_name=context.search_index_id,
            knowledge_source_name=context.knowledge_source_id,
            knowledge_base_name=context.knowledge_base_id,
            index_fingerprint=context.search_index_fingerprint,
            document_hashes=tuple(
                (
                    reference["sourceData"]["id"],
                    reference["sourceData"]["document_hash"],
                )
                for reference in references
            ),
        )
    )
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args: None,
    )

    result = interpret_retrieval_response(
        context,
        budget,
        ontology_scope,
        retrieval_scope,
        publication=publication,
        response=response,
        accounting=accounting,
    )

    assert result.coverage.coverage_status == expected_status
    assert len(result.citations) == returned_count
    assert len(result.presentations) == returned_count
    assert not hasattr(result, "answer")
    if expected_status == "partial":
        assert (
            result.coverage.missing_canonical_ids
            or result.coverage.unsupported_capability_codes
        )
        if warning:
            assert result.coverage.output_truncated


@pytest.mark.unit
def test_direct_vector_degradation_forces_partial_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology_scope = resolved_ontology_scope()
    retrieval_scope = resolved_retrieval_scope()
    context, budget = request_context("direct_hybrid_prefilter")
    documents = [
        _reference_document(context, entity_id, index)
        for index, entity_id in enumerate(retrieval_scope.canonical_member_ids)
    ]
    publication = SimpleNamespace(
        compiled=SimpleNamespace(
            source=None,
            l5a_result=None,
            index_name=context.search_index_id,
            knowledge_source_name=context.knowledge_source_id,
            knowledge_base_name=context.knowledge_base_id,
            index_fingerprint=context.search_index_fingerprint,
            document_hashes=tuple(
                (document["id"], document["document_hash"])
                for document in documents
            ),
        )
    )
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args: None,
    )

    result = interpret_retrieval_response(
        context,
        budget,
        ontology_scope,
        retrieval_scope,
        publication=publication,
        response={"value": documents},
        accounting=L5bRemoteAccounting(
            operation_refs=("direct-search:1",),
            request_bytes=100,
            response_bytes=1000,
            retry_count=0,
            retry_wait_ms=0,
            latency_ms=25,
            candidate_count=10,
        ),
        degradation_code="vector_unavailable_keyword_semantic_filtered",
    )

    assert result.coverage.coverage_status == "partial"
    assert result.coverage.unsupported_capability_codes == (
        "vector_unavailable_keyword_semantic_filtered",
    )
