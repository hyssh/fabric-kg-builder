"""Tests for bridge validation (SPEC-003 §12.9 BRG-001..010).

Verifies:
- Real model.yaml produces 0 bridge validation errors
- Compile-ontology CLI reports bridge-validation: OK for the shipped model
- Deliberately broken bindings (missing chunk_id column, missing blob_url,
  missing bridge relationship) are caught by the appropriate BRG gates
- BRG-009 warnings do not block the build (only errors do)
- A model with bridge errors causes compile-ontology to exit 5
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.ontology.bridge_validation import (
    BridgeViolation,
    validate_bridge,
)

# ---------------------------------------------------------------------------
# Paths to the real ontology fixtures
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent
MODEL_YAML = REPO_ROOT / "ontology" / "model.yaml"
IDS_LOCK = REPO_ROOT / "ontology" / "ids.lock.json"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_model() -> dict[str, Any]:
    raw = yaml.safe_load(MODEL_YAML.read_text(encoding="utf-8"))
    return raw.get("ontology", raw) if isinstance(raw, dict) else raw


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_minimal_model(**overrides: Any) -> dict[str, Any]:
    """Minimal valid model structure for targeted breakage tests."""
    base: dict[str, Any] = {
        "name": "TestOntology",
        "entityTypes": [
            {
                "name": "DocumentChunk",
                "module": "document-evidence",
                "properties": [
                    {"name": "display_name", "type": "string", "required": True},
                    {"name": "entity_id", "type": "string", "required": True},
                    {"name": "chunk_id", "type": "string", "required": True},
                    {"name": "related_entity_ids", "type": "string", "required": False},
                    {"name": "entity_search_keys", "type": "string", "required": False},
                ],
                "dataBinding": {
                    "table": "chunks",
                    "entityIdColumn": "chunk_id",
                    "displayNameColumn": "content",
                    "additionalColumns": [
                        {"property": "chunk_id", "column": "chunk_id"},
                        {"property": "related_entity_ids", "column": "related_entity_ids"},
                        {"property": "entity_search_keys", "column": "entity_search_keys"},
                    ],
                },
            },
            {
                "name": "SearchIndexRecord",
                "module": "retrieval",
                "properties": [
                    {"name": "display_name", "type": "string", "required": True},
                    {"name": "entity_id", "type": "string", "required": True},
                    {"name": "search_record_id", "type": "string", "required": True},
                ],
                "dataBinding": {
                    "table": "chunks",
                    "entityIdColumn": "chunk_id",
                    "displayNameColumn": "chunk_id",
                    "additionalColumns": [
                        {"property": "search_record_id", "column": "chunk_id"},
                    ],
                },
            },
            {
                "name": "Figure",
                "module": "document-evidence",
                "properties": [
                    {"name": "display_name", "type": "string", "required": True},
                    {"name": "blob_url", "type": "blob_url", "required": True},
                ],
                "dataBinding": {
                    "table": "document_elements",
                    "entityIdColumn": "document_element_id",
                    "displayNameColumn": "title",
                    "additionalColumns": [
                        {"property": "blob_url", "column": "blob_url"},
                    ],
                },
            },
            {
                "name": "ImageAsset",
                "module": "visual-evidence",
                "properties": [
                    {"name": "display_name", "type": "string", "required": True},
                    {"name": "blob_url", "type": "blob_url", "required": True},
                    {"name": "entity_id", "type": "string", "required": True},
                ],
                "dataBinding": {
                    "table": "visual_assets",
                    "entityIdColumn": "image_id",
                    "displayNameColumn": "caption",
                    "additionalColumns": [
                        {"property": "blob_url", "column": "blob_url"},
                        {"property": "entity_id", "column": "image_id"},
                    ],
                },
            },
            {
                "name": "Part",
                "module": "support-domain",
                "properties": [
                    {"name": "display_name", "type": "string", "required": True},
                    {"name": "entity_id", "type": "string", "required": True},
                    {"name": "canonical_key", "type": "string", "required": True},
                    {"name": "search_aliases", "type": "string", "required": False},
                ],
                "dataBinding": {
                    "table": "entities",
                    "entityIdColumn": "entity_id",
                    "displayNameColumn": "display_name",
                    "typeFilterColumn": "entity_type",
                    "typeFilterValue": "Part",
                    "additionalColumns": [
                        {"property": "canonical_key", "column": "canonical_key"},
                        {"property": "search_aliases", "column": "search_aliases"},
                    ],
                },
            },
        ],
        "relationshipTypes": [
            {
                "name": "evidenced_by",
                "module": "document-evidence",
                "sourceType": "Part",
                "targetType": "DocumentChunk",
                "inversePolicy": "none",
                "dataBinding": {
                    "table": "relationships",
                    "relationshipIdColumn": "relationship_id",
                    "sourceEntityIdColumn": "source_entity_id",
                    "targetEntityIdColumn": "target_entity_id",
                    "typeFilterColumn": "relationship_type",
                    "typeFilterValue": "evidenced_by",
                },
            },
            {
                "name": "shown_in",
                "module": "visual-evidence",
                "sourceType": "Part",
                "targetType": "Figure",
                "inversePolicy": "alias",
                "inverseName": "shows",
                "dataBinding": {
                    "table": "relationships",
                    "relationshipIdColumn": "relationship_id",
                    "sourceEntityIdColumn": "source_entity_id",
                    "targetEntityIdColumn": "target_entity_id",
                    "typeFilterColumn": "relationship_type",
                    "typeFilterValue": "shown_in",
                },
            },
            {
                "name": "indexed_as",
                "module": "retrieval",
                "sourceType": "DocumentChunk",
                "targetType": "SearchIndexRecord",
                "inversePolicy": "none",
                "dataBinding": {
                    "table": "relationships",
                    "relationshipIdColumn": "relationship_id",
                    "sourceEntityIdColumn": "source_entity_id",
                    "targetEntityIdColumn": "target_entity_id",
                    "typeFilterColumn": "relationship_type",
                    "typeFilterValue": "indexed_as",
                },
            },
        ],
    }
    base.update(overrides)
    return base


def _gate_ids(violations: list[BridgeViolation]) -> set[str]:
    return {v.gate_id for v in violations}


def _error_gate_ids(violations: list[BridgeViolation]) -> set[str]:
    return {v.gate_id for v in violations if v.severity == "error"}


# ---------------------------------------------------------------------------
# BRG-000: Real model produces 0 errors
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRealModelBridgeValidation:
    """The shipped model.yaml must produce zero bridge validation errors."""

    def test_real_model_zero_errors(self, real_model: dict[str, Any]) -> None:
        violations = validate_bridge(real_model)
        errors = [v for v in violations if v.severity == "error"]
        assert errors == [], (
            "Real model.yaml has bridge validation errors:\n"
            + "\n".join(f"  [{v.gate_id}] {v.message}" for v in errors)
        )

    def test_real_model_returns_violations_list(self, real_model: dict[str, Any]) -> None:
        result = validate_bridge(real_model)
        assert isinstance(result, list)

    def test_real_model_violations_are_bridge_violation_objects(self, real_model: dict[str, Any]) -> None:
        for v in validate_bridge(real_model):
            assert isinstance(v, BridgeViolation)
            assert v.severity in ("error", "warning")
            assert v.gate_id.startswith("BRG-")


# ---------------------------------------------------------------------------
# BRG-001: DocumentChunk required properties + column bindings
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBRG001:
    def test_missing_chunk_id_property_caught(self) -> None:
        model = _make_minimal_model()
        dc = next(e for e in model["entityTypes"] if e["name"] == "DocumentChunk")
        dc["properties"] = [p for p in dc["properties"] if p["name"] != "chunk_id"]
        dc["dataBinding"]["additionalColumns"] = [
            c for c in dc["dataBinding"]["additionalColumns"] if c["column"] != "chunk_id"
        ]
        violations = validate_bridge(model)
        assert "BRG-001" in _error_gate_ids(violations), (
            "Expected BRG-001 error for missing chunk_id property"
        )

    def test_missing_related_entity_ids_column_binding_caught(self) -> None:
        model = _make_minimal_model()
        dc = next(e for e in model["entityTypes"] if e["name"] == "DocumentChunk")
        dc["dataBinding"]["additionalColumns"] = [
            c for c in dc["dataBinding"]["additionalColumns"]
            if c["column"] != "related_entity_ids"
        ]
        violations = validate_bridge(model)
        assert "BRG-001" in _error_gate_ids(violations), (
            "Expected BRG-001 error for missing related_entity_ids column binding"
        )

    def test_missing_entity_search_keys_column_binding_caught(self) -> None:
        model = _make_minimal_model()
        dc = next(e for e in model["entityTypes"] if e["name"] == "DocumentChunk")
        dc["dataBinding"]["additionalColumns"] = [
            c for c in dc["dataBinding"]["additionalColumns"]
            if c["column"] != "entity_search_keys"
        ]
        violations = validate_bridge(model)
        assert "BRG-001" in _error_gate_ids(violations)

    def test_valid_document_chunk_no_brg001(self) -> None:
        model = _make_minimal_model()
        violations = validate_bridge(model)
        brg001_errors = [v for v in violations if v.gate_id == "BRG-001" and v.severity == "error"]
        assert brg001_errors == [], f"Unexpected BRG-001 errors: {brg001_errors}"


# ---------------------------------------------------------------------------
# BRG-002: SearchIndexRecord search_record_id
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBRG002:
    def test_missing_search_record_id_property_caught(self) -> None:
        model = _make_minimal_model()
        sir = next(e for e in model["entityTypes"] if e["name"] == "SearchIndexRecord")
        sir["properties"] = [p for p in sir["properties"] if p["name"] != "search_record_id"]
        violations = validate_bridge(model)
        assert "BRG-002" in _error_gate_ids(violations)

    def test_missing_chunk_id_column_binding_caught(self) -> None:
        model = _make_minimal_model()
        sir = next(e for e in model["entityTypes"] if e["name"] == "SearchIndexRecord")
        # Remove all references to chunk_id from the data binding
        sir["dataBinding"]["entityIdColumn"] = "record_id"
        sir["dataBinding"]["displayNameColumn"] = "record_id"
        sir["dataBinding"]["additionalColumns"] = []
        violations = validate_bridge(model)
        assert "BRG-002" in _error_gate_ids(violations)

    def test_valid_search_index_record_no_brg002(self) -> None:
        model = _make_minimal_model()
        brg002_errors = [v for v in validate_bridge(model) if v.gate_id == "BRG-002" and v.severity == "error"]
        assert brg002_errors == []


# ---------------------------------------------------------------------------
# BRG-003: support-domain entity required properties
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBRG003:
    def test_missing_entity_id_on_support_entity_caught(self) -> None:
        model = _make_minimal_model()
        part = next(e for e in model["entityTypes"] if e["name"] == "Part")
        part["properties"] = [p for p in part["properties"] if p["name"] != "entity_id"]
        violations = validate_bridge(model)
        assert "BRG-003" in _error_gate_ids(violations)

    def test_missing_canonical_key_on_support_entity_caught(self) -> None:
        model = _make_minimal_model()
        part = next(e for e in model["entityTypes"] if e["name"] == "Part")
        part["properties"] = [p for p in part["properties"] if p["name"] != "canonical_key"]
        violations = validate_bridge(model)
        assert "BRG-003" in _error_gate_ids(violations)

    def test_missing_search_aliases_on_support_entity_caught(self) -> None:
        model = _make_minimal_model()
        part = next(e for e in model["entityTypes"] if e["name"] == "Part")
        part["properties"] = [p for p in part["properties"] if p["name"] != "search_aliases"]
        violations = validate_bridge(model)
        assert "BRG-003" in _error_gate_ids(violations)

    def test_non_support_domain_entity_not_checked(self) -> None:
        """DocumentChunk (document-evidence module) missing canonical_key is NOT BRG-003."""
        model = _make_minimal_model()
        dc = next(e for e in model["entityTypes"] if e["name"] == "DocumentChunk")
        assert dc["module"] != "support-domain"
        # No BRG-003 violation expected for non-support-domain entity
        violations = [v for v in validate_bridge(model) if v.gate_id == "BRG-003"]
        error_msgs = [v.message for v in violations if "DocumentChunk" in v.message and v.severity == "error"]
        assert error_msgs == []


# ---------------------------------------------------------------------------
# BRG-004: blob_url on ImageAsset and Figure
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBRG004:
    def test_missing_blob_url_on_image_asset_caught(self) -> None:
        model = _make_minimal_model()
        ia = next(e for e in model["entityTypes"] if e["name"] == "ImageAsset")
        ia["properties"] = [p for p in ia["properties"] if p["name"] != "blob_url"]
        ia["dataBinding"]["additionalColumns"] = [
            c for c in ia["dataBinding"]["additionalColumns"] if c["property"] != "blob_url"
        ]
        violations = validate_bridge(model)
        assert "BRG-004" in _error_gate_ids(violations)

    def test_missing_blob_url_on_figure_caught(self) -> None:
        model = _make_minimal_model()
        fig = next(e for e in model["entityTypes"] if e["name"] == "Figure")
        fig["properties"] = [p for p in fig["properties"] if p["name"] != "blob_url"]
        fig["dataBinding"]["additionalColumns"] = []
        violations = validate_bridge(model)
        assert "BRG-004" in _error_gate_ids(violations)

    def test_wrong_blob_url_type_caught(self) -> None:
        model = _make_minimal_model()
        ia = next(e for e in model["entityTypes"] if e["name"] == "ImageAsset")
        for p in ia["properties"]:
            if p["name"] == "blob_url":
                p["type"] = "string"  # wrong type — should be "blob_url"
        violations = validate_bridge(model)
        assert "BRG-004" in _error_gate_ids(violations)


# ---------------------------------------------------------------------------
# BRG-005: Bridge relationships exist with inversePolicy
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBRG005:
    def test_missing_evidenced_by_caught(self) -> None:
        model = _make_minimal_model()
        model["relationshipTypes"] = [
            r for r in model["relationshipTypes"] if r["name"] != "evidenced_by"
        ]
        violations = validate_bridge(model)
        assert "BRG-005" in _error_gate_ids(violations)

    def test_missing_shown_in_caught(self) -> None:
        model = _make_minimal_model()
        model["relationshipTypes"] = [
            r for r in model["relationshipTypes"] if r["name"] != "shown_in"
        ]
        violations = validate_bridge(model)
        assert "BRG-005" in _error_gate_ids(violations)

    def test_missing_indexed_as_caught(self) -> None:
        model = _make_minimal_model()
        model["relationshipTypes"] = [
            r for r in model["relationshipTypes"] if r["name"] != "indexed_as"
        ]
        violations = validate_bridge(model)
        assert "BRG-005" in _error_gate_ids(violations)

    def test_missing_inverse_policy_caught(self) -> None:
        model = _make_minimal_model()
        for rt in model["relationshipTypes"]:
            if rt["name"] == "indexed_as":
                del rt["inversePolicy"]
        violations = validate_bridge(model)
        assert "BRG-005" in _error_gate_ids(violations)

    def test_all_bridge_rels_present_no_brg005(self) -> None:
        model = _make_minimal_model()
        brg005_errors = [v for v in validate_bridge(model) if v.gate_id == "BRG-005" and v.severity == "error"]
        assert brg005_errors == []


# ---------------------------------------------------------------------------
# BRG-006 / BRG-007 / BRG-008: Bridge relationship resolution
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBRG006_BRG007_BRG008:
    def test_brg007_missing_chunk_id_on_evidenced_by_target_caught(self) -> None:
        """evidenced_by target (DocumentChunk) missing chunk_id → BRG-007."""
        model = _make_minimal_model()
        dc = next(e for e in model["entityTypes"] if e["name"] == "DocumentChunk")
        dc["properties"] = [p for p in dc["properties"] if p["name"] != "chunk_id"]
        dc["dataBinding"]["additionalColumns"] = [
            c for c in dc["dataBinding"]["additionalColumns"] if c["column"] != "chunk_id"
        ]
        violations = validate_bridge(model)
        assert "BRG-007" in _error_gate_ids(violations)

    def test_brg006_missing_chunk_id_binding_on_indexed_as_target_caught(self) -> None:
        """indexed_as target (SearchIndexRecord) missing chunk_id binding → BRG-006."""
        model = _make_minimal_model()
        sir = next(e for e in model["entityTypes"] if e["name"] == "SearchIndexRecord")
        # Remove all references to chunk_id from the data binding
        sir["dataBinding"]["entityIdColumn"] = "record_id"
        sir["dataBinding"]["displayNameColumn"] = "record_id"
        sir["dataBinding"]["additionalColumns"] = []
        violations = validate_bridge(model)
        assert "BRG-006" in _error_gate_ids(violations)

    def test_brg008_missing_blob_url_binding_on_shown_in_target_caught(self) -> None:
        """shown_in target (Figure) missing blob_url binding → BRG-008."""
        model = _make_minimal_model()
        fig = next(e for e in model["entityTypes"] if e["name"] == "Figure")
        fig["properties"] = [p for p in fig["properties"] if p["name"] != "blob_url"]
        fig["dataBinding"]["additionalColumns"] = []
        violations = validate_bridge(model)
        assert "BRG-008" in _error_gate_ids(violations)

    def test_valid_model_no_brg006_007_008(self) -> None:
        model = _make_minimal_model()
        violations = validate_bridge(model)
        bad = [v for v in violations if v.gate_id in ("BRG-006", "BRG-007", "BRG-008") and v.severity == "error"]
        assert bad == [], f"Unexpected bridge resolution errors: {bad}"


# ---------------------------------------------------------------------------
# BRG-009: Warning for unlinked support-domain entities
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBRG009:
    def test_brg009_is_warning_not_error(self) -> None:
        """BRG-009 violations must be warnings, never errors."""
        model = _make_minimal_model()
        brg009 = [v for v in validate_bridge(model) if v.gate_id == "BRG-009"]
        for v in brg009:
            assert v.severity == "warning", f"BRG-009 violation must be warning, got: {v}"

    def test_brg009_does_not_block_minimal_model(self) -> None:
        """Warnings in BRG-009 must not produce errors."""
        model = _make_minimal_model()
        errors = [v for v in validate_bridge(model) if v.severity == "error"]
        assert errors == []


# ---------------------------------------------------------------------------
# BRG-010: entity_id column binding on support-domain nodes
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBRG010:
    def test_empty_entity_id_column_caught(self) -> None:
        model = _make_minimal_model()
        part = next(e for e in model["entityTypes"] if e["name"] == "Part")
        part["dataBinding"]["entityIdColumn"] = ""  # empty binding
        violations = validate_bridge(model)
        assert "BRG-010" in _error_gate_ids(violations)

    def test_valid_entity_id_column_no_brg010(self) -> None:
        model = _make_minimal_model()
        brg010_errors = [v for v in validate_bridge(model) if v.gate_id == "BRG-010" and v.severity == "error"]
        assert brg010_errors == []


# ---------------------------------------------------------------------------
# CLI integration: compile-ontology reports bridge-validation result
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCompileOntologyBridgeValidationCLI:
    """CLI integration tests for bridge validation wired into compile-ontology."""

    def test_real_model_bridge_validation_ok_in_output(self, tmp_path: Path) -> None:
        """compile-ontology must report bridge-validation OK for the shipped model."""
        out = tmp_path / "build" / "ontology"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "compile-ontology",
            "--model", str(MODEL_YAML),
            "--ids", str(IDS_LOCK),
            "--out", str(out),
        ])
        assert result.exit_code == 0, (
            f"Expected exit 0, got {result.exit_code}.\nOutput:\n{result.output}"
        )
        assert "Bridge validation" in result.output, (
            "Expected 'Bridge validation' line in compile-ontology output"
        )

    def test_real_model_exits_zero_with_bridge_validation(self, tmp_path: Path) -> None:
        """compile-ontology exits 0 when bridge validation passes."""
        out = tmp_path / "build" / "ontology"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "compile-ontology",
            "--model", str(MODEL_YAML),
            "--ids", str(IDS_LOCK),
            "--out", str(out),
        ])
        assert result.exit_code == 0

    def test_broken_bridge_binding_causes_exit_five(self, tmp_path: Path) -> None:
        """A model with a broken bridge binding (missing chunk_id column) must exit 5."""
        # Build a broken model: DocumentChunk missing related_entity_ids binding
        broken_model = {
            "ontology": {
                "name": "BrokenOntology",
                "entityTypes": [
                    {
                        "name": "DocumentChunk",
                        "module": "document-evidence",
                        "properties": [
                            {"name": "display_name", "type": "string", "required": True},
                            # Intentionally MISSING: chunk_id, related_entity_ids, entity_search_keys
                            # → triggers BRG-001, BRG-007
                        ],
                        "dataBinding": {
                            "table": "chunks",
                            "entityIdColumn": "chunk_id",
                            "displayNameColumn": "content",
                        },
                    },
                    {
                        "name": "SearchIndexRecord",
                        "module": "retrieval",
                        "properties": [
                            {"name": "display_name", "type": "string", "required": True},
                            {"name": "entity_id", "type": "string", "required": True},
                            {"name": "search_record_id", "type": "string", "required": True},
                        ],
                        "dataBinding": {
                            "table": "chunks",
                            "entityIdColumn": "chunk_id",
                            "displayNameColumn": "chunk_id",
                            "additionalColumns": [
                                {"property": "search_record_id", "column": "chunk_id"},
                            ],
                        },
                    },
                    {
                        "name": "Figure",
                        "module": "document-evidence",
                        "properties": [
                            {"name": "display_name", "type": "string", "required": True},
                            {"name": "blob_url", "type": "blob_url", "required": True},
                        ],
                        "dataBinding": {
                            "table": "document_elements",
                            "entityIdColumn": "document_element_id",
                            "displayNameColumn": "title",
                            "additionalColumns": [
                                {"property": "blob_url", "column": "blob_url"},
                            ],
                        },
                    },
                    {
                        "name": "ImageAsset",
                        "module": "visual-evidence",
                        "properties": [
                            {"name": "display_name", "type": "string", "required": True},
                            {"name": "blob_url", "type": "blob_url", "required": True},
                            {"name": "entity_id", "type": "string", "required": True},
                        ],
                        "dataBinding": {
                            "table": "visual_assets",
                            "entityIdColumn": "image_id",
                            "displayNameColumn": "caption",
                            "additionalColumns": [
                                {"property": "blob_url", "column": "blob_url"},
                            ],
                        },
                    },
                    {
                        "name": "Part",
                        "module": "support-domain",
                        "properties": [
                            {"name": "display_name", "type": "string", "required": True},
                            {"name": "entity_id", "type": "string", "required": True},
                            {"name": "canonical_key", "type": "string", "required": True},
                            {"name": "search_aliases", "type": "string", "required": False},
                        ],
                        "dataBinding": {
                            "table": "entities",
                            "entityIdColumn": "entity_id",
                            "displayNameColumn": "display_name",
                            "typeFilterColumn": "entity_type",
                            "typeFilterValue": "Part",
                            "additionalColumns": [
                                {"property": "canonical_key", "column": "canonical_key"},
                                {"property": "search_aliases", "column": "search_aliases"},
                            ],
                        },
                    },
                ],
                "relationshipTypes": [
                    {
                        "name": "evidenced_by",
                        "module": "document-evidence",
                        "sourceType": "Part",
                        "targetType": "DocumentChunk",
                        "inversePolicy": "none",
                        "dataBinding": {
                            "table": "relationships",
                            "relationshipIdColumn": "relationship_id",
                            "sourceEntityIdColumn": "source_entity_id",
                            "targetEntityIdColumn": "target_entity_id",
                            "typeFilterColumn": "relationship_type",
                            "typeFilterValue": "evidenced_by",
                        },
                    },
                    {
                        "name": "shown_in",
                        "module": "visual-evidence",
                        "sourceType": "Part",
                        "targetType": "Figure",
                        "inversePolicy": "alias",
                        "inverseName": "shows",
                        "dataBinding": {
                            "table": "relationships",
                            "relationshipIdColumn": "relationship_id",
                            "sourceEntityIdColumn": "source_entity_id",
                            "targetEntityIdColumn": "target_entity_id",
                            "typeFilterColumn": "relationship_type",
                            "typeFilterValue": "shown_in",
                        },
                    },
                    {
                        "name": "indexed_as",
                        "module": "retrieval",
                        "sourceType": "DocumentChunk",
                        "targetType": "SearchIndexRecord",
                        "inversePolicy": "none",
                        "dataBinding": {
                            "table": "relationships",
                            "relationshipIdColumn": "relationship_id",
                            "sourceEntityIdColumn": "source_entity_id",
                            "targetEntityIdColumn": "target_entity_id",
                            "typeFilterColumn": "relationship_type",
                            "typeFilterValue": "indexed_as",
                        },
                    },
                ],
            }
        }

        # Write broken model to temp path
        broken_model_path = tmp_path / "broken_model.yaml"
        broken_model_path.write_text(yaml.dump(broken_model), encoding="utf-8")

        # Build minimal ids.lock for the broken model
        broken_ids = {
            "entityTypes": {
                "DocumentChunk": "1000000000000000101",
                "SearchIndexRecord": "1000000000000000203",
                "Figure": "1000000000000000108",
                "ImageAsset": "1000000000000000112",
                "Part": "1000000000000000004",
            },
            "relationshipTypes": {
                "evidenced_by": "2000000000000000108",
                "shown_in": "2000000000000000109",
                "indexed_as": "2000000000000000117",
            },
        }
        broken_ids_path = tmp_path / "broken_ids.json"
        broken_ids_path.write_text(json.dumps(broken_ids), encoding="utf-8")

        out = tmp_path / "build" / "ontology"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "compile-ontology",
            "--model", str(broken_model_path),
            "--ids", str(broken_ids_path),
            "--out", str(out),
        ])
        assert result.exit_code == 5, (
            f"Expected exit 5 for bridge validation errors, got {result.exit_code}.\n"
            f"Output:\n{result.output}"
        )

    def test_summary_includes_bridge_validation_line(self, tmp_path: Path) -> None:
        """Summary section must include bridge-validation count."""
        out = tmp_path / "build" / "ontology"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "compile-ontology",
            "--model", str(MODEL_YAML),
            "--ids", str(IDS_LOCK),
            "--out", str(out),
        ])
        assert "Bridge validation" in result.output

    def test_full_tree_still_produced_with_bridge_validation(self, tmp_path: Path) -> None:
        """compile-ontology must still produce the full Fabric definition tree."""
        out = tmp_path / "build" / "ontology"
        runner = CliRunner()
        runner.invoke(cli, [
            "compile-ontology",
            "--model", str(MODEL_YAML),
            "--ids", str(IDS_LOCK),
            "--out", str(out),
        ])
        assert (out / "definition.json").exists()
        assert (out / ".platform").exists()
        assert (out / "EntityTypes").is_dir()
        assert (out / "RelationshipTypes").is_dir()

        # InlineBase64 parts present in definition.json
        definition = json.loads((out / "definition.json").read_text())
        assert len(definition.get("parts", [])) > 0
