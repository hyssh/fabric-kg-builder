from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import hmac
import json
from types import MappingProxyType
from types import SimpleNamespace
import threading
import time

import pytest
from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)

from fabric_kg_builder.agent import l6_integration as l6
from fabric_kg_builder.agent.l6_blob_authority import (
    AzureBlobL6GraphReceiptAuthority,
    L6BlobAuthorityError,
    L6BlobConflictError,
    L6GraphTransportRequest,
    L6BlobSigningError,
    L6OpaqueSigningMetadata,
)
from fabric_kg_builder.contracts.base import canonical_sha256
from tests.contract.test_c0_runtime_contracts import (
    resolved_ontology_scope,
    resolved_retrieval_scope,
    seal,
)
from tests.unit import test_l6_agent_integration as fixtures


def _http_error(status: int) -> HttpResponseError:
    error = HttpResponseError()
    error.status_code = status
    return error


@dataclass
class _StoredBlob:
    data: bytes
    etag: int = 1
    lease_id: str | None = None
    lease_expires: int = 0


class _Download:
    def __init__(self, stored: _StoredBlob):
        self._data = stored.data
        self.properties = SimpleNamespace(etag=f'"{stored.etag}"')

    def readall(self):
        return self._data


class _Lease:
    def __init__(self, backend, name, lease_id):
        self._backend = backend
        self._name = name
        self.id = lease_id

    def renew(self):
        with self._backend.lock:
            stored = self._backend.blobs[self._name]
            if (
                stored.lease_id != self.id
                or stored.lease_expires <= self._backend.now[0]
            ):
                raise _http_error(412)
            self._backend.renewals += 1
            stored.lease_expires = (
                self._backend.now[0] + self._backend.lease_seconds * 1000
            )

    def release(self):
        with self._backend.lock:
            stored = self._backend.blobs[self._name]
            if (
                stored.lease_id != self.id
                or stored.lease_expires <= self._backend.now[0]
            ):
                raise _http_error(412)
            stored.lease_id = None
            stored.lease_expires = 0


class _Blob:
    def __init__(self, backend, name):
        self.backend = backend
        self.name = name

    def upload_blob(
        self,
        data,
        *,
        overwrite=False,
        etag=None,
        match_condition=None,
        lease=None,
    ):
        del match_condition
        with self.backend.lock:
            stored = self.backend.blobs.get(self.name)
            if stored is None:
                self.backend.blobs[self.name] = _StoredBlob(bytes(data))
                return
            if not overwrite:
                raise ResourceExistsError()
            if self.backend.conflict_next_cas:
                self.backend.conflict_next_cas = False
                stored.etag += 1
            if etag != f'"{stored.etag}"':
                raise ResourceModifiedError()
            if (
                lease is None
                or stored.lease_id != lease.id
                or stored.lease_expires <= self.backend.now[0]
            ):
                raise _http_error(412)
            stored.data = bytes(data)
            stored.etag += 1

    def download_blob(self, *, lease=None):
        with self.backend.lock:
            stored = self.backend.blobs.get(self.name)
            if stored is None:
                raise ResourceNotFoundError()
            if (
                lease is not None
                and (
                    stored.lease_id != lease.id
                    or stored.lease_expires <= self.backend.now[0]
                )
            ):
                raise _http_error(412)
            return _Download(stored)

    def acquire_lease(self, *, lease_duration):
        with self.backend.lock:
            self.backend.lease_seconds = lease_duration
            stored = self.backend.blobs.get(self.name)
            if stored is None:
                raise ResourceNotFoundError()
            if (
                stored.lease_id is not None
                and stored.lease_expires > self.backend.now[0]
            ):
                raise _http_error(409)
            self.backend.next_lease += 1
            lease_id = f"lease-{self.backend.next_lease}"
            stored.lease_id = lease_id
            stored.lease_expires = (
                self.backend.now[0] + lease_duration * 1000
            )
            return _Lease(self.backend, self.name, lease_id)


class _Container:
    def __init__(self, backend):
        self.backend = backend

    def get_blob_client(self, name):
        return _Blob(self.backend, name)


class _BlobService:
    def __init__(self, now):
        self.now = now
        self.blobs = {}
        self.lock = threading.RLock()
        self.next_lease = 0
        self.lease_seconds = 15
        self.conflict_next_cas = False
        self.renewals = 0
        self.container = _Container(self)

    def get_container_client(self, name):
        assert name == "authority"
        return self.container


class _Signer:
    def __init__(self, key: bytes, version: int, now: int):
        self._key = key
        self.metadata = L6OpaqueSigningMetadata(
            authority_id="gxra-sha256:" + hashlib.sha256(key).hexdigest(),
            authority_version=version,
            algorithm="HMAC-SHA256",
            not_before_milliseconds=now - 1,
            not_after_milliseconds=now + 100_000,
        )

    def sign(self, payload):
        return hmac.new(self._key, payload, hashlib.sha256).hexdigest()

    def verify(self, payload, signature):
        return hmac.compare_digest(self.sign(payload), signature)


@dataclass(frozen=True)
class _SignerSnapshot:
    active: _Signer
    signers: object
    snapshot_version: int
    on_verify: object = None

    def active_signer(self, now_milliseconds):
        del now_milliseconds
        return self.active

    def verifier(self, authority_id, authority_version):
        signer = self.signers.get((authority_id, authority_version))
        if self.on_verify is not None:
            self.on_verify()
        return signer


class _SignerProvider:
    def __init__(self, signer):
        self.active = signer
        self.signers = {
            (signer.metadata.authority_id, signer.metadata.authority_version): signer
        }
        self.on_snapshot_verify = None

    def rotate(self, signer):
        self.active = signer
        self.signers[
            (signer.metadata.authority_id, signer.metadata.authority_version)
        ] = signer

    def snapshot(self):
        return _SignerSnapshot(
            active=self.active,
            signers=MappingProxyType(dict(self.signers)),
            snapshot_version=self.active.metadata.authority_version,
            on_verify=self.on_snapshot_verify,
        )


@pytest.fixture
def setup():
    now = [10_000]
    backend = _BlobService(now)
    provider = _SignerProvider(_Signer(b"first opaque key", 1, now[0]))
    authority = AzureBlobL6GraphReceiptAuthority(
        blob_service_client=backend,
        container_name="authority",
        signer_provider=provider,
        lease_seconds=15,
        clock_milliseconds=lambda: now[0],
        allow_test_legacy_callback=True,
    )
    ontology = resolved_ontology_scope()
    retrieval = resolved_retrieval_scope()
    query = fixtures._graph_query(ontology, run_seed="blob-authority")
    graph = fixtures._graph_result(ontology, query)
    _, _, budget, _, _ = fixtures._evidence()
    values = {
        "l6_run_id": query.l6_run_id,
        "graph_query": query,
        "ontology_scope": ontology,
        "retrieval_scope": retrieval,
        "budget": budget,
        "access": fixtures._access(),
        "authorities": fixtures._authorities(),
    }
    return now, backend, provider, authority, graph, values


def _issue(authority, graph, values):
    completed = authority.execute_graph_once(
        **values,
        execute=lambda: graph,
    )
    return authority.issue(
        graph_query=values["graph_query"],
        graph_result=completed,
        ontology_scope=values["ontology_scope"],
        retrieval_scope=values["retrieval_scope"],
        budget=values["budget"],
        access=values["access"],
        authorities=values["authorities"],
    )


def _budget_with_runtime(budget, milliseconds):
    values = budget.model_dump(
        mode="python", exclude={"budget_hash"}, round_trip=True
    )
    values["max_runtime_milliseconds"] = milliseconds
    return seal(type(budget), "budget_hash", values)


def _expectation(receipt):
    return l6.L6GraphReceiptExpectation(
        **{
            name: getattr(receipt, name)
            for name in l6.L6GraphReceiptExpectation.model_fields
        }
    )


def _evidence_capability(authority, graph, values):
    graph_receipt = _issue(authority, graph, values)
    evidence_result, context, budget, origin, origin_budget = fixtures._evidence()
    retrieval_claim = canonical_sha256(
        {"graph_receipt": graph_receipt.receipt_hash, "purpose": "evidence"}
    )
    authority.verify_and_consume(
        graph_receipt.graph_execution_receipt_id,
        graph_receipt.receipt_hash,
        l6._receipt_expectation(
            values["ontology_scope"],
            values["retrieval_scope"],
            context,
        ),
        retrieval_claim,
    )
    presentations = fixtures._stable_presentations(evidence_result)
    output_values = {
        "graph_execution_receipt_id": (
            graph_receipt.graph_execution_receipt_id
        ),
        "graph_execution_receipt_hash": graph_receipt.receipt_hash,
        "retrieval_claim_hash": retrieval_claim,
        "citations": evidence_result.citations,
        "presentations": presentations,
        "coverage_receipt": evidence_result.coverage,
    }
    output = l6.L6EvidenceToolOutput(
        **output_values,
        output_hash=canonical_sha256(output_values),
    )
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
        context=context,
        budget=budget,
        retrieval_scope=values["retrieval_scope"],
        originating_context=origin,
        originating_budget=origin_budget,
    )
    return graph_receipt, output, collection


class _CancellableTransport:
    connect_timeout_seconds = 0.02
    read_timeout_seconds = 0.02

    def __init__(self, graph, *, late_release=None):
        self.graph = graph
        self.late_release = late_release
        self.entered = threading.Event()
        self.cancelled = threading.Event()
        self.returned = threading.Event()
        self.calls = []

    def execute_graph(
        self,
        request,
        *,
        deadline_monotonic,
        remaining_timeout_seconds,
        cancellation,
    ):
        self.calls.append(
            (
                request,
                deadline_monotonic,
                remaining_timeout_seconds,
                cancellation,
            )
        )
        self.entered.set()
        assert cancellation.wait(2)
        self.cancelled.set()
        if self.late_release is not None:
            assert self.late_release.wait(2)
        self.returned.set()
        return self.graph


def _production_authority(backend, provider, transport):
    return AzureBlobL6GraphReceiptAuthority(
        blob_service_client=backend,
        container_name="authority",
        signer_provider=provider,
        lease_seconds=15,
        graph_transport=transport,
    )


@pytest.mark.unit
def test_production_construction_requires_cancellable_graph_transport(setup):
    _, backend, provider, _, _, _ = setup
    with pytest.raises(ValueError, match="requires a deadline-aware"):
        AzureBlobL6GraphReceiptAuthority(
            blob_service_client=backend,
            container_name="authority",
            signer_provider=provider,
            lease_seconds=15,
        )


@pytest.mark.unit
def test_production_rejects_uncooperative_callback_before_claim(setup):
    _, backend, provider, _, graph, values = setup
    authority = _production_authority(backend, provider, _CancellableTransport(graph))

    with pytest.raises(TypeError, match="deadline-aware cancellable transport"):
        authority.execute_graph_once(**values, execute=lambda: graph)

    assert backend.blobs == {}


@pytest.mark.unit
def test_production_construction_rejects_unbounded_transport(setup):
    _, backend, provider, _, _, _ = setup

    class UnboundedTransport:
        def execute_graph(self, request, **kwargs):
            del request, kwargs

    with pytest.raises(ValueError, match="finite positive connect/read timeouts"):
        _production_authority(backend, provider, UnboundedTransport())


@pytest.mark.unit
def test_deadline_cancels_transport_fails_run_and_ignores_late_result(setup):
    _, backend, provider, _, graph, values = setup
    late_release = threading.Event()
    transport = _CancellableTransport(graph, late_release=late_release)
    authority = _production_authority(backend, provider, transport)
    values = {
        **values,
        "budget": _budget_with_runtime(values["budget"], 60),
    }

    with pytest.raises(TimeoutError, match="sealed runtime budget"):
        authority.execute_graph_once(**values)

    assert transport.cancelled.wait(1)
    request, deadline, remaining, cancellation = transport.calls[0]
    assert isinstance(request, L6GraphTransportRequest)
    assert request == L6GraphTransportRequest(**values)
    assert 0 < remaining <= 0.06
    assert deadline > 0
    assert cancellation.is_set()
    run_blob = authority._run_blob(values["l6_run_id"])
    persisted = json.loads(run_blob.download_blob().readall())
    assert persisted["status"] == "failed"
    assert persisted.get("owner_hash") is None
    assert backend.blobs[run_blob.name].lease_id is None
    with pytest.raises(ValueError, match="exact completed run"):
        authority.issue(
            graph_query=values["graph_query"],
            graph_result=graph,
            ontology_scope=values["ontology_scope"],
            retrieval_scope=values["retrieval_scope"],
            budget=values["budget"],
            access=values["access"],
            authorities=values["authorities"],
        )

    late_release.set()
    assert transport.returned.wait(1)
    assert json.loads(run_blob.download_blob().readall())["status"] == "failed"


@pytest.mark.unit
def test_lease_renewal_stops_at_graph_deadline(setup):
    _, backend, provider, _, graph, values = setup
    transport = _CancellableTransport(graph)
    authority = _production_authority(backend, provider, transport)
    authority._lease_seconds = 0.03
    values = {
        **values,
        "budget": _budget_with_runtime(values["budget"], 75),
    }

    with pytest.raises(TimeoutError):
        authority.execute_graph_once(**values)

    assert backend.renewals > 0
    renewals_at_deadline = backend.renewals
    time.sleep(0.05)
    assert backend.renewals == renewals_at_deadline


@pytest.mark.unit
def test_deadline_failure_notifies_waiter_via_durable_terminal_state(setup):
    _, backend, provider, _, graph, values = setup
    transport = _CancellableTransport(graph)
    first = _production_authority(backend, provider, transport)
    second = _production_authority(backend, provider, transport)
    values = {
        **values,
        "budget": _budget_with_runtime(values["budget"], 100),
    }

    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(first.execute_graph_once, **values)
        assert transport.entered.wait(1)
        time.sleep(0.03)
        waiter = pool.submit(second.execute_graph_once, **values)
        with pytest.raises(TimeoutError):
            owner.result(timeout=1)
        with pytest.raises(ValueError, match="previously failed"):
            waiter.result(timeout=1)

    assert len(transport.calls) == 1


@pytest.mark.unit
def test_transport_error_is_safely_normalized_and_persisted(setup):
    _, backend, provider, _, _, values = setup

    class FailingTransport:
        connect_timeout_seconds = 0.01
        read_timeout_seconds = 0.01

        def execute_graph(self, request, **kwargs):
            del request, kwargs
            raise RuntimeError("secret endpoint and response body")

    authority = _production_authority(backend, provider, FailingTransport())
    with pytest.raises(L6BlobAuthorityError) as caught:
        authority.execute_graph_once(**values)

    assert str(caught.value) == "Graph transport execution failed"
    assert caught.value.__cause__ is None
    run = json.loads(
        authority._run_blob(values["l6_run_id"]).download_blob().readall()
    )
    assert run["status"] == "failed"


@pytest.mark.unit
def test_concurrent_authorities_claim_and_execute_once(setup):
    _, backend, provider, first, graph, values = setup
    second = AzureBlobL6GraphReceiptAuthority(
        blob_service_client=backend,
        container_name="authority",
        signer_provider=provider,
        lease_seconds=15,
        allow_test_legacy_callback=True,
    )
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def execute():
        calls.append(1)
        entered.set()
        assert release.wait(1)
        return graph

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(first.execute_graph_once, **values, execute=execute)
        assert entered.wait(1)
        two = pool.submit(second.execute_graph_once, **values, execute=execute)
        release.set()
        assert one.result() == graph
        assert two.result() == graph
    assert calls == [1]


@pytest.mark.unit
def test_compare_and_swap_conflict_is_sanitized(setup):
    _, backend, _, authority, _, _ = setup
    blob = authority._run_blob("l6r-sha256:" + "1" * 64)
    assert authority._create(blob, {"state": "initial"})
    lease = authority._acquire_lease(blob)
    document = authority._read(blob, lease=lease)
    backend.conflict_next_cas = True
    with pytest.raises(L6BlobConflictError, match="compare-and-swap"):
        authority._cas(
            blob,
            expected_etag=document.etag,
            value={"state": "changed"},
            lease=lease,
        )


@pytest.mark.unit
def test_expired_crashed_claim_is_recovered(setup):
    now, _, _, authority, graph, values = setup
    fingerprint = l6._graph_execution_fingerprint(
        graph_query=values["graph_query"],
        ontology_scope=values["ontology_scope"],
        retrieval_scope=values["retrieval_scope"],
        budget=values["budget"],
        access=values["access"],
        authorities=values["authorities"],
    )
    blob = authority._run_blob(values["l6_run_id"])
    authority._create(
        blob,
        {
            "schema_version": 1,
            "kind": "l6_graph_run",
            "l6_run_id": values["l6_run_id"],
            "execution_fingerprint": fingerprint,
            "status": "executing",
            "owner_hash": "a" * 64,
            "claim_expires_milliseconds": now[0] + 15_000,
        },
    )
    crashed_lease = authority._acquire_lease(blob)
    now[0] += 15_001
    calls = []
    result = authority.execute_graph_once(
        **values,
        execute=lambda: calls.append(1) or graph,
    )
    assert result == graph
    assert calls == [1]
    with pytest.raises(HttpResponseError):
        crashed_lease.release()


@pytest.mark.unit
def test_receipt_consume_is_atomic_and_one_time(setup):
    _, backend, provider, authority, graph, values = setup
    receipt = _issue(authority, graph, values)
    other = AzureBlobL6GraphReceiptAuthority(
        blob_service_client=backend,
        container_name="authority",
        signer_provider=provider,
        lease_seconds=15,
        allow_test_legacy_callback=True,
    )
    claim = "b" * 64
    consumed = authority.verify_and_consume(
        receipt.graph_execution_receipt_id,
        receipt.receipt_hash,
        _expectation(receipt),
        claim,
    )
    assert consumed == receipt
    with pytest.raises(ValueError, match="invalid or replayed"):
        other.verify_and_consume(
            receipt.graph_execution_receipt_id,
            receipt.receipt_hash,
            _expectation(receipt),
            claim,
        )


@pytest.mark.unit
def test_issue_and_verify_fail_closed_without_signer(setup):
    _, backend, provider, signed, graph, values = setup
    unsigned = AzureBlobL6GraphReceiptAuthority(
        blob_service_client=backend,
        container_name="authority",
        signer_provider=None,
        lease_seconds=15,
        allow_test_legacy_callback=True,
    )
    unsigned.execute_graph_once(**values, execute=lambda: graph)
    with pytest.raises(L6BlobSigningError, match="not configured"):
        unsigned.issue(
            graph_query=values["graph_query"],
            graph_result=graph,
            ontology_scope=values["ontology_scope"],
            retrieval_scope=values["retrieval_scope"],
            budget=values["budget"],
            access=values["access"],
            authorities=values["authorities"],
        )
    receipt = signed.issue(
        graph_query=values["graph_query"],
        graph_result=graph,
        ontology_scope=values["ontology_scope"],
        retrieval_scope=values["retrieval_scope"],
        budget=values["budget"],
        access=values["access"],
        authorities=values["authorities"],
    )
    assert provider.snapshot().verifier(
        receipt.authority_id, receipt.authority_version
    )
    with pytest.raises(L6BlobSigningError, match="not configured"):
        unsigned.verify_and_consume(
            receipt.graph_execution_receipt_id,
            receipt.receipt_hash,
            _expectation(receipt),
            "d" * 64,
        )


@pytest.mark.unit
def test_signer_rotation_keeps_old_receipts_verifiable(setup):
    now, _, provider, authority, graph, values = setup
    first = _issue(authority, graph, values)
    provider.rotate(_Signer(b"second opaque key", 2, now[0]))
    consumed = authority.verify_and_consume(
        first.graph_execution_receipt_id,
        first.receipt_hash,
        _expectation(first),
        "c" * 64,
    )
    assert consumed.authority_version == 1
    persisted = json.loads(
        authority._receipt_blob(first.graph_execution_receipt_id)
        .download_blob()
        .readall()
    )
    assert b"first opaque key" not in json.dumps(persisted).encode()
    assert persisted["receipt"]["authority_version"] == 1

    rotated_query = fixtures._graph_query(
        values["ontology_scope"], run_seed="blob-authority-rotated"
    )
    rotated_graph = fixtures._graph_result(
        values["ontology_scope"], rotated_query
    )
    rotated_values = {
        **values,
        "l6_run_id": rotated_query.l6_run_id,
        "graph_query": rotated_query,
    }
    second = _issue(authority, rotated_graph, rotated_values)
    assert second.authority_version == 2


@pytest.mark.unit
def test_evidence_issue_consume_and_replay(setup):
    now, _, provider, authority, graph, values = setup
    graph_receipt, output, collection = _evidence_capability(
        authority, graph, values
    )
    receipt = authority.issue_evidence(
        graph_receipt=graph_receipt,
        evidence_output=output,
        citation_collection=collection,
    )
    assert (
        authority.issue_evidence(
            graph_receipt=graph_receipt,
            evidence_output=output,
            citation_collection=collection,
        )
        == receipt
    )
    provider.rotate(_Signer(b"rotated evidence key", 2, now[0]))
    authority.verify_and_consume_evidence(receipt)
    with pytest.raises(ValueError, match="invalid or replayed"):
        authority.verify_and_consume_evidence(receipt)
    with pytest.raises(ValueError, match="evidence capability"):
        authority.issue_evidence(
            graph_receipt=graph_receipt,
            evidence_output=output,
            citation_collection=collection,
        )


@pytest.mark.unit
def test_evidence_issue_and_consume_are_atomic_across_authorities(setup):
    now, backend, provider, first, graph, values = setup
    graph_receipt, output, collection = _evidence_capability(
        first, graph, values
    )
    authorities = [
        first,
        *[
            AzureBlobL6GraphReceiptAuthority(
                blob_service_client=backend,
                container_name="authority",
                signer_provider=provider,
                lease_seconds=15,
                clock_milliseconds=lambda: now[0],
                allow_test_legacy_callback=True,
            )
            for _ in range(3)
        ],
    ]
    barrier = threading.Barrier(len(authorities))

    def issue(authority):
        barrier.wait()
        return authority.issue_evidence(
            graph_receipt=graph_receipt,
            evidence_output=output,
            citation_collection=collection,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        issued = list(pool.map(issue, authorities))
    assert len({item.receipt_hash for item in issued}) == 1

    consume_barrier = threading.Barrier(2)

    def consume(authority):
        consume_barrier.wait()
        try:
            authority.verify_and_consume_evidence(issued[0])
            return "consumed"
        except ValueError:
            return "replayed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(consume, authorities[:2]))
    assert sorted(outcomes) == ["consumed", "replayed"]


@pytest.mark.unit
def test_evidence_operations_use_one_immutable_signer_snapshot(setup):
    now, _, provider, authority, graph, values = setup
    graph_receipt, output, collection = _evidence_capability(
        authority, graph, values
    )
    second = _Signer(b"rotation during evidence issue", 2, now[0])
    issue_rotated = [False]

    def rotate_during_issue():
        if not issue_rotated[0]:
            issue_rotated[0] = True
            provider.rotate(second)

    provider.on_snapshot_verify = rotate_during_issue
    evidence_receipt = authority.issue_evidence(
        graph_receipt=graph_receipt,
        evidence_output=output,
        citation_collection=collection,
    )
    assert issue_rotated == [True]
    assert evidence_receipt.authority_version == 1
    assert evidence_receipt.keyring_snapshot_version == 1
    assert provider.active.metadata.authority_version == 2

    third = _Signer(b"rotation during evidence consume", 3, now[0])
    consume_rotated = [False]

    def rotate_and_drop_old_keys():
        if not consume_rotated[0]:
            consume_rotated[0] = True
            provider.active = third
            provider.signers = {
                (
                    third.metadata.authority_id,
                    third.metadata.authority_version,
                ): third
            }

    provider.on_snapshot_verify = rotate_and_drop_old_keys
    authority.verify_and_consume_evidence(evidence_receipt)
    assert consume_rotated == [True]
    assert provider.active.metadata.authority_version == 3
