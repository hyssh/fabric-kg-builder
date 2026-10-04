"""Migration helpers for canonical schema v1 -> v2 lineage contracts."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime
from typing import Any
from urllib.parse import unquote, urlparse

from fabric_kg_builder.lineage.common import (
    PIPELINE_VERSION,
    apply_common_lineage,
    build_source_locator,
    dump_json,
    now_utc,
    source_locator_json,
)
from fabric_kg_builder.model.ids import (
    content_hash,
    make_migrated_asset_id,
    make_migrated_asset_version_id,
    make_migrated_run_id,
)
from fabric_kg_builder.model.schemas import AssetRow, AssetVersionRow, ProcessingRunRow

_V2_TABLES = (
    "source_files",
    "document_elements",
    "chunks",
    "entities",
    "relationships",
    "evidence",
    "visual_assets",
    "visual_regions",
    "assets",
    "asset_versions",
    "processing_runs",
    "claims",
    "claim_evidence",
    "clusters",
    "cluster_memberships",
    "deployments",
)

_IMMUTABLE_LANDING_RE = re.compile(
    r"(?:^|/)raw/(?P<asset_id>[^/]+)/versions/"
    r"(?P<asset_version_id>[^/]+)/original(?:/|$)"
)


def _ensure_tables(table_rows: dict[str, list[dict]]) -> dict[str, list[dict]]:
    merged = {name: list(table_rows.get(name, [])) for name in _V2_TABLES}
    return merged


def _row_id(row: dict[str, Any], *fields: str) -> str:
    for field in fields:
        if row.get(field):
            return str(row[field])
    return "unknown"


def _registered_identity_from_blob_uri(
    blob_uri: str | None,
) -> tuple[str, str] | None:
    """Recover registry UUIDs from the immutable landing path in a Blob URI."""
    if not blob_uri:
        return None
    parsed = urlparse(blob_uri)
    path = unquote(parsed.path or blob_uri).replace("\\", "/")
    match = _IMMUTABLE_LANDING_RE.search(path)
    if not match:
        return None
    return match.group("asset_id"), match.group("asset_version_id")


def migrate_tables_to_v2(
    table_rows: dict[str, list[dict]],
    *,
    environment: str,
    project_id: str,
    domain_hash: str | None = None,
    domain_schema_version: str | None = None,
    pipeline_version: str = PIPELINE_VERSION,
) -> dict[str, list[dict]]:
    """Backfill lineage v2 tables and envelope fields without changing legacy IDs."""
    rows = _ensure_tables(deepcopy(table_rows))
    migrated_at = now_utc()

    source_files = rows["source_files"]
    if not source_files:
        synthetic_source_ids = sorted(
            {
                row.get("source_file_id")
                for table in ("document_elements", "chunks", "evidence", "visual_assets")
                for row in rows[table]
                if row.get("source_file_id")
            }
        )
        for source_file_id in synthetic_source_ids:
            source_files.append(
                {
                    "source_file_id": source_file_id,
                    "path": source_file_id,
                    "filename": source_file_id,
                    "source_type": "unknown",
                    "content_hash": content_hash(source_file_id),
                    "byte_size": None,
                    "ingested_at": migrated_at,
                    "schema_profile_path": None,
                    "row_count": None,
                    "notes": "migrated synthetic source_file",
                }
            )

    asset_rows: list[dict[str, Any]] = []
    asset_version_rows: list[dict[str, Any]] = []
    source_to_asset: dict[str, tuple[str, str, str | None, str | None]] = {}

    for source_file in source_files:
        source_file_id = source_file["source_file_id"]
        blob_uri = _extract_blob_uri(source_file)
        registered_identity = _registered_identity_from_blob_uri(blob_uri)
        registered_asset_id = registered_identity[0] if registered_identity else None
        registered_asset_version_id = (
            registered_identity[1] if registered_identity else None
        )
        migrated_asset_id = (
            source_file.get("asset_id")
            or registered_asset_id
            or make_migrated_asset_id(source_file_id)
        )
        migrated_asset_version_id = (
            source_file.get("asset_version_id")
            or registered_asset_version_id
            or make_migrated_asset_version_id(
                migrated_asset_id,
                source_file.get("content_hash") or content_hash(source_file_id),
            )
        )
        source_uri = source_file.get("path") or source_file_id
        asset_rows.append(
            AssetRow(
                asset_id=migrated_asset_id,
                project_id=project_id,
                original_name=source_file.get("filename") or source_file_id,
                media_type=source_file.get("source_type") or "unknown",
                source_uri=str(source_uri),
                classification_json=dump_json({"migration": "legacy-source-file"}),
                created_at=source_file.get("ingested_at") or migrated_at,
                created_by="migration:v1",
            ).model_dump(mode="json")
        )
        asset_version_rows.append(
            AssetVersionRow(
                asset_version_id=migrated_asset_version_id,
                asset_id=migrated_asset_id,
                version_identity=content_hash(f"migrated:{migrated_asset_id}:{source_file.get('content_hash') or source_file_id}"),
                content_hash=source_file.get("content_hash") or content_hash(source_file_id),
                size_bytes=int(source_file.get("byte_size") or 0),
                original_name=source_file.get("filename") or source_file_id,
                media_type=source_file.get("source_type") or "unknown",
                source_uri=str(source_uri),
                blob_uri=blob_uri or f"legacy://{source_file_id}",
                blob_version_id=None,
                landing_path=f"raw/{migrated_asset_id}/versions/{migrated_asset_version_id}/original/{source_file.get('filename') or 'unknown'}",
                metadata_json=dump_json({"migration": "legacy-source-file"}),
                registered_at=source_file.get("ingested_at") or migrated_at,
                landing_timestamp=source_file.get("ingested_at") or migrated_at,
                ingestion_status="migrated",
            ).model_dump(mode="json")
        )
        source_to_asset[source_file_id] = (migrated_asset_id, migrated_asset_version_id, blob_uri, str(source_uri))

    rows["assets"] = _dedupe_by_key(asset_rows, "asset_id")
    rows["asset_versions"] = _dedupe_by_key(asset_version_rows, "asset_version_id")

    run_rows = rows["processing_runs"]
    if run_rows:
        run_id = run_rows[0]["run_id"]
    else:
        scope = environment + ":" + ",".join(sorted(source_to_asset))
        run_id = make_migrated_run_id(scope)
        run_rows.append(
            ProcessingRunRow(
                run_id=run_id,
                environment=environment,
                started_at=migrated_at,
                completed_at=migrated_at,
                status="migrated",
                domain_hash=domain_hash,
                domain_schema_version=domain_schema_version,
                pipeline_version=f"{pipeline_version}:migration",
                adapter_versions_json=dump_json({}),
                prompt_versions_json=dump_json({}),
                model_deployments_json=dump_json({}),
                chunk_strategy_version=None,
                parent_run_id=None,
                stage_results_json=dump_json({"migration": "legacy-v1-to-v2"}),
                manifest_path=None,
            ).model_dump(mode="json")
        )
    rows["processing_runs"] = _dedupe_by_key(run_rows, "run_id")

    entity_rows_by_id = {row.get("entity_id"): row for row in rows["entities"] if row.get("entity_id")}
    evidence_rows_by_id = {row.get("evidence_id"): row for row in rows["evidence"] if row.get("evidence_id")}

    for source_file in rows["source_files"]:
        asset_id, asset_version_id, blob_uri, source_uri = source_to_asset[source_file["source_file_id"]]
        locator = source_locator_json(blob_uri=blob_uri, source_uri=source_uri)
        source_file.update(
            apply_common_lineage(
                source_file,
                project_id=project_id,
                asset_id=asset_id,
                asset_version_id=asset_version_id,
                run_id=run_id,
                locator_json=locator,
                domain_hash=domain_hash,
            )
        )

    for row in rows["document_elements"]:
        source_file_id = row.get("source_file_id")
        asset_id, asset_version_id, blob_uri, source_uri = _resolve_source_context(
            source_file_id,
            source_to_asset,
            fallback_token=_row_id(row, "document_element_id"),
        )
        locator = source_locator_json(
            blob_uri=row.get("blob_url") or blob_uri,
            page=row.get("page_number"),
            section_path=row.get("section_path"),
            source_uri=source_uri,
        )
        row.update(
            apply_common_lineage(
                row,
                project_id=project_id,
                asset_id=asset_id,
                asset_version_id=asset_version_id,
                run_id=run_id,
                parent_record_id=row.get("parent_element_id"),
                locator_json=locator,
                domain_hash=domain_hash,
            )
        )

    for row in rows["chunks"]:
        source_file_id = row.get("source_file_id")
        asset_id, asset_version_id, blob_uri, source_uri = _resolve_source_context(
            source_file_id,
            source_to_asset,
            fallback_token=_row_id(row, "chunk_id"),
        )
        locator = source_locator_json(
            blob_uri=row.get("blob_url") or blob_uri,
            page=row.get("page_number"),
            section_path=row.get("section_path"),
            source_uri=source_uri,
        )
        row.update(
            apply_common_lineage(
                row,
                project_id=project_id,
                asset_id=asset_id,
                asset_version_id=asset_version_id,
                run_id=run_id,
                parent_record_id=row.get("document_element_id"),
                locator_json=locator,
                domain_hash=domain_hash,
            )
        )

    for row in rows["evidence"]:
        source_file_id = row.get("source_file_id")
        asset_id, asset_version_id, blob_uri, source_uri = _resolve_source_context(
            source_file_id,
            source_to_asset,
            fallback_token=_row_id(row, "evidence_id"),
        )
        locator = source_locator_json(
            blob_uri=row.get("blob_url") or blob_uri,
            page=row.get("page_number"),
            section_path=row.get("section_path"),
            source_uri=source_uri,
        )
        parent_record_id = row.get("chunk_id") or row.get("document_element_id") or row.get("visual_region_id")
        row.update(
            apply_common_lineage(
                row,
                project_id=project_id,
                asset_id=asset_id,
                asset_version_id=asset_version_id,
                run_id=run_id,
                parent_record_id=parent_record_id,
                locator_json=locator,
                domain_hash=domain_hash,
            )
        )

    for row in rows["visual_assets"]:
        source_file_id = row.get("source_file_id")
        asset_id, asset_version_id, blob_uri, source_uri = _resolve_source_context(
            source_file_id,
            source_to_asset,
            fallback_token=_row_id(row, "image_id"),
        )
        locator = source_locator_json(
            blob_uri=row.get("blob_url") or blob_uri,
            page=row.get("page_number"),
            section_path=row.get("section_path"),
            source_uri=source_uri,
        )
        row.update(
            apply_common_lineage(
                row,
                project_id=project_id,
                asset_id=asset_id,
                asset_version_id=asset_version_id,
                run_id=run_id,
                parent_record_id=row.get("document_element_id"),
                locator_json=locator,
                domain_hash=domain_hash,
            )
        )

    visual_assets_by_id = {row.get("image_id"): row for row in rows["visual_assets"] if row.get("image_id")}

    for row in rows["visual_regions"]:
        visual_asset = visual_assets_by_id.get(row.get("image_id"))
        asset_id, asset_version_id, blob_uri, source_uri = _resolve_lineage_from_parent(
            visual_asset,
            fallback_token=_row_id(row, "visual_region_id"),
        )
        locator = source_locator_json(
            blob_uri=row.get("blob_url") or blob_uri,
            source_uri=source_uri,
            polygon=row.get("polygon_json"),
        )
        row.update(
            apply_common_lineage(
                row,
                project_id=project_id,
                asset_id=asset_id,
                asset_version_id=asset_version_id,
                run_id=run_id,
                parent_record_id=row.get("image_id"),
                locator_json=locator,
                domain_hash=domain_hash,
            )
        )

    for row in rows["entities"]:
        source_file_id = row.get("source_file_id")
        asset_id, asset_version_id, blob_uri, source_uri = _resolve_source_context(
            source_file_id,
            source_to_asset,
            fallback_token=_row_id(row, "entity_id"),
        )
        locator = source_locator_json(blob_uri=blob_uri, source_uri=source_uri)
        row.update(
            apply_common_lineage(
                row,
                project_id=project_id,
                asset_id=asset_id,
                asset_version_id=asset_version_id,
                run_id=run_id,
                parent_record_id=None,
                locator_json=locator,
                domain_hash=domain_hash,
            )
        )

    for row in rows["relationships"]:
        lineage_parent = evidence_rows_by_id.get(row.get("evidence_id")) or entity_rows_by_id.get(row.get("source_entity_id")) or entity_rows_by_id.get(row.get("target_entity_id"))
        asset_id, asset_version_id, blob_uri, source_uri = _resolve_lineage_from_parent(
            lineage_parent,
            fallback_token=_row_id(row, "relationship_id"),
        )
        locator = source_locator_json(blob_uri=blob_uri, source_uri=source_uri)
        row.update(
            apply_common_lineage(
                row,
                project_id=project_id,
                asset_id=asset_id,
                asset_version_id=asset_version_id,
                run_id=run_id,
                parent_record_id=row.get("evidence_id") or row.get("source_entity_id"),
                locator_json=locator,
                domain_hash=domain_hash,
            )
        )

    return rows


def _dedupe_by_key(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        unique[str(row[key])] = row
    return list(unique.values())


def _resolve_source_context(
    source_file_id: str | None,
    source_to_asset: dict[str, tuple[str, str, str | None, str | None]],
    *,
    fallback_token: str,
) -> tuple[str, str, str | None, str | None]:
    if source_file_id and source_file_id in source_to_asset:
        return source_to_asset[source_file_id]
    asset_id = make_migrated_asset_id(f"unknown:{fallback_token}")
    asset_version_id = make_migrated_asset_version_id(asset_id, content_hash(fallback_token))
    return asset_id, asset_version_id, None, None


def _resolve_lineage_from_parent(
    parent_row: dict[str, Any] | None,
    *,
    fallback_token: str,
) -> tuple[str, str, str | None, str | None]:
    if parent_row:
        return (
            parent_row.get("asset_id") or make_migrated_asset_id(f"unknown:{fallback_token}"),
            parent_row.get("asset_version_id") or make_migrated_asset_version_id(parent_row.get("asset_id") or make_migrated_asset_id(f"unknown:{fallback_token}"), parent_row.get("content_hash") or content_hash(fallback_token)),
            _extract_blob_uri(parent_row),
            _extract_source_uri(parent_row),
        )
    asset_id = make_migrated_asset_id(f"unknown:{fallback_token}")
    asset_version_id = make_migrated_asset_version_id(asset_id, content_hash(fallback_token))
    return asset_id, asset_version_id, None, None


def _extract_blob_uri(row: dict[str, Any]) -> str | None:
    if row.get("blob_url"):
        return row.get("blob_url")
    locator_raw = row.get("source_locator_json")
    if locator_raw:
        locator = json.loads(locator_raw)
        return locator.get("blob_uri")
    return None


def _extract_source_uri(row: dict[str, Any]) -> str | None:
    locator_raw = row.get("source_locator_json")
    if locator_raw:
        locator = json.loads(locator_raw)
        return locator.get("source_uri")
    return None
