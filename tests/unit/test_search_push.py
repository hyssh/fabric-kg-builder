"""Unit tests for search.push — push pipeline (mock mode).

Tests:
- push_chunk_docs upserts by chunk_id in mock mode
- push_chunk_docs skips unchanged docs by content_hash (change detection)
- push_chunk_docs handles empty input gracefully
- push_documents mock mode returns correct doc_count
- push_index mock mode returns PushResult with mock=True
- PushResult.skipped tracks how many were skipped
"""

from __future__ import annotations

import pytest

from fabric_kg_builder.search.push import PushResult, push_chunk_docs, push_documents, push_index


# ---------------------------------------------------------------------------
# Fixture: sample search docs
# ---------------------------------------------------------------------------


def _make_search_doc(chunk_id: str, content_hash: str, entity_ids: list[str] | None = None) -> dict:
    return {
        "chunk_id": chunk_id,
        "content": f"Content for {chunk_id}",
        "entity_ids": entity_ids or [],
        "entity_aliases": ["surface laptop 5"],
        "canonical_key": "device:surface-laptop-5",
        "entity_types": ["Device"],
        "graph_path": None,
        "blob_url": None,
        "source_file_id": "src:test_abc",
        "last_modified": "2026-06-24T14:00:00+00:00",
        "content_type": "section_text",
        "content_hash": content_hash,
    }


# ---------------------------------------------------------------------------
# Tests: push_chunk_docs — mock mode
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPushChunkDocs:
    def test_mock_returns_push_result(self) -> None:
        docs = [_make_search_doc("chunk:001", "hash_a")]
        result = push_chunk_docs("kg-chunks", docs, mock=True)
        assert isinstance(result, PushResult)
        assert result.mock is True

    def test_mock_mode_doc_count(self) -> None:
        docs = [
            _make_search_doc("chunk:001", "hash_a"),
            _make_search_doc("chunk:002", "hash_b"),
        ]
        result = push_chunk_docs("kg-chunks", docs, mock=True)
        assert result.doc_count == 2
        assert result.skipped == 0

    def test_change_detection_skips_unchanged_docs(self) -> None:
        """Docs whose content_hash matches existing_hashes must be skipped."""
        docs = [
            _make_search_doc("chunk:001", "hash_a"),
            _make_search_doc("chunk:002", "hash_b"),
        ]
        existing_hashes = {
            "chunk:001": "hash_a",  # unchanged — skip
        }
        result = push_chunk_docs(
            "kg-chunks", docs, mock=True, existing_hashes=existing_hashes
        )
        assert result.skipped == 1
        assert result.doc_count == 1  # only chunk:002 pushed

    def test_change_detection_pushes_new_docs(self) -> None:
        """New docs (not in existing_hashes) must always be pushed."""
        docs = [_make_search_doc("chunk:new", "hash_new")]
        result = push_chunk_docs(
            "kg-chunks", docs, mock=True, existing_hashes={}
        )
        assert result.doc_count == 1
        assert result.skipped == 0

    def test_change_detection_pushes_changed_docs(self) -> None:
        """Docs with different hash from stored must be re-pushed."""
        docs = [_make_search_doc("chunk:001", "hash_new")]
        existing_hashes = {"chunk:001": "hash_old"}
        result = push_chunk_docs(
            "kg-chunks", docs, mock=True, existing_hashes=existing_hashes
        )
        assert result.doc_count == 1
        assert result.skipped == 0

    def test_all_unchanged_skips_all(self) -> None:
        docs = [
            _make_search_doc("chunk:001", "hash_a"),
            _make_search_doc("chunk:002", "hash_b"),
        ]
        existing_hashes = {"chunk:001": "hash_a", "chunk:002": "hash_b"}
        result = push_chunk_docs(
            "kg-chunks", docs, mock=True, existing_hashes=existing_hashes
        )
        assert result.doc_count == 0
        assert result.skipped == 2

    def test_empty_docs_returns_zero(self) -> None:
        result = push_chunk_docs("kg-chunks", [], mock=True)
        assert result.doc_count == 0
        assert result.skipped == 0
        assert result.succeeded is True

    def test_index_name_preserved_in_result(self) -> None:
        result = push_chunk_docs("my-custom-index", [], mock=True)
        assert result.index_name == "my-custom-index"

    def test_push_result_str_includes_mock(self) -> None:
        result = push_chunk_docs("kg-chunks", [], mock=True)
        assert "MOCK" in str(result)

    def test_push_result_str_includes_skipped(self) -> None:
        docs = [_make_search_doc("chunk:001", "hash_a")]
        existing = {"chunk:001": "hash_a"}
        result = push_chunk_docs("kg-chunks", docs, mock=True, existing_hashes=existing)
        s = str(result)
        assert "skipped" in s.lower() or "1" in s


# ---------------------------------------------------------------------------
# Tests: push_documents — mock mode
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPushDocuments:
    def test_mock_returns_correct_doc_count(self) -> None:
        docs = [{"chunk_id": f"chunk:{i}", "content": f"doc {i}"} for i in range(5)]
        result = push_documents("kg-chunks", docs, mock=True)
        assert result.doc_count == 5
        assert result.mock is True

    def test_mock_succeeds(self) -> None:
        result = push_documents("kg-chunks", [], mock=True)
        assert result.succeeded is True

    def test_mock_no_endpoint_forces_mock(self) -> None:
        """No endpoint → always mock even if mock=False requested."""
        import os
        orig = os.environ.pop("AZURE_SEARCH_ENDPOINT", None)
        try:
            result = push_documents("kg-chunks", [], mock=False, endpoint="")
            assert result.mock is True
        finally:
            if orig:
                os.environ["AZURE_SEARCH_ENDPOINT"] = orig


# ---------------------------------------------------------------------------
# Tests: push_index — mock mode
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPushIndex:
    def test_mock_returns_push_result(self) -> None:
        result = push_index("kg-chunks", {"name": "kg-chunks", "fields": []}, mock=True)
        assert isinstance(result, PushResult)
        assert result.mock is True
        assert result.index_name == "kg-chunks"
