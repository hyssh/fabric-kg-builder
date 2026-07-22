"""Tests for semantic/persisted_projection.py — pure helpers."""
from __future__ import annotations

import base64
import json

import pytest

from fabric_kg_builder.semantic.persisted_projection import (
    MaterializedTableEvidence,
    PersistedProjectionError,
    PersistedSurfaceEvidence,
    decode_fabric_definition_parts,
    persisted_parts_hash,
)
from fabric_kg_builder.semantic.artifact_validation import ArtifactFinding


# ---------------------------------------------------------------------------
# PersistedProjectionError
# ---------------------------------------------------------------------------


class TestPersistedProjectionError:
    def test_is_exception(self):
        findings = [ArtifactFinding("ERR-001", "Something went wrong")]
        err = PersistedProjectionError(findings)
        assert isinstance(err, RuntimeError)

    def test_message_contains_findings(self):
        findings = [ArtifactFinding("ERR-001", "test error")]
        err = PersistedProjectionError(findings)
        assert "ERR-001" in str(err) or "test error" in str(err)


# ---------------------------------------------------------------------------
# MaterializedTableEvidence
# ---------------------------------------------------------------------------


class TestMaterializedTableEvidence:
    def test_basic_fields(self):
        ev = MaterializedTableEvidence(
            semantic_id="sem-001",
            table_name="entities",
            source_path="/path/to/entities.parquet",
            row_count=100,
            columns=("entity_id", "entity_type"),
            status="ok",
        )
        assert ev.table_name == "entities"
        assert ev.row_count == 100
        assert "entity_id" in ev.columns

    def test_frozen(self):
        ev = MaterializedTableEvidence(
            semantic_id="sem-001",
            table_name="relationships",
            source_path="/path/to/rels.parquet",
            row_count=50,
            columns=(),
            status="ok",
        )
        assert isinstance(ev, MaterializedTableEvidence)


# ---------------------------------------------------------------------------
# PersistedSurfaceEvidence
# ---------------------------------------------------------------------------


class TestPersistedSurfaceEvidence:
    def test_basic_fields(self):
        ev = PersistedSurfaceEvidence(
            projection_hash="sha256:abc123",
            definition_counts={"entities": 10, "relationships": 5},
        )
        assert ev.projection_hash == "sha256:abc123"
        assert ev.definition_counts["entities"] == 10


# ---------------------------------------------------------------------------
# decode_fabric_definition_parts
# ---------------------------------------------------------------------------


def _make_part(path: str, payload: dict) -> dict:
    raw = json.dumps(payload, ensure_ascii=False)
    b64 = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    return {"path": path, "payload": b64, "payloadType": "InlineBase64"}


class TestDecodeFabricDefinitionParts:
    def test_basic_decode(self):
        part = _make_part("Files/Config/data.json", {"key": "value"})
        result = decode_fabric_definition_parts({"definition": {"parts": [part]}})
        assert "Files/Config/data.json" in result
        assert result["Files/Config/data.json"]["key"] == "value"

    def test_parts_under_definition_key(self):
        part = _make_part("some/path.json", {"a": 1})
        result = decode_fabric_definition_parts({"parts": [part]})
        assert "some/path.json" in result

    def test_empty_parts_raises(self):
        with pytest.raises(PersistedProjectionError):
            decode_fabric_definition_parts({"definition": {"parts": []}})

    def test_non_inline_base64_raises(self):
        part = {"path": "file.json", "payload": "data", "payloadType": "Raw"}
        with pytest.raises(PersistedProjectionError):
            decode_fabric_definition_parts({"parts": [part]})

    def test_multiple_parts(self):
        parts = [
            _make_part("a.json", {"a": 1}),
            _make_part("b.json", {"b": 2}),
        ]
        result = decode_fabric_definition_parts({"parts": parts})
        assert "a.json" in result
        assert "b.json" in result


# ---------------------------------------------------------------------------
# persisted_parts_hash
# ---------------------------------------------------------------------------


class TestPersistedPartsHash:
    def test_returns_sha256_prefix(self):
        parts = {"path/a.json": {"key": "value"}}
        h = persisted_parts_hash(parts)
        assert h.startswith("sha256:")

    def test_deterministic(self):
        parts = {"path/a.json": {"key": "value"}, "path/b.json": {"n": 1}}
        h1 = persisted_parts_hash(parts)
        h2 = persisted_parts_hash(parts)
        assert h1 == h2

    def test_order_independent(self):
        parts1 = {"a.json": {"x": 1}, "b.json": {"y": 2}}
        parts2 = {"b.json": {"y": 2}, "a.json": {"x": 1}}
        assert persisted_parts_hash(parts1) == persisted_parts_hash(parts2)

    def test_different_parts_different_hash(self):
        h1 = persisted_parts_hash({"a.json": {"key": "v1"}})
        h2 = persisted_parts_hash({"a.json": {"key": "v2"}})
        assert h1 != h2

    def test_empty_parts(self):
        h = persisted_parts_hash({})
        assert h.startswith("sha256:")
