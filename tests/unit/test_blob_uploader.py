"""Unit tests for fabric_kg_builder.deploy.blob_uploader.BlobUploader.

Tests:
- upload() returns a blob URL.
- upload() is idempotent: second call with same asset_id returns existing URL.
- Dedup: if blob already exists (get_blob_properties succeeds), no re-upload.
- When blob does not exist, upload_blob is called.
- conftest make_blob_uploader mock satisfies the same upload() interface.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from fabric_kg_builder.config.schema import BlobStorageConfig
from fabric_kg_builder.deploy.blob_uploader import BlobUploader

# Import the conftest factory (available via pytest fixture discovery)
from tests.conftest import make_blob_uploader


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONFIG = BlobStorageConfig(
    account_name="fakeaccount",
    container="kg-assets",
    path_prefix="visual",
)

_BLOB_URL = "https://fakeaccount.blob.core.windows.net/kg-assets/visual/img123.png"

_IMAGE_DATA = b"fake_png_bytes"


def _make_blob_service_client(blob_exists: bool = False) -> MagicMock:
    """Build a fake BlobServiceClient mock."""
    from azure.core.exceptions import ResourceNotFoundError

    blob_client = MagicMock()
    blob_client.url = _BLOB_URL

    if blob_exists:
        blob_client.get_blob_properties.return_value = MagicMock()
    else:
        blob_client.get_blob_properties.side_effect = ResourceNotFoundError(
            "BlobNotFound"
        )

    service_client = MagicMock()
    service_client.get_blob_client.return_value = blob_client
    return service_client


# ---------------------------------------------------------------------------
# Tests: BlobUploader.upload
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_upload_returns_blob_url():
    svc = _make_blob_service_client(blob_exists=False)
    uploader = BlobUploader(_CONFIG, _blob_service_client=svc)

    url = uploader.upload("img123", _IMAGE_DATA, "png")

    assert url == _BLOB_URL


@pytest.mark.unit
def test_upload_calls_upload_blob_when_not_exists():
    svc = _make_blob_service_client(blob_exists=False)
    uploader = BlobUploader(_CONFIG, _blob_service_client=svc)

    uploader.upload("img123", _IMAGE_DATA, "png")

    blob_client = svc.get_blob_client.return_value
    blob_client.upload_blob.assert_called_once_with(_IMAGE_DATA, overwrite=False)


@pytest.mark.unit
def test_upload_dedup_skips_upload_when_blob_exists():
    """If the blob already exists, upload_blob must NOT be called."""
    svc = _make_blob_service_client(blob_exists=True)
    uploader = BlobUploader(_CONFIG, _blob_service_client=svc)

    url = uploader.upload("img123", _IMAGE_DATA, "png")

    blob_client = svc.get_blob_client.return_value
    blob_client.upload_blob.assert_not_called()
    assert url == _BLOB_URL


@pytest.mark.unit
def test_upload_reuses_blob_created_between_probe_and_upload():
    from azure.core.exceptions import ResourceExistsError

    svc = _make_blob_service_client(blob_exists=False)
    blob_client = svc.get_blob_client.return_value
    blob_client.upload_blob.side_effect = ResourceExistsError("BlobAlreadyExists")
    uploader = BlobUploader(_CONFIG, _blob_service_client=svc)

    url = uploader.upload("img123", _IMAGE_DATA, "png")

    assert url == _BLOB_URL
    blob_client.upload_blob.assert_called_once_with(_IMAGE_DATA, overwrite=False)


@pytest.mark.unit
def test_upload_surfaces_container_permission_failure():
    from azure.core.exceptions import HttpResponseError

    svc = _make_blob_service_client(blob_exists=False)
    svc.get_container_client.return_value.create_container.side_effect = (
        HttpResponseError("forbidden")
    )
    uploader = BlobUploader(_CONFIG, _blob_service_client=svc)

    with pytest.raises(HttpResponseError, match="forbidden"):
        uploader.upload("img123", _IMAGE_DATA, "png")


@pytest.mark.unit
def test_upload_uses_path_prefix_in_blob_name():
    svc = _make_blob_service_client(blob_exists=False)
    uploader = BlobUploader(_CONFIG, _blob_service_client=svc)

    uploader.upload("img456", _IMAGE_DATA, "jpg")

    svc.get_blob_client.assert_called_once_with(
        container="kg-assets",
        blob="visual/img456.jpg",
    )


@pytest.mark.unit
def test_upload_no_path_prefix():
    config_no_prefix = BlobStorageConfig(
        account_name="fakeaccount",
        container="kg-assets",
        path_prefix="",
    )
    svc = _make_blob_service_client(blob_exists=False)
    uploader = BlobUploader(config_no_prefix, _blob_service_client=svc)

    uploader.upload("imgabc", _IMAGE_DATA, "png")

    svc.get_blob_client.assert_called_once_with(
        container="kg-assets",
        blob="imgabc.png",
    )


@pytest.mark.unit
def test_upload_different_asset_ids_produce_different_blobs():
    svc = _make_blob_service_client(blob_exists=False)
    uploader = BlobUploader(_CONFIG, _blob_service_client=svc)

    uploader.upload("img_001", _IMAGE_DATA, "png")
    uploader.upload("img_002", _IMAGE_DATA, "png")

    calls = svc.get_blob_client.call_args_list
    blob_names = [c.kwargs["blob"] for c in calls]
    assert "visual/img_001.png" in blob_names
    assert "visual/img_002.png" in blob_names


@pytest.mark.unit
def test_upload_original_uses_immutable_version_path():
    from azure.core.exceptions import ResourceNotFoundError

    svc = MagicMock()
    blob_client = MagicMock()
    blob_client.url = "https://fakeaccount.blob.core.windows.net/kg-assets/raw/asset-1"
    blob_client.get_blob_properties.side_effect = ResourceNotFoundError("missing")
    blob_client.upload_blob.return_value = {"version_id": "blob-version-1"}
    svc.get_blob_client.return_value = blob_client
    uploader = BlobUploader(_CONFIG, _blob_service_client=svc)

    result = uploader.upload_original(
        asset_id="asset-1",
        asset_version_id="asset-version-1",
        data=b"original",
        original_name="report.pdf",
        media_type="application/pdf",
        metadata={"content_hash": "hash-1"},
        tags={"artifact_type": "original"},
    )

    assert result.landing_path == (
        "raw/asset-1/versions/asset-version-1/original/report.pdf"
    )
    assert result.blob_version_id == "blob-version-1"
    assert not result.idempotent_reuse
    svc.get_blob_client.assert_called_once_with(
        container="kg-assets",
        blob=(
            "visual/raw/asset-1/versions/asset-version-1/"
            "original/report.pdf"
        ),
    )
    upload_kwargs = blob_client.upload_blob.call_args.kwargs
    assert upload_kwargs["overwrite"] is False
    assert upload_kwargs["metadata"] == {"content_hash": "hash-1"}
    assert upload_kwargs["tags"] == {"artifact_type": "original"}
    assert upload_kwargs["content_settings"].content_type == "application/pdf"


@pytest.mark.unit
def test_upload_original_reuses_matching_immutable_blob():
    svc = MagicMock()
    blob_client = MagicMock()
    blob_client.url = "https://fake/original"
    blob_client.get_blob_properties.return_value = MagicMock(
        metadata={"content_hash": "hash-1"},
        version_id="blob-version-1",
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    svc.get_blob_client.return_value = blob_client
    uploader = BlobUploader(_CONFIG, _blob_service_client=svc)

    result = uploader.upload_original(
        asset_id="asset-1",
        asset_version_id="asset-version-1",
        data=b"original",
        original_name="report.pdf",
        media_type="application/pdf",
        metadata={"content_hash": "hash-1"},
        tags={},
    )

    assert result.idempotent_reuse
    blob_client.upload_blob.assert_not_called()


@pytest.mark.unit
def test_upload_original_retries_without_tags_when_hns_rejects_them():
    from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

    svc = MagicMock()
    blob_client = MagicMock()
    blob_client.url = "https://fake/original"
    blob_client.get_blob_properties.side_effect = ResourceNotFoundError("missing")
    tags_error = HttpResponseError("Blob Tags are unsupported")
    tags_error.error_code = (
        "FeatureNotYetSupportedForHierarchicalNamespaceAccounts"
    )
    blob_client.upload_blob.side_effect = [
        tags_error,
        {"version_id": "blob-version-1"},
    ]
    svc.get_blob_client.return_value = blob_client
    uploader = BlobUploader(_CONFIG, _blob_service_client=svc)

    result = uploader.upload_original(
        asset_id="asset-1",
        asset_version_id="asset-version-1",
        data=b"original",
        original_name="report.pdf",
        media_type="application/pdf",
        metadata={"content_hash": "hash-1"},
        tags={"artifact_type": "original"},
    )

    assert result.blob_version_id == "blob-version-1"
    assert blob_client.upload_blob.call_count == 2
    assert blob_client.upload_blob.call_args_list[0].kwargs["tags"] == {
        "artifact_type": "original"
    }
    assert "tags" not in blob_client.upload_blob.call_args_list[1].kwargs
    assert blob_client.upload_blob.call_args_list[1].kwargs["metadata"] == {
        "content_hash": "hash-1"
    }


@pytest.mark.unit
def test_upload_original_rejects_hash_collision():
    svc = MagicMock()
    blob_client = MagicMock()
    blob_client.url = "https://fake/original"
    blob_client.get_blob_properties.return_value = MagicMock(
        metadata={"content_hash": "different-hash"},
    )
    svc.get_blob_client.return_value = blob_client
    uploader = BlobUploader(_CONFIG, _blob_service_client=svc)

    with pytest.raises(FileExistsError, match="content hash does not match"):
        uploader.upload_original(
            asset_id="asset-1",
            asset_version_id="asset-version-1",
            data=b"original",
            original_name="report.pdf",
            media_type="application/pdf",
            metadata={"content_hash": "hash-1"},
            tags={},
        )


# ---------------------------------------------------------------------------
# Tests: conftest make_blob_uploader mock satisfies the interface
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_conftest_mock_blob_uploader_upload_returns_url():
    """The conftest make_blob_uploader mock must satisfy upload(asset_id, data, ext) -> str."""
    mock_uploader = make_blob_uploader()

    url = mock_uploader.upload("figure1", b"some bytes", "png")

    assert isinstance(url, str)
    assert "figure1" in url
    assert "png" in url


@pytest.mark.unit
def test_conftest_mock_blob_uploader_records_calls():
    mock_uploader = make_blob_uploader()

    mock_uploader.upload("fig1", b"bytes1", "png")
    mock_uploader.upload("fig2", b"bytes2", "jpg")

    assert mock_uploader.upload.call_count == 2


@pytest.mark.unit
def test_conftest_mock_blob_uploader_fixture(mock_blob_uploader):
    """Pytest fixture variant of the blob uploader mock."""
    url = mock_blob_uploader.upload("asset42", b"data", "png")
    assert "asset42" in url
