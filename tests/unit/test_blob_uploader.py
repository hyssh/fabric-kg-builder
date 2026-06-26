"""Unit tests for fabric_kg_builder.deploy.blob_uploader.BlobUploader.

Tests:
- upload() returns a blob URL.
- upload() is idempotent: second call with same asset_id returns existing URL.
- Dedup: if blob already exists (get_blob_properties succeeds), no re-upload.
- When blob does not exist, upload_blob is called.
- conftest make_blob_uploader mock satisfies the same upload() interface.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call

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
    blob_client = MagicMock()
    blob_client.url = _BLOB_URL

    if blob_exists:
        blob_client.get_blob_properties.return_value = MagicMock()
    else:
        blob_client.get_blob_properties.side_effect = Exception("BlobNotFound")

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
