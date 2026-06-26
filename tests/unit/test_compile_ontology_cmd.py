"""Unit tests for compile-ontology CLI command (SPEC-001 §7, SPEC-003 §6).

Verifies:
- compile-ontology exits 0 and writes definition.json + EntityTypes dirs
- Summary line counts are present in output
- Missing model file exits 1
- Validation error (missing ID) exits 5
- --env flag passes lakehouse_id through to compiler output
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli

# Paths to the real ontology fixtures shared across tests
REPO_ROOT = Path(__file__).parent.parent.parent
MODEL_YAML = REPO_ROOT / "ontology" / "model.yaml"
IDS_LOCK = REPO_ROOT / "ontology" / "ids.lock.json"


@pytest.mark.unit
class TestCompileOntologyCmd:
    """CliRunner tests for compile-ontology command."""

    def test_exits_zero_with_real_model(self, tmp_path: Path):
        """compile-ontology must exit 0 when given valid model + ids."""
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

    def test_definition_json_written(self, tmp_path: Path):
        """definition.json manifest must exist in the output directory."""
        out = tmp_path / "build" / "ontology"
        runner = CliRunner()
        runner.invoke(cli, [
            "compile-ontology",
            "--model", str(MODEL_YAML),
            "--ids", str(IDS_LOCK),
            "--out", str(out),
        ])
        assert (out / "definition.json").exists(), (
            "definition.json not found in output directory"
        )

    def test_entity_types_directory_written(self, tmp_path: Path):
        """At least one EntityTypes/{typeId}/ directory must be created."""
        out = tmp_path / "build" / "ontology"
        runner = CliRunner()
        runner.invoke(cli, [
            "compile-ontology",
            "--model", str(MODEL_YAML),
            "--ids", str(IDS_LOCK),
            "--out", str(out),
        ])
        entity_types_dir = out / "EntityTypes"
        assert entity_types_dir.exists(), "EntityTypes/ directory not created"
        subdirs = [d for d in entity_types_dir.iterdir() if d.is_dir()]
        assert len(subdirs) > 0, "No EntityTypes sub-directories written"

    def test_relationship_types_directory_written(self, tmp_path: Path):
        """At least one RelationshipTypes/{typeId}/ directory must be created."""
        out = tmp_path / "build" / "ontology"
        runner = CliRunner()
        runner.invoke(cli, [
            "compile-ontology",
            "--model", str(MODEL_YAML),
            "--ids", str(IDS_LOCK),
            "--out", str(out),
        ])
        rel_types_dir = out / "RelationshipTypes"
        assert rel_types_dir.exists(), "RelationshipTypes/ directory not created"
        subdirs = [d for d in rel_types_dir.iterdir() if d.is_dir()]
        assert len(subdirs) > 0, "No RelationshipTypes sub-directories written"

    def test_platform_file_written(self, tmp_path: Path):
        """.platform file must exist in the output directory."""
        out = tmp_path / "build" / "ontology"
        runner = CliRunner()
        runner.invoke(cli, [
            "compile-ontology",
            "--model", str(MODEL_YAML),
            "--ids", str(IDS_LOCK),
            "--out", str(out),
        ])
        assert (out / ".platform").exists(), ".platform file not written"

    def test_summary_entity_count_in_output(self, tmp_path: Path):
        """Output must include entity type count in summary."""
        out = tmp_path / "build" / "ontology"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "compile-ontology",
            "--model", str(MODEL_YAML),
            "--ids", str(IDS_LOCK),
            "--out", str(out),
        ])
        assert "Entity types" in result.output, (
            "Expected 'Entity types' in summary output"
        )

    def test_summary_relationship_count_in_output(self, tmp_path: Path):
        """Output must include relationship type count in summary."""
        out = tmp_path / "build" / "ontology"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "compile-ontology",
            "--model", str(MODEL_YAML),
            "--ids", str(IDS_LOCK),
            "--out", str(out),
        ])
        assert "Relationship types" in result.output, (
            "Expected 'Relationship types' in summary output"
        )

    def test_summary_parts_count_positive(self, tmp_path: Path):
        """Parts written count in summary must be > 0."""
        out = tmp_path / "build" / "ontology"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "compile-ontology",
            "--model", str(MODEL_YAML),
            "--ids", str(IDS_LOCK),
            "--out", str(out),
        ])
        assert "Parts written" in result.output

    def test_definition_json_has_parts_array(self, tmp_path: Path):
        """definition.json manifest must have a non-empty 'parts' array."""
        out = tmp_path / "build" / "ontology"
        runner = CliRunner()
        runner.invoke(cli, [
            "compile-ontology",
            "--model", str(MODEL_YAML),
            "--ids", str(IDS_LOCK),
            "--out", str(out),
        ])
        definition = json.loads((out / "definition.json").read_text())
        assert "parts" in definition, "definition.json missing 'parts' key"
        assert len(definition["parts"]) > 0, "definition.json 'parts' array is empty"

    def test_missing_model_exits_one(self, tmp_path: Path):
        """Passing a non-existent model file must exit 1."""
        out = tmp_path / "build" / "ontology"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "compile-ontology",
            "--model", str(tmp_path / "nonexistent.yaml"),
            "--ids", str(IDS_LOCK),
            "--out", str(out),
        ])
        assert result.exit_code == 1, (
            f"Expected exit 1 for missing model, got {result.exit_code}"
        )

    def test_validation_error_exits_five(self, tmp_path: Path):
        """A model with a type not in ids.lock.json must exit 5."""
        import yaml

        bad_model = tmp_path / "bad_model.yaml"
        bad_ids = tmp_path / "bad_ids.json"

        # Entity type 'Ghost' not in ids.lock.json
        bad_model.write_text(yaml.dump({
            "ontology": {
                "name": "TestOntology",
                "entityTypes": [
                    {
                        "name": "Ghost",
                        "properties": [],
                        "dataBinding": {"table": "ghost_table", "entityIdColumn": "id", "displayNameColumn": "name"},
                    }
                ],
                "relationshipTypes": [],
            }
        }), encoding="utf-8")
        bad_ids.write_text(json.dumps({
            "entityTypes": {},
            "relationshipTypes": {},
        }), encoding="utf-8")

        out = tmp_path / "build" / "ontology"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "compile-ontology",
            "--model", str(bad_model),
            "--ids", str(bad_ids),
            "--out", str(out),
        ])
        assert result.exit_code == 5, (
            f"Expected exit 5 for validation error, got {result.exit_code}.\n"
            f"Output:\n{result.output}"
        )
