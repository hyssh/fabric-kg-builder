"""Unit tests for search.embeddings.generate_embeddings.

Sprint 2: verify that generate_embeddings:
  - attaches 1536-dim vectors to each search doc via FoundryClient.embed() (MOCKED)
  - caches by content_hash — unchanged chunks skip the embed call
  - composes with linkage.derive_chunk_doc (fields present in output dicts)
  - returns one doc per input chunk
  - handles empty input gracefully

All tests use a mock FoundryClient — no live API calls.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, call

import pytest

from fabric_kg_builder.search.embeddings import _VECTOR_DIMS, generate_embeddings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chunk(
    chunk_id: str = "chunk:001",
    content_hash: str = "hash001",
    content: str = "Surface Laptop 5 battery replacement guide.",
    embedding_text: str | None = None,
) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "source_file_id": "src:test",
        "chunk_type": "section_text",
        "content": content,
        "embedding_text": embedding_text or content,
        "content_hash": content_hash,
        "created_at": "2026-06-24T14:00:00+00:00",
    }


def _make_mock_client(dims: int = _VECTOR_DIMS) -> MagicMock:
    """Return a mock FoundryClient whose embed() returns unit vectors."""
    client = MagicMock()
    client.embed.side_effect = lambda texts: [[0.1] * dims for _ in texts]
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGenerateEmbeddings:

    def test_returns_one_doc_per_chunk(self) -> None:
        """Output list length equals input chunk count."""
        chunks = [_make_chunk("c1", "h1"), _make_chunk("c2", "h2", content="Battery guide.")]
        client = _make_mock_client()
        docs = generate_embeddings(chunks, client)
        assert len(docs) == 2

    def test_attaches_1536_dim_vector(self) -> None:
        """Each doc has chunk_vector of length 1536."""
        chunks = [_make_chunk()]
        client = _make_mock_client()
        docs = generate_embeddings(chunks, client)
        assert "chunk_vector" in docs[0], "chunk_vector key missing from output doc"
        assert len(docs[0]["chunk_vector"]) == _VECTOR_DIMS, (
            f"Expected {_VECTOR_DIMS}-dim vector, got {len(docs[0]['chunk_vector'])}"
        )

    def test_calls_embed_with_embedding_text(self) -> None:
        """client.embed() is called with the embedding_text values."""
        chunk = _make_chunk(embedding_text="Surface kickstand replacement steps.")
        client = _make_mock_client()
        generate_embeddings([chunk], client)
        client.embed.assert_called_once_with(["Surface kickstand replacement steps."])

    def test_skips_unchanged_by_content_hash(self) -> None:
        """Chunks with the same content_hash are served from cache; embed() called once."""
        chunk = _make_chunk(content_hash="stable_hash")
        client = _make_mock_client()
        shared_cache: dict[str, list[float]] = {}

        # First call — should embed.
        docs1 = generate_embeddings([chunk], client, cache=shared_cache)
        assert client.embed.call_count == 1

        # Second call with the same chunk (same hash) — must NOT call embed again.
        docs2 = generate_embeddings([chunk], client, cache=shared_cache)
        assert client.embed.call_count == 1, (
            "embed() was called again for a chunk already in cache"
        )
        # Vectors must be identical.
        assert docs1[0]["chunk_vector"] == docs2[0]["chunk_vector"]

    def test_batches_embed_calls(self) -> None:
        """Chunks exceeding batch_size are split into multiple embed() calls."""
        chunks = [_make_chunk(f"c{i}", f"h{i}", content=f"Text {i}.") for i in range(10)]
        client = _make_mock_client()
        generate_embeddings(chunks, client, batch_size=3)
        # 10 chunks / 3 per batch → 4 calls (3+3+3+1)
        assert client.embed.call_count == 4

    def test_empty_input_returns_empty(self) -> None:
        """generate_embeddings([]) returns [] without calling embed."""
        client = _make_mock_client()
        docs = generate_embeddings([], client)
        assert docs == []
        client.embed.assert_not_called()

    def test_output_contains_linkage_fields(self) -> None:
        """Docs contain chunk_id and content (from linkage.derive_chunk_doc)."""
        chunk = _make_chunk(chunk_id="chunk:abc", content="Battery steps.")
        client = _make_mock_client()
        docs = generate_embeddings([chunk], client)
        doc = docs[0]
        assert doc["chunk_id"] == "chunk:abc"
        assert doc["content"] == "Battery steps."

    def test_fresh_cache_per_call_without_shared_cache(self) -> None:
        """Without a shared cache, each call re-embeds (no cross-call caching)."""
        chunk = _make_chunk(content_hash="myhash")
        client = _make_mock_client()
        generate_embeddings([chunk], client)
        generate_embeddings([chunk], client)
        assert client.embed.call_count == 2, (
            "Without a shared cache, each call should embed independently"
        )

    def test_custom_vector_field_name(self) -> None:
        """vector_field parameter controls the dict key written."""
        chunk = _make_chunk()
        client = _make_mock_client()
        docs = generate_embeddings([chunk], client, vector_field="my_vector")
        assert "my_vector" in docs[0]
        assert "chunk_vector" not in docs[0]

    def test_mixed_cached_and_uncached(self) -> None:
        """Only uncached chunks are embedded; cached ones skip the network call."""
        chunk_cached = _make_chunk("c1", "cached_hash", content="Cached text.")
        chunk_new = _make_chunk("c2", "new_hash", content="New text.")

        client = _make_mock_client()
        shared_cache: dict[str, list[float]] = {}

        # Pre-populate cache for chunk_cached.
        shared_cache["cached_hash"] = [0.5] * _VECTOR_DIMS

        docs = generate_embeddings([chunk_cached, chunk_new], client, cache=shared_cache)

        # Only the uncached chunk should have triggered embed().
        client.embed.assert_called_once_with(["New text."])
        assert len(docs) == 2
        assert docs[0]["chunk_vector"] == [0.5] * _VECTOR_DIMS  # from cache
        assert docs[1]["chunk_vector"] == [0.1] * _VECTOR_DIMS  # from mock embed
