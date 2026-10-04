"""Tests for serving/release_manager.py — pure helpers, result types, fake transport."""
from __future__ import annotations

import pytest

from fabric_kg_builder.serving.release_manager import (
    FakeSearchTransport,
    ReleaseResult,
    ReleaseManager,
    _project_schema_to_expected_shape,
)


# ---------------------------------------------------------------------------
# _project_schema_to_expected_shape
# ---------------------------------------------------------------------------


class TestProjectSchemaToExpectedShape:
    def test_scalar_returned_unchanged(self):
        result = _project_schema_to_expected_shape("stored", "expected")
        assert result == "stored"

    def test_dict_projection(self):
        stored = {"a": 1, "b": 2, "c": 3}
        expected = {"a": 10, "b": 20}  # Only a, b specified
        result = _project_schema_to_expected_shape(stored, expected)
        assert result == {"a": 1, "b": 2}  # c excluded
        assert "c" not in result

    def test_nested_dict_projection(self):
        stored = {"outer": {"inner_a": 1, "inner_b": 2, "inner_c": 3}}
        expected = {"outer": {"inner_a": 10}}
        result = _project_schema_to_expected_shape(stored, expected)
        assert result["outer"] == {"inner_a": 1}

    def test_list_projection_by_name(self):
        stored = [{"name": "a", "type": "str", "extra": "ignored"},
                  {"name": "b", "type": "int"}]
        expected = [{"name": "a", "type": "x"}]
        result = _project_schema_to_expected_shape(stored, expected)
        assert len(result) == 1
        assert result[0]["name"] == "a"
        assert result[0]["type"] == "str"  # stored value, not expected

    def test_list_without_name_key_uses_zip(self):
        stored = [10, 20, 30]
        expected = [1, 2]
        result = _project_schema_to_expected_shape(stored, expected)
        assert result == [10, 20]  # zip stops at expected length

    def test_expected_key_missing_from_stored_excluded(self):
        stored = {"a": 1}
        expected = {"a": 10, "missing": 99}
        result = _project_schema_to_expected_shape(stored, expected)
        assert "missing" not in result

    def test_empty_dict(self):
        result = _project_schema_to_expected_shape({}, {})
        assert result == {}

    def test_empty_list(self):
        result = _project_schema_to_expected_shape([], [])
        assert result == []


# ---------------------------------------------------------------------------
# ReleaseResult
# ---------------------------------------------------------------------------


class TestReleaseResult:
    def test_basic_ok_result(self):
        r = ReleaseResult(ok=True, index_name="kg-dev-v1")
        assert r.ok is True
        assert r.index_name == "kg-dev-v1"
        assert r.alias is None
        assert r.docs_found == 0
        assert r.errors == []

    def test_failed_result(self):
        r = ReleaseResult(ok=False, index_name="kg-dev-v1", errors=["Connection refused"])
        assert r.ok is False
        assert "Connection refused" in r.errors

    def test_with_alias(self):
        r = ReleaseResult(ok=True, index_name="kg-dev-v1-20260101", alias="kg-dev")
        assert r.alias == "kg-dev"


# ---------------------------------------------------------------------------
# FakeSearchTransport
# ---------------------------------------------------------------------------


class TestFakeSearchTransport:
    def test_instantiates(self):
        transport = FakeSearchTransport()
        assert transport is not None

    def test_create_index(self):
        transport = FakeSearchTransport()
        schema = {"fields": [{"name": "id", "type": "Edm.String", "key": True}]}
        headers = {}
        resp = transport.put(
            url="https://search.example.com/indexes/test-index",
            headers=headers,
            json=schema,
        )
        assert resp.status_code in (200, 201)

    def test_index_exists_returns_200(self):
        transport = FakeSearchTransport()
        schema = {"fields": [{"name": "id", "type": "Edm.String", "key": True}]}
        transport.put(
            "https://search.example.com/indexes/test-index",
            {},
            schema,
        )
        resp = transport.get(
            "https://search.example.com/indexes/test-index",
            {},
        )
        assert resp.status_code == 200

    def test_unknown_index_returns_404(self):
        transport = FakeSearchTransport()
        resp = transport.get("https://search.example.com/indexes/nonexistent", {})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# ReleaseManager
# ---------------------------------------------------------------------------


class TestReleaseManagerWithFakeTransport:
    def _make_manager(self) -> ReleaseManager:
        transport = FakeSearchTransport()
        return ReleaseManager(
            endpoint="https://search.example.com",
            transport=transport,
            token_provider=None,
        )

    def test_instantiates(self):
        mgr = self._make_manager()
        assert mgr is not None

    def test_get_or_create_index_creates_new(self):
        mgr = self._make_manager()
        schema = {"fields": [{"name": "id", "type": "Edm.String", "key": True}]}
        result = mgr.get_or_create_index("test-index", schema)
        assert isinstance(result, ReleaseResult)
        assert result.ok is True

    def test_get_or_create_index_idempotent(self):
        mgr = self._make_manager()
        schema = {"fields": [{"name": "id", "type": "Edm.String", "key": True}]}
        result1 = mgr.get_or_create_index("test-index", schema)
        result2 = mgr.get_or_create_index("test-index", schema)
        assert result1.ok
        assert result2.ok

    def test_count_probe_on_empty_index(self):
        mgr = self._make_manager()
        schema = {"fields": [{"name": "id", "type": "Edm.String", "key": True}]}
        mgr.get_or_create_index("test-index", schema)
        result = mgr.count_probe("test-index")
        assert isinstance(result, ReleaseResult)
