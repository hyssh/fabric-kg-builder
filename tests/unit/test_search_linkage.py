"""Unit tests for search.linkage — SPEC-002 §11 graph-to-search derivation.

Verifies:
- entity_ids (filterable) are populated from chunks.related_entity_ids
- entity_aliases (searchable) from chunks.entity_search_keys / entities.search_aliases
- canonical_key from primary linked entity
- entity_types from all linked entities
- graph_path injected at call time (not stored in Parquet)
- blob_url from chunk
- last_modified from chunks.created_at
- content_hash included for change-detection
- Filter-on-IDs / Search-on-aliases split enforced (§11.4)
- build_search_aliases and build_entity_search_keys helpers
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fabric_kg_builder.model.ids import content_hash, make_chunk_id, make_entity_id
from fabric_kg_builder.search.linkage import (
    build_entity_lookup,
    build_entity_search_keys,
    build_search_aliases,
    derive_chunk_doc,
    derive_chunk_search_docs,
)

_UTC = timezone.utc
_NOW = datetime(2026, 6, 24, 14, 0, 0, tzinfo=_UTC)
_SRC_ID = "src:linkage_test_abc123"


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _make_entity(
    entity_type: str = "Device",
    display_name: str = "Surface Laptop 5",
    aliases: list[str] | None = None,
    canonical_key: str | None = None,
) -> dict:
    eid = make_entity_id(entity_type, display_name)
    ck = canonical_key or f"{entity_type.lower()}:{display_name.lower().replace(' ', '-')}"
    sa = build_search_aliases(ck, display_name, aliases)
    return {
        "entity_id": eid,
        "entity_type": entity_type,
        "display_name": display_name,
        "canonical_key": ck,
        "aliases": aliases or [],
        "search_aliases": sa,
        "description": None,
        "properties_json": None,
        "source_file_id": _SRC_ID,
        "confidence": 0.9,
        "is_placeholder": False,
        "content_hash": content_hash(ck),
        "created_at": _NOW,
        "updated_at": _NOW,
    }


def _make_chunk(
    entity_ids: list[str] | None = None,
    search_keys: list[str] | None = None,
    chunk_type: str = "section_text",
    content: str = "Replace the battery in Surface Laptop 5.",
) -> dict:
    ch = content_hash(content)
    cid = make_chunk_id(_SRC_ID, chunk_type, ch)
    return {
        "chunk_id": cid,
        "source_file_id": _SRC_ID,
        "document_element_id": None,
        "chunk_type": chunk_type,
        "content": content,
        "content_html": None,
        "embedding_text": content,
        "blob_url": None,
        "page_number": 5,
        "section_path": "Battery Replacement",
        "table_id": None,
        "figure_id": None,
        "image_id": None,
        "related_entity_ids": entity_ids,
        "entity_search_keys": search_keys,
        "content_hash": ch,
        "created_at": _NOW,
    }


# ---------------------------------------------------------------------------
# Tests: build_search_aliases
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildSearchAliases:
    def test_basic(self) -> None:
        result = build_search_aliases(
            "device:surface-laptop-5", "Surface Laptop 5", ["SL5"]
        )
        assert "device:surface-laptop-5" in result
        assert "surface laptop 5" in result
        assert "sl5" in result

    def test_dedup(self) -> None:
        result = build_search_aliases(
            "device:surface-laptop-5",
            "Surface Laptop 5",
            ["Surface Laptop 5"],  # same as display_name lowercased
        )
        assert result.count("surface laptop 5") == 1

    def test_no_aliases(self) -> None:
        result = build_search_aliases("component:battery", "Battery", None)
        assert "component:battery" in result
        assert "battery" in result

    def test_order_canonical_key_first(self) -> None:
        result = build_search_aliases("component:battery", "Battery", ["BT-1"])
        assert result[0] == "component:battery"


# ---------------------------------------------------------------------------
# Tests: build_entity_search_keys
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildEntitySearchKeys:
    def test_flattens_search_aliases(self) -> None:
        ent = _make_entity("Device", "Surface Laptop 5", aliases=["SL5"])
        lookup = build_entity_lookup([ent])
        result = build_entity_search_keys([ent["entity_id"]], lookup)
        assert "device:surface-laptop-5" in result
        assert "surface laptop 5" in result
        assert "sl5" in result

    def test_deduplicates_across_entities(self) -> None:
        e1 = _make_entity("Device", "Surface Laptop 5")
        e2 = _make_entity("Component", "Battery", aliases=["Surface Laptop 5"])
        lookup = build_entity_lookup([e1, e2])
        result = build_entity_search_keys(
            [e1["entity_id"], e2["entity_id"]], lookup
        )
        # "surface laptop 5" should appear only once
        assert result.count("surface laptop 5") == 1

    def test_unknown_entity_skipped(self) -> None:
        result = build_entity_search_keys(["entity:does_not_exist"], {})
        assert result == []

    def test_none_entity_ids_returns_empty(self) -> None:
        result = build_entity_search_keys(None, {})
        assert result == []


# ---------------------------------------------------------------------------
# Tests: derive_chunk_doc
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeriveChunkDoc:
    def test_entity_ids_populated_from_related_entity_ids(self) -> None:
        ent = _make_entity("Device", "Surface Laptop 5")
        chunk = _make_chunk(entity_ids=[ent["entity_id"]])
        lookup = build_entity_lookup([ent])
        doc = derive_chunk_doc(chunk, lookup)
        # entity_ids must carry the opaque stable IDs (filterable; §11.4)
        assert ent["entity_id"] in doc["entity_ids"]

    def test_entity_aliases_from_search_keys(self) -> None:
        """entity_aliases (searchable) come from entity_search_keys — not from entity_ids."""
        ent = _make_entity("Device", "Surface Laptop 5", aliases=["SL5"])
        search_keys = build_search_aliases(
            ent["canonical_key"], ent["display_name"], ent["aliases"]
        )
        chunk = _make_chunk(
            entity_ids=[ent["entity_id"]],
            search_keys=search_keys,
        )
        lookup = build_entity_lookup([ent])
        doc = derive_chunk_doc(chunk, lookup)
        # entity_aliases must contain human-readable terms (searchable; §11.4)
        assert "surface laptop 5" in doc["entity_aliases"]
        assert "sl5" in doc["entity_aliases"]

    def test_entity_ids_not_in_entity_aliases(self) -> None:
        """entity_ids must never appear as searchable text — §11.4 anti-pattern check."""
        ent = _make_entity("Device", "Surface Laptop 5")
        chunk = _make_chunk(entity_ids=[ent["entity_id"]])
        lookup = build_entity_lookup([ent])
        doc = derive_chunk_doc(chunk, lookup)
        for eid in doc["entity_ids"]:
            assert eid not in doc["entity_aliases"], (
                f"entity_id '{eid}' must not appear in entity_aliases (§11.4 violation)"
            )

    def test_canonical_key_from_first_linked_entity(self) -> None:
        ent1 = _make_entity("Device", "Surface Laptop 5")
        ent2 = _make_entity("Component", "Battery")
        chunk = _make_chunk(entity_ids=[ent1["entity_id"], ent2["entity_id"]])
        lookup = build_entity_lookup([ent1, ent2])
        doc = derive_chunk_doc(chunk, lookup)
        assert doc["canonical_key"] == ent1["canonical_key"]

    def test_entity_types_from_all_linked_entities(self) -> None:
        ent1 = _make_entity("Device", "Surface Laptop 5")
        ent2 = _make_entity("Component", "Battery")
        chunk = _make_chunk(entity_ids=[ent1["entity_id"], ent2["entity_id"]])
        lookup = build_entity_lookup([ent1, ent2])
        doc = derive_chunk_doc(chunk, lookup)
        assert "Device" in doc["entity_types"]
        assert "Component" in doc["entity_types"]

    def test_blob_url_from_chunk(self) -> None:
        chunk = _make_chunk()
        chunk["blob_url"] = "https://fake.blob.core.windows.net/assets/fig.png"
        doc = derive_chunk_doc(chunk)
        assert doc["blob_url"] == "https://fake.blob.core.windows.net/assets/fig.png"

    def test_last_modified_is_iso_string(self) -> None:
        chunk = _make_chunk()
        doc = derive_chunk_doc(chunk)
        lm = doc["last_modified"]
        assert lm is not None
        assert "2026" in lm  # ISO string includes year

    def test_content_type_from_chunk_type(self) -> None:
        chunk = _make_chunk(chunk_type="procedure_step")
        doc = derive_chunk_doc(chunk)
        assert doc["content_type"] == "procedure_step"

    def test_no_entities_gives_empty_lists(self) -> None:
        chunk = _make_chunk(entity_ids=None)
        doc = derive_chunk_doc(chunk, {})
        assert doc["entity_ids"] == []
        assert doc["entity_aliases"] == []
        assert doc["entity_types"] == []
        assert doc["canonical_key"] == ""


# ---------------------------------------------------------------------------
# Tests: derive_chunk_search_docs (batch form)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeriveChunkSearchDocs:
    def test_returns_one_doc_per_chunk(self) -> None:
        chunks = [_make_chunk(), _make_chunk(content="Another chunk")]
        docs = derive_chunk_search_docs(chunks, [])
        assert len(docs) == 2

    def test_each_doc_has_chunk_id(self) -> None:
        chunk = _make_chunk()
        docs = derive_chunk_search_docs([chunk], [])
        assert docs[0]["chunk_id"] == chunk["chunk_id"]

    def test_content_hash_included_for_change_detection(self) -> None:
        chunk = _make_chunk()
        docs = derive_chunk_search_docs([chunk], [])
        assert docs[0]["content_hash"] == chunk["content_hash"]

    def test_graph_path_injected(self) -> None:
        chunk = _make_chunk()
        docs = derive_chunk_search_docs(
            [chunk], [],
            graph_path="Device --[has_component]--> Component"
        )
        assert docs[0]["graph_path"] == "Device --[has_component]--> Component"

    def test_graph_path_defaults_none(self) -> None:
        chunk = _make_chunk()
        docs = derive_chunk_search_docs([chunk], [])
        assert docs[0]["graph_path"] is None

    def test_entity_linkage_from_entities_list(self) -> None:
        """entity_ids and entity_aliases derived from canonical entity records."""
        ent = _make_entity("Device", "Surface Laptop 5", aliases=["SL5"])
        chunk = _make_chunk(entity_ids=[ent["entity_id"]])
        docs = derive_chunk_search_docs([chunk], [ent])
        doc = docs[0]
        # entity_ids (filterable) must contain the opaque ID
        assert ent["entity_id"] in doc["entity_ids"]
        # entity_aliases (searchable) derived from search_aliases
        assert "surface laptop 5" in doc["entity_aliases"]

    def test_entity_ids_vs_entity_aliases_separation(self) -> None:
        """The §11.4 filter-on-IDs/search-on-aliases split must hold across all docs."""
        ent = _make_entity("Device", "Surface Laptop 5")
        chunk = _make_chunk(entity_ids=[ent["entity_id"]])
        docs = derive_chunk_search_docs([chunk], [ent])
        doc = docs[0]
        # IDs must not appear in searchable aliases
        for eid in doc["entity_ids"]:
            assert eid not in doc["entity_aliases"], (
                f"§11.4 violation: entity_id '{eid}' found in entity_aliases"
            )

    def test_empty_chunks_returns_empty_list(self) -> None:
        docs = derive_chunk_search_docs([], [])
        assert docs == []

    def test_entity_search_keys_on_chunk_used_directly(self) -> None:
        """When chunk already has entity_search_keys, use them (§11.8)."""
        ent = _make_entity("Device", "Surface Laptop 5", aliases=["SL5"])
        prebuilt_keys = ["device:surface-laptop-5", "surface laptop 5", "sl5"]
        chunk = _make_chunk(
            entity_ids=[ent["entity_id"]],
            search_keys=prebuilt_keys,
        )
        docs = derive_chunk_search_docs([chunk], [ent])
        assert docs[0]["entity_aliases"] == prebuilt_keys
