"""Signed durable ownership authority for redacted Foundry connections."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any

from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.core import MatchConditions

from fabric_kg_builder.agent.l6_blob_authority import L6OpaqueSignerProvider
from fabric_kg_builder.agent.l7_deployment import (
    L7ConnectionOwnershipReceipt,
    L7DeploymentError,
    L7OwnershipAuthorityObservation,
)
from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256


def _add_exception_note(exc: BaseException, note: str) -> None:
    add_note = getattr(exc, "add_note", None)
    if callable(add_note):
        add_note(note)


class AzureBlobL7ConnectionOwnershipAuthority:
    """Immutable one-receipt-per-connection authority backed by Azure Blob."""

    def __init__(
        self,
        *,
        blob_service_client: Any,
        container_name: str,
        signer_provider: L6OpaqueSignerProvider,
        expected_authority_id: str,
        prefix: str = "l7-ownership/v1",
        readiness_ttl_seconds: int = 60,
        clock: Any | None = None,
    ) -> None:
        if not container_name:
            raise ValueError("ownership authority container is required")
        if readiness_ttl_seconds < 15 or readiness_ttl_seconds > 300:
            raise ValueError("ownership readiness TTL must be between 15 and 300")
        self._container = blob_service_client.get_container_client(container_name)
        self._signers = signer_provider
        self._expected_authority_id = expected_authority_id
        self._prefix = prefix.strip("/")
        self._readiness_ttl = readiness_ttl_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @classmethod
    def from_account_url(
        cls,
        *,
        account_url: str,
        container_name: str,
        signer_provider: L6OpaqueSignerProvider,
        expected_authority_id: str,
        credential: Any | None = None,
        **kwargs: Any,
    ) -> "AzureBlobL7ConnectionOwnershipAuthority":
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient

        resolved_credential = credential or DefaultAzureCredential()
        return cls(
            blob_service_client=BlobServiceClient(
                account_url=account_url,
                credential=resolved_credential,
            ),
            container_name=container_name,
            signer_provider=signer_provider,
            expected_authority_id=expected_authority_id,
            **kwargs,
        )

    def _blob(self, connection_id: str) -> Any:
        digest = hashlib.sha256(connection_id.encode("utf-8")).hexdigest()
        return self._container.get_blob_client(
            f"{self._prefix}/connections/{digest}.json"
        )

    def observe(self) -> L7OwnershipAuthorityObservation:
        now = self._clock()
        try:
            snapshot = self._signers.snapshot()
            signer = snapshot.active_signer(int(now.timestamp() * 1000))
        except (TypeError, ValueError) as exc:
            raise L7DeploymentError(
                "ownership signing authority is unavailable"
            ) from exc
        if signer.metadata.authority_id != self._expected_authority_id:
            raise L7DeploymentError(
                "ownership signer differs from configured authority"
            )
        values = {
            "backend": "azure_blob",
            "authority_id": signer.metadata.authority_id,
            "snapshot_version": snapshot.snapshot_version,
            "checked_at": now,
            "expires_at": now + timedelta(seconds=self._readiness_ttl),
        }
        return L7OwnershipAuthorityObservation(
            **values,
            observation_hash=canonical_sha256(values),
        )

    def read_verified(
        self,
        *,
        connection_id: str,
    ) -> L7ConnectionOwnershipReceipt | None:
        blob = self._blob(connection_id)
        try:
            raw = blob.download_blob().readall()
        except ResourceNotFoundError:
            return None
        except (
            HttpResponseError,
            ServiceRequestError,
            ServiceResponseError,
            TimeoutError,
        ) as exc:
            raise L7DeploymentError(
                "connection ownership receipt read failed"
            ) from exc
        try:
            receipt = L7ConnectionOwnershipReceipt.model_validate_json(raw)
        except (TypeError, ValueError) as exc:
            raise L7DeploymentError(
                "connection ownership receipt is invalid"
            ) from exc
        if (
            receipt.connection_id.casefold() != connection_id.casefold()
            or receipt.authority_id != self._expected_authority_id
        ):
            raise L7DeploymentError(
                "connection ownership receipt authority mismatch"
            )
        try:
            snapshot = self._signers.snapshot()
            verifier = snapshot.verifier(
                receipt.authority_id,
                receipt.authority_version,
            )
            verified = verifier is not None and verifier.verify(
                canonical_json(receipt.signing_payload).encode("utf-8"),
                receipt.signature,
            )
        except (TypeError, ValueError) as exc:
            raise L7DeploymentError(
                "connection ownership verifier is unavailable"
            ) from exc
        if not verified:
            raise L7DeploymentError(
                "connection ownership receipt signature failed"
            )
        return receipt

    def issue_attempt_created(
        self,
        *,
        connection_id: str,
        connection_etag: str,
        workspace_id: str,
        data_agent_id: str,
    ) -> L7ConnectionOwnershipReceipt:
        if not connection_etag:
            raise L7DeploymentError(
                "attempt-created connection omitted an ETag"
            )
        now = self._clock()
        try:
            snapshot = self._signers.snapshot()
            signer = snapshot.active_signer(int(now.timestamp() * 1000))
        except (TypeError, ValueError) as exc:
            raise L7DeploymentError(
                "ownership signing authority is unavailable"
            ) from exc
        if signer.metadata.authority_id != self._expected_authority_id:
            raise L7DeploymentError(
                "ownership signer differs from configured authority"
            )
        payload = {
            "connection_id": connection_id,
            "connection_etag": connection_etag,
            "category": "CustomKeys",
            "target": "-",
            "audience": "",
            "workspace_id": workspace_id,
            "data_agent_id": data_agent_id,
            "authority_id": signer.metadata.authority_id,
            "authority_version": signer.metadata.authority_version,
            "issued_at": now,
        }
        try:
            signature = signer.sign(
                canonical_json(
                    {
                        **payload,
                        "issued_at": now.isoformat().replace("+00:00", "Z"),
                    }
                ).encode("utf-8")
            )
        except (TypeError, ValueError) as exc:
            raise L7DeploymentError(
                "connection ownership signing failed"
            ) from exc
        sealed = {**payload, "signature": signature}
        receipt = L7ConnectionOwnershipReceipt(
            **sealed,
            receipt_hash=canonical_sha256(
                {
                    **sealed,
                    "issued_at": now.isoformat().replace("+00:00", "Z"),
                }
            ),
        )
        try:
            self._blob(connection_id).upload_blob(
                (
                    canonical_json(receipt.model_dump(mode="json")) + "\n"
                ).encode("utf-8"),
                overwrite=False,
            )
        except ResourceExistsError as exc:
            raise L7DeploymentError(
                "connection ownership receipt collision; use a new connection name"
            ) from exc
        except (
            HttpResponseError,
            ServiceRequestError,
            ServiceResponseError,
            TimeoutError,
        ) as exc:
            try:
                uncertain = self.read_verified(connection_id=connection_id)
                if uncertain == receipt:
                    self.delete_attempt_created(
                        connection_id=connection_id,
                        connection_etag=connection_etag,
                    )
            except BaseException as rollback_exc:
                _add_exception_note(
                    exc,
                    "uncertain ownership receipt reconciliation failed: "
                    f"{type(rollback_exc).__name__}",
                )
            raise L7DeploymentError(
                "connection ownership receipt persistence outcome was reconciled"
            ) from exc
        try:
            readback = self.read_verified(connection_id=connection_id)
            if readback != receipt:
                raise L7DeploymentError(
                    "connection ownership receipt exact readback failed"
                )
        except BaseException as exc:
            try:
                self.delete_attempt_created(
                    connection_id=connection_id,
                    connection_etag=connection_etag,
                )
            except BaseException as rollback_exc:
                _add_exception_note(
                    exc,
                    "conditional ownership receipt rollback failed: "
                    f"{type(rollback_exc).__name__}",
                )
            if isinstance(
                exc,
                (KeyboardInterrupt, SystemExit, asyncio.CancelledError),
            ):
                raise
            raise L7DeploymentError(
                "connection ownership receipt readback failed and was rolled back"
            ) from exc
        return receipt

    def delete_attempt_created(
        self,
        *,
        connection_id: str,
        connection_etag: str,
    ) -> None:
        blob = self._blob(connection_id)
        try:
            download = blob.download_blob()
            raw = download.readall()
            blob_etag = str(download.properties.etag)
        except ResourceNotFoundError:
            return
        except HttpResponseError as exc:
            raise L7DeploymentError(
                "connection ownership rollback read failed"
            ) from exc
        try:
            receipt = L7ConnectionOwnershipReceipt.model_validate_json(raw)
        except (TypeError, ValueError) as exc:
            raise L7DeploymentError(
                "connection ownership rollback receipt is invalid"
            ) from exc
        if (
            receipt.connection_id.casefold() != connection_id.casefold()
            or receipt.connection_etag != connection_etag
        ):
            raise L7DeploymentError(
                "connection ownership rollback authority mismatch"
            )
        try:
            blob.delete_blob(
                etag=blob_etag,
                match_condition=MatchConditions.IfNotModified,
            )
        except HttpResponseError as exc:
            raise L7DeploymentError(
                "connection ownership conditional rollback failed"
            ) from exc
