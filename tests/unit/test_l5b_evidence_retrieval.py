from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import secrets
import shutil
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
import pyarrow as pa

from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.contracts.base import canonical_json
from fabric_kg_builder.contracts import (
    AgenticRetrievalCoverageReceiptV1_1,
    AgenticRetrievalRequestContext,
    AgenticRetrievalRequestContextV1_1,
    ArtifactManifest,
    CoverageBudgetObservationV1_1,
    GovernedAssetReference,
    QueryBudget,
    QueryBudgetV1_1,
    StageReceipt,
    StageResourceMetrics,
)
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
    _agentic_runtime_seconds,
    _assertion_documents,
    _parse_immutable_locator,
    _safe_source_display_name,
    _safe_section_path,
    _scope_keys,
)
from fabric_kg_builder.serving.lifecycle_projection import run_l4
from fabric_kg_builder.serving.structured_publication import run_l5a
from tests.contract.test_c0_runtime_contracts import (
    HASH_A,
    HASH_B,
    locator,
    request_context as request_context_v1_0,
    request_context_v1_1,
    resolved_ontology_scope,
    resolved_retrieval_scope,
)
from tests.unit.test_l5a_structured_publication import (
    _FakeClient as _L5aClient,
    _assets,
    _crosswalk,
    _source_assets,
    _policy,
)
from tests.unit.test_schema2_projection_stage import _l3_with_sealed_manifest


def request_context(
    mode: str = "agentic_preview",
    *,
    entity_ids: tuple[str, ...] = (),
    fallback_for: AgenticRetrievalRequestContextV1_1 | None = None,
):
    kwargs = {}
    if entity_ids:
        kwargs["entity_ids"] = entity_ids
    context, budget, _, _ = request_context_v1_1(
        mode,
        vector_search_requests=1 if mode == "direct_hybrid_prefilter" else 0,
        **kwargs,
    )
    if mode == "direct_hybrid_prefilter":
        values = context.model_dump(
            mode="python",
            exclude={"request_context_hash"},
            round_trip=True,
        )
        values["fallback_for_request_context_id"] = (
            fallback_for.request_context_id if fallback_for is not None else None
        )
        values["fallback_for_request_context_hash"] = (
            fallback_for.request_context_hash if fallback_for is not None else None
        )
        context = AgenticRetrievalRequestContextV1_1(
            **values,
            request_context_hash=canonical_sha256(values),
        )
    return context, budget


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


class _TestCheckpointSigner:
    def __init__(
        self,
        *,
        key_id: str = "test-l5b-checkpoint",
        key_version: str = "1",
        algorithm: str = "HMAC-SHA256",
        secret: bytes | None = None,
        unavailable: bool = False,
        verify_result: bool | None = None,
        verify_error: bool = False,
        verify_exception: Exception | None = None,
        sign_error: bool = False,
        malformed_sign: bool = False,
    ) -> None:
        self._key_id = key_id
        self._key_version = key_version
        self._algorithm = algorithm
        self.secret = secret or secrets.token_bytes(32)
        self.unavailable = unavailable
        self.verify_result = verify_result
        self.verify_error = verify_error
        self.verify_exception = verify_exception
        self.sign_error = sign_error
        self.malformed_sign = malformed_sign

    @property
    def key_id(self):
        if self.unavailable:
            raise RuntimeError("signer unavailable")
        return self._key_id

    @property
    def key_version(self):
        return self._key_version

    @property
    def algorithm(self):
        return self._algorithm

    def sign(self, canonical_payload: bytes) -> str:
        if self.sign_error:
            raise RuntimeError("sign failed")
        if self.malformed_sign:
            return "not-a-valid-mac"
        return hmac.new(
            self.secret,
            canonical_payload,
            hashlib.sha256,
        ).hexdigest()

    def verify(self, canonical_payload: bytes, persisted_mac: str) -> bool:
        if self.verify_exception is not None:
            raise self.verify_exception
        if self.verify_error:
            raise RuntimeError("verify failed")
        expected = self.sign(canonical_payload)
        if self.verify_result is not None:
            return self.verify_result
        return hmac.compare_digest(expected, persisted_mac)


_TEST_CHECKPOINT_SIGNER = _TestCheckpointSigner()


def _compile_kwargs(kwargs):
    return {
        key: value
        for key, value in kwargs.items()
        if key != "checkpoint_integrity_signer"
    }


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
    all_spans = tuple(
        span for leaf in l3.leaves for span in leaf.evidence_spans
    )
    l5a = run_l5a(
        source,
        crosswalks=(crosswalk,),
        access_policy=policy,
        governed_assets=(
            *_assets(source, crosswalk, policy, target_ids),
            *_source_assets(
                _assets(source, crosswalk, policy, target_ids),
                all_spans,
                policy,
            ),
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
        "governed_assets": l5a.compiled.governed_assets,
        "target_id": "target:search-evidence",
        "index_name": "search-evidence-index",
        "knowledge_source_name": "search-evidence-source",
        "knowledge_base_name": "search-evidence-base",
        "checkpoint_integrity_signer": _TEST_CHECKPOINT_SIGNER,
    }
    return source, l5a, kwargs


@pytest.mark.unit
def test_l5b_compiles_exact_search_resources_from_sealed_authority(
    tmp_path: Path,
) -> None:
    source, l5a, kwargs = _inputs(tmp_path)
    compiled = compile_l5b_publication(source, l5a, **_compile_kwargs(kwargs))

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
    assert compiled.documents != ()
    for document in compiled.documents:
        assert document["lifecycle_state"] == "asserted"
        assert document["source_quote_is_verbatim"] is True
        assert document["source_quote"] == document["content"]
        assert document["quote_hash"]
        assert document["evidence_span_ids"]
        assert document["canonical_entity_ids"]
        assert (
            document["access_policy_hash"]
            == kwargs["access_policy"].policy_hash
        )
    assert compiled.vector_state_hash == canonical_sha256(
        [
            (item["id"], item["vector_state"], item["vector"])
            for item in compiled.documents
        ]
    )
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
        compile_l5b_publication(source, l5a, **_compile_kwargs(kwargs))


@pytest.mark.unit
def test_l5b_rejects_source_unit_semantic_tamper(tmp_path: Path) -> None:
    source, l5a, kwargs = _inputs(tmp_path)
    unit = kwargs["source_units"][0]
    kwargs["source_units"] = (
        unit.model_copy(update={"unit_kind": "heading"}),
    )

    with pytest.raises(L5bPublicationError, match="L5B_SOURCE_UNIT_TAMPERED"):
        compile_l5b_publication(source, l5a, **_compile_kwargs(kwargs))


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
            **{
                **_compile_kwargs(kwargs),
                "access_policy": stale,
            },
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
def test_l5b_requires_exact_l5a_governed_asset_set(tmp_path: Path) -> None:
    source, l5a, kwargs = _inputs(tmp_path)
    assets = kwargs["governed_assets"]
    for supplied in ((), (*assets, assets[0])):
        with pytest.raises(
            L5bPublicationError,
            match="L5B_GOVERNED_ASSET_SET_MISMATCH",
        ):
            compile_l5b_publication(
                source,
                l5a,
                **{
                    **_compile_kwargs(kwargs),
                    "governed_assets": supplied,
                },
            )

    original = assets[0]
    values = original.model_dump(
        mode="python",
        exclude={"asset_reference_hash"},
    )
    identity = dict(values["identity"])
    identity["source_file_id"] = "l5a-definition:misassigned"
    values["identity"] = identity
    values["source_file_id"] = "l5a-definition:misassigned"
    misassigned = GovernedAssetReference(
        **values,
        asset_reference_hash=canonical_sha256(values),
    )
    with pytest.raises(
        L5bPublicationError,
        match="L5B_GOVERNED_ASSET_SET_MISMATCH",
    ):
        compile_l5b_publication(
            source,
            l5a,
            **{
                **_compile_kwargs(kwargs),
                "governed_assets": (misassigned, *assets[1:]),
            },
        )


@pytest.mark.unit
def test_applicable_evidence_requires_exact_governed_source_asset(
    tmp_path: Path,
) -> None:
    source, l5a, kwargs = _inputs(tmp_path)
    span = next(
        span
        for spans in kwargs["evidence_partitions"].values()
        for span in spans
    )
    table_name = "l4_semantic_required_members"
    table = l5a.compiled.tables[table_name]
    rows = table.to_pylist()
    rows[0]["supporting_evidence_span_ids"] = [span.evidence_span_id]
    tables = dict(l5a.compiled.tables)
    tables[table_name] = pa.Table.from_pylist(rows, schema=table.schema)
    compiled = dataclasses.replace(l5a.compiled, tables=tables)
    evidence_l5a = dataclasses.replace(l5a, compiled=compiled)
    evidence = {
        item.evidence_span_id: item
        for spans in kwargs["evidence_partitions"].values()
        for item in spans
    }

    with pytest.raises(
        L5bPublicationError,
        match="L5B_GOVERNED_SOURCE_ASSET_MISSING",
    ):
        _assertion_documents(
            source,
            evidence_l5a,
            evidence=evidence,
            source_unit_manifest=kwargs["source_unit_manifest"],
            source_units=kwargs["source_units"],
            source_file_names=kwargs["source_file_names"],
            policy=kwargs["access_policy"],
            assets=tuple(
                asset
                for asset in kwargs["governed_assets"]
                if asset.asset_kind == "derived"
            ),
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "unsafe",
    (
        "https://example.test/manual.pdf",
        "file:///tmp/manual.pdf",
        "data:text/plain,manual",
        "/tmp/manual.pdf",
        r"C:\temp\manual.pdf",
        "../manual.pdf",
        "manual.pdf?sig=secret",
        "AccountKey=secret",
        "DefaultEndpointsProtocol=https;AccountName=x",
        "Host=db.example;Database=prod;User ID=admin;Pwd=not-a-secret",
        "AccountKey = not-a-secret",
        "Server = db; User ID = admin",
        "report\u202efdp.exe",
        "trusted.pdf\u0085FORGED",
        "api_key=supersecret.txt",
        "access-key : not-a-secret.txt",
        "manual%2Fsecret.pdf",
        "https%3A%2F%2Fexample.test%2Fmanual.pdf",
        "api%5Fkey%3Dsupersecret.txt",
        "API KEY = supersecret.pdf",
        "Account Key : x.pdf",
        "foo%252Fbar.pdf",
        "api%255Fkey%253Dsupersecret.pdf",
        "report-api key=supersecret.pdf",
        "report_api key=supersecret.pdf",
        "report(api key=supersecret).pdf",
        "prefixapikey=supersecret.pdf",
        "refresh-token-value=supersecret.pdf",
        "api-key-name=supersecret.pdf",
        "credential label: supersecret.pdf",
        "api\u00a0key = supersecret.pdf",
        "ａｐｉ　ｋｅｙ＝supersecret.pdf",
        "𝕒𝕡𝕚 𝕜𝕖𝕪=supersecret.pdf",
        quote("ａｐｉ ｋｅｙ＝supersecret.pdf"),
        quote(quote("ａｐｉ ｋｅｙ＝supersecret.pdf")),
    ),
)
def test_source_display_name_rejects_urls_paths_and_credentials(
    unsafe: str,
) -> None:
    with pytest.raises(L5bPublicationError, match="L5B_SOURCE_FILE_NAME_UNSAFE"):
        _safe_source_display_name(unsafe, source_file_id="source-file:manual")


@pytest.mark.unit
def test_source_display_name_allows_safe_unicode_filename() -> None:
    assert _safe_source_display_name(
        "製造 マニュアル 2026.pdf",
        source_file_id="source-file:manual",
    ) == "製造 マニュアル 2026.pdf"
    assert _safe_source_display_name(
        "Tokenization guide.pdf",
        source_file_id="source-file:manual",
    ) == "Tokenization guide.pdf"


@pytest.mark.unit
def test_section_path_uses_shared_display_policy() -> None:
    with pytest.raises(L5bPublicationError, match="L5B_DISPLAY_TEXT_UNSAFE"):
        _safe_section_path(
            ("prefixapikey=supersecret",),
            source_unit_id="source-unit:manual",
        )
    assert _safe_section_path(
        (
            "section:maintenance",
            "バッテリー 取り外し",
            "Tokenization: overview",
        ),
        source_unit_id="source-unit:manual",
    ) == (
        "section:maintenance",
        "バッテリー 取り外し",
        "Tokenization: overview",
    )


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
    require_l5b_publication_receipt(
        source,
        l5a,
        first,
        checkpoint_integrity_signer=_TEST_CHECKPOINT_SIGNER,
    )
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
    require_l5b_publication_receipt(
        source,
        l5a,
        second,
        checkpoint_integrity_signer=_TEST_CHECKPOINT_SIGNER,
    )

    forged_values = second.receipt.model_dump(mode="python")
    forged_values["completed_at_utc"] = (
        second.receipt.completed_at_utc + timedelta(seconds=1)
    )
    forged = dataclasses.replace(
        second,
        receipt=StageReceipt.model_validate(forged_values),
    )
    with pytest.raises(
        L5bPublicationError,
        match="L5B_PUBLICATION_RECEIPT_INVALID",
    ):
        require_l5b_publication_receipt(
            source,
            l5a,
            forged,
            checkpoint_integrity_signer=_TEST_CHECKPOINT_SIGNER,
        )


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


def _write_contract(path: Path, value) -> None:
    path.write_text(canonical_json(value) + "\n", encoding="utf-8")


def _coordinated_artifact_reseal(run_root: Path, filename: str) -> None:
    path = run_root / filename
    payload = json.loads(path.read_text("utf-8"))
    if isinstance(payload, dict):
        payload["description"] = f"coordinated tamper: {filename}"
    elif filename == "documents.json":
        payload.append({"id": "tampered-document"})
    else:
        payload = []
    _write_contract(path, payload)

    kind_by_file = {
        "index-definition.json": "l5b.search_index_definition",
        "knowledge-source-definition.json": "l5b.knowledge_source_definition",
        "knowledge-base-definition.json": "l5b.knowledge_base_definition",
        "documents.json": "l5b.evidence_documents",
        "projection-equivalence.json": "c0.projection_equivalence",
    }
    manifest = ArtifactManifest.model_validate_json(
        (run_root / "output-manifest.json").read_text("utf-8")
    )
    entries = []
    for entry in manifest.entries:
        if entry.contract_kind == kind_by_file[filename]:
            entries.append(entry.model_copy(update={
                "content_hash": canonical_sha256(payload),
                "byte_count": path.stat().st_size,
                "row_count": len(payload) if isinstance(payload, list) else 1,
            }))
        else:
            entries.append(entry)
    manifest_values = manifest.model_dump(
        mode="python",
        exclude={"manifest_hash"},
    )
    manifest_values["entries"] = tuple(entries)
    manifest_values["total_row_count"] = sum(
        entry.row_count or 0 for entry in entries
    )
    manifest_values["total_byte_count"] = sum(
        entry.byte_count for entry in entries
    )
    resealed_manifest = ArtifactManifest(
        **manifest_values,
        manifest_hash=canonical_sha256(manifest_values),
    )
    _write_contract(run_root / "output-manifest.json", resealed_manifest)

    metrics = StageResourceMetrics.model_validate_json(
        (run_root / "resource-metrics.json").read_text("utf-8")
    )
    artifact_files = (
        "index-definition.json",
        "knowledge-source-definition.json",
        "knowledge-base-definition.json",
        "documents.json",
        "projection-equivalence.json",
        "output-manifest.json",
    )
    metrics_values = metrics.model_dump(
        mode="python",
        exclude={"metrics_hash"},
    )
    metrics_values["storage_write_bytes"] = sum(
        (run_root / item).stat().st_size for item in artifact_files
    )
    resealed_metrics = StageResourceMetrics(
        **metrics_values,
        metrics_hash=canonical_sha256(metrics_values),
    )
    _write_contract(run_root / "resource-metrics.json", resealed_metrics)

    receipt = StageReceipt.model_validate_json(
        (run_root / "stage-receipt.json").read_text("utf-8")
    )
    receipt_values = receipt.model_dump(
        mode="python",
        exclude={"receipt_hash"},
    )
    receipt_values["output_manifest_hash"] = resealed_manifest.manifest_hash
    receipt_values["resource_metrics_hash"] = resealed_metrics.metrics_hash
    receipt_hash_values = {
        key: value
        for key, value in receipt_values.items()
        if key not in {"started_at_utc", "completed_at_utc"}
    }
    resealed_receipt = StageReceipt(
        **receipt_values,
        receipt_hash=canonical_sha256(receipt_hash_values),
    )
    _write_contract(run_root / "stage-receipt.json", resealed_receipt)


@pytest.mark.unit
@pytest.mark.parametrize(
    "filename",
    (
        "index-definition.json",
        "knowledge-source-definition.json",
        "knowledge-base-definition.json",
        "documents.json",
        "projection-equivalence.json",
    ),
)
def test_coordinated_local_reseal_cannot_authorize_reuse(
    tmp_path: Path,
    filename: str,
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
    _coordinated_artifact_reseal(first.run_root, filename)
    client.calls.clear()

    repaired = run_l5b(
        source,
        l5a,
        **kwargs,
        client=client,
        state_root=state_root,
    )

    assert not repaired.reused
    assert client.calls == ["inspect", "publish", "read_back"]


@pytest.mark.unit
def test_coordinated_metrics_receipt_and_token_reseal_cannot_authorize_reuse(
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
    metrics = StageResourceMetrics.model_validate_json(
        (first.run_root / "resource-metrics.json").read_text("utf-8")
    )
    metrics_values = metrics.model_dump(
        mode="python",
        exclude={"metrics_hash"},
    )
    metrics_values["network_request_bytes"] += 1
    resealed_metrics = StageResourceMetrics(
        **metrics_values,
        metrics_hash=canonical_sha256(metrics_values),
    )
    _write_contract(first.run_root / "resource-metrics.json", resealed_metrics)
    receipt = StageReceipt.model_validate_json(
        (first.run_root / "stage-receipt.json").read_text("utf-8")
    )
    receipt_values = receipt.model_dump(
        mode="python",
        exclude={"receipt_hash"},
    )
    receipt_values["resource_metrics_hash"] = resealed_metrics.metrics_hash
    receipt_values["remote_operation_refs"] = tuple(
        "publication-token:" + "f" * 32
        if item.startswith("publication-token:")
        else item
        for item in receipt.remote_operation_refs
    )
    resealed_receipt = StageReceipt(
        **receipt_values,
        receipt_hash=canonical_sha256({
            key: value
            for key, value in receipt_values.items()
            if key not in {"started_at_utc", "completed_at_utc"}
        }),
    )
    _write_contract(first.run_root / "stage-receipt.json", resealed_receipt)
    client.calls.clear()

    repaired = run_l5b(
        source,
        l5a,
        **kwargs,
        client=client,
        state_root=state_root,
    )

    assert not repaired.reused
    assert client.calls == ["inspect", "publish", "read_back"]


@pytest.mark.unit
def test_checkpoint_hmac_binds_receipt_timestamps(tmp_path: Path) -> None:
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
    receipt = StageReceipt.model_validate_json(
        (first.run_root / "stage-receipt.json").read_text("utf-8")
    )
    changed = receipt.model_dump(mode="python")
    changed["completed_at_utc"] = receipt.completed_at_utc + timedelta(seconds=1)
    resealed = StageReceipt.model_validate(changed)
    _write_contract(first.run_root / "stage-receipt.json", resealed)
    client.calls.clear()

    repaired = run_l5b(
        source,
        l5a,
        **kwargs,
        client=client,
        state_root=state_root,
    )

    assert not repaired.reused
    assert client.calls == ["inspect", "publish", "read_back"]


@pytest.mark.unit
def test_missing_checkpoint_provider_disables_reuse_and_persists_no_key(
    tmp_path: Path,
) -> None:
    source, l5a, kwargs = _inputs(tmp_path)
    kwargs["checkpoint_integrity_signer"] = None
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
    assert not (state_root / "integrity.key").exists()
    assert not (state_root / "checkpoints").exists()
    client.calls.clear()

    rebuilt = run_l5b(
        source,
        l5a,
        **kwargs,
        client=client,
        state_root=state_root,
    )

    assert not rebuilt.reused
    assert client.calls == ["inspect", "publish", "read_back"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "replacement",
    (
        _TestCheckpointSigner(key_version="2"),
        _TestCheckpointSigner(),
    ),
)
def test_checkpoint_key_rotation_or_replacement_invalidates_reuse(
    tmp_path: Path,
    replacement: _TestCheckpointSigner,
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
    kwargs["checkpoint_integrity_signer"] = replacement

    rebuilt = run_l5b(
        source,
        l5a,
        **kwargs,
        client=client,
        state_root=state_root,
    )

    assert not rebuilt.reused
    assert client.calls == ["inspect", "publish", "read_back"]


@pytest.mark.unit
def test_state_root_key_preseed_and_symlink_cannot_control_checkpoint(
    tmp_path: Path,
) -> None:
    source, l5a, kwargs = _inputs(tmp_path)
    client = _SearchClient(kwargs["access_policy"].policy_hash)
    state_root = tmp_path / ".fkg" / "l5b"
    state_root.mkdir(parents=True)
    attacker_key = tmp_path / "attacker.key"
    attacker_key.write_bytes(b"x" * 32)
    (state_root / "integrity.key").symlink_to(attacker_key)

    first = run_l5b(
        source,
        l5a,
        **kwargs,
        client=client,
        state_root=state_root,
    )
    checkpoint = (
        state_root
        / "checkpoints"
        / f"{first.compiled.fingerprint}.json"
    )
    persisted = checkpoint.read_text("utf-8")
    assert kwargs["checkpoint_integrity_signer"].secret.hex() not in persisted
    assert (state_root / "integrity.key").is_symlink()
    client.calls.clear()

    reused = run_l5b(
        source,
        l5a,
        **kwargs,
        client=client,
        state_root=state_root,
    )

    assert reused.reused
    assert client.calls == ["read_back"]


@pytest.mark.unit
def test_cross_run_checkpoint_replay_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "first").mkdir()
    (tmp_path / "second").mkdir()
    first_source, first_l5a, first_kwargs = _inputs(tmp_path / "first")
    second_source, second_l5a, second_kwargs = _inputs(tmp_path / "second")
    second_kwargs["index_name"] = "search-evidence-index-two"
    first_client = _SearchClient(first_kwargs["access_policy"].policy_hash)
    second_client = _SearchClient(second_kwargs["access_policy"].policy_hash)
    first_root = tmp_path / "first" / ".fkg" / "l5b"
    second_root = tmp_path / "second" / ".fkg" / "l5b"
    first = run_l5b(
        first_source,
        first_l5a,
        **first_kwargs,
        client=first_client,
        state_root=first_root,
    )
    second = run_l5b(
        second_source,
        second_l5a,
        **second_kwargs,
        client=second_client,
        state_root=second_root,
    )
    assert first.compiled.fingerprint != second.compiled.fingerprint
    first_checkpoint = (
        first_root / "checkpoints" / f"{first.compiled.fingerprint}.json"
    )
    second_checkpoint = (
        second_root / "checkpoints" / f"{second.compiled.fingerprint}.json"
    )
    shutil.copyfile(first_checkpoint, second_checkpoint)
    second_client.calls.clear()

    rebuilt = run_l5b(
        second_source,
        second_l5a,
        **second_kwargs,
        client=second_client,
        state_root=second_root,
    )

    assert not rebuilt.reused
    assert second_client.calls == ["inspect", "publish", "read_back"]


@pytest.mark.unit
def test_unsupported_checkpoint_signer_fails_before_remote_call(tmp_path: Path) -> None:
    source, l5a, kwargs = _inputs(tmp_path)
    kwargs["checkpoint_integrity_signer"] = _TestCheckpointSigner(
        algorithm="unsupported",
    )
    client = _SearchClient(kwargs["access_policy"].policy_hash)

    with pytest.raises(
        L5bPublicationError,
        match="L5B_CHECKPOINT_SIGNER_UNSUPPORTED",
    ):
        run_l5b(
            source,
            l5a,
            **kwargs,
            client=client,
            state_root=tmp_path / ".fkg" / "l5b",
        )

    assert client.calls == []


@pytest.mark.unit
@pytest.mark.parametrize("position", (0, 32, 63))
def test_checkpoint_mac_mismatch_at_any_position_rebuilds_safely(
    tmp_path: Path,
    position: int,
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
    checkpoint_path = (
        state_root / "checkpoints" / f"{first.compiled.fingerprint}.json"
    )
    checkpoint = json.loads(checkpoint_path.read_text("utf-8"))
    mac = checkpoint["checkpoint_mac"]
    replacement = "0" if mac[position] != "0" else "1"
    checkpoint["checkpoint_mac"] = (
        mac[:position] + replacement + mac[position + 1 :]
    )
    _write_contract(checkpoint_path, checkpoint)
    client.calls.clear()

    rebuilt = run_l5b(
        source,
        l5a,
        **kwargs,
        client=client,
        state_root=state_root,
    )

    assert not rebuilt.reused
    assert client.calls == ["inspect", "publish", "read_back"]


@pytest.mark.unit
@pytest.mark.parametrize("invalid_checkpoint", ([], "invalid", None))
def test_nonobject_checkpoint_rebuilds_safely(
    tmp_path: Path,
    invalid_checkpoint,
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
    checkpoint_path = (
        state_root / "checkpoints" / f"{first.compiled.fingerprint}.json"
    )
    _write_contract(checkpoint_path, invalid_checkpoint)
    client.calls.clear()

    rebuilt = run_l5b(
        source,
        l5a,
        **kwargs,
        client=client,
        state_root=state_root,
    )

    assert not rebuilt.reused
    assert client.calls == ["inspect", "publish", "read_back"]


@pytest.mark.unit
@pytest.mark.parametrize("verify_error", (False, True))
def test_signer_verify_false_or_exception_disables_reuse(
    tmp_path: Path,
    verify_error: bool,
) -> None:
    source, l5a, kwargs = _inputs(tmp_path)
    signer = _TestCheckpointSigner()
    kwargs["checkpoint_integrity_signer"] = signer
    client = _SearchClient(kwargs["access_policy"].policy_hash)
    state_root = tmp_path / ".fkg" / "l5b"
    run_l5b(
        source,
        l5a,
        **kwargs,
        client=client,
        state_root=state_root,
    )
    signer.verify_error = verify_error
    signer.verify_result = None if verify_error else False
    client.calls.clear()

    rebuilt = run_l5b(
        source,
        l5a,
        **kwargs,
        client=client,
        state_root=state_root,
    )

    assert not rebuilt.reused
    assert client.calls == ["inspect", "publish", "read_back"]


@pytest.mark.unit
def test_provider_specific_verify_exception_disables_reuse(
    tmp_path: Path,
) -> None:
    class ProviderError(Exception):
        pass

    source, l5a, kwargs = _inputs(tmp_path)
    signer = _TestCheckpointSigner()
    kwargs["checkpoint_integrity_signer"] = signer
    client = _SearchClient(kwargs["access_policy"].policy_hash)
    state_root = tmp_path / ".fkg" / "l5b"
    run_l5b(
        source,
        l5a,
        **kwargs,
        client=client,
        state_root=state_root,
    )
    signer.verify_exception = ProviderError("backend verification unavailable")
    client.calls.clear()

    rebuilt = run_l5b(
        source,
        l5a,
        **kwargs,
        client=client,
        state_root=state_root,
    )

    assert not rebuilt.reused
    assert client.calls == ["inspect", "publish", "read_back"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "signer",
    (
        _TestCheckpointSigner(key_id="default"),
        _TestCheckpointSigner(malformed_sign=True),
        _TestCheckpointSigner(sign_error=True),
    ),
)
def test_invalid_signer_identity_or_sign_failure_fails_closed(
    tmp_path: Path,
    signer: _TestCheckpointSigner,
) -> None:
    source, l5a, kwargs = _inputs(tmp_path)
    kwargs["checkpoint_integrity_signer"] = signer
    client = _SearchClient(kwargs["access_policy"].policy_hash)

    with pytest.raises(L5bPublicationError):
        run_l5b(
            source,
            l5a,
            **kwargs,
            client=client,
            state_root=tmp_path / ".fkg" / "l5b",
        )

    assert not (tmp_path / ".fkg" / "l5b" / "runs").exists()


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

    assert payload == {
        "intents": [{"type": "semantic", "search": "return exact evidence"}],
        "knowledgeSourceParams": [{
            "knowledgeSourceName": context.knowledge_source_id,
            "kind": "searchIndex",
            "filterAddOn": filter_text,
            "includeReferences": True,
            "includeReferenceSourceData": True,
            "maxOutputDocuments": min(
                budget.max_output_documents,
                budget.max_search_result_records,
            ),
            "failOnError": True,
        }],
        "outputMode": "extractiveData",
        "retrievalReasoningEffort": {
            "kind": context.retrieval_reasoning_effort,
        },
        "maxRuntimeInSeconds": 30,
        "maxOutputSize": budget.max_output_tokens,
        "maxOutputDocuments": min(
            budget.max_output_documents,
            budget.max_search_result_records,
        ),
        "includeActivity": True,
    }
    assert L5B_AGENTIC_API_VERSION == "2026-05-01-preview"
    escaped = canonical_scope_filter(
        canonical_entity_ids=("entity:x' or true or 'y",),
        canonical_type_ids=(),
        canonical_relationship_ids=(),
        access_policy_hash="a" * 64,
        asserted_publication_hash="b" * 64,
    )
    assert "entity:x'' or true or ''y" in escaped

    malformed = context.model_copy()
    object.__setattr__(malformed, "retrieval_reasoning_effort", "extreme")
    with pytest.raises(ValueError, match="unsupported retrieval reasoning effort"):
        build_agentic_retrieve_payload(
            malformed,
            budget,
            scope,
            query_text="return exact evidence",
            filter_add_on=filter_text,
        )


def _request_with_budget_changes(mode: str = "agentic_preview", **changes):
    context, budget = request_context(mode)
    budget_values = budget.model_dump(
        mode="python",
        exclude={"budget_hash"},
    )
    budget_values.update(changes)
    revised_budget = QueryBudgetV1_1(
        **budget_values,
        budget_hash=canonical_sha256(budget_values),
    )
    context_values = context.model_dump(
        mode="python",
        exclude={"request_context_hash"},
    )
    context_values["query_budget_hash"] = revised_budget.budget_hash
    revised_context = AgenticRetrievalRequestContextV1_1(
        **context_values,
        request_context_hash=canonical_sha256(context_values),
    )
    return revised_context, revised_budget


def _preview_payload(context, budget):
    scope = resolved_retrieval_scope()
    filter_text = canonical_scope_filter(
        canonical_entity_ids=context.filter_add_on.canonical_entity_ids,
        canonical_type_ids=context.filter_add_on.exact_type_ids,
        canonical_relationship_ids=(
            context.filter_add_on.canonical_relationship_ids
        ),
        access_policy_hash=context.acl_scope_hash,
        asserted_publication_hash=context.asserted_publication_hash,
    )
    return build_agentic_retrieve_payload(
        context,
        budget,
        scope,
        query_text="evidence",
        filter_add_on=filter_text,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("configured", "records", "expected"),
    ((1, 1, 1), (25, 1, 1), (1, 25, 1), (25, 25, 25)),
)
def test_preview_document_limits_are_exact_and_never_round_up(
    configured: int,
    records: int,
    expected: int,
) -> None:
    context, budget = _request_with_budget_changes(
        max_output_documents=configured,
        max_search_result_records=records,
    )

    payload = _preview_payload(context, budget)

    assert payload["maxOutputDocuments"] == expected
    assert payload["knowledgeSourceParams"][0]["maxOutputDocuments"] == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("dimension", "error_code"),
    (
        (
            "max_agentic_source_calls",
            "L5B_AGENTIC_SOURCE_CALL_BUDGET_UNAVAILABLE",
        ),
        (
            "max_agentic_internal_subqueries",
            "L5B_AGENTIC_SUBQUERY_BUDGET_UNAVAILABLE",
        ),
    ),
)
def test_preview_zero_activity_budget_fails_before_provider_call(
    dimension: str,
    error_code: str,
) -> None:
    context, budget = _request_with_budget_changes(**{dimension: 0})
    provider_calls: list[str] = []

    with pytest.raises(L5bPublicationError, match=error_code):
        _preview_payload(context, budget)

    assert provider_calls == []


@pytest.mark.unit
def test_validator_zero_source_one_record_case_fails_closed() -> None:
    context, budget = _request_with_budget_changes(
        max_agentic_source_calls=0,
        max_search_result_records=1,
    )

    with pytest.raises(
        L5bPublicationError,
        match="L5B_AGENTIC_SOURCE_CALL_BUDGET_UNAVAILABLE",
    ):
        _preview_payload(context, budget)


@pytest.mark.unit
def test_preview_one_source_and_subquery_budget_emits_one_of_each() -> None:
    context, budget = _request_with_budget_changes(
        max_agentic_source_calls=1,
        max_agentic_internal_subqueries=1,
        max_search_result_records=1,
    )

    payload = _preview_payload(context, budget)

    assert len(payload["intents"]) == 1
    assert len(payload["knowledgeSourceParams"]) == 1
    assert payload["maxOutputDocuments"] == 1


@pytest.mark.unit
def test_query_budget_boundary_matrix_shapes_all_outgoing_preview_limits() -> None:
    context, budget = _request_with_budget_changes(
        max_agentic_internal_subqueries=1,
        max_agentic_source_calls=1,
        max_output_documents=25,
        max_output_tokens=17,
        max_output_bytes=19,
        max_runtime_milliseconds=1999,
        max_search_result_records=1,
    )

    payload = _preview_payload(context, budget)

    assert len(payload["intents"]) <= budget.max_agentic_internal_subqueries
    assert len(payload["knowledgeSourceParams"]) <= budget.max_agentic_source_calls
    assert payload["maxOutputDocuments"] == 1
    assert payload["knowledgeSourceParams"][0]["maxOutputDocuments"] == 1
    assert payload["maxOutputSize"] == 17
    assert payload["maxRuntimeInSeconds"] * 1000 <= (
        budget.max_runtime_milliseconds
    )
    assert "maxOutputSizeInTokens" not in payload
    assert "vectorQueries" not in payload


@pytest.mark.unit
@pytest.mark.parametrize(
    ("changes", "field_name"),
    (
        ({"max_output_tokens": 2 ** 31}, "maxOutputSize"),
        (
            {
                "max_output_documents": 2 ** 31,
                "max_search_result_records": 2 ** 31,
            },
            "maxOutputDocuments",
        ),
        (
            {"max_runtime_milliseconds": (2 ** 31) * 1000},
            "maxRuntimeInSeconds",
        ),
    ),
)
def test_preview_rejects_provider_int32_overflow_before_call(
    changes: dict[str, int],
    field_name: str,
) -> None:
    context, budget = _request_with_budget_changes(**changes)
    provider_calls: list[str] = []

    with pytest.raises(
        L5bPublicationError,
        match=f"{field_name} must fit the provider signed int32 range",
    ):
        _preview_payload(context, budget)

    assert provider_calls == []


@pytest.mark.unit
def test_preview_accepts_exact_provider_int32_boundaries() -> None:
    maximum = (2 ** 31) - 1
    context, budget = _request_with_budget_changes(
        max_output_documents=maximum,
        max_search_result_records=maximum,
        max_output_tokens=maximum,
        max_runtime_milliseconds=(maximum * 1000) + 999,
    )

    payload = _preview_payload(context, budget)

    assert payload["maxOutputDocuments"] == maximum
    assert payload["knowledgeSourceParams"][0]["maxOutputDocuments"] == maximum
    assert payload["maxOutputSize"] == maximum
    assert payload["maxRuntimeInSeconds"] == maximum
    assert payload["maxRuntimeInSeconds"] * 1000 <= (
        budget.max_runtime_milliseconds
    )


def _request_with_runtime(milliseconds: int):
    return _request_with_budget_changes(
        max_runtime_milliseconds=milliseconds,
    )


@pytest.mark.unit
@pytest.mark.parametrize("milliseconds", (1, 999))
def test_agentic_timeout_rejects_unrepresentable_subsecond_budget(
    milliseconds: int,
) -> None:
    context, budget = _request_with_runtime(milliseconds)
    scope = resolved_retrieval_scope()
    filter_text = canonical_scope_filter(
        canonical_entity_ids=context.filter_add_on.canonical_entity_ids,
        canonical_type_ids=context.filter_add_on.exact_type_ids,
        canonical_relationship_ids=(
            context.filter_add_on.canonical_relationship_ids
        ),
        access_policy_hash=context.acl_scope_hash,
        asserted_publication_hash=context.asserted_publication_hash,
    )
    provider_calls: list[str] = []
    with pytest.raises(
        L5bPublicationError,
        match="L5B_PROVIDER_TIMEOUT_UNREPRESENTABLE",
    ):
        build_agentic_retrieve_payload(
            context,
            budget,
            scope,
            query_text="evidence",
            filter_add_on=filter_text,
        )
    assert provider_calls == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("milliseconds", "expected_seconds"),
    ((1000, 1), (1999, 1), (2000, 2)),
)
def test_agentic_timeout_floor_never_exceeds_budget(
    milliseconds: int,
    expected_seconds: int,
) -> None:
    context, budget = _request_with_runtime(milliseconds)
    scope = resolved_retrieval_scope()
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
        query_text="evidence",
        filter_add_on=filter_text,
    )
    assert payload["maxRuntimeInSeconds"] == expected_seconds
    assert payload["maxRuntimeInSeconds"] * 1000 <= milliseconds
    assert payload["retrievalReasoningEffort"] == {
        "kind": context.retrieval_reasoning_effort,
    }
    assert L5B_AGENTIC_API_VERSION == "2026-05-01-preview"


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


@pytest.mark.unit
def test_direct_fallback_requires_authorized_origin_and_separate_budget() -> None:
    origin, origin_budget = request_context("agentic_preview")
    context, budget = request_context(
        "direct_hybrid_prefilter",
        fallback_for=origin,
    )
    scope = resolved_retrieval_scope()

    with pytest.raises(
        L5bPublicationError,
        match="L5B_DIRECT_FALLBACK_UNAUTHORIZED",
    ):
        build_direct_search_payload(
            context,
            budget,
            scope,
            query_text="evidence",
            vector=None,
        )

    request = build_direct_search_payload(
        context,
        budget,
        scope,
        query_text="evidence",
        vector=None,
        originating_context=origin,
        originating_budget=origin_budget,
    )
    assert request.payload["top"] == min(
        budget.max_output_documents,
        budget.max_search_result_records,
    )


@pytest.mark.unit
def test_direct_vector_and_record_candidates_share_one_budget_shape() -> None:
    context, budget = _request_with_budget_changes(
        "direct_hybrid_prefilter",
        max_output_documents=25,
        max_search_result_records=1,
    )

    request = build_direct_search_payload(
        context,
        budget,
        resolved_retrieval_scope(),
        query_text="evidence",
        vector=(0.0,) * 1536,
        vector_available=True,
    )

    assert request.payload["top"] == 1
    assert request.payload["vectorQueries"][0]["k"] == 1


@pytest.mark.unit
def test_direct_rejects_provider_int32_overflow_before_call() -> None:
    context, budget = _request_with_budget_changes(
        "direct_hybrid_prefilter",
        max_output_documents=2 ** 31,
        max_search_result_records=2 ** 31,
    )

    with pytest.raises(
        L5bPublicationError,
        match="maxOutputDocuments must fit the provider signed int32 range",
    ):
        build_direct_search_payload(
            context,
            budget,
            resolved_retrieval_scope(),
            query_text="evidence",
            vector=(0.0,) * 1536,
            vector_available=True,
        )


@pytest.mark.unit
def test_candidate_accounting_remains_budget_defense_in_depth(
    monkeypatch,
) -> None:
    ontology_scope = resolved_ontology_scope()
    retrieval_scope = resolved_retrieval_scope()
    context, budget = _request_with_budget_changes(
        max_search_candidate_records=1,
    )
    document = _reference_document(
        context,
        retrieval_scope.canonical_member_ids[0],
        0,
    )
    publication = _runtime_publication(context, [document])
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args, **_kwargs: None,
    )
    response = _agentic_response(context, [document])
    response["activity"][0]["count"] = 2

    result = interpret_retrieval_response(
        context,
        budget,
        ontology_scope,
        retrieval_scope,
        publication=publication,
        checkpoint_integrity_signer=_TEST_CHECKPOINT_SIGNER,
        response=response,
        accounting=L5bRemoteAccounting(
            operation_refs=("retrieve:candidate-overage",),
            request_bytes=100,
            response_bytes=1000,
            retry_count=0,
            retry_wait_ms=0,
            latency_ms=25,
            candidate_count=2,
            output_tokens=0,
        ),
    )

    assert result.coverage.coverage_status == "partial"
    assert result.coverage.matched_document_count == 2
    assert result.coverage.budget.observed_search_result_records == 1
    assert result.coverage.budget.observed_output_documents == 1
    assert result.coverage.returned_document_count == 1
    assert result.coverage.reference_count == 1
    assert result.coverage.source_calls[0].matched_count == 2
    assert result.coverage.source_calls[0].returned_count == 1
    assert result.coverage.budget.budget_exhausted_dimensions == (
        "max_search_candidate_records",
    )
    assert any(
        failure.reason_code == "retrieval_budget_exhausted"
        for failure in result.coverage.failures
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
        "canonical_assertion_ids": [f"assertion:membership:{index}"],
        "required_member_manifest_ids": ["manifest:required-members"],
        "source_id": "source-file:manual",
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
        "governed_asset_reference_id": "governed-asset:manual",
        "governed_asset_reference_hash": HASH_A,
        "vector": None,
        "vector_state": "unavailable",
    }
    values["document_hash"] = canonical_sha256(values)
    return values


@pytest.mark.unit
@pytest.mark.parametrize("scalar", ("[]", '"text"', "1", "null"))
def test_locator_rejects_nonobject_json(scalar: str) -> None:
    with pytest.raises((TypeError, ValueError)):
        _parse_immutable_locator(scalar)


@pytest.mark.unit
@pytest.mark.parametrize(
    "mutation",
    ("duplicate", "extra", "missing", "section-string", "section-object", "section-mixed"),
)
def test_locator_rejects_schema_and_section_shape_drift(mutation: str) -> None:
    document = _reference_document(request_context()[0], "entity:member-0", 0)
    raw = json.loads(document["immutable_locator_json"])
    if mutation == "duplicate":
        locator_json = document["immutable_locator_json"].replace(
            '"page":4',
            '"page":4,"page":4',
            1,
        )
    else:
        if mutation == "extra":
            raw["unexpected"] = "value"
        elif mutation == "missing":
            raw.pop("locator_hash")
        elif mutation == "section-string":
            raw["section_path"] = "section:maintenance"
        elif mutation == "section-object":
            raw["section_path"] = {"label": "maintenance"}
        else:
            raw["section_path"] = ["maintenance", 3]
        locator_json = canonical_json(raw)
    with pytest.raises((TypeError, ValueError)):
        _parse_immutable_locator(locator_json)


def _runtime_publication(context, documents):
    policy = SimpleNamespace(
        access_policy_id="access-policy:evidence",
        policy_hash=context.acl_scope_hash,
        authorization_resource_id="authorization-resource:evidence",
        principal_scopes=(
            PrincipalScope(
                principal_type="managed_identity",
                principal_id="principal:reader",
                resource_scope_ids=("scope:generic",),
            ),
        ),
    )
    asset = SimpleNamespace(
        governed_asset_reference_id="governed-asset:manual",
        asset_reference_hash=HASH_A,
        source_file_id="source-file:manual",
        asset_id="asset:manual",
        asset_version_id="asset-version:manual:1",
        content_hash=HASH_B,
    )
    return SimpleNamespace(
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
            documents=tuple(documents),
            access_policy=policy,
            governed_assets=(asset,),
        )
    )


def _agentic_response(context, documents):
    return {
        "requestId": "provider-request:bounded",
        "references": [
            {
                "id": f"search-reference:{index}",
                "activitySource": 1,
                "sourceData": document,
            }
            for index, document in enumerate(documents)
        ],
        "activity": [{
            "id": 1,
            "type": "searchIndex",
            "knowledgeSourceName": context.knowledge_source_id,
            "count": len(documents),
            "elapsedMs": 25,
            "searchIndexArguments": {
                "search": "exact evidence",
                "filter": "sealed-filter",
            },
        }],
    }


def _retrieval_accounting(count: int) -> L5bRemoteAccounting:
    return L5bRemoteAccounting(
        operation_refs=("retrieve:scope-test",),
        request_bytes=100,
        response_bytes=1000,
        retry_count=0,
        retry_wait_ms=0,
        latency_ms=25,
        candidate_count=count,
        output_tokens=0,
    )


def _mutate_returned_document(document: dict, surface: str) -> None:
    if surface == "locator":
        locator_values = json.loads(document["immutable_locator_json"])
        locator_values["page"] = 5
        locator_values["locator_hash"] = canonical_sha256({
            key: value
            for key, value in locator_values.items()
            if key != "locator_hash"
        })
        document["immutable_locator_json"] = canonical_json(locator_values)
        document["immutable_locator_hash"] = locator_values["locator_hash"]
        document["page"] = 5
    elif surface == "quote":
        document["content"] = "SPOOFED QUOTE"
        document["source_quote"] = "SPOOFED QUOTE"
        document["quote_hash"] = canonical_sha256("SPOOFED QUOTE")
    elif surface == "acl":
        document["acl_scope_keys"] = ["scope:spoofed"]
    elif surface == "scope":
        document["canonical_entity_ids"] = ["entity:spoofed"]
    elif surface == "display":
        document["original_document_name"] = "Spoofed Manual.pdf"
    elif surface == "evidence":
        document["evidence_span_ids"] = ["evidence:spoofed"]
        document["evidence_span_hashes"] = [HASH_B]
    elif surface == "hash":
        document["document_hash"] = "f" * 64
    elif surface == "missing":
        document.pop("source_unit_hash")
    elif surface == "extra":
        document["unexpected_field"] = "spoofed"


@pytest.mark.unit
@pytest.mark.parametrize(
    "surface",
    ("locator", "quote", "acl", "scope", "display", "evidence", "hash", "missing", "extra"),
)
@pytest.mark.parametrize("coordinated_hash", (False, True))
def test_returned_document_spoof_never_reaches_citation(
    surface: str,
    coordinated_hash: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology_scope = resolved_ontology_scope()
    retrieval_scope = resolved_retrieval_scope()
    context, budget = request_context()
    sealed_documents = [
        _reference_document(context, entity_id, index)
        for index, entity_id in enumerate(retrieval_scope.canonical_member_ids)
    ]
    returned_documents = [dict(item) for item in sealed_documents]
    _mutate_returned_document(returned_documents[0], surface)
    if coordinated_hash and surface != "hash":
        returned_documents[0]["document_hash"] = canonical_sha256({
            key: value
            for key, value in returned_documents[0].items()
            if key != "document_hash"
        })
    publication = _runtime_publication(context, sealed_documents)
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args, **_kwargs: None,
    )

    result = interpret_retrieval_response(
        context,
        budget,
        ontology_scope,
        retrieval_scope,
        publication=publication,
        checkpoint_integrity_signer=_TEST_CHECKPOINT_SIGNER,
        response=_agentic_response(context, returned_documents),
        accounting=_retrieval_accounting(len(returned_documents)),
    )

    assert result.coverage.coverage_status == "partial"
    assert len(result.citations) == len(returned_documents) - 1
    assert result.coverage.matched_document_count == len(returned_documents)
    assert result.coverage.returned_document_count == len(result.citations)
    assert result.coverage.reference_count == len(result.citations)
    assert result.coverage.budget.observed_output_documents == len(
        result.citations
    )
    assert result.coverage.source_calls[0].returned_count == len(
        result.citations
    )
    assert all(
        citation.search_document_id != sealed_documents[0]["id"]
        for citation in result.citations
    )
    assert any(
        failure.reason_code == "citation_invalid"
        for failure in result.coverage.failures
    )
    assert all(
        citation.exact_authorized_quote != "SPOOFED QUOTE"
        for citation in result.citations
    )


@pytest.mark.unit
@pytest.mark.parametrize("duplicate_kind", ("reference", "document"))
def test_duplicate_search_response_identity_is_quarantined(
    duplicate_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology_scope = resolved_ontology_scope()
    retrieval_scope = resolved_retrieval_scope()
    context, budget = request_context()
    sealed_documents = [
        _reference_document(context, entity_id, index)
        for index, entity_id in enumerate(retrieval_scope.canonical_member_ids)
    ]
    response = _agentic_response(context, sealed_documents)
    duplicate = dict(response["references"][0])
    if duplicate_kind == "document":
        duplicate["id"] = "search-reference:duplicate"
    response["references"].append(duplicate)
    response["activity"][0]["count"] += 1
    publication = _runtime_publication(context, sealed_documents)
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args, **_kwargs: None,
    )

    result = interpret_retrieval_response(
        context,
        budget,
        ontology_scope,
        retrieval_scope,
        publication=publication,
        checkpoint_integrity_signer=_TEST_CHECKPOINT_SIGNER,
        response=response,
        accounting=_retrieval_accounting(len(response["references"])),
    )

    assert result.coverage.coverage_status == "partial"
    assert all(
        citation.search_document_id != sealed_documents[0]["id"]
        for citation in result.citations
    )


@pytest.mark.unit
def test_preverification_attacker_metadata_is_never_echoed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malicious = "api_key=supersecret\n" + "X" * 2000
    ontology_scope = resolved_ontology_scope()
    retrieval_scope = resolved_retrieval_scope()
    context, budget = request_context()
    sealed_documents = [
        _reference_document(context, entity_id, index)
        for index, entity_id in enumerate(retrieval_scope.canonical_member_ids)
    ]
    returned_documents = [dict(item) for item in sealed_documents]
    returned_documents[0]["source_quote"] = malicious
    returned_documents[0]["content"] = malicious
    returned_documents[0]["quote_hash"] = canonical_sha256(malicious)
    returned_documents[0]["document_hash"] = canonical_sha256({
        key: value
        for key, value in returned_documents[0].items()
        if key != "document_hash"
    })
    unknown = dict(sealed_documents[1])
    unknown["id"] = malicious
    unknown["document_hash"] = canonical_sha256({
        key: value for key, value in unknown.items() if key != "document_hash"
    })
    response = _agentic_response(context, returned_documents)
    response["requestId"] = malicious
    response["correlationId"] = malicious
    response["activity"][0]["warning"] = malicious
    response["references"].extend((
        {
            "id": malicious,
            "activitySource": 1,
            "sourceData": unknown,
        },
        {
            "id": malicious,
            "activitySource": 1,
            "sourceData": unknown,
        },
    ))
    response["activity"][0]["count"] = len(response["references"])
    publication = _runtime_publication(context, sealed_documents)
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args, **_kwargs: None,
    )

    result = interpret_retrieval_response(
        context,
        budget,
        ontology_scope,
        retrieval_scope,
        publication=publication,
        checkpoint_integrity_signer=_TEST_CHECKPOINT_SIGNER,
        response=response,
        accounting=L5bRemoteAccounting(
            operation_refs=("retrieve:opaque-test",),
            request_bytes=100,
            response_bytes=1000,
            retry_count=0,
            retry_wait_ms=0,
            latency_ms=25,
            candidate_count=len(response["references"]),
            output_tokens=0,
            warning_codes=(malicious,),
        ),
    )
    persisted_result = canonical_json({
        "coverage": result.coverage.model_dump(mode="json"),
        "citations": [
            item.model_dump(mode="json") for item in result.citations
        ],
        "presentations": [
            item.model_dump(mode="json") for item in result.presentations
        ],
    })

    assert result.coverage.coverage_status == "partial"
    assert malicious not in persisted_result
    assert sealed_documents[0]["id"] in {
        canonical_id
        for failure in result.coverage.failures
        for canonical_id in failure.canonical_ids
    }


@pytest.mark.unit
def test_sealed_unrelated_document_is_quarantined_before_citation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology_scope = resolved_ontology_scope()
    retrieval_scope = resolved_retrieval_scope()
    context, budget = request_context()
    valid = [
        _reference_document(context, entity_id, index)
        for index, entity_id in enumerate(retrieval_scope.canonical_member_ids)
    ]
    spoof = _reference_document(context, "entity:unrelated", 99)
    spoof["content"] = "OUT-OF-SCOPE SECRET QUOTE"
    spoof["source_quote"] = "OUT-OF-SCOPE SECRET QUOTE"
    spoof["quote_hash"] = canonical_sha256(spoof["source_quote"])
    spoof["document_hash"] = canonical_sha256({
        key: value for key, value in spoof.items() if key != "document_hash"
    })
    documents = [*valid, spoof]
    publication = _runtime_publication(context, documents)
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args, **_kwargs: None,
    )

    result = interpret_retrieval_response(
        context,
        budget,
        ontology_scope,
        retrieval_scope,
        publication=publication,
        checkpoint_integrity_signer=_TEST_CHECKPOINT_SIGNER,
        response=_agentic_response(context, documents),
        accounting=_retrieval_accounting(len(documents)),
    )

    assert result.coverage.coverage_status == "partial"
    assert len(result.citations) == len(valid)
    assert "entity:unrelated" in result.coverage.orphan_canonical_ids
    assert any(
        failure.reason_code == "unexpected_member"
        and failure.canonical_ids == ("entity:unrelated",)
        for failure in result.coverage.failures
    )
    assert all(
        citation.exact_authorized_quote != "OUT-OF-SCOPE SECRET QUOTE"
        for citation in result.citations
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mutation", "expected_reason", "expected_id"),
    (
        ("type", "hierarchy_scope_mismatch", "type:unrelated"),
        (
            "relationship-endpoint",
            "unexpected_member",
            "entity:unrelated-endpoint",
        ),
        ("acl", "citation_unauthorized", "scope:unauthorized"),
        ("property", "scope_key_missing", "property:unscoped"),
        (
            "member-manifest",
            "collection_hash_mismatch",
            "manifest:unrelated",
        ),
        (
            "member-manifest-missing",
            "collection_hash_mismatch",
            "manifest:required-members",
        ),
        ("source", "citation_invalid", "source-file:unrelated"),
        ("asset-missing", "citation_invalid", "governed-asset:manual"),
        ("publication-missing", "projection_hash_stale", HASH_A),
        (
            "section-path",
            "citation_invalid",
            "dimension:section_path",
        ),
    ),
)
def test_scope_dimension_mismatch_never_exposes_document(
    mutation: str,
    expected_reason: str,
    expected_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology_scope = resolved_ontology_scope()
    retrieval_scope = resolved_retrieval_scope()
    context, budget = request_context()
    documents = [
        _reference_document(context, entity_id, index)
        for index, entity_id in enumerate(retrieval_scope.canonical_member_ids)
    ]
    changed = dict(documents[0])
    changed["source_quote"] = f"QUARANTINED {mutation}"
    changed["content"] = changed["source_quote"]
    changed["quote_hash"] = canonical_sha256(changed["source_quote"])
    if mutation == "type":
        changed["canonical_type_ids"] = ["type:unrelated"]
    elif mutation == "relationship-endpoint":
        changed["canonical_entity_ids"] = [
            retrieval_scope.canonical_member_ids[0],
            "entity:unrelated-endpoint",
        ]
    elif mutation == "acl":
        changed["acl_scope_keys"] = ["scope:unauthorized"]
    elif mutation == "property":
        changed["canonical_property_ids"] = ["property:unscoped"]
    elif mutation == "member-manifest":
        changed["required_member_manifest_ids"] = ["manifest:unrelated"]
    elif mutation == "member-manifest-missing":
        changed["required_member_manifest_ids"] = []
    elif mutation == "asset-missing":
        changed["governed_asset_reference_id"] = None
        changed["governed_asset_reference_hash"] = None
    elif mutation == "publication-missing":
        changed["asserted_publication_hash"] = None
    elif mutation == "section-path":
        malicious_section = "trusted\u202efdp.exe"
        changed["section_path"] = [malicious_section]
        changed_locator = json.loads(changed["immutable_locator_json"])
        changed_locator["section_path"] = [malicious_section]
        changed["immutable_locator_json"] = canonical_json(changed_locator)
    else:
        changed["source_id"] = "source-file:unrelated"
    changed["document_hash"] = canonical_sha256({
        key: value for key, value in changed.items() if key != "document_hash"
    })
    documents[0] = changed
    publication = _runtime_publication(context, documents)
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args, **_kwargs: None,
    )

    result = interpret_retrieval_response(
        context,
        budget,
        ontology_scope,
        retrieval_scope,
        publication=publication,
        checkpoint_integrity_signer=_TEST_CHECKPOINT_SIGNER,
        response=_agentic_response(context, documents),
        accounting=_retrieval_accounting(len(documents)),
    )

    assert result.coverage.coverage_status == "partial"
    assert len(result.citations) == len(documents) - 1
    assert all(
        citation.exact_authorized_quote != changed["source_quote"]
        for citation in result.citations
    )
    assert any(
        failure.reason_code == expected_reason
        and any(expected_id in item for item in failure.canonical_ids)
        for failure in result.coverage.failures
    )


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
    publication = _runtime_publication(
        context,
        [reference["sourceData"] for reference in references],
    )
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args, **_kwargs: None,
    )

    result = interpret_retrieval_response(
        context,
        budget,
        ontology_scope,
        retrieval_scope,
        publication=publication,
        checkpoint_integrity_signer=_TEST_CHECKPOINT_SIGNER,
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
    publication = _runtime_publication(context, documents)
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args, **_kwargs: None,
    )

    result = interpret_retrieval_response(
        context,
        budget,
        ontology_scope,
        retrieval_scope,
        publication=publication,
        checkpoint_integrity_signer=_TEST_CHECKPOINT_SIGNER,
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
    assert result.coverage.budget.observed_agentic_retrieval_invocations == 0
    assert result.coverage.budget.observed_agentic_internal_subqueries == 0
    assert result.coverage.budget.observed_agentic_source_calls == 0
    assert result.coverage.budget.observed_direct_search_requests == 1
    assert "max_agentic_source_calls" not in (
        result.coverage.budget.budget_exhausted_dimensions
    )


@pytest.mark.unit
def test_direct_success_is_complete_with_mode_specific_accounting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology_scope = resolved_ontology_scope()
    retrieval_scope = resolved_retrieval_scope()
    context, budget = request_context("direct_hybrid_prefilter")
    documents = [
        _reference_document(context, entity_id, index)
        for index, entity_id in enumerate(retrieval_scope.canonical_member_ids)
    ]
    publication = _runtime_publication(context, documents)
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args, **_kwargs: None,
    )

    result = interpret_retrieval_response(
        context,
        budget,
        ontology_scope,
        retrieval_scope,
        publication=publication,
        checkpoint_integrity_signer=_TEST_CHECKPOINT_SIGNER,
        response={"value": documents},
        accounting=L5bRemoteAccounting(
            operation_refs=("direct-search:complete",),
            request_bytes=100,
            response_bytes=1000,
            retry_count=0,
            retry_wait_ms=0,
            latency_ms=25,
            candidate_count=len(documents),
        ),
    )

    assert result.coverage.coverage_status == "complete"
    assert result.coverage.budget.observed_agentic_retrieval_invocations == 0
    assert result.coverage.budget.observed_agentic_internal_subqueries == 0
    assert result.coverage.budget.observed_agentic_source_calls == 0
    assert result.coverage.budget.observed_direct_search_requests == 1
    assert result.coverage.budget.budget_exhausted_dimensions == ()


@pytest.mark.unit
def test_blocked_agentic_request_can_use_authorized_direct_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology_scope = resolved_ontology_scope()
    retrieval_scope = resolved_retrieval_scope()
    origin, origin_budget = _request_with_budget_changes(
        max_agentic_source_calls=0,
    )
    with pytest.raises(
        L5bPublicationError,
        match="L5B_AGENTIC_SOURCE_CALL_BUDGET_UNAVAILABLE",
    ):
        _preview_payload(origin, origin_budget)

    context, budget = request_context(
        "direct_hybrid_prefilter",
        fallback_for=origin,
    )
    build_direct_search_payload(
        context,
        budget,
        retrieval_scope,
        query_text="evidence",
        vector=(0.0,) * 1536,
        vector_available=True,
        originating_context=origin,
        originating_budget=origin_budget,
    )
    documents = [
        _reference_document(context, entity_id, index)
        for index, entity_id in enumerate(retrieval_scope.canonical_member_ids)
    ]
    publication = _runtime_publication(context, documents)
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args, **_kwargs: None,
    )

    result = interpret_retrieval_response(
        context,
        budget,
        ontology_scope,
        retrieval_scope,
        publication=publication,
        checkpoint_integrity_signer=_TEST_CHECKPOINT_SIGNER,
        response={"value": documents},
        accounting=L5bRemoteAccounting(
            operation_refs=("direct-search:fallback",),
            request_bytes=100,
            response_bytes=1000,
            retry_count=0,
            retry_wait_ms=0,
            latency_ms=25,
            candidate_count=len(documents),
        ),
        originating_context=origin,
        originating_budget=origin_budget,
        fallback_reason_code="retrieval_budget_exhausted",
    )

    assert result.coverage.coverage_status == "complete"
    assert result.coverage.fallback_used is True
    assert result.coverage.budget.observed_agentic_source_calls == 0
    assert result.coverage.budget.observed_direct_search_requests == 1


@pytest.mark.unit
def test_direct_candidate_overage_exhausts_shared_record_budget_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology_scope = resolved_ontology_scope()
    retrieval_scope = resolved_retrieval_scope()
    context, budget = _request_with_budget_changes(
        "direct_hybrid_prefilter",
        max_search_candidate_records=10,
    )
    documents = [
        _reference_document(context, entity_id, index)
        for index, entity_id in enumerate(retrieval_scope.canonical_member_ids)
    ]
    publication = _runtime_publication(context, documents)
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args, **_kwargs: None,
    )

    result = interpret_retrieval_response(
        context,
        budget,
        ontology_scope,
        retrieval_scope,
        publication=publication,
        checkpoint_integrity_signer=_TEST_CHECKPOINT_SIGNER,
        response={"value": documents},
        accounting=L5bRemoteAccounting(
            operation_refs=("direct-search:mixed-accounting",),
            request_bytes=100,
            response_bytes=1000,
            retry_count=0,
            retry_wait_ms=0,
            latency_ms=25,
            candidate_count=11,
        ),
    )

    assert result.coverage.coverage_status == "partial"
    assert result.coverage.matched_document_count == 11
    assert result.coverage.returned_document_count == len(documents)
    assert result.coverage.reference_count == len(documents)
    assert result.coverage.budget.observed_search_result_records == len(documents)
    assert result.coverage.budget.observed_output_documents == len(documents)
    assert result.coverage.source_calls[0].matched_count == 11
    assert result.coverage.source_calls[0].returned_count == len(documents)
    assert result.coverage.budget.observed_agentic_source_calls == 0
    assert result.coverage.budget.observed_direct_search_requests == 1
    assert result.coverage.budget.budget_exhausted_dimensions == (
        "max_search_candidate_records",
    )


@pytest.mark.unit
@pytest.mark.parametrize("mode", ("agentic_preview", "direct_hybrid_prefilter"))
def test_candidate_count_below_verified_returns_fails_closed(
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology_scope = resolved_ontology_scope()
    retrieval_scope = resolved_retrieval_scope()
    context, budget = request_context(mode)
    document = _reference_document(
        context,
        retrieval_scope.canonical_member_ids[0],
        0,
    )
    publication = _runtime_publication(context, [document])
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args, **_kwargs: None,
    )
    response = (
        _agentic_response(context, [document])
        if mode == "agentic_preview"
        else {"value": [document]}
    )

    with pytest.raises(
        L5bPublicationError,
        match="L5B_REMOTE_ACCOUNTING_CONTRADICTORY",
    ):
        interpret_retrieval_response(
            context,
            budget,
            ontology_scope,
            retrieval_scope,
            publication=publication,
            checkpoint_integrity_signer=_TEST_CHECKPOINT_SIGNER,
            response=response,
            accounting=L5bRemoteAccounting(
                operation_refs=(f"{mode}:contradictory",),
                request_bytes=100,
                response_bytes=1000,
                retry_count=0,
                retry_wait_ms=0,
                latency_ms=25,
                candidate_count=0,
                output_tokens=0 if mode == "agentic_preview" else None,
            ),
        )


@pytest.mark.unit
def test_duplicate_search_activity_ids_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology_scope = resolved_ontology_scope()
    retrieval_scope = resolved_retrieval_scope()
    context, budget = request_context()
    document = _reference_document(
        context,
        retrieval_scope.canonical_member_ids[0],
        0,
    )
    publication = _runtime_publication(context, [document])
    response = _agentic_response(context, [document])
    response["activity"].append(dict(response["activity"][0]))
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args, **_kwargs: None,
    )
    with pytest.raises(
        L5bPublicationError,
        match="duplicate Search activity IDs",
    ):
        interpret_retrieval_response(
            context,
            budget,
            ontology_scope,
            retrieval_scope,
            publication=publication,
            checkpoint_integrity_signer=_TEST_CHECKPOINT_SIGNER,
            response=response,
            accounting=_retrieval_accounting(1),
        )


@pytest.mark.unit
@pytest.mark.parametrize("mode", ("agentic_preview", "direct_hybrid_prefilter"))
def test_zero_result_accounting_stays_zero(
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology_scope = resolved_ontology_scope()
    retrieval_scope = resolved_retrieval_scope()
    context, budget = request_context(mode)
    publication = _runtime_publication(context, [])
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args, **_kwargs: None,
    )
    response = (
        {
            "requestId": "provider-request:zero",
            "references": [],
            "activity": [{
                "id": 1,
                "type": "searchIndex",
                "knowledgeSourceName": context.knowledge_source_id,
                "count": 0,
                "searchIndexArguments": {"search": "none"},
            }],
        }
        if mode == "agentic_preview"
        else {"value": []}
    )

    result = interpret_retrieval_response(
        context,
        budget,
        ontology_scope,
        retrieval_scope,
        publication=publication,
        checkpoint_integrity_signer=_TEST_CHECKPOINT_SIGNER,
        response=response,
        accounting=L5bRemoteAccounting(
            operation_refs=(f"{mode}:zero",),
            request_bytes=100,
            response_bytes=100,
            retry_count=0,
            retry_wait_ms=0,
            latency_ms=25,
            candidate_count=0,
            output_tokens=0 if mode == "agentic_preview" else None,
        ),
    )

    assert result.coverage.matched_document_count == 0
    assert result.coverage.returned_document_count == 0
    assert result.coverage.reference_count == 0
    assert result.coverage.budget.observed_output_documents == 0
    assert result.coverage.budget.observed_search_result_records == 0
    assert result.coverage.source_calls[0].matched_count == 0
    assert result.coverage.source_calls[0].returned_count == 0


@pytest.mark.unit
def test_l5b_requires_exact_runtime_1_1_context_and_budget() -> None:
    context, budget = request_context()
    scope = resolved_retrieval_scope()
    filter_text = canonical_scope_filter(
        canonical_entity_ids=context.filter_add_on.canonical_entity_ids,
        canonical_type_ids=context.filter_add_on.exact_type_ids,
        canonical_relationship_ids=context.filter_add_on.canonical_relationship_ids,
        access_policy_hash=context.acl_scope_hash,
        asserted_publication_hash=context.asserted_publication_hash,
    )

    assert context.identity.contract_version == "1.1.0"
    assert budget.identity.contract_version == "1.1.0"
    assert (
        context.query_budget_schema_hash
        == "2d744838296209d78da2e2c8b7df7ab5f030af400d45a3d04d62b7d763f92b52"
    )
    assert context.query_budget_hash == budget.budget_hash

    old_context, old_budget = request_context_v1_0()
    with pytest.raises(ValueError, match="RequestContext@1.1.0"):
        build_agentic_retrieve_payload(
            old_context,
            old_budget,
            scope,
            query_text="evidence",
            filter_add_on=filter_text,
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("changes", "dimension"),
    (
        ({"max_ontology_graph_scope_requests": 0}, "max_ontology_graph_scope_requests"),
        ({"max_graph_result_records": 1}, "max_graph_result_records"),
    ),
)
def test_graph_budget_observations_are_exact_and_exhaustion_is_typed(
    changes: dict[str, int],
    dimension: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology_scope = resolved_ontology_scope()
    retrieval_scope = resolved_retrieval_scope()
    context, budget = _request_with_budget_changes(**changes)
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args, **_kwargs: None,
    )

    result = interpret_retrieval_response(
        context,
        budget,
        ontology_scope,
        retrieval_scope,
        publication=_runtime_publication(context, []),
        response=_agentic_response(context, []),
        accounting=_retrieval_accounting(0),
    )

    assert isinstance(result.coverage, AgenticRetrievalCoverageReceiptV1_1)
    assert result.coverage.coverage_status == "abstain"
    assert result.coverage.budget.observed_ontology_graph_scope_requests == 1
    assert result.coverage.budget.observed_graph_result_records == len(
        ontology_scope.included_canonical_ids
    )
    assert result.coverage.budget.budget_exhausted_dimensions == (dimension,)
    assert sum(
        failure.reason_code == "retrieval_budget_exhausted"
        for failure in result.coverage.failures
    ) == 1


@pytest.mark.unit
def test_provider_agentic_source_and_subquery_overrun_emits_valid_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology_scope = resolved_ontology_scope()
    retrieval_scope = resolved_retrieval_scope()
    context, budget = _request_with_budget_changes(
        max_agentic_internal_subqueries=1,
        max_agentic_source_calls=1,
    )
    response = _agentic_response(context, [])
    second = dict(response["activity"][0])
    second["id"] = 2
    second["searchIndexArguments"] = {"search": "second"}
    response["activity"].append(second)
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args, **_kwargs: None,
    )

    result = interpret_retrieval_response(
        context,
        budget,
        ontology_scope,
        retrieval_scope,
        publication=_runtime_publication(context, []),
        response=response,
        accounting=_retrieval_accounting(0),
    )

    assert len(result.coverage.planned_subqueries) == 2
    assert len(result.coverage.source_calls) == 2
    assert result.coverage.budget.budget_exhausted_dimensions == (
        "max_agentic_internal_subqueries",
        "max_agentic_source_calls",
    )
    assert result.coverage.coverage_status == "abstain"


@pytest.mark.unit
def test_direct_vector_embedding_and_retry_overrun_is_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology_scope = resolved_ontology_scope()
    retrieval_scope = resolved_retrieval_scope()
    context, budget = _request_with_budget_changes(
        "direct_hybrid_prefilter",
        max_vector_search_requests=1,
        max_embedding_calls=1,
        max_embedding_items=1,
        max_retry_count=1,
        max_retry_wait_milliseconds=10,
    )
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args, **_kwargs: None,
    )

    result = interpret_retrieval_response(
        context,
        budget,
        ontology_scope,
        retrieval_scope,
        publication=_runtime_publication(context, []),
        response={"value": []},
        accounting=L5bRemoteAccounting(
            operation_refs=("direct:overrun",),
            request_bytes=100,
            response_bytes=100,
            retry_count=2,
            retry_wait_ms=20,
            latency_ms=25,
            vector_search_requests=2,
            embedding_calls=2,
            embedding_items=2,
        ),
    )

    assert result.coverage.budget.budget_exhausted_dimensions == (
        "max_embedding_calls",
        "max_embedding_items",
        "max_retry_count",
        "max_retry_wait_milliseconds",
        "max_vector_search_requests",
    )
    assert result.coverage.budget.observed_agentic_source_calls == 0
    assert result.coverage.budget.observed_direct_search_requests == 1
    assert result.coverage.coverage_status == "abstain"


@pytest.mark.unit
def test_exact_exhaustion_rejects_false_missing_and_duplicate_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology_scope = resolved_ontology_scope()
    retrieval_scope = resolved_retrieval_scope()
    context, budget = _request_with_budget_changes(max_graph_result_records=1)
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args, **_kwargs: None,
    )
    result = interpret_retrieval_response(
        context,
        budget,
        ontology_scope,
        retrieval_scope,
        publication=_runtime_publication(context, []),
        response=_agentic_response(context, []),
        accounting=_retrieval_accounting(0),
    )
    values = result.coverage.budget.model_dump(mode="python", round_trip=True)

    for dimensions in (
        (),
        ("max_graph_result_records", "max_output_documents"),
        ("max_graph_result_records", "max_graph_result_records"),
    ):
        values["budget_exhausted_dimensions"] = dimensions
        with pytest.raises(ValueError):
            CoverageBudgetObservationV1_1.model_validate(values)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("activity_count", "accounting_count"),
    ((100, 10), (10, 100)),
)
def test_agentic_candidate_accounting_mismatch_fails_closed(
    activity_count: int,
    accounting_count: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology_scope = resolved_ontology_scope()
    retrieval_scope = resolved_retrieval_scope()
    context, budget = request_context()
    response = _agentic_response(context, [])
    response["activity"][0]["count"] = activity_count
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(
        L5bPublicationError,
        match="activity and adapter candidate accounting disagree",
    ):
        interpret_retrieval_response(
            context,
            budget,
            ontology_scope,
            retrieval_scope,
            publication=_runtime_publication(context, []),
            response=response,
            accounting=L5bRemoteAccounting(
                operation_refs=("agentic:candidate-conflict",),
                request_bytes=100,
                response_bytes=100,
                retry_count=0,
                retry_wait_ms=0,
                latency_ms=25,
                candidate_count=accounting_count,
                output_tokens=0,
            ),
        )


@pytest.mark.unit
def test_agentic_multi_call_candidates_reconcile_to_exact_sum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology_scope = resolved_ontology_scope()
    retrieval_scope = resolved_retrieval_scope()
    context, budget = request_context()
    response = _agentic_response(context, [])
    response["activity"][0]["id"] = 1
    response["activity"][0]["count"] = 4
    response["activity"][0].pop("searchIndexArguments")
    second = dict(response["activity"][0])
    second["id"] = 2
    second["count"] = 6
    response["activity"].append(second)
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args, **_kwargs: None,
    )

    result = interpret_retrieval_response(
        context,
        budget,
        ontology_scope,
        retrieval_scope,
        publication=_runtime_publication(context, []),
        response=response,
        accounting=L5bRemoteAccounting(
            operation_refs=("agentic:aggregate",),
            request_bytes=100,
            response_bytes=100,
            retry_count=0,
            retry_wait_ms=0,
            latency_ms=25,
            candidate_count=10,
            output_tokens=0,
        ),
    )

    assert result.coverage.matched_document_count == 10
    assert result.coverage.budget.observed_search_candidate_records == 10
    assert tuple(call.matched_count for call in result.coverage.source_calls) == (4, 6)
    assert len({call.request_hash for call in result.coverage.source_calls}) == 2
    assert result.coverage.budget.budget_exhausted_dimensions == ()


@pytest.mark.unit
def test_official_preview_activity_and_reference_int32_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology_scope = resolved_ontology_scope()
    retrieval_scope = resolved_retrieval_scope()
    context, budget = request_context()
    document = _reference_document(
        context,
        retrieval_scope.canonical_member_ids[0],
        0,
    )
    response = _agentic_response(context, [document])
    assert response["activity"][0]["id"] == 1
    assert response["references"][0]["activitySource"] == 1
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args, **_kwargs: None,
    )

    result = interpret_retrieval_response(
        context,
        budget,
        ontology_scope,
        retrieval_scope,
        publication=_runtime_publication(context, [document]),
        response=response,
        accounting=_retrieval_accounting(1),
    )

    assert result.coverage.matched_document_count == 1
    assert result.coverage.returned_document_count == 1
    assert result.coverage.source_calls[0].matched_count == 1
    assert result.coverage.source_calls[0].returned_count == 1


@pytest.mark.unit
def test_two_idless_agentic_activities_cannot_be_aggregated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology_scope = resolved_ontology_scope()
    retrieval_scope = resolved_retrieval_scope()
    context, budget = request_context()
    response = _agentic_response(context, [])
    response["activity"] = [
        {
            "type": "searchIndex",
            "count": count,
            "searchIndexArguments": {"search": f"query-{index}"},
        }
        for index, count in enumerate((4, 6))
    ]
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(
        L5bPublicationError,
        match="provider Search activity identity is invalid",
    ):
        interpret_retrieval_response(
            context,
            budget,
            ontology_scope,
            retrieval_scope,
            publication=_runtime_publication(context, []),
            response=response,
            accounting=L5bRemoteAccounting(
                operation_refs=("agentic:idless-aggregate",),
                request_bytes=100,
                response_bytes=100,
                retry_count=0,
                retry_wait_ms=0,
                latency_ms=25,
                candidate_count=10,
                output_tokens=0,
            ),
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "activity_id",
    (
        None,
        "",
        "1",
        [],
        True,
        1.0,
        -(2 ** 31) - 1,
        2 ** 31,
        "control\nid",
        "x" * 257,
    ),
)
def test_agentic_activity_id_must_be_signed_int32(
    activity_id: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology_scope = resolved_ontology_scope()
    retrieval_scope = resolved_retrieval_scope()
    context, budget = request_context()
    response = _agentic_response(context, [])
    response["activity"][0]["id"] = activity_id
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(
        L5bPublicationError,
        match="provider Search activity identity is invalid",
    ):
        interpret_retrieval_response(
            context,
            budget,
            ontology_scope,
            retrieval_scope,
            publication=_runtime_publication(context, []),
            response=response,
            accounting=_retrieval_accounting(0),
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "activity_ids",
    (("1", "2"), (1, "2"), ("1", 2)),
)
def test_agentic_activity_string_spoof_or_mixed_type_fails_closed(
    activity_ids: tuple[object, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology_scope = resolved_ontology_scope()
    retrieval_scope = resolved_retrieval_scope()
    context, budget = request_context()
    response = _agentic_response(context, [])
    response["activity"] = [
        {
            "id": activity_id,
            "type": "searchIndex",
            "count": 0,
            "searchIndexArguments": {"search": f"query-{index}"},
        }
        for index, activity_id in enumerate(activity_ids)
    ]
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(
        L5bPublicationError,
        match="provider Search activity identity is invalid",
    ):
        interpret_retrieval_response(
            context,
            budget,
            ontology_scope,
            retrieval_scope,
            publication=_runtime_publication(context, []),
            response=response,
            accounting=_retrieval_accounting(0),
        )


@pytest.mark.unit
def test_agentic_activity_signed_int32_boundaries_are_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology_scope = resolved_ontology_scope()
    retrieval_scope = resolved_retrieval_scope()
    context, budget = request_context()
    response = _agentic_response(context, [])
    response["activity"] = [
        {
            "id": activity_id,
            "type": "searchIndex",
            "count": 0,
            "searchIndexArguments": {"search": f"query-{index}"},
        }
        for index, activity_id in enumerate((-(2 ** 31), (2 ** 31) - 1))
    ]
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args, **_kwargs: None,
    )

    result = interpret_retrieval_response(
        context,
        budget,
        ontology_scope,
        retrieval_scope,
        publication=_runtime_publication(context, []),
        response=response,
        accounting=_retrieval_accounting(0),
    )

    assert len(result.coverage.source_calls) == 2
    assert len({call.source_call_id for call in result.coverage.source_calls}) == 2


@pytest.mark.unit
@pytest.mark.parametrize(
    "activity_source",
    ("1", 1.0, True, None, -(2 ** 31) - 1, 2 ** 31),
)
def test_agentic_reference_activity_source_must_be_signed_int32(
    activity_source: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology_scope = resolved_ontology_scope()
    retrieval_scope = resolved_retrieval_scope()
    context, budget = request_context()
    document = _reference_document(
        context,
        retrieval_scope.canonical_member_ids[0],
        0,
    )
    response = _agentic_response(context, [document])
    response["references"][0]["activitySource"] = activity_source
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(
        L5bPublicationError,
        match="provider Search activity identity is invalid",
    ):
        interpret_retrieval_response(
            context,
            budget,
            ontology_scope,
            retrieval_scope,
            publication=_runtime_publication(context, [document]),
            response=response,
            accounting=_retrieval_accounting(1),
        )


@pytest.mark.unit
def test_agentic_activity_id_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology_scope = resolved_ontology_scope()
    retrieval_scope = resolved_retrieval_scope()
    context, budget = request_context()
    response = _agentic_response(context, [])
    response["activity"][0].pop("id")
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(
        L5bPublicationError,
        match="provider Search activity identity is invalid",
    ):
        interpret_retrieval_response(
            context,
            budget,
            ontology_scope,
            retrieval_scope,
            publication=_runtime_publication(context, []),
            response=response,
            accounting=_retrieval_accounting(0),
        )


@pytest.mark.unit
@pytest.mark.parametrize("fallback", (False, True))
def test_direct_candidate_accounting_reconciles_provider_count(
    fallback: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology_scope = resolved_ontology_scope()
    retrieval_scope = resolved_retrieval_scope()
    origin = origin_budget = None
    if fallback:
        origin, origin_budget = request_context()
    context, budget = request_context(
        "direct_hybrid_prefilter",
        fallback_for=origin,
    )
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args, **_kwargs: None,
    )
    exact_accounting = L5bRemoteAccounting(
        operation_refs=("direct:candidate-exact",),
        request_bytes=100,
        response_bytes=100,
        retry_count=0,
        retry_wait_ms=0,
        latency_ms=25,
        candidate_count=10,
    )
    result = interpret_retrieval_response(
        context,
        budget,
        ontology_scope,
        retrieval_scope,
        publication=_runtime_publication(context, []),
        response={"@odata.count": 10, "value": []},
        accounting=exact_accounting,
        originating_context=origin,
        originating_budget=origin_budget,
        fallback_reason_code="source_failure" if fallback else None,
    )

    assert result.coverage.matched_document_count == 10
    assert result.coverage.budget.observed_search_candidate_records == 10
    assert result.coverage.source_calls[0].matched_count == 10

    with pytest.raises(
        L5bPublicationError,
        match="direct provider and adapter candidate accounting disagree",
    ):
        interpret_retrieval_response(
            context,
            budget,
            ontology_scope,
            retrieval_scope,
            publication=_runtime_publication(context, []),
            response={"@odata.count": 100, "value": []},
            accounting=L5bRemoteAccounting(
                operation_refs=("direct:candidate-conflict",),
                request_bytes=100,
                response_bytes=100,
                retry_count=0,
                retry_wait_ms=0,
                latency_ms=25,
                candidate_count=10,
            ),
            originating_context=origin,
            originating_budget=origin_budget,
            fallback_reason_code="source_failure" if fallback else None,
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    "counts",
    ((-1,), ("10",), ((2 ** 31),), (((2 ** 31) - 1), 1)),
)
def test_agentic_candidate_activity_malformed_or_overflow_fails_closed(
    counts: tuple[object, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ontology_scope = resolved_ontology_scope()
    retrieval_scope = resolved_retrieval_scope()
    context, budget = request_context()
    response = _agentic_response(context, [])
    response["activity"] = []
    for index, count in enumerate(counts):
        response["activity"].append({
            "id": index + 1,
            "type": "searchIndex",
            "knowledgeSourceName": context.knowledge_source_id,
            "count": count,
            "searchIndexArguments": {"search": f"query-{index}"},
        })
    monkeypatch.setattr(
        "fabric_kg_builder.serving.evidence_retrieval."
        "require_l5b_publication_receipt",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(
        L5bPublicationError,
        match="activity candidate accounting is invalid",
    ):
        interpret_retrieval_response(
            context,
            budget,
            ontology_scope,
            retrieval_scope,
            publication=_runtime_publication(context, []),
            response=response,
            accounting=L5bRemoteAccounting(
                operation_refs=("agentic:malformed-count",),
                request_bytes=100,
                response_bytes=100,
                retry_count=0,
                retry_wait_ms=0,
                latency_ms=25,
                candidate_count=0,
                output_tokens=0,
            ),
        )
