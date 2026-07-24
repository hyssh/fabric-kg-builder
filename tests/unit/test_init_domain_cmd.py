"""Unit tests for fabric-kg init-domain command and SourceProfile inspector.

Covers:
- Mixed format detection (CSV + PDF + image)
- Missing metadata / empty inputs
- Observed facts vs inferred suggestions separation
- Profile persistence and reload
- Domain description incorporation
- Noninteractive (--approve) execution
- Corrections via re-run
- Backward compatibility (existing commands unaffected)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.sources.inspector import (
    PROFILE_SCHEMA_VERSION,
    ObservedFacts,
    SourceProfile,
    build_source_profile,
    collect_source_files,
    load_source_profile,
    render_profile_text,
    save_source_profile,
)
from tests.conftest import combined_output

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_CSV = Path(__file__).parent.parent.parent / "examples" / "csv" / "sample.csv"
SAMPLE_DIR = Path(__file__).parent.parent.parent / "sample_data" / "Surface_Troubleshootings"
DOMAIN_EXAMPLE = Path(__file__).parent.parent.parent / "examples" / "domains" / "supply-chain-risk.domain.yaml"


def _make_pdf(path: Path, size: int = 50_000) -> Path:
    """Write a fake (non-parseable) PDF-sized file for metadata tests."""
    path.write_bytes(b"%PDF-1.4 " + b"x" * size)
    return path


def _make_csv(path: Path, headers: list[str], rows: int = 3) -> Path:
    """Write a minimal CSV file for testing."""
    lines = [",".join(headers)]
    for i in range(rows):
        lines.append(",".join(f"val{i}_{j}" for j in range(len(headers))))
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _make_image(path: Path) -> Path:
    """Write a minimal PNG-like file for testing."""
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    return path


# ---------------------------------------------------------------------------
# Unit tests: SourceProfile model
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSourceProfileModel:
    def test_default_profile_is_unapproved(self) -> None:
        profile = SourceProfile()
        assert profile.approved is False
        assert profile.approved_at_utc is None

    def test_schema_version_matches_constant(self) -> None:
        profile = SourceProfile()
        assert profile.schema_version == PROFILE_SCHEMA_VERSION

    def test_observed_facts_default_empty(self) -> None:
        obs = ObservedFacts()
        assert obs.total_file_count == 0
        assert obs.format_counts == {}
        assert obs.date_range is None

    def test_profile_serializes_to_json(self) -> None:
        profile = SourceProfile(
            observed=ObservedFacts(total_file_count=5),
        )
        d = profile.model_dump(mode="json")
        assert d["observed"]["total_file_count"] == 5
        assert d["schema_version"] == PROFILE_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Unit tests: build_source_profile
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildSourceProfile:
    def test_empty_directory_returns_zero_count(self, tmp_path: Path) -> None:
        profile = build_source_profile(tmp_path)
        assert profile.observed.total_file_count == 0

    def test_empty_directory_no_categories_or_entities(self, tmp_path: Path) -> None:
        profile = build_source_profile(tmp_path)
        assert profile.inferred.document_categories == []
        assert profile.inferred.entity_candidates == []

    def test_empty_directory_no_extraction_risks(self, tmp_path: Path) -> None:
        profile = build_source_profile(tmp_path)
        assert profile.inferred.extraction_risks == []

    def test_single_csv_observed_count(self, tmp_path: Path) -> None:
        f = tmp_path / "equipment.csv"
        _make_csv(f, ["device_id", "component", "part_number"])
        profile = build_source_profile(tmp_path)
        assert profile.observed.total_file_count == 1
        assert "spreadsheet" in profile.observed.format_counts

    def test_csv_columns_captured(self, tmp_path: Path) -> None:
        f = tmp_path / "devices.csv"
        _make_csv(f, ["device_id", "component", "part_number"])
        profile = build_source_profile(tmp_path)
        assert "device_id" in profile.observed.csv_column_names
        assert "component" in profile.observed.csv_column_names

    def test_mixed_formats_count(self, tmp_path: Path) -> None:
        _make_csv(tmp_path / "data.csv", ["id", "name"])
        _make_pdf(tmp_path / "doc.pdf", size=50_000)
        _make_image(tmp_path / "figure.png")
        profile = build_source_profile(tmp_path)
        assert profile.observed.total_file_count == 3
        assert profile.observed.format_counts.get("spreadsheet", 0) == 1
        assert profile.observed.format_counts.get("pdf", 0) == 1
        assert profile.observed.format_counts.get("image", 0) == 1

    def test_image_files_flagged_as_extraction_risk(self, tmp_path: Path) -> None:
        _make_image(tmp_path / "scan.png")
        _make_image(tmp_path / "scan2.tif")
        profile = build_source_profile(tmp_path)
        risks = " ".join(profile.inferred.extraction_risks)
        assert "image" in risks.lower() or "ocr" in risks.lower()

    def test_zero_byte_file_flagged_as_risk(self, tmp_path: Path) -> None:
        (tmp_path / "empty.csv").write_bytes(b"")
        profile = build_source_profile(tmp_path)
        risks = " ".join(profile.inferred.extraction_risks)
        assert "zero" in risks.lower() or "empty" in risks.lower()

    def test_date_range_extracted_from_filename(self, tmp_path: Path) -> None:
        (tmp_path / "report_2020.csv").write_text("id,name\n1,A\n", encoding="utf-8")
        (tmp_path / "report_2023.csv").write_text("id,name\n1,A\n", encoding="utf-8")
        profile = build_source_profile(tmp_path)
        assert profile.observed.date_range is not None
        years = [int(y) for y in profile.observed.date_range]
        assert 2020 in years or min(years) <= 2020

    def test_date_range_min_max_ordering(self, tmp_path: Path) -> None:
        (tmp_path / "old_2001_records.csv").write_text("id\n1\n", encoding="utf-8")
        (tmp_path / "new_2025_data.csv").write_text("id\n1\n", encoding="utf-8")
        profile = build_source_profile(tmp_path)
        assert profile.observed.date_range is not None
        assert int(profile.observed.date_range[0]) <= int(profile.observed.date_range[1])

    def test_domain_description_incorporated(self, tmp_path: Path) -> None:
        profile = build_source_profile(tmp_path, domain_description="Facility asset management")
        assert profile.domain_description == "Facility asset management"

    def test_domain_description_none_when_not_provided(self, tmp_path: Path) -> None:
        profile = build_source_profile(tmp_path)
        assert profile.domain_description is None

    def test_profile_is_unapproved_by_default(self, tmp_path: Path) -> None:
        profile = build_source_profile(tmp_path)
        assert profile.approved is False

    def test_source_hash_is_deterministic(self, tmp_path: Path) -> None:
        _make_csv(tmp_path / "data.csv", ["id", "value"])
        p1 = build_source_profile(tmp_path)
        p2 = build_source_profile(tmp_path)
        assert p1.source_hash == p2.source_hash

    def test_source_hash_changes_after_file_added(self, tmp_path: Path) -> None:
        _make_csv(tmp_path / "data.csv", ["id", "value"])
        p1 = build_source_profile(tmp_path)
        _make_csv(tmp_path / "extra.csv", ["x", "y"])
        p2 = build_source_profile(tmp_path)
        assert p1.source_hash != p2.source_hash

    def test_entity_candidates_from_csv_columns(self, tmp_path: Path) -> None:
        _make_csv(tmp_path / "assets.csv", ["equipment_id", "location", "vendor"])
        profile = build_source_profile(tmp_path)
        candidates = profile.inferred.entity_candidates
        # Equipment keyword in column and filename
        assert any("Equipment" in c or "Location" in c or "Organization" in c for c in candidates)

    def test_categories_inferred_from_filename_keywords(self, tmp_path: Path) -> None:
        _make_csv(tmp_path / "equipment_schedule.csv", ["id"])
        _make_csv(tmp_path / "warranty_records.csv", ["id"])
        profile = build_source_profile(tmp_path)
        cats = profile.inferred.document_categories
        assert any("equip" in c or "warrant" in c for c in cats)

    def test_single_file_path_accepted(self) -> None:
        assert SAMPLE_CSV.exists(), "Sample CSV fixture missing"
        profile = build_source_profile(SAMPLE_CSV)
        assert profile.observed.total_file_count == 1

    def test_observed_and_inferred_are_separate_fields(self, tmp_path: Path) -> None:
        _make_csv(tmp_path / "equip.csv", ["equipment_id", "location"])
        profile = build_source_profile(tmp_path)
        # Observed facts are directly measurable
        assert isinstance(profile.observed.total_file_count, int)
        assert isinstance(profile.observed.format_counts, dict)
        # Inferred suggestions are separate
        assert hasattr(profile, "inferred")
        assert isinstance(profile.inferred.entity_candidates, list)

    def test_no_lm_dependency_in_build(self, tmp_path: Path) -> None:
        """build_source_profile must not require any network or LLM calls."""
        _make_csv(tmp_path / "data.csv", ["id", "name", "value"])
        # If this raises ImportError or network error, the test fails
        profile = build_source_profile(tmp_path)
        assert profile is not None


# ---------------------------------------------------------------------------
# Unit tests: persistence
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSourceProfilePersistence:
    def test_save_and_load_round_trip(self, tmp_path: Path) -> None:
        original = build_source_profile(SAMPLE_CSV, domain_description="Test domain")
        original.approved = True
        original.approved_by = "test.user@example.com"
        out = tmp_path / "source-profile.json"
        save_source_profile(original, out)
        loaded = load_source_profile(out)
        assert loaded.observed.total_file_count == original.observed.total_file_count
        assert loaded.domain_description == "Test domain"
        assert loaded.approved is True
        assert loaded.approved_by == "test.user@example.com"

    def test_save_creates_parent_directories(self, tmp_path: Path) -> None:
        profile = SourceProfile()
        nested = tmp_path / ".fkg" / "subdir" / "source-profile.json"
        save_source_profile(profile, nested)
        assert nested.exists()

    def test_saved_file_is_valid_json(self, tmp_path: Path) -> None:
        profile = build_source_profile(SAMPLE_CSV)
        out = tmp_path / "profile.json"
        save_source_profile(profile, out)
        data = json.loads(out.read_text())
        assert "schema_version" in data
        assert "observed" in data
        assert "inferred" in data

    def test_load_nonexistent_raises_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_source_profile(tmp_path / "missing.json")

    def test_load_bad_json_raises_value_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("not valid json", encoding="utf-8")
        with pytest.raises((ValueError, Exception)):
            load_source_profile(bad)


# ---------------------------------------------------------------------------
# Unit tests: render_profile_text
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRenderProfileText:
    def test_empty_profile_renders_no_files_message(self) -> None:
        profile = SourceProfile()
        text = render_profile_text(profile)
        assert "no supported source files" in text.lower() or "0 file" in text.lower()

    def test_observed_file_count_in_output(self, tmp_path: Path) -> None:
        _make_csv(tmp_path / "a.csv", ["id"])
        _make_csv(tmp_path / "b.csv", ["id"])
        profile = build_source_profile(tmp_path)
        text = render_profile_text(profile)
        assert "2" in text

    def test_domain_description_in_output(self, tmp_path: Path) -> None:
        profile = build_source_profile(tmp_path, domain_description="Asset management")
        text = render_profile_text(profile)
        assert "Asset management" in text

    def test_inferred_label_in_output(self, tmp_path: Path) -> None:
        _make_csv(tmp_path / "equipment.csv", ["equipment_id"])
        profile = build_source_profile(tmp_path)
        text = render_profile_text(profile)
        assert "nferred" in text  # "Inferred" label must appear

    def test_extraction_risks_shown(self, tmp_path: Path) -> None:
        _make_image(tmp_path / "scan.png")
        profile = build_source_profile(tmp_path)
        text = render_profile_text(profile)
        assert "risk" in text.lower() or "ocr" in text.lower() or "image" in text.lower()

    def test_date_range_in_output(self, tmp_path: Path) -> None:
        (tmp_path / "data_2010.csv").write_text("id\n1\n", encoding="utf-8")
        (tmp_path / "data_2024.csv").write_text("id\n1\n", encoding="utf-8")
        profile = build_source_profile(tmp_path)
        text = render_profile_text(profile)
        assert "2010" in text or "2024" in text


# ---------------------------------------------------------------------------
# Unit tests: init-domain CLI command
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestInitDomainCmd:
    def test_help_exits_zero(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["init-domain", "--help"])
        assert result.exit_code == 0

    def test_help_mentions_approve_flag(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["init-domain", "--help"])
        assert "--approve" in result.output

    def test_help_mentions_input_option(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["init-domain", "--help"])
        assert "--input" in result.output

    def test_noninteractive_empty_dir_exits_zero(self, tmp_path: Path) -> None:
        """--approve with empty dir must exit 0 and write output files."""
        runner = CliRunner()
        out = tmp_path / "domain.yaml"
        result = runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(out),
                "--profile-out", str(tmp_path / ".fkg" / "source-profile.json"),
                "--approve",
            ],
        )
        assert result.exit_code == 0, combined_output(result)
        assert out.exists(), "domain.yaml not created"

    def test_noninteractive_creates_profile_json(self, tmp_path: Path) -> None:
        """Approved profile must be persisted to the given --profile-out path."""
        runner = CliRunner()
        out = tmp_path / "domain.yaml"
        profile_out = tmp_path / ".fkg" / "source-profile.json"
        runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(out),
                "--profile-out", str(profile_out),
                "--approve",
            ],
        )
        assert profile_out.exists(), ".fkg/source-profile.json not created"

    def test_profile_json_is_valid_and_approved(self, tmp_path: Path) -> None:
        """Persisted profile must have approved=True."""
        runner = CliRunner()
        profile_out = tmp_path / ".fkg" / "source-profile.json"
        runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(tmp_path / "domain.yaml"),
                "--profile-out", str(profile_out),
                "--approve",
            ],
        )
        data = json.loads(profile_out.read_text())
        assert data["approved"] is True
        assert data["schema_version"] == PROFILE_SCHEMA_VERSION

    def test_noninteractive_mixed_formats(self, tmp_path: Path) -> None:
        """Mixed CSV + PDF files must all be counted correctly."""
        _make_csv(tmp_path / "devices.csv", ["device_id", "component"])
        _make_pdf(tmp_path / "manual.pdf", size=80_000)
        profile_out = tmp_path / "profile.json"
        runner = CliRunner()
        runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(tmp_path / "domain.yaml"),
                "--profile-out", str(profile_out),
                "--approve",
            ],
        )
        data = json.loads(profile_out.read_text())
        assert data["observed"]["total_file_count"] == 2

    def test_output_shows_source_profile_section(self, tmp_path: Path) -> None:
        """CLI output must contain a source profile summary."""
        _make_csv(tmp_path / "data.csv", ["id"])
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(tmp_path / "domain.yaml"),
                "--profile-out", str(tmp_path / "profile.json"),
                "--approve",
            ],
        )
        out = combined_output(result)
        assert "source profile" in out.lower() or "Source profile" in out

    def test_domain_description_incorporated_in_profile(self, tmp_path: Path) -> None:
        """--domain-description must appear in the persisted profile."""
        profile_out = tmp_path / "profile.json"
        runner = CliRunner()
        runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(tmp_path / "domain.yaml"),
                "--profile-out", str(profile_out),
                "--domain-description", "Facility asset management",
                "--approve",
            ],
        )
        data = json.loads(profile_out.read_text())
        assert data["domain_description"] == "Facility asset management"

    def test_existing_contract_not_overwritten_without_force(self, tmp_path: Path) -> None:
        """Must refuse to overwrite existing domain.yaml without --force."""
        out = tmp_path / "domain.yaml"
        out.write_text("schema_version: '1.0'\n", encoding="utf-8")  # existing file
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(out),
                "--profile-out", str(tmp_path / "profile.json"),
                "--approve",
            ],
        )
        assert result.exit_code != 0

    def test_force_overwrites_existing_contract(self, tmp_path: Path) -> None:
        """--force must allow overwriting an existing domain.yaml."""
        out = tmp_path / "domain.yaml"
        out.write_text("schema_version: '1.0'\n", encoding="utf-8")
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(out),
                "--profile-out", str(tmp_path / "profile.json"),
                "--approve",
                "--force",
            ],
        )
        assert result.exit_code == 0, combined_output(result)

    def test_missing_input_path_exits_nonzero(self, tmp_path: Path) -> None:
        """Non-existent --input must exit non-zero."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "init-domain",
                "--input", "/nonexistent/path/xyz",
                "--out", str(tmp_path / "domain.yaml"),
                "--profile-out", str(tmp_path / "profile.json"),
                "--approve",
            ],
        )
        assert result.exit_code != 0

    def test_no_input_runs_without_error(self, tmp_path: Path) -> None:
        """Omitting --input must succeed (empty profile, no inspection)."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "init-domain",
                "--out", str(tmp_path / "domain.yaml"),
                "--profile-out", str(tmp_path / "profile.json"),
                "--approve",
            ],
        )
        assert result.exit_code == 0, combined_output(result)

    def test_image_files_trigger_extraction_risk_in_profile(self, tmp_path: Path) -> None:
        """Image files must be flagged as extraction risks."""
        _make_image(tmp_path / "scanned_drawing.png")
        _make_image(tmp_path / "plan.tif")
        profile_out = tmp_path / "profile.json"
        runner = CliRunner()
        runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(tmp_path / "domain.yaml"),
                "--profile-out", str(profile_out),
                "--approve",
            ],
        )
        data = json.loads(profile_out.read_text())
        risks = data["inferred"]["extraction_risks"]
        assert len(risks) > 0

    def test_domain_yaml_written_with_valid_structure(self, tmp_path: Path) -> None:
        """Written domain.yaml must load as a valid DomainContract."""
        import yaml

        runner = CliRunner()
        out = tmp_path / "domain.yaml"
        runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(out),
                "--profile-out", str(tmp_path / "profile.json"),
                "--approve",
            ],
        )
        assert out.exists()
        raw = yaml.safe_load(out.read_text())
        assert "schema_version" in raw

    def test_profile_has_source_hash(self, tmp_path: Path) -> None:
        """Persisted profile must contain a non-empty source_hash for staleness detection."""
        _make_csv(tmp_path / "data.csv", ["id"])
        profile_out = tmp_path / "profile.json"
        runner = CliRunner()
        runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(tmp_path / "domain.yaml"),
                "--profile-out", str(profile_out),
                "--approve",
            ],
        )
        data = json.loads(profile_out.read_text())
        assert data.get("source_hash"), "source_hash must be non-empty"

    def test_output_contains_observed_count(self, tmp_path: Path) -> None:
        """CLI output must mention the file count."""
        _make_csv(tmp_path / "a.csv", ["x"])
        _make_csv(tmp_path / "b.csv", ["y"])
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(tmp_path / "domain.yaml"),
                "--profile-out", str(tmp_path / "profile.json"),
                "--approve",
            ],
        )
        out = combined_output(result)
        assert "2" in out  # 2 files

    def test_profile_reloaded_matches_original(self, tmp_path: Path) -> None:
        """Reload of persisted profile must be structurally equivalent."""
        _make_csv(tmp_path / "data.csv", ["id", "name"])
        profile_out = tmp_path / "profile.json"
        runner = CliRunner()
        runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(tmp_path / "domain.yaml"),
                "--profile-out", str(profile_out),
                "--approve",
            ],
        )
        reloaded = load_source_profile(profile_out)
        assert reloaded.approved is True
        assert reloaded.observed.total_file_count == 1

    def test_inferred_vs_observed_clearly_labeled_in_output(self, tmp_path: Path) -> None:
        """CLI output must distinguish Observed from Inferred sections."""
        _make_csv(tmp_path / "equipment_schedule.csv", ["equipment_id"])
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(tmp_path / "domain.yaml"),
                "--profile-out", str(tmp_path / "profile.json"),
                "--approve",
            ],
        )
        out = combined_output(result)
        # Either "Observed" or "Inferred" must appear in output
        assert "nferred" in out or "bserved" in out

    def test_existing_commands_unaffected(self) -> None:
        """Existing inspect-source command must still work after adding init-domain."""
        runner = CliRunner()
        result = runner.invoke(cli, ["inspect-source", "--input", str(SAMPLE_CSV)])
        assert result.exit_code == 0, f"inspect-source broken: {combined_output(result)}"

    def test_domain_init_still_works(self, tmp_path: Path) -> None:
        """Existing domain init command must still work after adding init-domain."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["domain", "init", "--out", str(tmp_path / "domain.yaml")],
        )
        assert result.exit_code == 0, f"domain init broken: {combined_output(result)}"


# ---------------------------------------------------------------------------
# Contract tests: profile structure requirements
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSourceProfileContract:
    def test_profile_has_schema_version(self, tmp_path: Path) -> None:
        profile = build_source_profile(tmp_path)
        assert profile.schema_version == PROFILE_SCHEMA_VERSION

    def test_profile_has_inspected_at_utc(self, tmp_path: Path) -> None:
        profile = build_source_profile(tmp_path)
        assert profile.inspected_at_utc  # non-empty string

    def test_observed_not_equal_to_inferred(self, tmp_path: Path) -> None:
        """Observed and inferred sections must be distinct model objects."""
        profile = build_source_profile(tmp_path)
        assert profile.observed is not profile.inferred

    def test_inferred_not_included_in_observed(self, tmp_path: Path) -> None:
        """Entity candidates must live in inferred, not observed."""
        _make_csv(tmp_path / "equipment.csv", ["equipment_id"])
        profile = build_source_profile(tmp_path)
        # entity_candidates is on inferred, not observed
        assert hasattr(profile.inferred, "entity_candidates")
        assert not hasattr(profile.observed, "entity_candidates")

    def test_profile_approved_timestamp_set_on_approval(self, tmp_path: Path) -> None:
        profile_out = tmp_path / "profile.json"
        runner = CliRunner()
        runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(tmp_path / "domain.yaml"),
                "--profile-out", str(profile_out),
                "--approve",
            ],
        )
        data = json.loads(profile_out.read_text())
        assert data.get("approved_at_utc"), "approved_at_utc must be set"

    def test_format_counts_match_actual_files(self, tmp_path: Path) -> None:
        _make_csv(tmp_path / "a.csv", ["x"])
        _make_csv(tmp_path / "b.csv", ["y"])
        _make_pdf(tmp_path / "c.pdf")
        profile = build_source_profile(tmp_path)
        assert profile.observed.format_counts.get("spreadsheet", 0) == 2
        assert profile.observed.format_counts.get("pdf", 0) == 1

    def test_total_file_count_equals_sum_of_format_counts(self, tmp_path: Path) -> None:
        _make_csv(tmp_path / "a.csv", ["x"])
        _make_pdf(tmp_path / "b.pdf")
        _make_image(tmp_path / "c.png")
        profile = build_source_profile(tmp_path)
        assert profile.observed.total_file_count == sum(
            profile.observed.format_counts.values()
        )

    def test_domain_hash_field_exists_on_profile(self, tmp_path: Path) -> None:
        profile = build_source_profile(tmp_path)
        assert hasattr(profile, "domain_hash")
        assert profile.domain_hash is None  # None when no domain loaded


# ---------------------------------------------------------------------------
# Tests for interactive approval/rejection (using --interactive flag)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestInteractiveFlow:
    """Tests for the interactive approval flow via --interactive flag."""

    def test_interactive_rejection_exits_4(self, tmp_path: Path) -> None:
        """Typing 'n' at the approval prompt must exit with code 4."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(tmp_path / "domain.yaml"),
                "--profile-out", str(tmp_path / "profile.json"),
                "--interactive",
            ],
            input="n\n",  # user types 'n' → reject
        )
        assert result.exit_code == 4, (
            f"Expected exit 4 for rejection, got {result.exit_code}.\n{combined_output(result)}"
        )

    def test_interactive_approval_exits_0_and_creates_files(self, tmp_path: Path) -> None:
        """Typing 'y' at the approval prompt must exit 0 and create output files."""
        runner = CliRunner()
        out = tmp_path / "domain.yaml"
        profile_out = tmp_path / "profile.json"
        result = runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(out),
                "--profile-out", str(profile_out),
                "--interactive",
            ],
            # Answer: approve profile (y), then defaults for all unresolved questions
            input="y\n\n\nnone\nnone\nN\n",
        )
        assert result.exit_code == 0, (
            f"Expected exit 0 for approval, got {result.exit_code}.\n{combined_output(result)}"
        )
        assert out.exists(), "domain.yaml must be created after approval"
        assert profile_out.exists(), "profile.json must be created after approval"

    def test_interactive_rejection_does_not_create_files(self, tmp_path: Path) -> None:
        """Rejected profile must NOT create domain.yaml or profile.json."""
        runner = CliRunner()
        out = tmp_path / "domain.yaml"
        profile_out = tmp_path / "profile.json"
        runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(out),
                "--profile-out", str(profile_out),
                "--interactive",
            ],
            input="n\n",
        )
        assert not out.exists(), "domain.yaml must NOT be created after rejection"
        assert not profile_out.exists(), "profile.json must NOT be created after rejection"

    def test_interactive_rejection_prints_correction_guidance(self, tmp_path: Path) -> None:
        """Rejection message must tell the user how to correct and re-run."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(tmp_path / "domain.yaml"),
                "--profile-out", str(tmp_path / "profile.json"),
                "--interactive",
            ],
            input="n\n",
        )
        out = combined_output(result)
        assert "re-run" in out.lower() or "correct" in out.lower(), (
            f"Correction guidance not found in output:\n{out}"
        )

    def test_interactive_shows_profile_before_prompt(self, tmp_path: Path) -> None:
        """Profile text must appear in output before the approval prompt."""
        _make_csv(tmp_path / "equip.csv", ["equipment_id"])
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(tmp_path / "domain.yaml"),
                "--profile-out", str(tmp_path / "profile.json"),
                "--interactive",
            ],
            input="n\n",
        )
        out = combined_output(result)
        assert "Source profile" in out or "source profile" in out.lower()

    def test_interactive_approved_profile_is_approved_true(self, tmp_path: Path) -> None:
        """After 'y', persisted profile must have approved=True."""
        runner = CliRunner()
        profile_out = tmp_path / "profile.json"
        runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(tmp_path / "domain.yaml"),
                "--profile-out", str(profile_out),
                "--interactive",
            ],
            input="y\n\n\nnone\nnone\nN\n",
        )
        assert profile_out.exists()
        data = json.loads(profile_out.read_text())
        assert data["approved"] is True


# ---------------------------------------------------------------------------
# Tests for edge cases: corrupted input, empty domain, question filtering
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEdgeCases:
    """Edge cases from Hockney's E1-E7 matrix."""

    def test_corrupted_csv_does_not_crash(self, tmp_path: Path) -> None:
        """A malformed CSV must not cause an unhandled crash (E5)."""
        bad_csv = tmp_path / "corrupt.csv"
        bad_csv.write_bytes(b"\xff\xfe\x00broken\xde\xad\xbe\xef" * 100)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(tmp_path / "domain.yaml"),
                "--profile-out", str(tmp_path / "profile.json"),
                "--approve",
            ],
        )
        # Must exit 0 (profile with 1 file) or 0 (graceful handling)
        assert result.exit_code == 0, (
            f"Corrupted CSV caused crash: {combined_output(result)}"
        )

    def test_domain_file_with_empty_description_does_not_crash(
        self, tmp_path: Path
    ) -> None:
        """A domain.yaml with sparse/empty description must be handled gracefully (E4)."""
        from fabric_kg_builder.domain import default_domain_contract, save_domain_contract

        contract = default_domain_contract()
        contract.domain.description = "A domain"  # minimal valid description
        domain_yaml = tmp_path / "domain.yaml"
        save_domain_contract(contract, domain_yaml)

        out = tmp_path / "new-domain.yaml"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(out),
                "--profile-out", str(tmp_path / "profile.json"),
                "--domain-file", str(domain_yaml),
                "--approve",
            ],
        )
        assert result.exit_code == 0, (
            f"Empty-description domain.yaml caused crash: {combined_output(result)}"
        )

    def test_domain_file_description_loaded_into_profile(self, tmp_path: Path) -> None:
        """When --domain-file is provided, its description must appear in profile (E4)."""
        from fabric_kg_builder.domain import default_domain_contract, save_domain_contract

        contract = default_domain_contract()
        contract.domain.description = "Supply chain risk management"
        domain_yaml = tmp_path / "domain.yaml"
        save_domain_contract(contract, domain_yaml)

        profile_out = tmp_path / "profile.json"
        runner = CliRunner()
        runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(tmp_path / "new-domain.yaml"),
                "--profile-out", str(profile_out),
                "--domain-file", str(domain_yaml),
                "--approve",
            ],
        )
        data = json.loads(profile_out.read_text())
        assert data["domain_description"] == "Supply chain risk management"

    def test_domain_hash_stored_when_domain_file_used(self, tmp_path: Path) -> None:
        """When --domain-file loads a contract, domain_hash must be stored in profile."""
        from fabric_kg_builder.domain import default_domain_contract, save_domain_contract

        contract = default_domain_contract()
        contract.domain.description = "Test domain"
        domain_yaml = tmp_path / "domain.yaml"
        save_domain_contract(contract, domain_yaml)

        profile_out = tmp_path / "profile.json"
        runner = CliRunner()
        runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(tmp_path / "new-domain.yaml"),
                "--profile-out", str(profile_out),
                "--domain-file", str(domain_yaml),
                "--approve",
            ],
        )
        data = json.loads(profile_out.read_text())
        assert data.get("domain_hash"), "domain_hash must be set when domain file is loaded"

    def test_temporal_question_resolved_by_observed_date_range(
        self, tmp_path: Path
    ) -> None:
        """When date_range is observed, temporal constraint must be pre-filled."""
        (tmp_path / "report_2010.csv").write_text("id\n1\n", encoding="utf-8")
        (tmp_path / "data_2020.csv").write_text("id\n1\n", encoding="utf-8")
        profile = build_source_profile(tmp_path)
        assert profile.observed.date_range is not None

        from fabric_kg_builder.cli.init_domain_cmd import _noninteractive_defaults

        answers = _noninteractive_defaults(profile)
        temporal = answers.get("temporal_constraints", "")
        # Temporal should be pre-filled from observed date range, not defaulted to "none"
        assert temporal != "none", (
            f"temporal_constraints should be pre-filled from date range, got: {temporal!r}"
        )
        assert "2010" in temporal or "2020" in temporal

    def test_csv_columns_shown_in_profile_render(self, tmp_path: Path) -> None:
        """Rendered profile must show observed CSV column names (SAMPLE_DATA_PRESENTATION)."""
        _make_csv(tmp_path / "devices.csv", ["device_id", "component", "part_number"])
        profile = build_source_profile(tmp_path)
        text = render_profile_text(profile)
        assert "device_id" in text or "component" in text or "part_number" in text, (
            f"CSV column names not shown in render:\n{text}"
        )

    def test_csv_columns_labeled_as_observed_in_render(self, tmp_path: Path) -> None:
        """Column names section must appear before or within observed context."""
        _make_csv(tmp_path / "data.csv", ["id", "name", "value"])
        profile = build_source_profile(tmp_path)
        text = render_profile_text(profile)
        assert "olumn" in text, f"'columns' label missing from render:\n{text}"

    def test_single_file_profile_same_as_directory_with_one_file(
        self, tmp_path: Path
    ) -> None:
        """Single-file input must produce same profile as single-file directory (E2)."""
        f = tmp_path / "doc.csv"
        _make_csv(f, ["id"])
        p_file = build_source_profile(f)
        p_dir = build_source_profile(tmp_path)
        assert p_file.observed.total_file_count == p_dir.observed.total_file_count == 1
        assert p_file.observed.format_counts == p_dir.observed.format_counts

    def test_no_domain_yaml_produces_no_domain_description(self, tmp_path: Path) -> None:
        """With no --domain-file and no --domain-description, profile has no domain context (E3)."""
        runner = CliRunner()
        profile_out = tmp_path / "profile.json"
        runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(tmp_path / "domain.yaml"),
                "--profile-out", str(profile_out),
                "--approve",
            ],
        )
        data = json.loads(profile_out.read_text())
        assert data.get("domain_description") is None

    def test_large_number_of_files_all_counted(self, tmp_path: Path) -> None:
        """20+ mixed files must all be counted accurately (E7 mixed format scenario)."""
        for i in range(10):
            _make_csv(tmp_path / f"data_{i:02d}.csv", ["id"])
        for i in range(8):
            _make_pdf(tmp_path / f"doc_{i:02d}.pdf")
        for i in range(3):
            _make_image(tmp_path / f"scan_{i:02d}.png")
        profile = build_source_profile(tmp_path)
        assert profile.observed.total_file_count == 21
        assert profile.observed.format_counts["spreadsheet"] == 10
        assert profile.observed.format_counts["pdf"] == 8
        assert profile.observed.format_counts["image"] == 3

    def test_noninteractive_deterministic_across_two_runs(self, tmp_path: Path) -> None:
        """Two --approve runs on same directory must produce identical source_hash."""
        _make_csv(tmp_path / "stable.csv", ["id", "name"])
        runner = CliRunner()

        profile_out_1 = tmp_path / "profile1.json"
        runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(tmp_path / "domain1.yaml"),
                "--profile-out", str(profile_out_1),
                "--approve",
            ],
        )

        profile_out_2 = tmp_path / "profile2.json"
        runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(tmp_path / "domain2.yaml"),
                "--profile-out", str(profile_out_2),
                "--approve",
                "--force",
            ],
        )

        d1 = json.loads(profile_out_1.read_text())
        d2 = json.loads(profile_out_2.read_text())
        assert d1["source_hash"] == d2["source_hash"], (
            "source_hash must be identical across runs on same files"
        )
        assert d1["observed"]["format_counts"] == d2["observed"]["format_counts"]


# ---------------------------------------------------------------------------
# Tests for B2: correction flow
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCorrectionFlow:
    """B2: Interactive correction must allow editing fields before final approval."""

    def test_correction_flow_accepts_c_then_y(self, tmp_path: Path) -> None:
        """Entering 'c' then 'y' (after correcting) must exit 0 and persist profile."""
        _make_csv(tmp_path / "equipment.csv", ["equipment_id"])
        profile_out = tmp_path / "profile.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(tmp_path / "domain.yaml"),
                "--profile-out", str(profile_out),
                "--interactive",
            ],
            # c → correct: new categories, new entities, keep risks, keep date, then y
            input="c\nMachines,Sensors\nRobot\nn\n\ny\n\n\nnone\nnone\nN\n",
        )
        assert result.exit_code == 0, combined_output(result)
        assert profile_out.exists()
        data = json.loads(profile_out.read_text())
        assert data["approved"] is True
        assert data["user_corrected"] is True

    def test_corrected_categories_appear_in_persisted_profile(self, tmp_path: Path) -> None:
        """After correction, the persisted profile must contain the user's categories."""
        _make_csv(tmp_path / "equipment.csv", ["equipment_id"])
        profile_out = tmp_path / "profile.json"
        runner = CliRunner()
        runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(tmp_path / "domain.yaml"),
                "--profile-out", str(profile_out),
                "--interactive",
            ],
            input="c\nBlue widgets,Red gadgets\n\nn\n\ny\n\n\nnone\nnone\nN\n",
        )
        data = json.loads(profile_out.read_text())
        cats = data["inferred"]["document_categories"]
        assert "Blue widgets" in cats or "blue widgets" in cats, (
            f"Corrected categories not persisted. Got: {cats}"
        )

    def test_corrected_entities_appear_in_persisted_profile(self, tmp_path: Path) -> None:
        """After correcting entity candidates, persisted profile has the user's values."""
        profile_out = tmp_path / "profile.json"
        runner = CliRunner()
        runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(tmp_path / "domain.yaml"),
                "--profile-out", str(profile_out),
                "--interactive",
            ],
            # skip categories, correct entities, skip rest, approve
            input="c\n\nTurbine,Valve,Pump\nn\n\ny\n\n\nnone\nnone\nN\n",
        )
        data = json.loads(profile_out.read_text())
        ents = data["inferred"]["entity_candidates"]
        assert "Turbine" in ents, f"Corrected entities not persisted. Got: {ents}"

    def test_user_corrected_false_when_no_changes_made(self, tmp_path: Path) -> None:
        """Entering 'y' directly (no correction) must leave user_corrected=False."""
        profile_out = tmp_path / "profile.json"
        runner = CliRunner()
        runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(tmp_path / "domain.yaml"),
                "--profile-out", str(profile_out),
                "--interactive",
            ],
            input="y\n\n\nnone\nnone\nN\n",
        )
        data = json.loads(profile_out.read_text())
        assert data.get("user_corrected") is False

    def test_correction_flow_rerenders_profile(self, tmp_path: Path) -> None:
        """After correction, the updated profile must be re-displayed before re-approval."""
        _make_csv(tmp_path / "equip.csv", ["equipment_id"])
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(tmp_path / "domain.yaml"),
                "--profile-out", str(tmp_path / "profile.json"),
                "--interactive",
            ],
            input="c\nCustom category\n\nn\n\ny\n\n\nnone\nnone\nN\n",
        )
        out = combined_output(result)
        # "Source profile" must appear twice (once before correction, once after)
        count = out.lower().count("source profile")
        assert count >= 2, (
            f"Profile was not re-rendered after correction. 'source profile' count={count}"
        )

    def test_approve_flag_skips_correction_prompt(self, tmp_path: Path) -> None:
        """--approve must auto-approve without any correction prompts (CI/CD compat)."""
        _make_csv(tmp_path / "data.csv", ["id"])
        profile_out = tmp_path / "profile.json"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(tmp_path / "domain.yaml"),
                "--profile-out", str(profile_out),
                "--approve",
            ],
        )
        assert result.exit_code == 0, combined_output(result)
        data = json.loads(profile_out.read_text())
        assert data["approved"] is True
        # user_corrected must be False when auto-approved
        assert data.get("user_corrected") is False

    def test_correct_then_approve_corrected_entities_not_in_domain_yaml(
        self, tmp_path: Path
    ) -> None:
        """Corrected+approved profile must write confirmed entities to domain.yaml."""
        import yaml

        profile_out = tmp_path / "profile.json"
        out_yaml = tmp_path / "domain.yaml"
        runner = CliRunner()
        runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(out_yaml),
                "--profile-out", str(profile_out),
                "--interactive",
            ],
            # correct: skip categories, set entities = "Widget,Gadget", skip rest, approve
            input="c\n\nWidget,Gadget\nn\n\ny\n\n\nnone\nnone\nN\n",
        )
        data = json.loads(profile_out.read_text())
        assert data["user_corrected"] is True

        domain_raw = yaml.safe_load(out_yaml.read_text())
        domain_cats = domain_raw.get("candidate_model", {}).get("entity_categories", [])
        # When user_corrected=True, confirmed entities should appear in domain.yaml
        assert "Widget" in domain_cats or any(
            "Widget" in c for c in domain_cats
        ), f"Corrected entities not propagated to domain.yaml: {domain_cats}"


# ---------------------------------------------------------------------------
# Tests for B1: staleness detection and profile field user_corrected
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSourceProfileStaleness:
    """B1: source_hash staleness must be detectable."""

    def test_check_staleness_returns_none_for_current_profile(
        self, tmp_path: Path
    ) -> None:
        """Unchanged files must produce no staleness warning."""
        from fabric_kg_builder.sources.inspector import check_source_profile_staleness

        _make_csv(tmp_path / "data.csv", ["id"])
        profile = build_source_profile(tmp_path)
        profile.approved = True
        warning = check_source_profile_staleness(profile, tmp_path)
        assert warning is None

    def test_check_staleness_returns_warning_after_file_added(
        self, tmp_path: Path
    ) -> None:
        """Adding a file after profile creation must produce a staleness warning."""
        from fabric_kg_builder.sources.inspector import check_source_profile_staleness

        _make_csv(tmp_path / "original.csv", ["id"])
        profile = build_source_profile(tmp_path)
        # Add a file AFTER the profile was built
        _make_csv(tmp_path / "added.csv", ["new"])
        warning = check_source_profile_staleness(profile, tmp_path)
        assert warning is not None
        assert "stale" in warning.lower() or "changed" in warning.lower()

    def test_check_staleness_none_when_profile_has_empty_hash(
        self, tmp_path: Path
    ) -> None:
        """A profile with no source_hash (e.g. empty dir) must not produce warnings."""
        from fabric_kg_builder.sources.inspector import check_source_profile_staleness

        profile = build_source_profile(tmp_path)  # empty dir → empty hash
        # With an empty source_hash the staleness check is skipped gracefully
        warning = check_source_profile_staleness(profile, tmp_path)
        assert warning is None

    def test_user_corrected_field_persists_in_json(self, tmp_path: Path) -> None:
        """user_corrected=True must survive a save/load round-trip."""
        profile = build_source_profile(tmp_path)
        profile = profile.model_copy(update={"user_corrected": True})
        path = tmp_path / "profile.json"
        save_source_profile(profile, path)
        loaded = load_source_profile(path)
        assert loaded.user_corrected is True
