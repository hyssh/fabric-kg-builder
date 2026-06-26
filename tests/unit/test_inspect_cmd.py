"""Unit tests for fabric-kg inspect-source command — SPEC-001 §7."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from tests.conftest import combined_output, make_cli_runner

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SAMPLE_CSV = Path(__file__).parent.parent.parent / "examples" / "csv" / "sample.csv"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestInspectSourceCmd:
    """CliRunner-based tests for inspect-source command."""

    def test_sample_csv_exits_zero(self) -> None:
        """inspect-source on a valid CSV must exit 0."""
        runner = CliRunner()
        result = runner.invoke(cli, ["inspect-source", "--input", str(SAMPLE_CSV)])
        assert result.exit_code == 0, (
            f"Expected exit 0, got {result.exit_code}.\nOutput:\n{result.output}"
        )

    def test_output_contains_column_names(self) -> None:
        """Table output must include each column name from sample.csv."""
        runner = CliRunner()
        result = runner.invoke(cli, ["inspect-source", "--input", str(SAMPLE_CSV)])
        assert result.exit_code == 0
        for col in ("device_id", "device_name", "component", "part_number", "quantity", "description"):
            assert col in result.output, (
                f"Column '{col}' not found in output.\nOutput:\n{result.output}"
            )

    def test_output_contains_row_count(self) -> None:
        """Table output must show row count of 6 for sample.csv."""
        runner = CliRunner()
        result = runner.invoke(cli, ["inspect-source", "--input", str(SAMPLE_CSV)])
        assert result.exit_code == 0
        assert "6" in result.output, f"Row count '6' not in output:\n{result.output}"

    def test_output_contains_source_type(self) -> None:
        """Table output must show source_type 'csv'."""
        runner = CliRunner()
        result = runner.invoke(cli, ["inspect-source", "--input", str(SAMPLE_CSV)])
        assert result.exit_code == 0
        assert "csv" in result.output.lower()

    def test_out_dir_creates_schema_profile_json(self, tmp_path: Path) -> None:
        """With --out, schema-profile.json must be created in the given directory."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["inspect-source", "--input", str(SAMPLE_CSV), "--out", str(tmp_path)],
        )
        assert result.exit_code == 0, f"exit {result.exit_code}\n{result.output}"
        profile_file = tmp_path / "schema-profile.json"
        assert profile_file.exists(), "schema-profile.json was not created"

    def test_out_dir_schema_profile_is_valid_json(self, tmp_path: Path) -> None:
        """schema-profile.json must be valid JSON."""
        runner = CliRunner()
        runner.invoke(
            cli,
            ["inspect-source", "--input", str(SAMPLE_CSV), "--out", str(tmp_path)],
        )
        profile_file = tmp_path / "schema-profile.json"
        data = json.loads(profile_file.read_text())  # raises on invalid JSON
        assert data is not None

    def test_out_dir_schema_profile_has_correct_row_count(self, tmp_path: Path) -> None:
        """schema-profile.json must reflect 6 data rows."""
        runner = CliRunner()
        runner.invoke(
            cli,
            ["inspect-source", "--input", str(SAMPLE_CSV), "--out", str(tmp_path)],
        )
        data = json.loads((tmp_path / "schema-profile.json").read_text())
        assert data["row_count"] == 6

    def test_out_dir_schema_profile_has_columns_key(self, tmp_path: Path) -> None:
        """schema-profile.json must contain 'columns' list."""
        runner = CliRunner()
        runner.invoke(
            cli,
            ["inspect-source", "--input", str(SAMPLE_CSV), "--out", str(tmp_path)],
        )
        data = json.loads((tmp_path / "schema-profile.json").read_text())
        assert "columns" in data
        assert isinstance(data["columns"], list)
        assert len(data["columns"]) == 6  # sample.csv has 6 columns

    def test_bad_path_exits_nonzero(self) -> None:
        """Non-existent path must cause non-zero exit."""
        runner = CliRunner()
        result = runner.invoke(
            cli, ["inspect-source", "--input", "does_not_exist_xyz_abc.csv"]
        )
        assert result.exit_code != 0

    def test_bad_path_shows_error_message(self) -> None:
        """Non-existent path must emit an error message."""
        runner = make_cli_runner()
        result = runner.invoke(
            cli, ["inspect-source", "--input", "does_not_exist_xyz_abc.csv"]
        )
        all_output = combined_output(result)
        assert any(
            kw in all_output.lower() for kw in ("error", "not found")
        ), f"No error message found. Output: {all_output!r}"

    def test_unsupported_extension_exits_code_3(self, tmp_path: Path) -> None:
        """Unsupported file extension must exit with code 3."""
        bad_file = tmp_path / "data.txt"
        bad_file.write_text("hello world\n")
        runner = CliRunner()
        result = runner.invoke(cli, ["inspect-source", "--input", str(bad_file)])
        assert result.exit_code == 3, (
            f"Expected exit 3 for unsupported type, got {result.exit_code}.\n{result.output}"
        )

    def test_json_format_output_is_parseable(self) -> None:
        """--format json must emit parseable JSON with 'columns' and 'row_count'."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["inspect-source", "--input", str(SAMPLE_CSV), "--format", "json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "columns" in data
        assert "row_count" in data
        assert data["row_count"] == 6
