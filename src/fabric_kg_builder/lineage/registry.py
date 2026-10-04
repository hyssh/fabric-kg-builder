"""Asset registry, immutable landing, run manifests, and deployment helpers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from fabric_kg_builder.lineage.common import (
    DEFAULT_ENVIRONMENT,
    PIPELINE_VERSION,
    default_project_id,
    dump_json,
    infer_media_type,
    normalize_source_uri,
    now_utc,
    safe_original_name,
)
from fabric_kg_builder.model.ids import (
    make_asset_id,
    make_asset_version_id,
    make_asset_version_identity,
    make_deployment_id,
    make_run_id,
)
from fabric_kg_builder.model.schemas import (
    AssetRow,
    AssetVersionRow,
    DeploymentRow,
    ProcessingRunRow,
)


@dataclass(frozen=True)
class BlobWriteResult:
    blob_uri: str
    blob_version_id: str | None
    landing_path: str
    landing_timestamp: Any
    idempotent_reuse: bool


def _file_uri_to_path(source_uri: str) -> Path:
    """Convert a local file URI to a native path, including Windows drives."""
    parsed = urlparse(source_uri)
    if parsed.scheme != "file":
        raise ValueError(f"Expected a file URI, got {source_uri}")
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        uri_path = f"//{parsed.netloc}{parsed.path}"
    else:
        uri_path = parsed.path
    return Path(url2pathname(uri_path))


class LocalLandingStore:
    """Filesystem-backed landing zone used for offline tests and local fallback."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def upload_original(
        self,
        *,
        asset_id: str,
        asset_version_id: str,
        data: bytes,
        original_name: str,
        media_type: str,
        metadata: dict[str, str],
        tags: dict[str, str],
    ) -> BlobWriteResult:
        safe_name = safe_original_name(original_name)
        landing_path = f"raw/{asset_id}/versions/{asset_version_id}/original/{safe_name}"
        blob_path = self.root / landing_path.replace("/", "\\")
        blob_path.parent.mkdir(parents=True, exist_ok=True)
        if blob_path.exists():
            existing = hashlib.sha256(blob_path.read_bytes()).hexdigest()
            if existing != metadata["content_hash"]:
                raise FileExistsError(
                    f"Immutable landing collision at {blob_path}: existing bytes differ."
                )
            return BlobWriteResult(
                blob_uri=blob_path.resolve().as_uri(),
                blob_version_id=metadata.get("content_hash"),
                landing_path=landing_path,
                landing_timestamp=now_utc(),
                idempotent_reuse=True,
            )
        blob_path.write_bytes(data)
        meta_path = blob_path.with_suffix(blob_path.suffix + ".metadata.json")
        meta_path.write_text(
            json.dumps({"metadata": metadata, "tags": tags}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return BlobWriteResult(
            blob_uri=blob_path.resolve().as_uri(),
            blob_version_id=metadata.get("content_hash"),
            landing_path=landing_path,
            landing_timestamp=now_utc(),
            idempotent_reuse=False,
        )


class AssetRegistry:
    """Persistent local registry for assets, versions, runs, and deployments."""

    def __init__(
        self,
        store_path: str | Path,
        *,
        landing_root: str | Path | None = None,
        blob_uploader: Any | None = None,
        project_id: str | None = None,
        environment: str = DEFAULT_ENVIRONMENT,
        created_by: str = "local-user",
    ) -> None:
        self.store_path = Path(store_path)
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.project_id = project_id or default_project_id()
        self.environment = environment
        self.created_by = created_by
        self._blob_uploader = blob_uploader
        self._local_landing = LocalLandingStore(
            landing_root or self.store_path.parent / "landing"
        )

    def _empty_store(self) -> dict[str, Any]:
        return {
            "schema_version": "2.0",
            "assets": [],
            "asset_versions": [],
            "processing_runs": [],
            "deployments": [],
        }

    def load(self) -> dict[str, Any]:
        if not self.store_path.exists():
            return self._empty_store()
        return json.loads(self.store_path.read_text(encoding="utf-8"))

    def save(self, store: dict[str, Any]) -> None:
        self.store_path.write_text(json.dumps(store, indent=2, sort_keys=True, default=str), encoding="utf-8")

    def start_run(
        self,
        *,
        domain_hash: str | None = None,
        domain_schema_version: str | None = None,
        pipeline_version: str = PIPELINE_VERSION,
        adapter_versions: dict[str, str] | None = None,
        prompt_versions: dict[str, str] | None = None,
        model_deployments: dict[str, str] | None = None,
        chunk_strategy_version: str | None = None,
        parent_run_id: str | None = None,
        run_id: str | None = None,
    ) -> ProcessingRunRow:
        store = self.load()
        started_at = now_utc()
        row = ProcessingRunRow(
            run_id=run_id or make_run_id(),
            environment=self.environment,
            started_at=started_at,
            completed_at=None,
            status="running",
            domain_hash=domain_hash,
            domain_schema_version=domain_schema_version,
            pipeline_version=pipeline_version,
            adapter_versions_json=dump_json(adapter_versions or {}),
            prompt_versions_json=dump_json(prompt_versions or {}),
            model_deployments_json=dump_json(model_deployments or {}),
            chunk_strategy_version=chunk_strategy_version,
            parent_run_id=parent_run_id,
            stage_results_json=dump_json({}),
            manifest_path=str((self.store_path.parent / "runs" / f"{run_id or ''}.json").as_posix()) if run_id else None,
        )
        store["processing_runs"] = [
            existing for existing in store["processing_runs"] if existing["run_id"] != row.run_id
        ] + [row.model_dump()]
        self.save(store)
        return row

    def complete_run(
        self,
        run_id: str,
        *,
        status: str,
        stage_results: dict[str, Any] | None = None,
    ) -> ProcessingRunRow:
        store = self.load()
        run = None
        for entry in store["processing_runs"]:
            if entry["run_id"] == run_id:
                entry["status"] = status
                entry["completed_at"] = now_utc().isoformat()
                entry["stage_results_json"] = dump_json(stage_results or {})
                manifest_path = self.store_path.parent / "runs" / f"{run_id}.json"
                manifest_path.parent.mkdir(parents=True, exist_ok=True)
                redacted_manifest = {
                    "run_id": run_id,
                    "environment": entry["environment"],
                    "status": status,
                    "domain_hash": entry.get("domain_hash"),
                    "domain_schema_version": entry.get("domain_schema_version"),
                    "pipeline_version": entry.get("pipeline_version"),
                    "adapter_versions": json.loads(entry.get("adapter_versions_json") or "{}"),
                    "prompt_versions": json.loads(entry.get("prompt_versions_json") or "{}"),
                    "model_deployments": json.loads(entry.get("model_deployments_json") or "{}"),
                    "chunk_strategy_version": entry.get("chunk_strategy_version"),
                    "stage_results": stage_results or {},
                }
                manifest_path.write_text(json.dumps(redacted_manifest, indent=2, sort_keys=True), encoding="utf-8")
                entry["manifest_path"] = manifest_path.as_posix()
                run = ProcessingRunRow.model_validate(entry)
                break
        if run is None:
            raise KeyError(f"Unknown run_id: {run_id}")
        self.save(store)
        return run

    def _find_asset_by_source_uri(self, store: dict[str, Any], source_uri: str) -> dict[str, Any] | None:
        for asset in store["assets"]:
            if asset["source_uri"] == source_uri:
                return asset
        return None

    def _find_asset_version(
        self,
        store: dict[str, Any],
        asset_id: str,
        version_identity: str,
    ) -> dict[str, Any] | None:
        for version in store["asset_versions"]:
            if version["asset_id"] == asset_id and version["version_identity"] == version_identity:
                return version
        return None

    def _uploader(self):
        if self._blob_uploader is not None:
            return self._blob_uploader
        return self._local_landing

    def register_file(
        self,
        path: str | Path,
        *,
        run_id: str,
        classification: dict[str, Any] | None = None,
    ) -> tuple[AssetRow, AssetVersionRow, dict[str, Any]]:
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"Asset path not found: {file_path}")
        raw_bytes = file_path.read_bytes()
        content_hash_value = hashlib.sha256(raw_bytes).hexdigest()
        source_uri = normalize_source_uri(file_path)
        media_type = infer_media_type(file_path)
        original_name = file_path.name
        size_bytes = len(raw_bytes)
        registered_at = now_utc()

        store = self.load()
        asset_entry = self._find_asset_by_source_uri(store, source_uri)
        if asset_entry is None:
            asset_row = AssetRow(
                asset_id=make_asset_id(),
                project_id=self.project_id,
                original_name=original_name,
                media_type=media_type,
                source_uri=source_uri,
                classification_json=dump_json(classification or {}),
                created_at=registered_at,
                created_by=self.created_by,
            )
            store["assets"].append(asset_row.model_dump())
            asset_entry = asset_row.model_dump()
        else:
            asset_row = AssetRow.model_validate(asset_entry)

        version_identity = make_asset_version_identity(asset_row.asset_id, content_hash_value)
        version_entry = self._find_asset_version(store, asset_row.asset_id, version_identity)
        if version_entry is not None:
            return asset_row, AssetVersionRow.model_validate(version_entry), {"idempotent": True}

        safe_name = safe_original_name(original_name)
        metadata = {
            "asset_id": asset_row.asset_id,
            "content_hash": content_hash_value,
            "original_name": original_name,
            "media_type": media_type,
            "size_bytes": str(size_bytes),
            "run_id": run_id,
        }
        tags = {
            "project_id": self.project_id,
            "asset_id": asset_row.asset_id,
            "run_id": run_id,
            "artifact_type": "original",
            "environment": self.environment,
            "processing_status": "registered",
        }
        uploader = self._uploader()
        asset_version_id = make_asset_version_id()
        write_result = uploader.upload_original(
            asset_id=asset_row.asset_id,
            asset_version_id=asset_version_id,
            data=raw_bytes,
            original_name=safe_name,
            media_type=media_type,
            metadata=metadata,
            tags=tags,
        )
        version_row = AssetVersionRow(
            asset_version_id=asset_version_id,
            asset_id=asset_row.asset_id,
            version_identity=version_identity,
            content_hash=content_hash_value,
            size_bytes=size_bytes,
            original_name=original_name,
            media_type=media_type,
            source_uri=source_uri,
            blob_uri=write_result.blob_uri,
            blob_version_id=write_result.blob_version_id,
            landing_path=write_result.landing_path,
            metadata_json=dump_json(metadata),
            registered_at=registered_at,
            landing_timestamp=write_result.landing_timestamp,
            ingestion_status="registered",
        )
        store["asset_versions"].append(version_row.model_dump())
        self.save(store)
        return asset_row, version_row, {
            "idempotent": False,
            "landing_path": write_result.landing_path,
            "blob_uri": write_result.blob_uri,
            "tags": tags,
            "metadata": metadata,
        }

    def list_assets(self, *, asset_id: str | None = None) -> list[dict[str, Any]]:
        store = self.load()
        versions_by_asset: dict[str, list[dict[str, Any]]] = {}
        for version in store["asset_versions"]:
            versions_by_asset.setdefault(version["asset_id"], []).append(version)
        rows: list[dict[str, Any]] = []
        for asset in sorted(store["assets"], key=lambda item: (item["created_at"], item["asset_id"])):
            if asset_id and asset["asset_id"] != asset_id:
                continue
            versions = sorted(
                versions_by_asset.get(asset["asset_id"], []),
                key=lambda item: (item["registered_at"], item["asset_version_id"]),
            )
            latest = versions[-1] if versions else None
            rows.append({
                **asset,
                "version_count": len(versions),
                "latest_asset_version_id": latest["asset_version_id"] if latest else None,
                "latest_content_hash": latest["content_hash"] if latest else None,
                "latest_blob_uri": latest["blob_uri"] if latest else None,
                "latest_ingestion_status": latest["ingestion_status"] if latest else None,
            })
        return rows

    def retry_asset(self, asset_id: str, *, run_id: str) -> list[AssetVersionRow]:
        store = self.load()
        retried: list[AssetVersionRow] = []
        for version in store["asset_versions"]:
            if version["asset_id"] != asset_id or version["ingestion_status"] == "registered":
                continue
            source_uri = version["source_uri"]
            if not source_uri.startswith("file://"):
                raise ValueError(f"Retry only supports file:// source URIs, got {source_uri}")
            file_path = _file_uri_to_path(source_uri)
            raw_bytes = file_path.read_bytes()
            uploader = self._uploader()
            metadata = json.loads(version.get("metadata_json") or "{}")
            tags = {
                "project_id": self.project_id,
                "asset_id": asset_id,
                "run_id": run_id,
                "artifact_type": "original",
                "environment": self.environment,
                "processing_status": "registered",
            }
            write_result = uploader.upload_original(
                asset_id=asset_id,
                asset_version_id=version["asset_version_id"],
                data=raw_bytes,
                original_name=version["original_name"],
                media_type=version["media_type"],
                metadata=metadata,
                tags=tags,
            )
            version["blob_uri"] = write_result.blob_uri
            version["blob_version_id"] = write_result.blob_version_id
            version["landing_path"] = write_result.landing_path
            version["landing_timestamp"] = write_result.landing_timestamp.isoformat()
            version["ingestion_status"] = "registered"
            retried.append(AssetVersionRow.model_validate(version))
            self.save(store)
        return retried


def record_deployment(
    deployments: list[dict[str, Any]],
    *,
    run_id: str,
    environment: str,
    artifact_type: str,
    artifact_version: str | None,
    target_resource_id: str | None,
    target_name: str | None,
    target_record_locator: str | None,
    status: str,
    operation_id: str | None = None,
    error_code: str | None = None,
    record_ids: list[str] | None = None,
) -> DeploymentRow:
    started_at = now_utc()
    row = DeploymentRow(
        deployment_id=make_deployment_id(),
        run_id=run_id,
        environment=environment,
        artifact_type=artifact_type,
        artifact_version=artifact_version,
        target_resource_id=target_resource_id,
        target_name=target_name,
        target_record_locator=target_record_locator,
        started_at=started_at,
        completed_at=started_at,
        status=status,
        operation_id=operation_id,
        error_code=error_code,
        record_ids_json=dump_json(record_ids or []),
    )
    deployments.append(row.model_dump(mode="json"))
    return row
