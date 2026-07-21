"""search.linkage — derive AI Search document fields from canonical Parquet rows.

Fenster module: consumes chunks / document_elements / entities Parquet tables
and produces flat dicts ready for JSON serialisation and AI Search upsert.

Contract (SPEC-002 §11):
  kg-chunks documents    — key=chunk_id
  kg-document-elements   — key=document_element_id

Entity-linkage denormalisation strategy (§11.3/§11.4):
  entity_ids      <- chunk.related_entity_ids          (filterable via search.in(), NOT searchable)
  entity_aliases  <- chunk.entity_search_keys          (searchable/BM25, NOT filterable)
  canonical_key   <- entities[primary entity].canonical_key
  entity_types    <- entities[each linked entity].entity_type
  graph_path      <- None at compile time (injected at push time by retrieval layer)

Filter-on-IDs / Search-on-aliases rule (SPEC-002 §11.4)
  entity_ids   → OData search.in() filter ONLY — never BM25 search text
  entity_aliases → BM25 keyword ONLY — never filterable

Canonical lineage + security fields (M6 SRV-002):
  project_id, asset_id, asset_version_id, run_id   — lineage envelope (filterable)
  source_file_id, document_element_id, chunk_id    — record IDs (filterable)
  source_locator_json, schema_version, domain_hash — source location + provenance (retrievable)
  sensitivity_label, acl_json                      — security placeholders (retrievable)

Public API
----------
build_entity_lookup(entities)        → {entity_id: row_dict}
build_search_aliases(canonical_key, display_name, aliases) → list[str]
build_entity_search_keys(related_entity_ids, entity_lookup) → list[str]
derive_chunk_search_docs(chunks, entities, *, graph_path)  → list[dict]
derive_chunk_doc(chunk, entities_by_id)                    → dict  (single-chunk)
derive_document_element_doc(element, entities_by_id)       → dict  (single-element)
derive_visual_docs(assets, regions, entities)              → list[dict]
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Entity lookup builder
# ---------------------------------------------------------------------------


def build_entity_lookup(entities: list[Any]) -> dict[str, dict[str, Any]]:
    """Build an entity_id -> entity-row dict from a list of entity rows.

    Accepts both plain dicts and Pydantic EntityRow objects.
    """
    lookup: dict[str, dict[str, Any]] = {}
    for ent in entities:
        row = ent if isinstance(ent, dict) else ent.model_dump()
        eid = row.get("entity_id")
        if eid:
            lookup[eid] = row
    return lookup


def _semantic_type_id(entity: dict[str, Any]) -> str | None:
    """Return the approved semantic type ID carried by an entity row."""
    value = entity.get("properties_json")
    if isinstance(value, dict):
        metadata = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
        metadata = parsed if isinstance(parsed, dict) else {}
    else:
        metadata = {}
    if metadata.get("semantic_lane") != "authoritative":
        return None
    semantic_id = metadata.get("semantic_type_id")
    return str(semantic_id) if semantic_id else None


# ---------------------------------------------------------------------------
# search_aliases builder — SPEC-002 §11.8
# ---------------------------------------------------------------------------


def build_search_aliases(
    canonical_key: str,
    display_name: str,
    aliases: list[str] | None,
) -> list[str]:
    """Derive search_aliases for one entity per SPEC-002 §11.8.

    Returns [canonical_key, display_name.lower(), alias.lower(), ...]
    deduplicated, preserving insertion order.
    """
    keys: list[str] = [canonical_key, display_name.lower()]
    for a in aliases or []:
        keys.append(a.lower())
    return list(dict.fromkeys(keys))


# ---------------------------------------------------------------------------
# entity_search_keys builder — SPEC-002 §11.8
# ---------------------------------------------------------------------------


def build_entity_search_keys(
    related_entity_ids: list[str] | None,
    entity_lookup: dict[str, dict[str, Any]],
) -> list[str]:
    """Flatten search_aliases for all entities in *related_entity_ids*.

    Entities absent from *entity_lookup* are silently skipped.
    Returns a deduplicated list preserving insertion order.
    """
    keys: list[str] = []
    for eid in related_entity_ids or []:
        ent = entity_lookup.get(eid)
        if ent:
            keys.extend(ent.get("search_aliases") or [])
    return list(dict.fromkeys(keys))


def build_entity_mention_index(
    entities: list[Any],
) -> dict[str, list[tuple[str, str]]]:
    """Build a deterministic exact-mention index for unlinked text rows."""
    index: dict[str, set[tuple[str, str]]] = {}
    for raw in entities:
        entity = raw if isinstance(raw, dict) else raw.model_dump()
        entity_id = str(entity.get("entity_id") or "")
        if not entity_id:
            continue
        candidates = [
            entity.get("display_name"),
            *(entity.get("aliases") or []),
            *(entity.get("search_aliases") or []),
        ]
        for candidate in candidates:
            normalized = _normalize_mention(candidate)
            if not _usable_mention(normalized):
                continue
            first_token = normalized.split(" ", 1)[0]
            index.setdefault(first_token, set()).add(
                (normalized, entity_id)
            )
    return {
        token: sorted(values, key=lambda item: (-len(item[0]), item))
        for token, values in sorted(index.items())
    }


def infer_entity_mentions(
    text: str | None,
    mention_index: dict[str, list[tuple[str, str]]],
) -> list[str]:
    """Return entity IDs whose aliases occur as whole normalized phrases."""
    normalized = _normalize_mention(text)
    if not normalized:
        return []
    padded = f" {normalized} "
    matches: dict[str, int] = {}
    for token in sorted(set(normalized.split())):
        for candidate, entity_id in mention_index.get(token, []):
            position = padded.find(f" {candidate} ")
            if position >= 0:
                matches[entity_id] = min(
                    position,
                    matches.get(entity_id, position),
                )
    return [
        entity_id
        for entity_id, _ in sorted(
            matches.items(),
            key=lambda item: (item[1], item[0]),
        )
    ]


# ---------------------------------------------------------------------------
# Batch chunk search-doc derivation — SPEC-002 §11.3
# ---------------------------------------------------------------------------


def derive_chunk_search_docs(
    chunks: list[Any],
    entities: list[Any],
    *,
    graph_path: str | None = None,
) -> list[dict[str, Any]]:
    """Derive one AI Search document dict per chunk (batch form).

    Per SPEC-002 §11.3, derives all entity-linkage fields from canonical
    Parquet rows.  No I/O; no live AI Search calls.

    Parameters
    ----------
    chunks:
        List of chunk row dicts or ChunkRow Pydantic objects.
    entities:
        List of entity row dicts or EntityRow Pydantic objects.
    graph_path:
        Optional GQL traversal path injected at push time.  Not stored in
        Parquet.  Pass None when not available.

    Returns
    -------
    list[dict]
        One AI Search document per chunk.  Each dict includes:
        chunk_id, content, embedding_text, entity_ids (filterable),
        canonical_key (filterable), entity_aliases (searchable/BM25 only),
        entity_types (filterable+facetable), graph_path (retrievable),
        blob_url, source_file_id, last_modified (ISO-8601), content_type,
        content_hash (for push-pipeline change detection).
    """
    entities_by_id = build_entity_lookup(entities)
    mention_index = build_entity_mention_index(entities)
    docs: list[dict[str, Any]] = []
    for raw in chunks:
        chunk = raw if isinstance(raw, dict) else raw.model_dump()
        doc = derive_chunk_doc(
            chunk,
            entities_by_id,
            entity_mention_index=mention_index,
        )
        # Attach graph_path and content_hash (needed for change detection)
        doc["graph_path"] = graph_path
        doc["content_hash"] = chunk.get("content_hash", "")
        docs.append(doc)
    return docs


# ---------------------------------------------------------------------------
# Single-item derivation functions
# ---------------------------------------------------------------------------


def derive_chunk_doc(
    chunk: dict[str, Any],
    entities_by_id: Optional[dict[str, dict[str, Any]]] = None,
    *,
    entity_mention_index: (
        dict[str, list[tuple[str, str]]] | None
    ) = None,
) -> dict[str, Any]:
    """Return a kg-chunks AI Search document dict from a chunk Parquet row.

    Parameters
    ----------
    chunk:
        Dict representing one row from the chunks Parquet table (all columns).
    entities_by_id:
        Optional lookup from entity_id -> entity row dict.
        When provided, used to populate canonical_key and entity_types.
        When None, those fields default to empty / null.

    Returns
    -------
    dict
        Flat AI Search document with all required fields.
        chunk_vector is left absent — callers attach it via embeddings.
    """
    entities_by_id = entities_by_id or {}

    entity_ids: list[str] = _to_list(chunk.get("related_entity_ids")) or []
    if not entity_ids and entity_mention_index:
        entity_ids = infer_entity_mentions(
            chunk.get("content") or chunk.get("embedding_text"),
            entity_mention_index,
        )

    # entity_aliases: use pre-populated entity_search_keys when available;
    # otherwise derive on the fly from entity_lookup (§11.8)
    raw_search_keys = _to_list(chunk.get("entity_search_keys"))
    if raw_search_keys:
        entity_aliases: list[str] = raw_search_keys
    else:
        entity_aliases = build_entity_search_keys(entity_ids, entities_by_id)

    canonical_key: str = ""
    entity_types: list[str] = []
    semantic_type_ids: list[str] = []
    for eid in entity_ids:
        ent = entities_by_id.get(eid)
        if ent:
            entity_types.append(ent.get("entity_type", ""))
            semantic_id = _semantic_type_id(ent)
            if semantic_id:
                semantic_type_ids.append(semantic_id)
            if not canonical_key:
                canonical_key = ent.get("canonical_key", "")

    doc: dict[str, Any] = {
        "chunk_id": chunk["chunk_id"],
        "content": chunk.get("content", ""),
        "embedding_text": chunk.get("embedding_text") or chunk.get("content", ""),
        "entity_ids": entity_ids,
        "entity_aliases": entity_aliases,
        "canonical_key": canonical_key,
        "entity_types": entity_types,
        "semantic_type_ids": list(dict.fromkeys(semantic_type_ids)),
        "graph_path": None,
        "blob_url": _lineage_blob_url(chunk),
        "source_path": chunk.get("source_file_id", ""),
        "last_modified": _iso(chunk.get("created_at")),
        "content_type": chunk.get("chunk_type", ""),
        # ── M6 SRV-002: canonical lineage fields ──────────────────────────
        "project_id": chunk.get("project_id", ""),
        "asset_id": chunk.get("asset_id", ""),
        "asset_version_id": chunk.get("asset_version_id", ""),
        "run_id": chunk.get("run_id", ""),
        "source_file_id": chunk.get("source_file_id", ""),
        "document_element_id": chunk.get("document_element_id"),
        "source_locator_json": chunk.get("source_locator_json"),
        "schema_version": chunk.get("schema_version", ""),
        "domain_hash": chunk.get("domain_hash"),
        # ── Security placeholders (not populated at compile time) ──────────
        "sensitivity_label": None,
        "acl_json": None,
    }
    return doc


def derive_document_element_doc(
    element: dict[str, Any],
    entities_by_id: Optional[dict[str, dict[str, Any]]] = None,
    *,
    entity_mention_index: (
        dict[str, list[tuple[str, str]]] | None
    ) = None,
) -> dict[str, Any]:
    """Return a kg-document-elements AI Search document dict.

    Parameters
    ----------
    element:
        Dict representing one row from document_elements Parquet table.
    entities_by_id:
        Optional entity lookup; note document_elements don't carry related_entity_ids
        directly in the schema — callers can pre-enrich the element dict if desired.

    Returns
    -------
    dict
        Flat AI Search document.  element_vector is absent — attach separately.
    """
    entities_by_id = entities_by_id or {}

    entity_ids: list[str] = _to_list(element.get("related_entity_ids")) or []
    if not entity_ids and entity_mention_index:
        entity_ids = infer_entity_mentions(
            element.get("content") or element.get("content_html"),
            entity_mention_index,
        )
    entity_aliases: list[str] = (
        _to_list(element.get("entity_search_keys"))
        or build_entity_search_keys(entity_ids, entities_by_id)
    )

    canonical_key = ""
    entity_types: list[str] = []
    semantic_type_ids: list[str] = []
    for eid in entity_ids:
        ent = entities_by_id.get(eid)
        if ent:
            entity_types.append(ent.get("entity_type", ""))
            semantic_id = _semantic_type_id(ent)
            if semantic_id:
                semantic_type_ids.append(semantic_id)
            if not canonical_key:
                canonical_key = ent.get("canonical_key", "")

    doc: dict[str, Any] = {
        "document_element_id": element["document_element_id"],
        "content": element.get("content") or "",
        "content_html": element.get("content_html"),
        "element_type": element.get("element_type", ""),
        "page_number": element.get("page_number"),
        "section_path": element.get("section_path"),
        "entity_ids": entity_ids,
        "entity_aliases": entity_aliases,
        "canonical_key": canonical_key,
        "entity_types": entity_types,
        "semantic_type_ids": list(dict.fromkeys(semantic_type_ids)),
        "graph_path": None,
        "blob_url": _lineage_blob_url(element),
        "source_path": element.get("source_file_id", ""),
        "last_modified": _iso(element.get("extracted_at")),
        "content_type": element.get("element_type", ""),
        # ── M6 SRV-002: canonical lineage fields ──────────────────────────
        "project_id": element.get("project_id", ""),
        "asset_id": element.get("asset_id", ""),
        "asset_version_id": element.get("asset_version_id", ""),
        "run_id": element.get("run_id", ""),
        "source_file_id": element.get("source_file_id", ""),
        "source_locator_json": element.get("source_locator_json"),
        "schema_version": element.get("schema_version", ""),
        "domain_hash": element.get("domain_hash"),
        # ── Security placeholders (not populated at compile time) ──────────
        "sensitivity_label": None,
        "acl_json": None,
    }
    return doc


def derive_visual_docs(
    assets: list[dict[str, Any]],
    regions: list[dict[str, Any]],
    entities_by_id: Optional[dict[str, dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Return kg-visual-assets documents for visual assets and their regions."""
    entities_by_id = entities_by_id or {}
    assets_by_id = {asset["image_id"]: asset for asset in assets}
    docs = [_derive_visual_asset_doc(asset) for asset in assets]
    docs.extend(
        _derive_visual_region_doc(region, assets_by_id.get(region["image_id"]), entities_by_id)
        for region in regions
    )
    return docs


def _derive_visual_asset_doc(asset: dict[str, Any]) -> dict[str, Any]:
    """Return one visual-asset search document."""
    content = "\n".join(
        value for value in (
            asset.get("caption"),
            asset.get("alt_text"),
            asset.get("description"),
        ) if value
    )
    return {
        "visual_id": asset["image_id"],
        "record_type": "asset",
        "image_id": asset["image_id"],
        "visual_region_id": None,
        "document_element_id": asset.get("document_element_id"),
        "content": content,
        "embedding_text": content,
        "asset_type": asset.get("asset_type", ""),
        "region_type": None,
        "page_number": asset.get("page_number"),
        "section_path": asset.get("section_path"),
        "polygon_json": None,
        "blob_url": _lineage_blob_url(asset),
        "source_path": asset.get("source_file_id", ""),
        "last_modified": _iso(asset.get("created_at")),
        "entity_ids": [],
        "canonical_key": "",
        "entity_types": [],
        "semantic_type_ids": [],
        "graph_path": None,
        # ── M6 SRV-002: lineage fields ────────────────────────────────────
        "project_id": asset.get("project_id", ""),
        "asset_id": asset.get("asset_id", ""),
        "asset_version_id": asset.get("asset_version_id", ""),
        "run_id": asset.get("run_id", ""),
        "source_file_id": asset.get("source_file_id", ""),
        "source_locator_json": asset.get("source_locator_json"),
        "schema_version": asset.get("schema_version", ""),
        "domain_hash": asset.get("domain_hash"),
        "sensitivity_label": None,
        "acl_json": None,
    }


def _derive_visual_region_doc(
    region: dict[str, Any],
    asset: Optional[dict[str, Any]],
    entities_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Return one visual-region search document with its asset context."""
    entity_id = region.get("identified_entity_id")
    entity = entities_by_id.get(entity_id) if entity_id else None
    content = "\n".join(value for value in (region.get("label"), region.get("text")) if value)
    return {
        "visual_id": region["visual_region_id"],
        "record_type": "region",
        "image_id": region["image_id"],
        "visual_region_id": region["visual_region_id"],
        "document_element_id": asset.get("document_element_id") if asset else None,
        "content": content,
        "embedding_text": content,
        "asset_type": asset.get("asset_type", "") if asset else "",
        "region_type": region.get("region_type", ""),
        "page_number": asset.get("page_number") if asset else None,
        "section_path": asset.get("section_path") if asset else None,
        "polygon_json": region.get("normalized_polygon_json") or region.get("polygon_json"),
        "blob_url": region.get("blob_url") or (asset.get("blob_url") if asset else None),
        "source_path": asset.get("source_file_id", "") if asset else "",
        "last_modified": _iso(region.get("created_at")),
        "entity_ids": [entity_id] if entity_id else [],
        "canonical_key": entity.get("canonical_key", "") if entity else "",
        "entity_types": [entity.get("entity_type", "")] if entity else [],
        "semantic_type_ids": (
            [_semantic_type_id(entity)]
            if entity and _semantic_type_id(entity)
            else []
        ),
        "graph_path": None,
        # ── M6 SRV-002: lineage fields ────────────────────────────────────
        "project_id": region.get("project_id", ""),
        "asset_id": region.get("asset_id", ""),
        "asset_version_id": region.get("asset_version_id", ""),
        "run_id": region.get("run_id", ""),
        "source_file_id": asset.get("source_file_id", "") if asset else "",
        "source_locator_json": region.get("source_locator_json"),
        "schema_version": region.get("schema_version", ""),
        "domain_hash": region.get("domain_hash"),
        "sensitivity_label": None,
        "acl_json": None,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _to_list(value: Any) -> list[str]:
    """Normalise None / list / pyarrow list-like to a plain Python list."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v is not None]
    try:
        return [str(v) for v in list(value) if v is not None]
    except (TypeError, ValueError):
        return []


def _normalize_mention(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(
        re.sub(r"[^0-9a-z]+", " ", str(value).casefold()).split()
    )


def _usable_mention(value: str) -> bool:
    if not value:
        return False
    compact = value.replace(" ", "")
    if len(compact) < 3:
        return False
    return " " in value or len(compact) >= 4 or any(
        character.isdigit() for character in compact
    )


def _lineage_blob_url(row: dict[str, Any]) -> str | None:
    explicit = row.get("blob_url")
    if explicit:
        return str(explicit)
    raw = row.get("source_locator_json")
    if isinstance(raw, str) and raw.strip():
        try:
            locator = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
    elif isinstance(raw, dict):
        locator = raw
    else:
        return None
    if not isinstance(locator, dict):
        return None
    for key in (
        "blob_uri",
        "blobUri",
        "blob_url",
        "blobUrl",
        "landing_uri",
        "landingUri",
    ):
        value = locator.get(key)
        if value:
            return str(value)
    return None


def _iso(value: Any) -> Optional[str]:
    """Return ISO-8601 string if value has an isoformat method, else None."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value) if value else None
