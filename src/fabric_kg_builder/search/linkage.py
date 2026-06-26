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

Public API
----------
build_entity_lookup(entities)        → {entity_id: row_dict}
build_search_aliases(canonical_key, display_name, aliases) → list[str]
build_entity_search_keys(related_entity_ids, entity_lookup) → list[str]
derive_chunk_search_docs(chunks, entities, *, graph_path)  → list[dict]
derive_chunk_doc(chunk, entities_by_id)                    → dict  (single-chunk)
derive_document_element_doc(element, entities_by_id)       → dict  (single-element)
"""

from __future__ import annotations

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
    docs: list[dict[str, Any]] = []
    for raw in chunks:
        chunk = raw if isinstance(raw, dict) else raw.model_dump()
        doc = derive_chunk_doc(chunk, entities_by_id)
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

    # entity_aliases: use pre-populated entity_search_keys when available;
    # otherwise derive on the fly from entity_lookup (§11.8)
    raw_search_keys = _to_list(chunk.get("entity_search_keys"))
    if raw_search_keys:
        entity_aliases: list[str] = raw_search_keys
    else:
        entity_aliases = build_entity_search_keys(entity_ids, entities_by_id)

    canonical_key: str = ""
    entity_types: list[str] = []
    for eid in entity_ids:
        ent = entities_by_id.get(eid)
        if ent:
            entity_types.append(ent.get("entity_type", ""))
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
        "graph_path": None,
        "blob_url": chunk.get("blob_url"),
        "source_path": chunk.get("source_file_id", ""),
        "last_modified": _iso(chunk.get("created_at")),
        "content_type": chunk.get("chunk_type", ""),
    }
    return doc


def derive_document_element_doc(
    element: dict[str, Any],
    entities_by_id: Optional[dict[str, dict[str, Any]]] = None,
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
    entity_aliases: list[str] = _to_list(element.get("entity_search_keys")) or []

    canonical_key = ""
    entity_types: list[str] = []
    for eid in entity_ids:
        ent = entities_by_id.get(eid)
        if ent:
            entity_types.append(ent.get("entity_type", ""))
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
        "graph_path": None,
        "blob_url": element.get("blob_url"),
        "source_path": element.get("source_file_id", ""),
        "last_modified": _iso(element.get("extracted_at")),
        "content_type": element.get("element_type", ""),
    }
    return doc


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


def _iso(value: Any) -> Optional[str]:
    """Return ISO-8601 string if value has an isoformat method, else None."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value) if value else None
