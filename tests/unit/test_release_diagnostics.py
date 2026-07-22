"""Tests for release/diagnostics.py — pure helper functions."""
from __future__ import annotations

import pytest

from fabric_kg_builder.release.diagnostics import (
    _is_fingerprint,
    _is_hash,
    _looks_like_timestamp,
    _normalize_key,
    _normalize_timestamp,
    fingerprint,
)


class TestFingerprint:
    def test_returns_prefixed_string(self):
        result = fingerprint("test-value")
        assert result.startswith("fp:")

    def test_deterministic(self):
        assert fingerprint("hello") == fingerprint("hello")

    def test_different_values_different_fingerprints(self):
        assert fingerprint("hello") != fingerprint("world")

    def test_dict_input(self):
        result = fingerprint({"key": "value"})
        assert result.startswith("fp:")

    def test_custom_length(self):
        result = fingerprint("test", length=8)
        # Length should affect the hash portion
        assert result.startswith("fp:")

    def test_list_input(self):
        result = fingerprint([1, 2, 3])
        assert result.startswith("fp:")


class TestIsFingerprint:
    def test_valid_fingerprint(self):
        fp = fingerprint("test")
        assert _is_fingerprint(fp) is True

    def test_non_fingerprint_string(self):
        assert _is_fingerprint("sha256:abc") is False

    def test_plain_string(self):
        assert _is_fingerprint("hello") is False

    def test_none(self):
        assert _is_fingerprint(None) is False


class TestIsHash:
    def test_valid_sha256(self):
        sha = "sha256:" + "a" * 64
        assert _is_hash(sha) is True

    def test_short_hash(self):
        assert _is_hash("sha256:abc123") is False

    def test_no_prefix(self):
        assert _is_hash("a" * 64) is False

    def test_none(self):
        assert _is_hash(None) is False


class TestNormalizeTimestamp:
    def test_valid_utc_timestamp(self):
        result = _normalize_timestamp("2025-01-15T12:00:00Z")
        assert result is not None
        assert "Z" in result

    def test_valid_offset_timestamp(self):
        result = _normalize_timestamp("2025-01-15T12:00:00+00:00")
        assert result is not None

    def test_invalid_string(self):
        result = _normalize_timestamp("not-a-date")
        assert result is None

    def test_none_value(self):
        result = _normalize_timestamp(None)
        assert result is None

    def test_empty_string(self):
        result = _normalize_timestamp("")
        assert result is None

    def test_no_timezone(self):
        # No timezone info → returns None
        result = _normalize_timestamp("2025-01-15T12:00:00")
        assert result is None


class TestLooksLikeTimestamp:
    def test_valid_timestamp(self):
        assert _looks_like_timestamp("2025-01-15T12:00:00Z") is True

    def test_invalid_string(self):
        assert _looks_like_timestamp("hello") is False

    def test_none(self):
        assert _looks_like_timestamp(None) is False


class TestNormalizeKey:
    def test_snake_case(self):
        assert _normalize_key("workspace_id") == "workspaceid"

    def test_camel_case(self):
        assert _normalize_key("workspaceId") == "workspaceid"

    def test_kebab_case(self):
        assert _normalize_key("workspace-id") == "workspaceid"

    def test_mixed_case(self):
        assert _normalize_key("WorkspaceID") == "workspaceid"

    def test_numbers_preserved(self):
        assert _normalize_key("field123") == "field123"

    def test_empty_string(self):
        assert _normalize_key("") == ""
