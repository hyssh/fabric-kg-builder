"""Tests for lineage/registry.py — LocalLandingStore, BlobWriteResult."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from fabric_kg_builder.lineage.registry import (
    AssetRegistry,
    BlobWriteResult,
    LocalLandingStore,
    _file_uri_to_path,
)


# ---------------------------------------------------------------------------
# BlobWriteResult
# ---------------------------------------------------------------------------


class TestBlobWriteResult:
    def test_basic_fields(self):
        from datetime import datetime, timezone
        r = BlobWriteResult(
            blob_uri="file:///tmp/test.pdf",
            blob_version_id="sha256:abc",
            landing_path="raw/asset-1/original/test.pdf",
            landing_timestamp=datetime.now(timezone.utc),
            idempotent_reuse=False,
        )
        assert r.blob_uri == "file:///tmp/test.pdf"
        assert r.idempotent_reuse is False

    def test_reuse_flag(self):
        from datetime import datetime, timezone
        r = BlobWriteResult(
            blob_uri="file:///tmp/test.pdf",
            blob_version_id=None,
            landing_path="raw/asset-1/original/test.pdf",
            landing_timestamp=datetime.now(timezone.utc),
            idempotent_reuse=True,
        )
        assert r.idempotent_reuse is True


# ---------------------------------------------------------------------------
# _file_uri_to_path
# ---------------------------------------------------------------------------


class TestFileUriToPath:
    def test_converts_file_uri(self, tmp_path):
        f = tmp_path / "test.pdf"
        f.touch()
        uri = f.as_uri()
        result = _file_uri_to_path(uri)
        assert isinstance(result, Path)

    def test_raises_on_non_file_uri(self):
        with pytest.raises(ValueError, match="file URI"):
            _file_uri_to_path("https://example.com/doc.pdf")


# ---------------------------------------------------------------------------
# LocalLandingStore
# ---------------------------------------------------------------------------


def _content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TestLocalLandingStore:
    def test_upload_creates_file(self, tmp_path):
        store = LocalLandingStore(tmp_path / "landing")
        data = b"PDF content here"
        result = store.upload_original(
            asset_id="asset-001",
            asset_version_id="ver-001",
            data=data,
            original_name="document.pdf",
            media_type="application/pdf",
            metadata={"content_hash": _content_hash(data)},
            tags={},
        )
        assert result.blob_uri.startswith("file://")
        assert result.idempotent_reuse is False
        assert result.landing_path is not None

    def test_upload_idempotent_same_content(self, tmp_path):
        store = LocalLandingStore(tmp_path / "landing")
        data = b"same content"
        hash_val = _content_hash(data)
        meta = {"content_hash": hash_val}

        r1 = store.upload_original(
            asset_id="asset-001",
            asset_version_id="ver-001",
            data=data,
            original_name="doc.pdf",
            media_type="application/pdf",
            metadata=meta,
            tags={},
        )
        r2 = store.upload_original(
            asset_id="asset-001",
            asset_version_id="ver-001",
            data=data,
            original_name="doc.pdf",
            media_type="application/pdf",
            metadata=meta,
            tags={},
        )
        assert r1.blob_uri == r2.blob_uri
        assert r2.idempotent_reuse is True

    def test_upload_collision_different_content_raises(self, tmp_path):
        """When different bytes are detected for an existing file, FileExistsError is raised."""
        store = LocalLandingStore(tmp_path / "landing")
        data1 = b"first content"
        meta1 = {"content_hash": _content_hash(data1)}

        store.upload_original(
            asset_id="asset-001",
            asset_version_id="ver-001",
            data=data1,
            original_name="doc.pdf",
            media_type="application/pdf",
            metadata=meta1,
            tags={},
        )
        # Find the landed file and corrupt it
        landed_files = list((tmp_path / "landing").rglob("*.pdf"))
        assert len(landed_files) == 1
        # Corrupt the landed file content
        landed_files[0].write_bytes(b"corrupted content")
        # Now re-upload the original data — stored hash (corrupted) won't match meta hash
        with pytest.raises(FileExistsError):
            store.upload_original(
                asset_id="asset-001",
                asset_version_id="ver-001",
                data=data1,
                original_name="doc.pdf",
                media_type="application/pdf",
                metadata=meta1,
                tags={},
            )

    def test_root_dir_created(self, tmp_path):
        root = tmp_path / "new_landing"
        assert not root.exists()
        LocalLandingStore(root)
        assert root.exists()


# ---------------------------------------------------------------------------
# AssetRegistry
# ---------------------------------------------------------------------------


class TestAssetRegistry:
    def test_empty_store_on_missing_file(self, tmp_path):
        registry = AssetRegistry(tmp_path / "registry.json")
        store = registry.load()
        assert store["schema_version"] == "2.0"
        assert store["assets"] == []
        assert store["processing_runs"] == []

    def test_save_and_load_round_trip(self, tmp_path):
        registry = AssetRegistry(tmp_path / "registry.json")
        store = registry.load()
        store["assets"].append({"asset_id": "a-001", "name": "Test Asset"})
        registry.save(store)

        registry2 = AssetRegistry(tmp_path / "registry.json")
        loaded = registry2.load()
        assert len(loaded["assets"]) == 1
        assert loaded["assets"][0]["asset_id"] == "a-001"

    def test_start_run_returns_run_id(self, tmp_path):
        registry = AssetRegistry(tmp_path / "registry.json")
        row = registry.start_run(domain_hash="h-001")
        assert hasattr(row, "run_id")
        assert isinstance(row.run_id, str)
        assert len(row.run_id) > 0

    def test_start_run_stored(self, tmp_path):
        registry = AssetRegistry(tmp_path / "registry.json")
        row = registry.start_run(domain_hash="h-001")
        store = registry.load()
        run_ids = [r.get("run_id") if isinstance(r, dict) else r.run_id 
                   for r in store.get("processing_runs", [])]
        assert row.run_id in run_ids
