"""Tests for runtime/executors.py — pure helper functions."""
from __future__ import annotations

import pytest

from fabric_kg_builder.runtime.executors import (
    _failure,
    _remote_error_message,
    _request_ids,
    _source_locator,
    _utc_now,
)


class TestUtcNow:
    def test_returns_string(self):
        result = _utc_now()
        assert isinstance(result, str)

    def test_has_timezone_marker(self):
        result = _utc_now()
        assert "+" in result or "Z" in result or "T" in result


class TestRequestIds:
    def test_empty_headers(self):
        assert _request_ids(None) == []

    def test_extracts_request_id_header(self):
        headers = {"x-ms-request-id": "req-123"}
        result = _request_ids(headers)
        assert "req-123" in result

    def test_case_insensitive_header_key(self):
        headers = {"X-MS-REQUEST-ID": "req-456"}
        result = _request_ids(headers)
        assert "req-456" in result

    def test_multiple_headers(self):
        headers = {
            "x-ms-request-id": "req-1",
            "apim-request-id": "req-2",
        }
        result = _request_ids(headers)
        assert "req-1" in result
        assert "req-2" in result

    def test_body_requestId(self):
        result = _request_ids({}, body={"requestId": "body-req-1"})
        assert "body-req-1" in result

    def test_body_correlationId(self):
        result = _request_ids({}, body={"correlationId": "corr-1"})
        assert "corr-1" in result

    def test_deduplicates(self):
        headers = {"x-ms-request-id": "req-1"}
        result = _request_ids(headers, body={"requestId": "req-1"})
        assert result.count("req-1") == 1

    def test_non_dict_body_ignored(self):
        result = _request_ids({}, body="not a dict")
        assert result == []


class TestFailure:
    def test_basic_failure_string(self):
        result = _failure(
            error="Something went wrong",
            remediation="Try again later",
            elapsed_ms=123.0,
        )
        assert result["status"] == "failed"
        assert result["error_message"] == "Something went wrong"
        assert result["remediation"] == "Try again later"
        assert result["latency_ms"] == 123.0

    def test_failure_with_exception(self):
        err = ValueError("Invalid value")
        result = _failure(
            error=err,
            remediation="Check input",
            elapsed_ms=50.0,
        )
        assert result["error_type"] == "ValueError"
        assert result["status"] == "failed"

    def test_failure_with_http_status(self):
        result = _failure(
            error="Unauthorized",
            remediation="Check credentials",
            elapsed_ms=10.0,
            http_status=401,
        )
        assert result["http_status"] == 401

    def test_failure_has_timestamp(self):
        result = _failure(error="err", remediation="fix", elapsed_ms=1.0)
        assert "timestamp_utc" in result
        assert isinstance(result["timestamp_utc"], str)

    def test_failure_request_ids(self):
        result = _failure(
            error="err",
            remediation="fix",
            elapsed_ms=1.0,
            request_ids=["req-1", "req-2"],
        )
        assert result["request_ids"] == ["req-1", "req-2"]


class TestRemoteErrorMessage:
    def test_non_dict_returns_none(self):
        assert _remote_error_message("not a dict") is None
        assert _remote_error_message(None) is None
        assert _remote_error_message(42) is None

    def test_with_error_code_and_message(self):
        body = {"error": {"code": "Unauthorized", "message": "Access denied"}}
        result = _remote_error_message(body)
        assert "Unauthorized" in result
        assert "Access denied" in result

    def test_top_level_code_message(self):
        body = {"code": "NotFound", "message": "Resource not found"}
        result = _remote_error_message(body)
        assert "NotFound" in result

    def test_empty_dict(self):
        result = _remote_error_message({})
        assert result is None

    def test_code_only(self):
        body = {"error": {"code": "Forbidden"}}
        result = _remote_error_message(body)
        assert result == "Forbidden"

    def test_message_only(self):
        body = {"error": {"message": "Something failed"}}
        result = _remote_error_message(body)
        assert result == "Something failed"


class TestSourceLocator:
    def test_plain_string(self):
        assert _source_locator("https://blob.example.com/file.pdf") == "https://blob.example.com/file.pdf"

    def test_empty_string_returns_none(self):
        assert _source_locator("") is None

    def test_whitespace_returns_none(self):
        assert _source_locator("   ") is None

    def test_json_string_with_blob_uri(self):
        import json
        payload = {"blob_uri": "https://blob.example.com/file.pdf"}
        result = _source_locator(json.dumps(payload))
        assert result == "https://blob.example.com/file.pdf"

    def test_json_string_with_blob_url(self):
        import json
        payload = {"blob_url": "https://blob.example.com/file.pdf"}
        result = _source_locator(json.dumps(payload))
        assert result == "https://blob.example.com/file.pdf"

    def test_dict_with_blob_uri(self):
        result = _source_locator({"blob_uri": "https://blob.example.com/f.pdf"})
        assert result == "https://blob.example.com/f.pdf"

    def test_dict_without_known_keys(self):
        result = _source_locator({"unknown_key": "value"})
        assert result is None

    def test_none_value(self):
        result = _source_locator(None)
        assert result is None

    def test_source_uri_key(self):
        result = _source_locator({"source_uri": "https://source.example.com/f.pdf"})
        assert result == "https://source.example.com/f.pdf"

    def test_landing_path_key(self):
        result = _source_locator({"landing_path": "raw/asset/original/file.pdf"})
        assert result == "raw/asset/original/file.pdf"
