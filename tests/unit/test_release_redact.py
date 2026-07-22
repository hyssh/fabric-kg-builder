"""Tests for release/redact.py — evidence and ledger redaction."""

from __future__ import annotations

import pytest

from fabric_kg_builder.release.redact import (
    _REDACTED_PLACEHOLDER,
    _SOURCE_CONTENT_FIELDS,
    _looks_like_secret,
    assert_no_secrets,
    assert_no_source_content,
    redact_dict,
    redact_evidence_manifest,
    redact_ledger,
    redact_value,
)


# ---------------------------------------------------------------------------
# _looks_like_secret
# ---------------------------------------------------------------------------


class TestLooksLikeSecret:
    def test_sas_token_detected(self) -> None:
        value = "sv=2021-08-06&se=2024-01-01T00:00:00Z&sp=r"
        assert _looks_like_secret(value)

    def test_account_key_detected(self) -> None:
        value = "AccountKey=" + "A" * 40
        assert _looks_like_secret(value)

    def test_api_key_detected(self) -> None:
        value = "api_key=" + "X" * 25
        assert _looks_like_secret(value)

    def test_bearer_token_detected(self) -> None:
        # A 32-char alphanumeric token (e.g., Cognitive Services key)
        value = "a" * 32  # matches the 32-char pattern at end of string
        assert _looks_like_secret(value)

    def test_api_key_detected(self) -> None:
        value = "api_key=" + "X" * 25
        assert _looks_like_secret(value)

    def test_normal_string_not_secret(self) -> None:
        assert not _looks_like_secret("hello world")
        assert not _looks_like_secret("some-resource-id")

    def test_arm_resource_id_not_secret(self) -> None:
        value = "/subscriptions/sub-id/resourceGroups/rg/providers/Microsoft.Fabric/workspaces/ws"
        assert not _looks_like_secret(value)

    def test_timestamp_not_secret(self) -> None:
        assert not _looks_like_secret("2024-01-01T00:00:00Z")

    def test_blob_uri_not_secret(self) -> None:
        assert not _looks_like_secret("https://myaccount.blob.core.windows.net/container/blob.json")


# ---------------------------------------------------------------------------
# redact_value
# ---------------------------------------------------------------------------


class TestRedactValue:
    def test_source_content_field_always_redacted(self) -> None:
        for field_name in ["content", "text", "source_text", "raw_text", "body"]:
            result = redact_value(field_name, "some customer content")
            assert result == _REDACTED_PLACEHOLDER

    def test_secret_value_redacted(self) -> None:
        result = redact_value("my_key", "AccountKey=" + "A" * 40)
        assert result == _REDACTED_PLACEHOLDER

    def test_non_secret_value_preserved(self) -> None:
        result = redact_value("status", "active")
        assert result == "active"

    def test_integer_value_preserved(self) -> None:
        result = redact_value("count", 42)
        assert result == 42

    def test_boolean_preserved(self) -> None:
        result = redact_value("enabled", True)
        assert result is True

    def test_none_preserved(self) -> None:
        result = redact_value("field", None)
        assert result is None

    def test_blob_uri_preserved(self) -> None:
        uri = "https://storage.blob.core.windows.net/cont/file.json"
        result = redact_value("url", uri)
        assert result == uri


# ---------------------------------------------------------------------------
# redact_dict
# ---------------------------------------------------------------------------


class TestRedactDict:
    def test_source_content_nested(self) -> None:
        data = {"content": "customer data here"}
        result = redact_dict(data)
        assert result["content"] == _REDACTED_PLACEHOLDER

    def test_nested_dict_redacted(self) -> None:
        data = {"inner": {"text": "secret text"}}
        result = redact_dict(data)
        assert result["inner"]["text"] == _REDACTED_PLACEHOLDER

    def test_list_of_dicts_redacted(self) -> None:
        data = {"items": [{"text": "item text"}]}
        result = redact_dict(data)
        assert result["items"][0]["text"] == _REDACTED_PLACEHOLDER

    def test_non_sensitive_fields_preserved(self) -> None:
        data = {"status": "active", "count": 5, "resource_id": "res-001"}
        result = redact_dict(data)
        assert result == data

    def test_does_not_modify_original(self) -> None:
        data = {"content": "original"}
        redact_dict(data)
        assert data["content"] == "original"

    def test_list_of_strings_checked(self) -> None:
        # List items that are strings get redacted if secret
        data = {"keys": ["AccountKey=" + "A" * 40, "normal"]}
        result = redact_dict(data)
        assert result["keys"][0] == _REDACTED_PLACEHOLDER
        assert result["keys"][1] == "normal"


# ---------------------------------------------------------------------------
# redact_evidence_manifest / redact_ledger
# ---------------------------------------------------------------------------


class TestRedactManifest:
    def test_manifest_content_redacted(self) -> None:
        manifest = {
            "evidence_id": "ev-001",
            "status": "pass",
            "source_text": "customer content",
        }
        result = redact_evidence_manifest(manifest)
        assert result["source_text"] == _REDACTED_PLACEHOLDER
        assert result["evidence_id"] == "ev-001"

    def test_ledger_content_redacted(self) -> None:
        ledger = {
            "resource_id": "res-001",
            "kind": "KGOntology",
            "raw_text": "internal content",
        }
        result = redact_ledger(ledger)
        assert result["raw_text"] == _REDACTED_PLACEHOLDER
        assert result["resource_id"] == "res-001"


# ---------------------------------------------------------------------------
# assert_no_source_content / assert_no_secrets
# ---------------------------------------------------------------------------


class TestAssertNoSourceContent:
    def test_passes_for_clean_data(self) -> None:
        data = {"status": "ok", "resource_id": "res-001"}
        assert_no_source_content(data)  # should not raise

    def test_raises_for_content_field(self) -> None:
        data = {"content": "some text here"}
        with pytest.raises(AssertionError, match="content"):
            assert_no_source_content(data)

    def test_nested_content_detected(self) -> None:
        data = {"inner": {"text": "customer text"}}
        with pytest.raises(AssertionError, match="text"):
            assert_no_source_content(data)

    def test_redacted_placeholder_does_not_raise(self) -> None:
        data = {"content": _REDACTED_PLACEHOLDER}
        assert_no_source_content(data)  # should not raise (already redacted)

    def test_list_nested_checked(self) -> None:
        data = {"items": [{"source_text": "leaked"}]}
        with pytest.raises(AssertionError):
            assert_no_source_content(data)


class TestAssertNoSecrets:
    def test_passes_for_clean_data(self) -> None:
        data = {"status": "ok", "resource_id": "res-001"}
        assert_no_secrets(data)  # should not raise

    def test_raises_for_account_key(self) -> None:
        data = {"connection": "AccountKey=" + "B" * 40}
        with pytest.raises(AssertionError, match="secret"):
            assert_no_secrets(data)

    def test_nested_secret_detected(self) -> None:
        data = {"config": {"api_key": "api_key=" + "X" * 25}}
        with pytest.raises(AssertionError):
            assert_no_secrets(data)

    def test_list_secret_detected(self) -> None:
        data = {"tokens": ["AccountKey=" + "B" * 40]}
        with pytest.raises(AssertionError):
            assert_no_secrets(data)
