"""Shared lineage v2 helpers."""

from __future__ import annotations

import json
import mimetypes
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fabric_kg_builder.model.schemas import DEFAULT_PROJECT_ID, SCHEMA_VERSION_V2

PROJECT_ID_ENV_VAR = "FABRIC_KG_PROJECT_ID"
PIPELINE_VERSION = "m2-lineage-v2"
DEFAULT_ENVIRONMENT = "dev"

TABLE_ID_FIELDS: dict[str, str] = {
    "source_files": "source_file_id",
    "document_elements": "document_element_id",
    "chunks": "chunk_id",
    "entities": "entity_id",
    "relationships": "relationship_id",
    "evidence": "evidence_id",
    "visual_assets": "image_id",
    "visual_regions": "visual_region_id",
    "assets": "asset_id",
    "asset_versions": "asset_version_id",
    "processing_runs": "run_id",
    "claims": "claim_id",
    "clusters": "cluster_id",
    "deployments": "deployment_id",
    # M5 SPEC-006 §7.3 drawing tables — schema owned by M5
    "drawing_elements": "drawing_element_id",
    "drawing_relationships": "relationship_id",
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def default_project_id() -> str:
    return os.environ.get(PROJECT_ID_ENV_VAR, DEFAULT_PROJECT_ID) or DEFAULT_PROJECT_ID


def normalize_source_uri(path_or_uri: str | Path) -> str:
    if isinstance(path_or_uri, Path):
        return path_or_uri.resolve().as_uri()
    raw = str(path_or_uri).strip()
    parsed = urlparse(raw)
    if parsed.scheme:
        return raw
    return Path(raw).resolve().as_uri()


def infer_media_type(path_or_name: str | Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path_or_name), strict=False)
    return guessed or "application/octet-stream"


def safe_original_name(name: str) -> str:
    value = Path(name).name.strip() or "asset.bin"
    value = re.sub(r"[^A-Za-z0-9._-]", "_", value)
    return value or "asset.bin"


def build_source_locator(
    *,
    blob_uri: str | None = None,
    blob_version_id: str | None = None,
    page: int | None = None,
    sheet: str | None = None,
    slide: int | None = None,
    section_path: str | list[str] | None = None,
    cell_range: str | None = None,
    char_start: int | None = None,
    char_end: int | None = None,
    polygon: Any = None,
    sheet_zone: str | None = None,
    tile_id: str | None = None,
    coordinate_system: str | None = None,
    transform: Any = None,
    native_layer_id: str | None = None,
    native_object_id: str | None = None,
    source_uri: str | None = None,
) -> dict[str, Any]:
    if isinstance(section_path, str):
        normalized_section_path = [part for part in section_path.split("/") if part]
    else:
        normalized_section_path = section_path
    return {
        "blob_uri": blob_uri,
        "blob_version_id": blob_version_id,
        "page": page,
        "sheet": sheet,
        "slide": slide,
        "section_path": normalized_section_path,
        "cell_range": cell_range,
        "char_start": char_start,
        "char_end": char_end,
        "polygon": polygon,
        "sheet_zone": sheet_zone,
        "tile_id": tile_id,
        "coordinate_system": coordinate_system,
        "transform": transform,
        "native_layer_id": native_layer_id,
        "native_object_id": native_object_id,
        "source_uri": source_uri,
    }


def source_locator_json(**kwargs: Any) -> str:
    return json.dumps(build_source_locator(**kwargs), ensure_ascii=False, sort_keys=True)


def apply_common_lineage(
    row: dict[str, Any],
    *,
    project_id: str,
    asset_id: str,
    asset_version_id: str,
    run_id: str,
    parent_record_id: str | None = None,
    locator_json: str | None = None,
    domain_hash: str | None = None,
    schema_version: str = SCHEMA_VERSION_V2,
) -> dict[str, Any]:
    enriched = dict(row)
    enriched["project_id"] = project_id
    enriched["asset_id"] = asset_id
    enriched["asset_version_id"] = asset_version_id
    enriched["run_id"] = run_id
    enriched["parent_record_id"] = parent_record_id
    enriched["source_locator_json"] = locator_json
    enriched["schema_version"] = schema_version
    enriched["domain_hash"] = domain_hash
    return enriched


def ensure_lineage_defaults(row: dict[str, Any], project_id: str | None = None) -> dict[str, Any]:
    enriched = dict(row)
    enriched.setdefault("project_id", project_id or DEFAULT_PROJECT_ID)
    enriched.setdefault("asset_id", "")
    enriched.setdefault("asset_version_id", "")
    enriched.setdefault("run_id", "")
    enriched.setdefault("parent_record_id", None)
    enriched.setdefault("source_locator_json", None)
    enriched.setdefault("schema_version", SCHEMA_VERSION_V2)
    enriched.setdefault("domain_hash", None)
    return enriched


def parse_json_text(value: str | None) -> Any:
    if not value:
        return None
    return json.loads(value)


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
