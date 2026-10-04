"""Tests for serving/index_version.py — deterministic versioned index naming."""
from __future__ import annotations

import pytest

from fabric_kg_builder.serving.index_version import (
    EmbeddingMismatchError,
    assert_embedding_match,
    compute_index_fingerprint,
    physical_index_name,
    stable_alias,
)


_SAMPLE_SCHEMA = {
    "fields": [
        {"name": "id", "type": "Edm.String", "key": True},
        {"name": "content", "type": "Edm.String"},
        {"name": "embedding", "type": "Collection(Edm.Single)", "dimensions": 1536},
    ],
    "vectorSearch": {
        "algorithms": [{"name": "hnsw-config", "kind": "hnsw"}]
    },
}


class TestComputeIndexFingerprint:
    def test_returns_8_char_hex(self):
        fp = compute_index_fingerprint(_SAMPLE_SCHEMA, "text-embedding-3-large", 1536)
        assert len(fp) == 8
        assert all(c in "0123456789abcdef" for c in fp)

    def test_deterministic_for_same_inputs(self):
        fp1 = compute_index_fingerprint(_SAMPLE_SCHEMA, "text-embedding-3-large", 1536)
        fp2 = compute_index_fingerprint(_SAMPLE_SCHEMA, "text-embedding-3-large", 1536)
        assert fp1 == fp2

    def test_different_model_gives_different_fingerprint(self):
        fp1 = compute_index_fingerprint(_SAMPLE_SCHEMA, "text-embedding-3-large", 1536)
        fp2 = compute_index_fingerprint(_SAMPLE_SCHEMA, "text-embedding-ada-002", 1536)
        assert fp1 != fp2

    def test_different_dimensions_gives_different_fingerprint(self):
        fp1 = compute_index_fingerprint(_SAMPLE_SCHEMA, "model-a", 1536)
        fp2 = compute_index_fingerprint(_SAMPLE_SCHEMA, "model-a", 3072)
        assert fp1 != fp2

    def test_different_schema_gives_different_fingerprint(self):
        schema2 = {
            "fields": [
                {"name": "id", "type": "Edm.String", "key": True},
            ],
        }
        fp1 = compute_index_fingerprint(_SAMPLE_SCHEMA, "model", 1536)
        fp2 = compute_index_fingerprint(schema2, "model", 1536)
        assert fp1 != fp2

    def test_underscore_keys_excluded(self):
        schema_with_comments = {**_SAMPLE_SCHEMA, "_comment": "This is a comment", "_sprint": "S2"}
        fp_clean = compute_index_fingerprint(_SAMPLE_SCHEMA, "model", 1536)
        fp_with_comments = compute_index_fingerprint(schema_with_comments, "model", 1536)
        assert fp_clean == fp_with_comments

    def test_empty_schema(self):
        fp = compute_index_fingerprint({}, "model", 1536)
        assert len(fp) == 8

    def test_nested_underscore_keys_excluded(self):
        schema = {
            "fields": [{"name": "id", "_internal": "skip", "type": "Edm.String"}]
        }
        schema_no_underscore = {
            "fields": [{"name": "id", "type": "Edm.String"}]
        }
        fp1 = compute_index_fingerprint(schema, "model", 1536)
        fp2 = compute_index_fingerprint(schema_no_underscore, "model", 1536)
        assert fp1 == fp2


class TestPhysicalIndexName:
    def test_format(self):
        name = physical_index_name("kg-chunks", "a3f8e901")
        assert name == "kg-chunks-v-a3f8e901"

    def test_different_bases_differ(self):
        n1 = physical_index_name("idx1", "abc123")
        n2 = physical_index_name("idx2", "abc123")
        assert n1 != n2

    def test_different_fingerprints_differ(self):
        n1 = physical_index_name("idx", "aaaa1111")
        n2 = physical_index_name("idx", "bbbb2222")
        assert n1 != n2


class TestStableAlias:
    def test_alias_equals_base_name(self):
        assert stable_alias("kg-chunks") == "kg-chunks"

    def test_alias_equals_base_name_with_variant(self):
        assert stable_alias("my-index") == "my-index"


class TestAssertEmbeddingMatch:
    def test_passes_on_matching_fingerprint(self):
        fp = compute_index_fingerprint(_SAMPLE_SCHEMA, "text-embedding-3-large", 1536)
        # Should not raise
        assert_embedding_match(_SAMPLE_SCHEMA, "text-embedding-3-large", 1536, stored_fingerprint=fp)

    def test_raises_on_model_mismatch(self):
        fp = compute_index_fingerprint(_SAMPLE_SCHEMA, "text-embedding-3-large", 1536)
        with pytest.raises(EmbeddingMismatchError, match="mismatch"):
            assert_embedding_match(_SAMPLE_SCHEMA, "different-model", 1536, stored_fingerprint=fp)

    def test_raises_on_dimensions_mismatch(self):
        fp = compute_index_fingerprint(_SAMPLE_SCHEMA, "model", 1536)
        with pytest.raises(EmbeddingMismatchError):
            assert_embedding_match(_SAMPLE_SCHEMA, "model", 3072, stored_fingerprint=fp)

    def test_raises_on_schema_mismatch(self):
        fp = compute_index_fingerprint(_SAMPLE_SCHEMA, "model", 1536)
        different_schema = {"fields": [{"name": "other", "type": "Edm.String"}]}
        with pytest.raises(EmbeddingMismatchError):
            assert_embedding_match(different_schema, "model", 1536, stored_fingerprint=fp)

    def test_error_message_contains_fingerprints(self):
        fp = compute_index_fingerprint(_SAMPLE_SCHEMA, "model", 1536)
        with pytest.raises(EmbeddingMismatchError) as exc_info:
            assert_embedding_match(_SAMPLE_SCHEMA, "other-model", 1536, stored_fingerprint=fp)
        assert fp in str(exc_info.value)
