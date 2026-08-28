"""Durable Azure Blob authority for L6 Graph runs and receipts.

The adapter uses one blob per run and one blob per receipt.  All mutations are
protected by a finite blob lease and an ETag precondition; no container listing
is used.  Credentials and signing keys are deliberately outside the persisted
state and are supplied by callers.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import re
import secrets
import threading
import time
from typing import Any, Callable, Mapping, Protocol

from azure.core import MatchConditions
from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)

from fabric_kg_builder.agent import l6_integration as l6
from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class L6BlobAuthorityError(RuntimeError):
    """A sanitized durable-authority failure."""


class L6BlobConflictError(L6BlobAuthorityError):
    """The persisted object changed before a conditional mutation."""


class L6BlobSigningError(L6BlobAuthorityError):
    """Receipt signing or verification is unavailable or invalid."""


@dataclass(frozen=True)
class L6OpaqueSigningMetadata:
    """Non-secret key metadata safe to embed in a receipt."""

    authority_id: str
    authority_version: int
    algorithm: str
    not_before_milliseconds: int
    not_after_milliseconds: int
    state: str = "active"

    def validate(self, now_milliseconds: int) -> None:
        if (
            not re.fullmatch(r"gxra-sha256:[0-9a-f]{64}", self.authority_id)
            or self.authority_version < 1
            or self.algorithm != "HMAC-SHA256"
            or self.state != "active"
            or not self.not_before_milliseconds
            <= now_milliseconds
            <= self.not_after_milliseconds
        ):
            raise L6BlobSigningError("no valid L6 receipt signing key is available")


class L6OpaqueSigner(Protocol):
    """Opaque HSM/Key Vault compatible signer; key bytes are never exposed."""

    @property
    def metadata(self) -> L6OpaqueSigningMetadata: ...

    def sign(self, payload: bytes) -> str: ...

    def verify(self, payload: bytes, signature: str) -> bool: ...


class L6OpaqueSignerSnapshot(Protocol):
    """One immutable view of active and historical signing keys."""

    def active_signer(self, now_milliseconds: int) -> L6OpaqueSigner: ...

    def verifier(
        self, authority_id: str, authority_version: int
    ) -> L6OpaqueSigner | None: ...

    @property
    def snapshot_version(self) -> int: ...


class L6OpaqueSignerProvider(Protocol):
    """Atomically supplies immutable signer snapshots across key rotations."""

    def snapshot(self) -> L6OpaqueSignerSnapshot: ...


@dataclass(frozen=True)
class L6GraphTransportRequest:
    """The complete, immutable authority context for one Graph request."""

    l6_run_id: str
    graph_query: l6.L6GraphQuery
    ontology_scope: l6.ResolvedOntologyScope
    retrieval_scope: l6.ResolvedRetrievalScope
    budget: l6.QueryBudgetV1_1
    access: l6.L6AccessContext
    authorities: "l6.L6Authorities"


class L6DeadlineAwareGraphTransport(Protocol):
    """Cancellable Graph I/O with bounded connect and read operations.

    Implementations must use finite positive ``connect_timeout_seconds`` and
    ``read_timeout_seconds`` and clamp every blocking operation to the smaller
    of its configured timeout and the current time remaining before
    ``deadline_monotonic``. They must also abort promptly when ``cancellation``
    is set. The authority does not pretend that abandoning a worker thread
    cancels transport I/O; cancellation is an explicit transport capability.
    """

    connect_timeout_seconds: float
    read_timeout_seconds: float

    def execute_graph(
        self,
        request: L6GraphTransportRequest,
        *,
        deadline_monotonic: float,
        remaining_timeout_seconds: float,
        cancellation: threading.Event,
    ) -> l6.L6GraphResult: ...


@dataclass(frozen=True)
class _BlobDocument:
    value: dict[str, Any]
    etag: str


class AzureBlobL6GraphReceiptAuthority:
    """Multi-process L6 run and one-time Graph receipt authority."""

    uses_configured_transport = True

    def __init__(
        self,
        *,
        blob_service_client: Any,
        container_name: str,
        signer_provider: L6OpaqueSignerProvider | None,
        prefix: str = "l6-authority/v1",
        lease_seconds: int = 30,
        operation_timeout_seconds: float = 5.0,
        poll_interval_seconds: float = 0.05,
        clock_milliseconds: Callable[[], int] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        graph_transport: L6DeadlineAwareGraphTransport | None = None,
        allow_test_legacy_callback: bool = False,
    ) -> None:
        if not 15 <= lease_seconds <= 60:
            raise ValueError("lease_seconds must be between 15 and 60")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if operation_timeout_seconds <= 0:
            raise ValueError("operation_timeout_seconds must be positive")
        self._container = blob_service_client.get_container_client(container_name)
        self._signer_provider = signer_provider
        self._prefix = prefix.strip("/")
        self._lease_seconds = lease_seconds
        self._operation_timeout = operation_timeout_seconds
        self._poll_interval = poll_interval_seconds
        self._clock = clock_milliseconds or (lambda: int(time.time() * 1000))
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self._graph_transport = graph_transport
        self._allow_test_legacy_callback = allow_test_legacy_callback
        if graph_transport is None and not allow_test_legacy_callback:
            raise ValueError(
                "production L6 Blob authority requires a deadline-aware "
                "cancellable Graph transport"
            )
        if graph_transport is not None:
            for name in ("connect_timeout_seconds", "read_timeout_seconds"):
                value = getattr(graph_transport, name, None)
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value <= 0
                ):
                    raise ValueError(
                        "Graph transport requires finite positive connect/read timeouts"
                    )
            if not callable(getattr(graph_transport, "execute_graph", None)):
                raise TypeError("Graph transport is not deadline-aware and cancellable")

    @classmethod
    def from_account_url(
        cls,
        *,
        account_url: str,
        container_name: str,
        signer_provider: L6OpaqueSignerProvider | None,
        credential: Any | None = None,
        **kwargs: Any,
    ) -> "AzureBlobL6GraphReceiptAuthority":
        """Create an authority with an injected credential or Azure default auth."""

        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient

        resolved_credential = credential or DefaultAzureCredential()
        client = BlobServiceClient(
            account_url=account_url,
            credential=resolved_credential,
        )
        return cls(
            blob_service_client=client,
            container_name=container_name,
            signer_provider=signer_provider,
            **kwargs,
        )

    def _run_blob(self, l6_run_id: str) -> Any:
        digest = hashlib.sha256(l6_run_id.encode("utf-8")).hexdigest()
        return self._container.get_blob_client(f"{self._prefix}/runs/{digest}.json")

    def _receipt_blob(self, receipt_id: str) -> Any:
        digest = receipt_id.removeprefix("gxr-sha256:")
        if not _HASH_RE.fullmatch(digest):
            raise ValueError("Graph execution receipt ID must be opaque")
        return self._container.get_blob_client(
            f"{self._prefix}/receipts/{digest}.json"
        )

    def _evidence_blob(self, receipt_id: str) -> Any:
        digest = receipt_id.removeprefix("exr-sha256:")
        if not _HASH_RE.fullmatch(digest):
            raise ValueError("evidence execution receipt ID must be opaque")
        return self._container.get_blob_client(
            f"{self._prefix}/evidence/{digest}.json"
        )

    @staticmethod
    def _encode(value: Mapping[str, Any]) -> bytes:
        return canonical_json(value).encode("utf-8")

    @staticmethod
    def _decode(raw: bytes) -> dict[str, Any]:
        import json

        value = json.loads(raw)
        if not isinstance(value, dict):
            raise L6BlobAuthorityError("durable L6 authority state is malformed")
        return value

    @staticmethod
    def _model_from_json(model: Any, value: Any) -> Any:
        """Validate JSON-originated arrays as immutable model tuples."""

        return model.model_validate_json(canonical_json(value))

    def _read(self, blob: Any, *, lease: Any | None = None) -> _BlobDocument:
        try:
            download = blob.download_blob(lease=lease)
            raw = download.readall()
            etag = str(download.properties.etag)
        except ResourceNotFoundError as exc:
            raise L6BlobAuthorityError(
                "durable L6 authority state was not found"
            ) from exc
        except HttpResponseError as exc:
            raise L6BlobAuthorityError(
                "durable L6 authority state could not be read"
            ) from exc
        return _BlobDocument(self._decode(raw), etag)

    def _create(self, blob: Any, value: Mapping[str, Any]) -> bool:
        try:
            blob.upload_blob(self._encode(value), overwrite=False)
            return True
        except ResourceExistsError:
            return False
        except HttpResponseError as exc:
            raise L6BlobAuthorityError(
                "durable L6 authority state could not be created"
            ) from exc

    def _acquire_lease(self, blob: Any) -> Any | None:
        try:
            return blob.acquire_lease(lease_duration=self._lease_seconds)
        except HttpResponseError as exc:
            if exc.status_code in {409, 412}:
                return None
            raise L6BlobAuthorityError(
                "durable L6 authority lease could not be acquired"
            ) from exc

    def _cas(
        self,
        blob: Any,
        *,
        expected_etag: str,
        value: Mapping[str, Any],
        lease: Any,
    ) -> None:
        try:
            blob.upload_blob(
                self._encode(value),
                overwrite=True,
                etag=expected_etag,
                match_condition=MatchConditions.IfNotModified,
                lease=lease,
            )
        except (ResourceModifiedError, ResourceExistsError) as exc:
            raise L6BlobConflictError(
                "durable L6 authority compare-and-swap conflict"
            ) from exc
        except HttpResponseError as exc:
            if exc.status_code in {409, 412}:
                raise L6BlobConflictError(
                    "durable L6 authority compare-and-swap conflict"
                ) from exc
            raise L6BlobAuthorityError(
                "durable L6 authority state could not be updated"
            ) from exc

    @staticmethod
    def _release(lease: Any | None) -> None:
        if lease is None:
            return
        try:
            lease.release()
        except HttpResponseError:
            # An expired finite lease is already released server-side.
            return

    def _wait(self, deadline: float) -> None:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise TimeoutError(
                "L6 Graph execution wait exceeded sealed runtime budget"
            )
        self._sleep(min(self._poll_interval, remaining))

    def _acquire_required_lease(self, blob: Any, *, description: str) -> Any:
        deadline = self._monotonic() + self._operation_timeout
        while True:
            lease = self._acquire_lease(blob)
            if lease is not None:
                return lease
            if self._monotonic() >= deadline:
                raise L6BlobConflictError(f"{description} remained leased")
            self._sleep(
                min(self._poll_interval, deadline - self._monotonic())
            )

    def _claim_run(
        self,
        *,
        blob: Any,
        l6_run_id: str,
        execution_fingerprint: str,
        owner_hash: str,
        deadline: float,
    ) -> tuple[Any, _BlobDocument]:
        initial = {
            "schema_version": 1,
            "kind": "l6_graph_run",
            "l6_run_id": l6_run_id,
            "execution_fingerprint": execution_fingerprint,
            "status": "executing",
            "owner_hash": owner_hash,
            "claim_expires_milliseconds": (
                self._clock() + self._lease_seconds * 1000
            ),
        }
        self._create(blob, initial)
        while True:
            lease = self._acquire_lease(blob)
            if lease is None:
                self._wait(deadline)
                continue
            keep_lease = False
            try:
                document = self._read(blob, lease=lease)
                state = document.value
                if state.get("execution_fingerprint") != execution_fingerprint:
                    raise ValueError(
                        "L6 run already claimed by different Graph execution authority"
                    )
                if state.get("status") == "completed":
                    keep_lease = True
                    return lease, document
                if state.get("status") == "failed":
                    raise ValueError("L6 run Graph execution previously failed")
                if state.get("status") != "executing":
                    raise L6BlobAuthorityError(
                        "durable L6 run has an invalid state"
                    )
                if (
                    state.get("owner_hash") == owner_hash
                    or int(state.get("claim_expires_milliseconds", 0))
                    <= self._clock()
                ):
                    claimed = {
                        **state,
                        "owner_hash": owner_hash,
                        "claim_expires_milliseconds": (
                            self._clock() + self._lease_seconds * 1000
                        ),
                    }
                    self._cas(
                        blob,
                        expected_etag=document.etag,
                        value=claimed,
                        lease=lease,
                    )
                    claimed_document = self._read(blob, lease=lease)
                    keep_lease = True
                    return lease, claimed_document
            finally:
                if not keep_lease:
                    self._release(lease)
            self._wait(deadline)

    def _fail_owned_run(
        self,
        *,
        blob: Any,
        lease: Any,
        owner_hash: str,
        l6_run_id: str,
        fingerprint: str,
    ) -> None:
        latest = self._read(blob, lease=lease)
        if (
            latest.value.get("owner_hash") != owner_hash
            or latest.value.get("status") != "executing"
        ):
            return
        failed = {
            **latest.value,
            "status": "failed",
            "failure_hash": canonical_sha256(
                {"run": l6_run_id, "fingerprint": fingerprint}
            ),
        }
        failed.pop("owner_hash", None)
        failed.pop("claim_expires_milliseconds", None)
        self._cas(
            blob,
            expected_etag=latest.etag,
            value=failed,
            lease=lease,
        )

    def execute_graph_once(
        self,
        *,
        l6_run_id: str,
        graph_query: l6.L6GraphQuery,
        ontology_scope: l6.ResolvedOntologyScope,
        retrieval_scope: l6.ResolvedRetrievalScope,
        budget: l6.QueryBudgetV1_1,
        access: l6.L6AccessContext,
        authorities: "l6.L6Authorities",
        execute: Callable[[], l6.L6GraphResult] | None = None,
    ) -> l6.L6GraphResult:
        """Execute through the configured cancellable transport.

        ``execute`` exists only for in-memory unit tests created with
        ``allow_test_legacy_callback=True``. Production authorities reject it;
        synchronous callbacks cannot provide cancellation or bounded I/O.
        """

        if execute is not None and not self._allow_test_legacy_callback:
            raise TypeError(
                "production Graph execution requires a deadline-aware "
                "cancellable transport"
            )
        if execute is None and self._graph_transport is None:
            raise TypeError(
                "Graph execution requires a deadline-aware cancellable transport"
            )
        if l6_run_id != graph_query.l6_run_id:
            raise ValueError("Graph execution run identity mismatch")
        l6._validate_graph_query(
            graph_query, ontology_scope, retrieval_scope, budget
        )
        fingerprint = l6._graph_execution_fingerprint(
            graph_query=graph_query,
            ontology_scope=ontology_scope,
            retrieval_scope=retrieval_scope,
            budget=budget,
            access=access,
            authorities=authorities,
        )
        deadline = (
            self._monotonic() + budget.max_runtime_milliseconds / 1000
        )
        owner_hash = canonical_sha256({"owner_nonce": secrets.token_hex(32)})
        blob = self._run_blob(l6_run_id)
        lease, document = self._claim_run(
            blob=blob,
            l6_run_id=l6_run_id,
            execution_fingerprint=fingerprint,
            owner_hash=owner_hash,
            deadline=deadline,
        )
        if document.value["status"] == "completed":
            self._release(lease)
            return self._model_from_json(
                l6.L6GraphResult,
                document.value["graph_result"]
            )

        stop_renewal = threading.Event()
        renewal_failed = threading.Event()
        cancellation = threading.Event()

        def renew() -> None:
            interval = self._lease_seconds / 3
            while True:
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    cancellation.set()
                    return
                if stop_renewal.wait(min(interval, remaining)):
                    return
                if self._monotonic() >= deadline:
                    cancellation.set()
                    return
                try:
                    lease.renew()
                except HttpResponseError:
                    renewal_failed.set()
                    cancellation.set()
                    return

        renewal = threading.Thread(target=renew, daemon=True)
        renewal.start()
        finished = threading.Event()
        outcome: dict[str, Any] = {}

        request = L6GraphTransportRequest(
            l6_run_id=l6_run_id,
            graph_query=graph_query,
            ontology_scope=ontology_scope,
            retrieval_scope=retrieval_scope,
            budget=budget,
            access=access,
            authorities=authorities,
        )

        def invoke() -> None:
            try:
                if execute is not None:
                    outcome["result"] = execute()
                else:
                    transport = self._graph_transport
                    assert transport is not None
                    remaining = max(0.0, deadline - self._monotonic())
                    outcome["result"] = transport.execute_graph(
                        request,
                        deadline_monotonic=deadline,
                        remaining_timeout_seconds=remaining,
                        cancellation=cancellation,
                    )
            except BaseException:
                # Transport details can contain endpoints, tokens, or response
                # bodies. Only a fixed public failure crosses this boundary.
                outcome["failed"] = True
            finally:
                finished.set()

        worker = threading.Thread(target=invoke, daemon=True)
        worker.start()
        try:
            deadline_expired = False
            while not finished.is_set():
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    deadline_expired = True
                    break
                finished.wait(remaining)

            # The deadline wins every race. A result published at or after it
            # is deliberately ignored and can never transition the run.
            if self._monotonic() >= deadline:
                deadline_expired = True
            if deadline_expired:
                cancellation.set()
                stop_renewal.set()
                if not renewal_failed.is_set():
                    self._fail_owned_run(
                        blob=blob,
                        lease=lease,
                        owner_hash=owner_hash,
                        l6_run_id=l6_run_id,
                        fingerprint=fingerprint,
                    )
                raise TimeoutError(
                    "L6 Graph execution exceeded sealed runtime budget"
                )
            if renewal_failed.is_set():
                raise L6BlobConflictError(
                    "durable L6 authority lease was lost during execution"
                )
            if outcome.get("failed"):
                cancellation.set()
                if not renewal_failed.is_set():
                    self._fail_owned_run(
                        blob=blob,
                        lease=lease,
                        owner_hash=owner_hash,
                        l6_run_id=l6_run_id,
                        fingerprint=fingerprint,
                    )
                raise L6BlobAuthorityError("Graph transport execution failed")
            result = outcome.get("result")
            try:
                l6._validate_graph_result(graph_query, ontology_scope, result)
            except Exception:
                cancellation.set()
                if not renewal_failed.is_set():
                    self._fail_owned_run(
                        blob=blob,
                        lease=lease,
                        owner_hash=owner_hash,
                        l6_run_id=l6_run_id,
                        fingerprint=fingerprint,
                    )
                raise ValueError("Graph transport returned an invalid result") from None
            if self._monotonic() >= deadline:
                cancellation.set()
                stop_renewal.set()
                if not renewal_failed.is_set():
                    self._fail_owned_run(
                        blob=blob,
                        lease=lease,
                        owner_hash=owner_hash,
                        l6_run_id=l6_run_id,
                        fingerprint=fingerprint,
                    )
                raise TimeoutError(
                    "L6 Graph execution exceeded sealed runtime budget"
                )
            latest = self._read(blob, lease=lease)
            if (
                latest.value.get("owner_hash") != owner_hash
                or latest.value.get("status") != "executing"
            ):
                raise L6BlobConflictError(
                    "durable L6 run ownership changed during execution"
                )
            completed = {
                **latest.value,
                "status": "completed",
                "graph_result_hash": result.response_hash,
                "graph_result": result.model_dump(mode="json"),
            }
            completed.pop("owner_hash", None)
            completed.pop("claim_expires_milliseconds", None)
            self._cas(
                blob,
                expected_etag=latest.etag,
                value=completed,
                lease=lease,
            )
            return result
        finally:
            cancellation.set()
            stop_renewal.set()
            renewal.join(timeout=1)
            self._release(lease)

    def _signer_snapshot(self) -> L6OpaqueSignerSnapshot:
        if self._signer_provider is None:
            raise L6BlobSigningError("L6 receipt signer is not configured")
        try:
            snapshot = self._signer_provider.snapshot()
        except HttpResponseError as exc:
            raise L6BlobSigningError(
                "L6 receipt signing key is unavailable"
            ) from exc
        if snapshot.snapshot_version < 1:
            raise L6BlobSigningError("L6 signer snapshot metadata is invalid")
        return snapshot

    @staticmethod
    def _signing_key(
        snapshot: L6OpaqueSignerSnapshot, now: int
    ) -> L6OpaqueSigner:
        try:
            signer = snapshot.active_signer(now)
        except HttpResponseError as exc:
            raise L6BlobSigningError(
                "L6 receipt signing key is unavailable"
            ) from exc
        signer.metadata.validate(now)
        return signer

    @staticmethod
    def _sign_payload(
        signer: L6OpaqueSigner, values: Mapping[str, Any]
    ) -> str:
        try:
            tag = signer.sign(canonical_json(values).encode("utf-8"))
        except HttpResponseError as exc:
            raise L6BlobSigningError("L6 receipt signing failed") from exc
        if not _HASH_RE.fullmatch(tag):
            raise L6BlobSigningError(
                "L6 receipt signer returned an invalid signature"
            )
        return tag

    def _verify_signature(
        self,
        receipt: l6.L6GraphExecutionReceipt | l6.L6EvidenceExecutionReceipt,
        *,
        snapshot: L6OpaqueSignerSnapshot,
        failure_message: str,
    ) -> None:
        try:
            verifier = snapshot.verifier(
                receipt.authority_id, receipt.authority_version
            )
        except HttpResponseError as exc:
            raise L6BlobSigningError(
                "L6 receipt verification key is unavailable"
            ) from exc
        if verifier is None:
            raise L6BlobSigningError("L6 receipt verification key is unavailable")
        metadata = verifier.metadata
        metadata.validate(self._clock())
        if (
            metadata.authority_id != receipt.authority_id
            or metadata.authority_version != receipt.authority_version
            or metadata.algorithm != receipt.authentication_algorithm
        ):
            raise L6BlobSigningError(failure_message)
        payload = canonical_json(
            l6._graph_receipt_auth_payload(receipt.model_dump(mode="json"))
        ).encode("utf-8")
        try:
            verified = verifier.verify(payload, receipt.authentication_tag)
        except HttpResponseError as exc:
            raise L6BlobSigningError(
                "L6 receipt verification failed"
            ) from exc
        if not verified:
            raise L6BlobSigningError(failure_message)

    def issue(
        self,
        *,
        graph_query: l6.L6GraphQuery,
        graph_result: l6.L6GraphResult,
        ontology_scope: l6.ResolvedOntologyScope,
        retrieval_scope: l6.ResolvedRetrievalScope,
        budget: l6.QueryBudgetV1_1,
        access: l6.L6AccessContext,
        authorities: "l6.L6Authorities",
    ) -> l6.L6GraphExecutionReceipt:
        fingerprint = l6._graph_execution_fingerprint(
            graph_query=graph_query,
            ontology_scope=ontology_scope,
            retrieval_scope=retrieval_scope,
            budget=budget,
            access=access,
            authorities=authorities,
        )
        run_blob = self._run_blob(graph_query.l6_run_id)
        run_lease = self._acquire_required_lease(
            run_blob, description="completed L6 run"
        )
        try:
            run = self._read(run_blob, lease=run_lease).value
            if (
                run.get("status") != "completed"
                or run.get("execution_fingerprint") != fingerprint
                or run.get("graph_result_hash") != graph_result.response_hash
                or run.get("graph_result") != graph_result.model_dump(mode="json")
            ):
                raise ValueError(
                    "Graph receipt requires exact completed run authority"
                )
            now = self._clock()
            snapshot = self._signer_snapshot()
            signer = self._signing_key(snapshot, now)
            metadata = signer.metadata
            receipt_id = "gxr-sha256:" + canonical_sha256(
                {
                    "l6_run_id": graph_query.l6_run_id,
                    "execution_fingerprint": fingerprint,
                }
            )
            values = {
                "graph_execution_receipt_id": receipt_id,
                "authority_id": metadata.authority_id,
                "authority_version": metadata.authority_version,
                "authentication_algorithm": metadata.algorithm,
                "issued_at_milliseconds": now,
                "l6_run_id": graph_query.l6_run_id,
                "graph_execution_fingerprint": fingerprint,
                "graph_request_id": graph_query.graph_request_id,
                "graph_request_hash": graph_query.request_hash,
                "graph_result_hash": graph_result.response_hash,
                "resolved_ontology_scope_id": ontology_scope.resolved_ontology_scope_id,
                "resolved_ontology_scope_hash": ontology_scope.resolved_scope_hash,
                "resolved_retrieval_scope_id": retrieval_scope.resolved_retrieval_scope_id,
                "resolved_retrieval_scope_hash": retrieval_scope.retrieval_scope_hash,
                "canonical_scope_id": ontology_scope.canonical_scope_id,
                "graph_model_hash": ontology_scope.graph_model_hash,
                "search_index_fingerprint": ontology_scope.search_index_fingerprint,
                "asserted_publication_hash": ontology_scope.asserted_publication_hash,
                "publication_crosswalk_hash": ontology_scope.publication_crosswalk_hash,
                "acl_scope_hash": ontology_scope.acl_scope_hash,
                "returned_canonical_ids": graph_result.returned_canonical_ids,
                "returned_assertion_ids": tuple(
                    sorted(item.assertion_id for item in graph_result.assertions)
                ),
                "assertion_count": len(graph_result.assertions),
                "graph_complete": True,
                "accounting": graph_result.accounting,
                "execution_status": "succeeded",
            }
            authentication_tag = self._sign_payload(signer, values)
            sealed = {**values, "authentication_tag": authentication_tag}
            receipt = l6.L6GraphExecutionReceipt(
                **sealed,
                receipt_hash=canonical_sha256(sealed),
            )
            receipt_blob = self._receipt_blob(receipt_id)
            created = self._create(
                receipt_blob,
                {
                    "schema_version": 1,
                    "kind": "l6_graph_receipt",
                    "state": "issued",
                    "receipt": receipt.model_dump(mode="json"),
                },
            )
            if created:
                return receipt
            existing = self._read(receipt_blob).value
            prior = self._model_from_json(
                l6.L6GraphExecutionReceipt,
                existing.get("receipt")
            )
            if (
                existing.get("state") != "issued"
                or prior.graph_execution_fingerprint != fingerprint
                or prior.graph_result_hash != graph_result.response_hash
            ):
                raise ValueError(
                    "Graph run already has a different or consumed receipt"
                )
            return prior
        finally:
            self._release(run_lease)

    def verify_and_consume(
        self,
        receipt_id: str,
        receipt_hash: str,
        expectation: l6.L6GraphReceiptExpectation,
        retrieval_claim_hash: str,
    ) -> l6.L6GraphExecutionReceipt:
        if not _HASH_RE.fullmatch(retrieval_claim_hash):
            raise ValueError("Graph execution receipt is invalid or replayed")
        snapshot = self._signer_snapshot()
        blob = self._receipt_blob(receipt_id)
        lease = self._acquire_required_lease(
            blob, description="Graph execution receipt"
        )
        try:
            document = self._read(blob, lease=lease)
            receipt = self._model_from_json(
                l6.L6GraphExecutionReceipt,
                document.value.get("receipt")
            )
            if (
                document.value.get("state") != "issued"
                or receipt.receipt_hash != receipt_hash
                or not l6._receipt_matches_expectation(receipt, expectation)
            ):
                raise ValueError("Graph execution receipt is invalid or replayed")
            self._verify_signature(
                receipt,
                snapshot=snapshot,
                failure_message="Graph receipt authentication failed",
            )
            consumed = {
                **document.value,
                "state": "consumed_for_retrieval",
                "retrieval_claim_hash": retrieval_claim_hash,
            }
            self._cas(
                blob,
                expected_etag=document.etag,
                value=consumed,
                lease=lease,
            )
            return receipt
        finally:
            self._release(lease)

    def issue_evidence(
        self,
        *,
        graph_receipt: l6.L6GraphExecutionReceipt,
        evidence_output: l6.L6EvidenceToolOutput,
        citation_collection: l6.L6CitationPresentationCollection,
    ) -> l6.L6EvidenceExecutionReceipt:
        if (
            evidence_output.graph_execution_receipt_id
            != graph_receipt.graph_execution_receipt_id
            or evidence_output.graph_execution_receipt_hash
            != graph_receipt.receipt_hash
            or citation_collection.coverage_receipt_hash
            != evidence_output.coverage.coverage_receipt_hash
            or tuple(citation_collection.presentations)
            != tuple(evidence_output.presentations)
        ):
            raise ValueError("evidence chain differs from Graph authority")
        evidence_fingerprint = canonical_sha256(
            {
                "graph_receipt": graph_receipt.receipt_hash,
                "retrieval_claim": evidence_output.retrieval_claim_hash,
                "evidence_output": evidence_output.output_hash,
                "collection": citation_collection.collection_hash,
            }
        )
        snapshot = self._signer_snapshot()
        graph_blob = self._receipt_blob(
            graph_receipt.graph_execution_receipt_id
        )
        graph_lease = self._acquire_required_lease(
            graph_blob, description="Graph execution receipt"
        )
        try:
            graph_document = self._read(graph_blob, lease=graph_lease)
            persisted_graph = self._model_from_json(
                l6.L6GraphExecutionReceipt,
                graph_document.value.get("receipt"),
            )
            if (
                persisted_graph != graph_receipt
                or graph_document.value.get("retrieval_claim_hash")
                != evidence_output.retrieval_claim_hash
            ):
                raise ValueError(
                    "evidence requires exact consumed Graph retrieval claim"
                )
            graph_state = graph_document.value.get("state")
            if graph_state == "evidence_consumed":
                raise ValueError("Graph receipt already has an evidence capability")
            if graph_state == "evidence_receipt_issued":
                if (
                    graph_document.value.get("evidence_fingerprint")
                    != evidence_fingerprint
                ):
                    raise ValueError(
                        "Graph receipt already has an evidence capability"
                    )
                existing_blob = self._evidence_blob(
                    str(graph_document.value.get("evidence_receipt_id"))
                )
                existing = self._read(existing_blob).value
                if existing.get("state") != "issued":
                    raise ValueError(
                        "Graph receipt already has an evidence capability"
                    )
                prior = self._model_from_json(
                    l6.L6EvidenceExecutionReceipt,
                    existing.get("receipt"),
                )
                self._verify_signature(
                    prior,
                    snapshot=snapshot,
                    failure_message="evidence receipt authentication failed",
                )
                return prior
            if graph_state != "consumed_for_retrieval":
                raise ValueError(
                    "evidence requires consumed Graph retrieval authority"
                )
            self._verify_signature(
                graph_receipt,
                snapshot=snapshot,
                failure_message="Graph receipt authentication failed",
            )
            now = self._clock()
            signer = self._signing_key(snapshot, now)
            metadata = signer.metadata
            receipt_id = "exr-sha256:" + canonical_sha256(
                {
                    "graph_receipt_id": graph_receipt.graph_execution_receipt_id,
                    "evidence_fingerprint": evidence_fingerprint,
                }
            )
            snapshot_version = snapshot.snapshot_version
            if snapshot_version < 1:
                raise L6BlobSigningError(
                    "L6 signer snapshot metadata is invalid"
                )
            values = {
                "evidence_execution_receipt_id": receipt_id,
                "authority_id": metadata.authority_id,
                "authority_version": metadata.authority_version,
                "authentication_algorithm": metadata.algorithm,
                "issued_at_milliseconds": now,
                "l6_run_id": graph_receipt.l6_run_id,
                "graph_request_hash": graph_receipt.graph_request_hash,
                "keyring_snapshot_version": snapshot_version,
                "retrieval_claim_hash": evidence_output.retrieval_claim_hash,
                "graph_execution_receipt_id": (
                    graph_receipt.graph_execution_receipt_id
                ),
                "graph_execution_receipt_hash": graph_receipt.receipt_hash,
                "graph_authority_id": graph_receipt.authority_id,
                "evidence_output_hash": evidence_output.output_hash,
                "coverage_receipt_id": evidence_output.coverage.coverage_receipt_id,
                "coverage_receipt_hash": (
                    evidence_output.coverage.coverage_receipt_hash
                ),
                "citation_envelope_hashes": (
                    citation_collection.citation_envelope_hashes
                ),
                "source_response_hashes": (
                    citation_collection.source_response_hashes
                ),
                "search_index_fingerprint": (
                    citation_collection.search_index_fingerprint
                ),
                "asserted_publication_hash": (
                    citation_collection.asserted_publication_hash
                ),
                "required_canonical_id_set_hash": (
                    evidence_output.coverage.required_canonical_id_set_hash
                ),
                "citation_collection_hash": citation_collection.collection_hash,
            }
            tag = self._sign_payload(signer, values)
            sealed = {**values, "authentication_tag": tag}
            receipt = l6.L6EvidenceExecutionReceipt(
                **sealed,
                receipt_hash=canonical_sha256(sealed),
            )
            evidence_blob = self._evidence_blob(receipt_id)
            created = self._create(
                evidence_blob,
                {
                    "schema_version": 1,
                    "kind": "l6_evidence_receipt",
                    "state": "issued",
                    "evidence_fingerprint": evidence_fingerprint,
                    "receipt": receipt.model_dump(mode="json"),
                },
            )
            if not created:
                existing = self._read(evidence_blob).value
                prior = self._model_from_json(
                    l6.L6EvidenceExecutionReceipt,
                    existing.get("receipt"),
                )
                if (
                    existing.get("state") != "issued"
                    or existing.get("evidence_fingerprint")
                    != evidence_fingerprint
                ):
                    raise ValueError(
                        "Graph receipt already has an evidence capability"
                    )
                self._verify_signature(
                    prior,
                    snapshot=snapshot,
                    failure_message="evidence receipt authentication failed",
                )
                receipt = prior
            updated_graph = {
                **graph_document.value,
                "state": "evidence_receipt_issued",
                "evidence_fingerprint": evidence_fingerprint,
                "evidence_receipt_id": receipt.evidence_execution_receipt_id,
                "evidence_receipt_hash": receipt.receipt_hash,
            }
            self._cas(
                graph_blob,
                expected_etag=graph_document.etag,
                value=updated_graph,
                lease=graph_lease,
            )
            return receipt
        finally:
            self._release(graph_lease)

    def verify_and_consume_evidence(
        self,
        receipt: l6.L6EvidenceExecutionReceipt,
    ) -> None:
        snapshot = self._signer_snapshot()
        graph_blob = self._receipt_blob(
            receipt.graph_execution_receipt_id
        )
        graph_lease = self._acquire_required_lease(
            graph_blob, description="Graph execution receipt"
        )
        evidence_blob = self._evidence_blob(
            receipt.evidence_execution_receipt_id
        )
        try:
            graph_document = self._read(graph_blob, lease=graph_lease)
            if (
                graph_document.value.get("state")
                != "evidence_receipt_issued"
                or graph_document.value.get("evidence_receipt_id")
                != receipt.evidence_execution_receipt_id
                or graph_document.value.get("evidence_receipt_hash")
                != receipt.receipt_hash
            ):
                raise ValueError(
                    "evidence execution receipt is invalid or replayed"
                )
            evidence_document = self._read(evidence_blob)
            persisted = self._model_from_json(
                l6.L6EvidenceExecutionReceipt,
                evidence_document.value.get("receipt"),
            )
            if (
                evidence_document.value.get("state") != "issued"
                or persisted != receipt
            ):
                raise ValueError(
                    "evidence execution receipt is invalid or replayed"
                )
            graph_receipt = self._model_from_json(
                l6.L6GraphExecutionReceipt,
                graph_document.value.get("receipt"),
            )
            self._verify_signature(
                graph_receipt,
                snapshot=snapshot,
                failure_message="Graph receipt authentication failed",
            )
            self._verify_signature(
                receipt,
                snapshot=snapshot,
                failure_message="evidence receipt authentication failed",
            )
            consumed_graph = {
                **graph_document.value,
                "state": "evidence_consumed",
            }
            self._cas(
                graph_blob,
                expected_etag=graph_document.etag,
                value=consumed_graph,
                lease=graph_lease,
            )
        finally:
            self._release(graph_lease)


__all__ = [
    "AzureBlobL6GraphReceiptAuthority",
    "L6BlobAuthorityError",
    "L6BlobConflictError",
    "L6BlobSigningError",
    "L6OpaqueSigner",
    "L6OpaqueSignerProvider",
    "L6OpaqueSignerSnapshot",
    "L6OpaqueSigningMetadata",
]
