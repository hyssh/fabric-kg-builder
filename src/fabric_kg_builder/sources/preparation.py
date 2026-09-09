"""Shared immutable asset indexes for pre-approval discovery and approved L2."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fabric_kg_builder.enrichment.schema2_sources import IndexedSourceCorpusReader
from fabric_kg_builder.model.schemas import AssetRow, AssetVersionRow
from fabric_kg_builder.sources.corpus import SourceCorpusManifest


def indexed_corpus_reader(
    corpus: SourceCorpusManifest, source_path: Path, *, project_id: str,
    layout_cache: Path | None = None, layout_identity: dict[str, Any] | None = None,
    adapter_versions: dict[str, str] | None = None,
) -> IndexedSourceCorpusReader:
    now = datetime.now(timezone.utc)
    assets, versions = [], []
    for entry in corpus.entries:
        if entry.disposition != "eligible":
            continue
        uri = f"https://fabric-kg.invalid/assets/{entry.asset_id}"
        assets.append(AssetRow(
            asset_id=entry.asset_id, project_id=project_id,
            original_name=Path(entry.relative_source_ref).name,
            media_type=entry.media_type, source_uri=uri,
            created_at=now, created_by="fabric-kg",
        ))
        versions.append(AssetVersionRow(
            asset_version_id=entry.asset_version_id, asset_id=entry.asset_id,
            version_identity=entry.original_byte_hash, content_hash=entry.original_byte_hash,
            size_bytes=entry.byte_count, original_name=Path(entry.relative_source_ref).name,
            media_type=entry.media_type, source_uri=uri,
            blob_uri=f"{uri}/versions/{entry.asset_version_id}",
            blob_version_id=entry.original_byte_hash,
            landing_path=entry.relative_source_ref, registered_at=now,
            landing_timestamp=now, ingestion_status="ready",
        ))
    source_path = Path(source_path)
    return IndexedSourceCorpusReader(
        source_root=source_path if source_path.is_dir() else source_path.parent,
        assets=tuple(assets), versions=tuple(versions),
        layout_cache=layout_cache.resolve() if layout_cache is not None else None,
        layout_identity=layout_identity, adapter_versions=adapter_versions,
    )
