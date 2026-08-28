from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from azure.core.exceptions import ServiceRequestError

from fabric_kg_builder.agent.l7_deployment import L7DeploymentError
from fabric_kg_builder.agent.l7_ownership import (
    AzureBlobL7ConnectionOwnershipAuthority,
)
from tests.unit.test_l6_blob_authority import (
    _Blob,
    _BlobService,
    _Signer,
    _SignerProvider,
    _http_error,
)


def _authority():
    now_ms = [1_800_000_000_000]
    backend = _BlobService(now_ms)
    signer = _Signer(b"opaque ownership key", 1, now_ms[0])
    provider = _SignerProvider(signer)
    authority = AzureBlobL7ConnectionOwnershipAuthority(
        blob_service_client=backend,
        container_name="authority",
        signer_provider=provider,
        expected_authority_id=signer.metadata.authority_id,
        clock=lambda: datetime.fromtimestamp(
            now_ms[0] / 1000,
            tz=timezone.utc,
        ),
    )
    return backend, signer, authority


def test_attempt_created_receipt_is_signed_persisted_and_exact():
    _, signer, authority = _authority()
    receipt = authority.issue_attempt_created(
        connection_id="/subscriptions/sub/connections/release-owned",
        connection_etag='"etag-1"',
        workspace_id="workspace",
        data_agent_id="data-agent",
    )
    assert receipt.authority_id == signer.metadata.authority_id
    assert authority.read_verified(
        connection_id=receipt.connection_id
    ) == receipt


def test_missing_receipt_is_not_adoptable():
    _, _, authority = _authority()
    assert (
        authority.read_verified(
            connection_id="/subscriptions/sub/connections/preexisting"
        )
        is None
    )


def test_forged_receipt_fails_closed():
    backend, _, authority = _authority()
    receipt = authority.issue_attempt_created(
        connection_id="/subscriptions/sub/connections/release-owned",
        connection_etag='"etag-1"',
        workspace_id="workspace",
        data_agent_id="data-agent",
    )
    blob = authority._blob(receipt.connection_id)
    stored = backend.blobs[blob.name]
    value = json.loads(stored.data)
    value["workspace_id"] = "attacker-workspace"
    stored.data = json.dumps(value).encode("utf-8")
    with pytest.raises(L7DeploymentError, match="invalid"):
        authority.read_verified(connection_id=receipt.connection_id)


def test_existing_receipt_collision_requires_new_connection_name():
    _, _, authority = _authority()
    values = {
        "connection_id": "/subscriptions/sub/connections/release-owned",
        "connection_etag": '"etag-1"',
        "workspace_id": "workspace",
        "data_agent_id": "data-agent",
    }
    authority.issue_attempt_created(**values)
    with pytest.raises(L7DeploymentError, match="collision"):
        authority.issue_attempt_created(**values)


def test_attempt_receipt_rollback_is_connection_etag_conditional(monkeypatch):
    backend, _, authority = _authority()
    receipt = authority.issue_attempt_created(
        connection_id="/subscriptions/sub/connections/release-owned",
        connection_etag='"etag-1"',
        workspace_id="workspace",
        data_agent_id="data-agent",
    )

    def delete_blob(self, *, etag, match_condition):
        del match_condition
        stored = self.backend.blobs[self.name]
        if etag != f'"{stored.etag}"':
            raise AssertionError("blob ETag mismatch")
        del self.backend.blobs[self.name]

    monkeypatch.setattr(_Blob, "delete_blob", delete_blob, raising=False)
    with pytest.raises(L7DeploymentError, match="authority mismatch"):
        authority.delete_attempt_created(
            connection_id=receipt.connection_id,
            connection_etag='"other-etag"',
        )
    authority.delete_attempt_created(
        connection_id=receipt.connection_id,
        connection_etag=receipt.connection_etag,
    )
    assert receipt.connection_id
    assert backend.blobs == {}


@pytest.mark.parametrize(
    "transport_error",
    [_http_error(503), ServiceRequestError("response lost after commit")],
)
def test_commit_then_blob_transport_error_reconciles_orphan(
    monkeypatch,
    transport_error,
):
    backend, _, authority = _authority()
    original_upload = _Blob.upload_blob

    def upload_then_fail(self, data, **kwargs):
        original_upload(self, data, **kwargs)
        raise transport_error

    def delete_blob(self, *, etag, match_condition):
        del match_condition
        stored = self.backend.blobs[self.name]
        assert etag == f'"{stored.etag}"'
        del self.backend.blobs[self.name]

    monkeypatch.setattr(_Blob, "upload_blob", upload_then_fail)
    monkeypatch.setattr(_Blob, "delete_blob", delete_blob, raising=False)
    with pytest.raises(L7DeploymentError, match="outcome was reconciled"):
        authority.issue_attempt_created(
            connection_id="/subscriptions/sub/connections/release-owned",
            connection_etag='"etag-1"',
            workspace_id="workspace",
            data_agent_id="data-agent",
        )
    assert backend.blobs == {}
