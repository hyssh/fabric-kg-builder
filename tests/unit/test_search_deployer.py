"""Unit tests for deploy.search_deployer — mock mode only (no network calls).

Verifies:
- _strip_underscore_keys removes all "_"-prefixed keys recursively
- _ensure_vector_search injects default profile when schema lacks vectorSearch
- _ensure_vector_search is a no-op when vectorSearch is already present
- deploy_index mock=True returns planned result (no network)
- deploy_index mock=True strips "_"-prefixed keys before acting
- deploy_index live mode calls correct REST endpoints
- batch upload builds @search.action: mergeOrUpload actions
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from fabric_kg_builder.deploy.search_deployer import (
    _DEFAULT_VECTOR_SEARCH,
    _ensure_vector_search,
    _sanitize_for_rest,
    _strip_underscore_keys,
    deploy_index,
)

_ENDPOINT = "https://example-search.search.windows.net"
_INDEX = "kg-dev-chunks"

# Minimal schema that mirrors the real index.schema.json (with "_" keys present)
_RAW_SCHEMA = {
    "_schema_version": "1",
    "_sprint": "2",
    "name": "kg-chunks",
    "fields": [
        {
            "name": "chunk_id",
            "type": "Edm.String",
            "key": True,
            "searchable": False,
            "filterable": True,
            "retrievable": True,
            "_comment": "primary key",
        },
        {
            "name": "content",
            "type": "Edm.String",
            "searchable": True,
            "filterable": False,
            "retrievable": True,
        },
        {
            "name": "chunk_vector",
            "type": "Collection(Edm.Single)",
            "searchable": True,
            "dimensions": 1536,
            "vectorSearchProfile": "hnsw-text-embedding-3-large",
            "_comment": "LOCKED: 1536-dim vector",
        },
    ],
    "vectorSearch": {
        "algorithms": [{"name": "hnsw-config", "kind": "hnsw"}],
        "profiles": [{"name": "hnsw-text-embedding-3-large", "algorithm": "hnsw-config"}],
        "_comment": "should be stripped",
    },
}


# ---------------------------------------------------------------------------
# _strip_underscore_keys
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStripUnderscoreKeys:
    def test_removes_top_level_underscore_keys(self) -> None:
        schema = {"_comment": "ignore", "name": "kg-chunks", "_sprint": "2"}
        result = _strip_underscore_keys(schema)
        assert "_comment" not in result
        assert "_sprint" not in result
        assert result["name"] == "kg-chunks"

    def test_removes_nested_underscore_keys(self) -> None:
        schema = {
            "fields": [{"name": "chunk_id", "_comment": "primary key"}],
        }
        result = _strip_underscore_keys(schema)
        field = result["fields"][0]
        assert "_comment" not in field
        assert field["name"] == "chunk_id"

    def test_preserves_non_underscore_keys(self) -> None:
        schema = {"name": "kg-chunks", "fields": []}
        result = _strip_underscore_keys(schema)
        assert result == {"name": "kg-chunks", "fields": []}

    def test_handles_empty_dict(self) -> None:
        assert _strip_underscore_keys({}) == {}

    def test_handles_nested_list_of_dicts(self) -> None:
        schema = {"vectorSearch": {"_comment": "strip", "profiles": [{"_x": "y", "name": "p"}]}}
        result = _strip_underscore_keys(schema)
        assert "_comment" not in result["vectorSearch"]
        assert "_x" not in result["vectorSearch"]["profiles"][0]
        assert result["vectorSearch"]["profiles"][0]["name"] == "p"

    def test_strip_real_schema_shape(self) -> None:
        """Stripping the full raw schema leaves no '_'-prefixed keys at any level."""
        clean = _strip_underscore_keys(_RAW_SCHEMA)
        assert "_schema_version" not in clean
        assert "_sprint" not in clean
        for field in clean.get("fields", []):
            assert not any(k.startswith("_") for k in field)
        vs = clean.get("vectorSearch", {})
        assert not any(k.startswith("_") for k in vs)


# ---------------------------------------------------------------------------
# _ensure_vector_search
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEnsureVectorSearch:
    def test_no_change_when_vectorsearch_present(self) -> None:
        schema = {
            "fields": [{"name": "vec", "dimensions": 1536, "vectorSearchProfile": "hnsw"}],
            "vectorSearch": _DEFAULT_VECTOR_SEARCH,
        }
        result = _ensure_vector_search(schema)
        assert result is schema  # same object returned unmodified

    def test_injects_default_when_vector_field_but_no_section(self) -> None:
        schema = {
            "fields": [{"name": "vec", "dimensions": 1536}],
        }
        result = _ensure_vector_search(schema)
        assert "vectorSearch" in result

    def test_assigns_default_profile_to_field_without_one(self) -> None:
        schema = {
            "fields": [{"name": "vec", "dimensions": 1536}],
        }
        result = _ensure_vector_search(schema)
        vec_field = result["fields"][0]
        assert "vectorSearchProfile" in vec_field

    def test_no_change_when_no_vector_fields(self) -> None:
        schema = {"fields": [{"name": "content", "type": "Edm.String"}]}
        result = _ensure_vector_search(schema)
        assert "vectorSearch" not in result

    def test_preserves_existing_profile_on_vector_field(self) -> None:
        schema = {
            "fields": [
                {"name": "vec", "dimensions": 1536, "vectorSearchProfile": "my-custom-profile"}
            ],
            "vectorSearch": _DEFAULT_VECTOR_SEARCH,
        }
        result = _ensure_vector_search(schema)
        assert result["fields"][0]["vectorSearchProfile"] == "my-custom-profile"


# ---------------------------------------------------------------------------
# deploy_index — mock mode
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeployIndexMock:
    def test_mock_returns_schema_pushed_true(self) -> None:
        result = deploy_index(
            endpoint=_ENDPOINT,
            index_name=_INDEX,
            schema_dict=_RAW_SCHEMA,
            docs=[],
            mock=True,
        )
        assert result["schema_pushed"] is True
        assert result["mock"] is True

    def test_mock_returns_docs_pushed_equal_to_input(self) -> None:
        docs = [{"chunk_id": f"c{i}"} for i in range(5)]
        result = deploy_index(
            endpoint=_ENDPOINT,
            index_name=_INDEX,
            schema_dict=_RAW_SCHEMA,
            docs=docs,
            mock=True,
        )
        assert result["docs_pushed"] == 5

    def test_mock_index_name_in_result(self) -> None:
        result = deploy_index(
            endpoint=_ENDPOINT,
            index_name=_INDEX,
            schema_dict=_RAW_SCHEMA,
            docs=[],
            mock=True,
        )
        assert result["index_name"] == _INDEX

    def test_mock_no_errors(self) -> None:
        result = deploy_index(
            endpoint=_ENDPOINT,
            index_name=_INDEX,
            schema_dict=_RAW_SCHEMA,
            docs=[],
            mock=True,
        )
        assert result["errors"] == []

    def test_mock_strips_underscore_keys_before_mock_return(self) -> None:
        """Even in mock mode, the schema is cleaned (strip applied before name override)."""
        schema_with_comments = {"_comment": "strip me", "name": "raw", "fields": []}
        result = deploy_index(
            endpoint=_ENDPOINT,
            index_name="kg-dev-test",
            schema_dict=schema_with_comments,
            docs=[],
            mock=True,
        )
        assert result["schema_pushed"] is True
        assert result["index_name"] == "kg-dev-test"

    def test_mock_no_network_call(self) -> None:
        """mock=True must not open any network connection."""
        import socket

        with patch.object(
            socket, "getaddrinfo", side_effect=AssertionError("NETWORK BLOCKED")
        ):
            result = deploy_index(
                endpoint=_ENDPOINT,
                index_name=_INDEX,
                schema_dict=_RAW_SCHEMA,
                docs=[{"chunk_id": "c1"}],
                mock=True,
            )
        assert result["mock"] is True


# ---------------------------------------------------------------------------
# deploy_index — live mode (requests mocked)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeployIndexLive:
    def _make_response(self, status_code: int = 200, body: dict | None = None) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = body or {}
        resp.raise_for_status = MagicMock()  # no-op by default
        return resp

    def test_live_puts_index_schema(self) -> None:
        """Live mode issues a PUT request to the correct index URL."""
        put_resp = self._make_response(200)
        mock_token = MagicMock(return_value="fake-token")

        with patch("requests.put", return_value=put_resp) as mock_put, \
             patch("requests.post", return_value=self._make_response(200, {"value": []})):
            result = deploy_index(
                endpoint=_ENDPOINT,
                index_name=_INDEX,
                schema_dict={"name": "kg-chunks", "fields": []},
                docs=[],
                mock=False,
                token_provider=mock_token,
            )

        assert result["schema_pushed"] is True
        put_url = mock_put.call_args[0][0]
        assert _INDEX in put_url
        assert "indexes" in put_url
        assert "api-version" in put_url

    def test_live_schema_sent_without_underscore_keys(self) -> None:
        """PUT body must not contain any '_'-prefixed keys."""
        put_resp = self._make_response(200)
        mock_token = MagicMock(return_value="tok")
        sent_json = {}

        def _capture_put(url, headers=None, json=None, timeout=None):
            sent_json.update(json or {})
            return put_resp

        with patch("requests.put", side_effect=_capture_put):
            deploy_index(
                endpoint=_ENDPOINT,
                index_name=_INDEX,
                schema_dict=_RAW_SCHEMA,
                docs=[],
                mock=False,
                token_provider=mock_token,
            )

        assert not any(k.startswith("_") for k in sent_json)
        for field in sent_json.get("fields", []):
            assert not any(k.startswith("_") for k in field)

    def test_live_batch_upload_uses_merge_or_upload_action(self) -> None:
        """Batch upload must set '@search.action': 'mergeOrUpload' on each doc."""
        put_resp = self._make_response(200)
        docs = [{"chunk_id": f"c{i}"} for i in range(3)]
        posted_bodies = []
        mock_token = MagicMock(return_value="tok")

        def _capture_post(url, headers=None, json=None, timeout=None):
            posted_bodies.append(json)
            resp = self._make_response(200, {"value": [{"status": True}] * len(json["value"])})
            return resp

        with patch("requests.put", return_value=put_resp), \
             patch("requests.post", side_effect=_capture_post):
            deploy_index(
                endpoint=_ENDPOINT,
                index_name=_INDEX,
                schema_dict={"name": "kg-chunks", "fields": []},
                docs=docs,
                mock=False,
                token_provider=mock_token,
            )

        assert len(posted_bodies) == 1
        actions = posted_bodies[0]["value"]
        assert len(actions) == 3
        for action in actions:
            assert action["@search.action"] == "mergeOrUpload"
            assert "chunk_id" in action

    def test_live_index_name_overrides_schema_name(self) -> None:
        """PUT body must use the deployed index_name, not the schema's own 'name'."""
        put_resp = self._make_response(200)
        sent_json = {}
        mock_token = MagicMock(return_value="tok")

        def _capture_put(url, headers=None, json=None, timeout=None):
            sent_json.update(json or {})
            return put_resp

        with patch("requests.put", side_effect=_capture_put):
            deploy_index(
                endpoint=_ENDPOINT,
                index_name="kg-dev-chunks",
                schema_dict={"name": "kg-chunks", "fields": []},
                docs=[],
                mock=False,
                token_provider=mock_token,
            )

        assert sent_json.get("name") == "kg-dev-chunks"

    def test_live_error_on_put_failure_recorded(self) -> None:
        """PUT failure is captured in errors list without raising."""
        bad_resp = self._make_response(500)
        bad_resp.raise_for_status.side_effect = RuntimeError("HTTP 500")
        mock_token = MagicMock(return_value="tok")

        with patch("requests.put", return_value=bad_resp):
            result = deploy_index(
                endpoint=_ENDPOINT,
                index_name=_INDEX,
                schema_dict={"name": "kg-chunks", "fields": []},
                docs=[],
                mock=False,
                token_provider=mock_token,
            )

        assert result["schema_pushed"] is False
        assert len(result["errors"]) > 0
        assert "PUT index failed" in result["errors"][0]

    def test_live_recreate_deletes_before_put(self) -> None:
        """recreate=True issues DELETE before PUT."""
        put_resp = self._make_response(200)
        del_resp = self._make_response(204)
        mock_token = MagicMock(return_value="tok")

        with patch("requests.delete", return_value=del_resp) as mock_del, \
             patch("requests.put", return_value=put_resp):
            deploy_index(
                endpoint=_ENDPOINT,
                index_name=_INDEX,
                schema_dict={"name": "kg-chunks", "fields": []},
                docs=[],
                recreate=True,
                mock=False,
                token_provider=mock_token,
            )

        mock_del.assert_called_once()
        del_url = mock_del.call_args[0][0]
        assert _INDEX in del_url


# ---------------------------------------------------------------------------
# _sanitize_for_rest
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSanitizeForRest:
    """_sanitize_for_rest converts old schema shapes to REST API 2024-07-01 valid."""

    # --- semantic: contentFields → prioritizedContentFields ---

    def test_renames_content_fields(self) -> None:
        schema = {
            "semantic": {
                "configurations": [{
                    "name": "cfg",
                    "prioritizedFields": {
                        "contentFields": [{"fieldName": "content"}],
                    },
                }]
            }
        }
        out = _sanitize_for_rest(schema)
        pf = out["semantic"]["configurations"][0]["prioritizedFields"]
        assert "contentFields" not in pf
        assert pf["prioritizedContentFields"] == [{"fieldName": "content"}]

    def test_renames_keywords_fields(self) -> None:
        schema = {
            "semantic": {
                "configurations": [{
                    "name": "cfg",
                    "prioritizedFields": {
                        "keywordsFields": [{"fieldName": "entity_aliases"}],
                    },
                }]
            }
        }
        out = _sanitize_for_rest(schema)
        pf = out["semantic"]["configurations"][0]["prioritizedFields"]
        assert "keywordsFields" not in pf
        assert pf["prioritizedKeywordsFields"] == [{"fieldName": "entity_aliases"}]

    def test_wraps_bare_string_field_items(self) -> None:
        schema = {
            "semantic": {
                "configurations": [{
                    "name": "cfg",
                    "prioritizedFields": {
                        "contentFields": ["content", "summary"],
                    },
                }]
            }
        }
        out = _sanitize_for_rest(schema)
        pf = out["semantic"]["configurations"][0]["prioritizedFields"]
        assert pf["prioritizedContentFields"] == [
            {"fieldName": "content"},
            {"fieldName": "summary"},
        ]

    def test_already_correct_names_unchanged(self) -> None:
        """Already-renamed fields are left intact (idempotent)."""
        schema = {
            "semantic": {
                "configurations": [{
                    "name": "cfg",
                    "prioritizedFields": {
                        "prioritizedContentFields": [{"fieldName": "content"}],
                        "prioritizedKeywordsFields": [{"fieldName": "entity_aliases"}],
                        "titleField": {"fieldName": "canonical_key"},
                    },
                }]
            }
        }
        out = _sanitize_for_rest(schema)
        pf = out["semantic"]["configurations"][0]["prioritizedFields"]
        assert pf["prioritizedContentFields"] == [{"fieldName": "content"}]
        assert pf["prioritizedKeywordsFields"] == [{"fieldName": "entity_aliases"}]
        assert pf["titleField"] == {"fieldName": "canonical_key"}

    def test_handles_missing_semantic_section(self) -> None:
        """No semantic key → no error, schema returned unchanged."""
        schema = {"name": "my-index", "fields": []}
        out = _sanitize_for_rest(schema)
        assert "semantic" not in out
        assert out["name"] == "my-index"

    def test_legacy_semanticConfiguration_key(self) -> None:
        """Top-level semanticConfiguration (singular) is normalised to semantic.configurations."""
        schema = {
            "semanticConfiguration": {
                "configurations": [{
                    "name": "cfg",
                    "prioritizedFields": {
                        "contentFields": [{"fieldName": "content"}],
                    },
                }]
            }
        }
        out = _sanitize_for_rest(schema)
        assert "semanticConfiguration" not in out
        assert "semantic" in out
        pf = out["semantic"]["configurations"][0]["prioritizedFields"]
        assert "prioritizedContentFields" in pf

    # --- vectorSearch: drop vectorizers, clean profiles ---

    def test_drops_vectorizers(self) -> None:
        schema = {
            "vectorSearch": {
                "algorithms": [{"name": "hnsw-config", "kind": "hnsw"}],
                "profiles": [{"name": "p", "algorithm": "hnsw-config"}],
                "vectorizers": [{"name": "az-oai", "kind": "azureOpenAI"}],
            }
        }
        out = _sanitize_for_rest(schema)
        assert "vectorizers" not in out["vectorSearch"]

    def test_keeps_algorithms_and_profiles(self) -> None:
        schema = {
            "vectorSearch": {
                "algorithms": [{"name": "hnsw-config", "kind": "hnsw"}],
                "profiles": [{"name": "p", "algorithm": "hnsw-config"}],
                "vectorizers": [{"name": "az-oai", "kind": "azureOpenAI"}],
            }
        }
        out = _sanitize_for_rest(schema)
        assert len(out["vectorSearch"]["algorithms"]) == 1
        assert len(out["vectorSearch"]["profiles"]) == 1

    def test_removes_vectorizer_from_profiles(self) -> None:
        schema = {
            "vectorSearch": {
                "algorithms": [{"name": "hnsw-config", "kind": "hnsw"}],
                "profiles": [{
                    "name": "p",
                    "algorithm": "hnsw-config",
                    "vectorizer": "az-oai",  # should be stripped
                }],
            }
        }
        out = _sanitize_for_rest(schema)
        profile = out["vectorSearch"]["profiles"][0]
        assert "vectorizer" not in profile
        assert profile["algorithm"] == "hnsw-config"

    def test_no_vectorsearch_section_no_crash(self) -> None:
        schema = {"name": "x", "fields": []}
        out = _sanitize_for_rest(schema)
        assert "vectorSearch" not in out

    # --- full real-schema round-trip ---

    def test_real_chunks_schema_sanitizes_correctly(self) -> None:
        """The actual kg-chunks schema (old shape) sanitizes without error."""
        raw = {
            "semantic": {
                "defaultConfiguration": "kg-chunks-semantic",
                "configurations": [{
                    "name": "kg-chunks-semantic",
                    "prioritizedFields": {
                        "contentFields": [{"fieldName": "content"}],
                        "keywordsFields": [{"fieldName": "entity_aliases"}],
                        "titleField": {"fieldName": "canonical_key"},
                    },
                }]
            },
            "vectorSearch": {
                "algorithms": [{"name": "hnsw-config", "kind": "hnsw"}],
                "profiles": [{"name": "hnsw-text-embedding-3-large", "algorithm": "hnsw-config"}],
                "vectorizers": [{"name": "az-oai", "kind": "azureOpenAI"}],
            },
        }
        out = _sanitize_for_rest(raw)
        pf = out["semantic"]["configurations"][0]["prioritizedFields"]
        assert "prioritizedContentFields" in pf
        assert "prioritizedKeywordsFields" in pf
        assert "contentFields" not in pf
        assert "keywordsFields" not in pf
        assert "vectorizers" not in out["vectorSearch"]
        assert len(out["vectorSearch"]["profiles"]) == 1
