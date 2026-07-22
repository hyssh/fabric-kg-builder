"""Tests for semantic/artifact_validation.py — pure helpers and ArtifactFinding."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fabric_kg_builder.semantic.artifact_validation import (
    ArtifactFinding,
    SemanticArtifactValidationError,
    _canonical_object_hash,
    _load_json,
    _sha256,
)


class TestArtifactFinding:
    def test_fields(self):
        f = ArtifactFinding("ART-001", "Artifact file missing")
        assert f.code == "ART-001"
        assert f.message == "Artifact file missing"


class TestSemanticArtifactValidationError:
    def test_formats_message(self):
        findings = [
            ArtifactFinding("ART-001", "File missing"),
            ArtifactFinding("ART-002", "Hash mismatch"),
        ]
        err = SemanticArtifactValidationError(findings)
        assert "ART-001" in str(err)
        assert "ART-002" in str(err)
        assert err.findings == tuple(findings)

    def test_is_value_error(self):
        with pytest.raises(ValueError):
            raise SemanticArtifactValidationError([ArtifactFinding("E", "msg")])


class TestLoadJson:
    def test_missing_file_adds_finding(self, tmp_path):
        findings = []
        result = _load_json(tmp_path / "nonexistent.json", findings)
        assert result == {}
        assert len(findings) == 1
        assert "ARTIFACT_MISSING" in findings[0].code

    def test_valid_json_file(self, tmp_path):
        f = tmp_path / "data.json"
        f.write_text('{"key": "value"}')
        findings = []
        result = _load_json(f, findings)
        assert result == {"key": "value"}
        assert findings == []

    def test_invalid_json_adds_finding(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("not json content")
        findings = []
        result = _load_json(f, findings)
        assert result == {}
        assert len(findings) == 1
        assert "INVALID_JSON" in findings[0].code

    def test_non_dict_json_adds_finding(self, tmp_path):
        f = tmp_path / "array.json"
        f.write_text('[1, 2, 3]')
        findings = []
        result = _load_json(f, findings)
        assert result == {}
        assert len(findings) == 1
        assert "INVALID_SHAPE" in findings[0].code


class TestSha256:
    def test_returns_sha256_prefix(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_bytes(b"hello world")
        result = _sha256(f)
        assert result.startswith("sha256:")
        assert len(result) == 7 + 64  # sha256: + 64 hex chars

    def test_deterministic(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_bytes(b"hello world")
        assert _sha256(f) == _sha256(f)


class TestCanonicalObjectHash:
    def test_returns_sha256_prefix(self):
        h = _canonical_object_hash({"key": "value"})
        assert h.startswith("sha256:")

    def test_deterministic(self):
        h1 = _canonical_object_hash({"a": 1, "b": 2})
        h2 = _canonical_object_hash({"b": 2, "a": 1})
        assert h1 == h2  # sort_keys=True

    def test_different_values_different_hashes(self):
        h1 = _canonical_object_hash({"a": 1})
        h2 = _canonical_object_hash({"a": 2})
        assert h1 != h2
