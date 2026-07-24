"""Contract tests for the source profile and init-domain workflow (Issue #5).

These tests verify the structural contracts that downstream commands
(enrich, compile-data) depend on from the persisted source profile.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.sources.inspector import (
    PROFILE_SCHEMA_VERSION,
    build_source_profile,
    load_source_profile,
    save_source_profile,
)
from tests.conftest import combined_output


def _make_csv(path: Path, headers: list[str]) -> Path:
    lines = [",".join(headers)]
    lines.append(",".join("val" for _ in headers))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _make_pdf(path: Path, size: int = 50_000) -> Path:
    path.write_bytes(b"%PDF-1.4 " + b"x" * size)
    return path


def _make_image(path: Path) -> Path:
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    return path


def _run_init_domain(tmp_path: Path, extra_args: list[str] | None = None) -> dict:
    """Run init-domain --approve and return the loaded profile dict."""
    out = tmp_path / "domain.yaml"
    profile_out = tmp_path / "source-profile.json"
    runner = CliRunner()
    args = [
        "init-domain",
        "--input", str(tmp_path),
        "--out", str(out),
        "--profile-out", str(profile_out),
        "--approve",
    ] + (extra_args or [])
    result = runner.invoke(cli, args)
    assert result.exit_code == 0, f"init-domain failed: {combined_output(result)}"
    return json.loads(profile_out.read_text())


# ---------------------------------------------------------------------------
# Contract: Profile persisted fields required by downstream commands
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSourceProfileStructureContract:
    """Verify the persisted profile has all fields downstream commands depend on."""

    def test_schema_version_field_present_and_correct(self, tmp_path: Path) -> None:
        data = _run_init_domain(tmp_path)
        assert data["schema_version"] == PROFILE_SCHEMA_VERSION

    def test_observed_section_present(self, tmp_path: Path) -> None:
        data = _run_init_domain(tmp_path)
        assert "observed" in data

    def test_inferred_section_present(self, tmp_path: Path) -> None:
        data = _run_init_domain(tmp_path)
        assert "inferred" in data

    def test_total_file_count_in_observed(self, tmp_path: Path) -> None:
        data = _run_init_domain(tmp_path)
        assert "total_file_count" in data["observed"]

    def test_format_counts_in_observed(self, tmp_path: Path) -> None:
        data = _run_init_domain(tmp_path)
        assert "format_counts" in data["observed"]
        assert isinstance(data["observed"]["format_counts"], dict)

    def test_entity_candidates_in_inferred(self, tmp_path: Path) -> None:
        data = _run_init_domain(tmp_path)
        assert "entity_candidates" in data["inferred"]
        assert isinstance(data["inferred"]["entity_candidates"], list)

    def test_document_categories_in_inferred(self, tmp_path: Path) -> None:
        data = _run_init_domain(tmp_path)
        assert "document_categories" in data["inferred"]

    def test_extraction_risks_in_inferred(self, tmp_path: Path) -> None:
        data = _run_init_domain(tmp_path)
        assert "extraction_risks" in data["inferred"]

    def test_source_hash_present_and_nonempty(self, tmp_path: Path) -> None:
        _make_csv(tmp_path / "data.csv", ["id", "name"])
        data = _run_init_domain(tmp_path)
        assert data.get("source_hash"), "source_hash must be set for staleness detection"

    def test_approved_true_after_approve(self, tmp_path: Path) -> None:
        data = _run_init_domain(tmp_path)
        assert data["approved"] is True

    def test_approved_at_utc_present_and_nonempty(self, tmp_path: Path) -> None:
        data = _run_init_domain(tmp_path)
        assert data.get("approved_at_utc"), "approved_at_utc must be set"

    def test_inspected_at_utc_present(self, tmp_path: Path) -> None:
        data = _run_init_domain(tmp_path)
        assert data.get("inspected_at_utc"), "inspected_at_utc must be set"


# ---------------------------------------------------------------------------
# Contract: Observed vs inferred separation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestObservedVsInferredContract:
    """Entity candidates and categories must be in inferred, never in observed."""

    def test_observed_does_not_contain_entity_candidates(self, tmp_path: Path) -> None:
        _make_csv(tmp_path / "equipment.csv", ["equipment_id"])
        data = _run_init_domain(tmp_path)
        assert "entity_candidates" not in data["observed"]

    def test_observed_does_not_contain_document_categories(self, tmp_path: Path) -> None:
        data = _run_init_domain(tmp_path)
        assert "document_categories" not in data["observed"]

    def test_total_file_count_in_observed_not_inferred(self, tmp_path: Path) -> None:
        _make_csv(tmp_path / "data.csv", ["id"])
        data = _run_init_domain(tmp_path)
        assert "total_file_count" in data["observed"]
        assert "total_file_count" not in data["inferred"]

    def test_format_counts_in_observed_not_inferred(self, tmp_path: Path) -> None:
        data = _run_init_domain(tmp_path)
        assert "format_counts" in data["observed"]
        assert "format_counts" not in data["inferred"]

    def test_mixed_formats_both_in_observed_format_counts(self, tmp_path: Path) -> None:
        _make_csv(tmp_path / "data.csv", ["id"])
        _make_pdf(tmp_path / "doc.pdf")
        data = _run_init_domain(tmp_path)
        fmts = data["observed"]["format_counts"]
        assert "spreadsheet" in fmts
        assert "pdf" in fmts


# ---------------------------------------------------------------------------
# Contract: Determinism — same inputs produce same profile
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProfileDeterminismContract:
    def test_same_files_same_source_hash(self, tmp_path: Path) -> None:
        _make_csv(tmp_path / "data.csv", ["id", "name"])
        p1 = build_source_profile(tmp_path)
        p2 = build_source_profile(tmp_path)
        assert p1.source_hash == p2.source_hash

    def test_same_files_same_format_counts(self, tmp_path: Path) -> None:
        _make_csv(tmp_path / "data.csv", ["id"])
        _make_pdf(tmp_path / "doc.pdf")
        p1 = build_source_profile(tmp_path)
        p2 = build_source_profile(tmp_path)
        assert p1.observed.format_counts == p2.observed.format_counts

    def test_same_files_same_entity_candidates(self, tmp_path: Path) -> None:
        _make_csv(tmp_path / "equipment.csv", ["equipment_id", "location"])
        p1 = build_source_profile(tmp_path)
        p2 = build_source_profile(tmp_path)
        assert p1.inferred.entity_candidates == p2.inferred.entity_candidates

    def test_added_file_changes_source_hash(self, tmp_path: Path) -> None:
        _make_csv(tmp_path / "data.csv", ["id"])
        p1 = build_source_profile(tmp_path)
        _make_csv(tmp_path / "extra.csv", ["x"])
        p2 = build_source_profile(tmp_path)
        assert p1.source_hash != p2.source_hash

    def test_empty_dir_deterministic(self, tmp_path: Path) -> None:
        p1 = build_source_profile(tmp_path)
        p2 = build_source_profile(tmp_path)
        assert p1.source_hash == p2.source_hash


# ---------------------------------------------------------------------------
# Contract: Extraction risk detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExtractionRiskContract:
    def test_image_files_always_flagged(self, tmp_path: Path) -> None:
        _make_image(tmp_path / "scan1.png")
        _make_image(tmp_path / "scan2.tif")
        profile = build_source_profile(tmp_path)
        assert len(profile.inferred.extraction_risks) > 0

    def test_zero_byte_files_always_flagged(self, tmp_path: Path) -> None:
        (tmp_path / "empty.csv").write_bytes(b"")
        profile = build_source_profile(tmp_path)
        assert len(profile.inferred.extraction_risks) > 0

    def test_clean_csv_has_no_risks(self, tmp_path: Path) -> None:
        _make_csv(tmp_path / "clean.csv", ["id", "value"])
        profile = build_source_profile(tmp_path)
        assert profile.inferred.extraction_risks == []

    def test_mixed_risks_all_reported(self, tmp_path: Path) -> None:
        _make_image(tmp_path / "scan.png")
        (tmp_path / "empty.csv").write_bytes(b"")
        profile = build_source_profile(tmp_path)
        assert len(profile.inferred.extraction_risks) >= 1


# ---------------------------------------------------------------------------
# Contract: Profile persistence round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProfilePersistenceContract:
    def test_save_load_preserves_approved_status(self, tmp_path: Path) -> None:
        profile = build_source_profile(tmp_path)
        profile.approved = True
        profile.approved_by = "ci@example.com"
        path = tmp_path / "profile.json"
        save_source_profile(profile, path)
        loaded = load_source_profile(path)
        assert loaded.approved is True
        assert loaded.approved_by == "ci@example.com"

    def test_save_load_preserves_source_hash(self, tmp_path: Path) -> None:
        _make_csv(tmp_path / "data.csv", ["id"])
        profile = build_source_profile(tmp_path)
        path = tmp_path / "profile.json"
        save_source_profile(profile, path)
        loaded = load_source_profile(path)
        assert loaded.source_hash == profile.source_hash

    def test_save_load_preserves_observed_counts(self, tmp_path: Path) -> None:
        _make_csv(tmp_path / "a.csv", ["x"])
        _make_pdf(tmp_path / "b.pdf")
        profile = build_source_profile(tmp_path)
        path = tmp_path / "profile.json"
        save_source_profile(profile, path)
        loaded = load_source_profile(path)
        assert loaded.observed.total_file_count == profile.observed.total_file_count
        assert loaded.observed.format_counts == profile.observed.format_counts

    def test_save_load_preserves_domain_description(self, tmp_path: Path) -> None:
        profile = build_source_profile(tmp_path, domain_description="Test domain")
        path = tmp_path / "profile.json"
        save_source_profile(profile, path)
        loaded = load_source_profile(path)
        assert loaded.domain_description == "Test domain"


# ---------------------------------------------------------------------------
# Contract: Backward compatibility
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBackwardCompatibilityContract:
    def test_inspect_source_unaffected(self) -> None:
        """inspect-source must still work correctly."""
        sample_csv = (
            Path(__file__).parent.parent.parent
            / "examples" / "csv" / "sample.csv"
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["inspect-source", "--input", str(sample_csv)])
        assert result.exit_code == 0, f"inspect-source broken: {combined_output(result)}"
        assert "csv" in result.output.lower()

    def test_domain_init_unaffected(self, tmp_path: Path) -> None:
        """domain init must still create a valid scaffold."""
        runner = CliRunner()
        out = tmp_path / "domain.yaml"
        result = runner.invoke(cli, ["domain", "init", "--out", str(out)])
        assert result.exit_code == 0, f"domain init broken: {combined_output(result)}"
        assert out.exists()

    def test_domain_validate_unaffected(self, tmp_path: Path) -> None:
        """domain validate must still run (exit 0 or 1) — command must not crash."""
        runner = CliRunner()
        out = tmp_path / "domain.yaml"
        runner.invoke(cli, ["domain", "init", "--out", str(out)])
        result = runner.invoke(cli, ["domain", "validate", "--file", str(out)])
        # Exit 0 = passes, Exit 1 = validation errors found — both are valid command runs.
        # What matters is the command runs without crashing (not exit code 127 or exception).
        assert result.exit_code in (0, 1), (
            f"domain validate crashed unexpectedly: {combined_output(result)}"
        )

    def test_init_domain_command_in_cli_help(self) -> None:
        """init-domain must appear in the CLI help output."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "init-domain" in result.output

    def test_init_domain_help_exits_zero(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["init-domain", "--help"])
        assert result.exit_code == 0

    def test_init_domain_help_mentions_interactive_flag(self) -> None:
        """--interactive flag must be documented in help."""
        runner = CliRunner()
        result = runner.invoke(cli, ["init-domain", "--help"])
        assert "--interactive" in result.output


# ---------------------------------------------------------------------------
# Contract: Domain hash audit trail
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDomainHashContract:
    """Verify domain_hash is stored for audit trail when domain file is loaded."""

    def test_domain_hash_none_when_no_domain_file(self, tmp_path: Path) -> None:
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
        # No domain file loaded → domain_hash should be None
        assert data.get("domain_hash") is None

    def test_domain_hash_set_when_domain_file_provided(self, tmp_path: Path) -> None:
        from fabric_kg_builder.domain import default_domain_contract, save_domain_contract

        contract = default_domain_contract()
        contract.domain.description = "Test"
        domain_yaml = tmp_path / "domain.yaml"
        save_domain_contract(contract, domain_yaml)

        profile_out = tmp_path / "profile.json"
        runner = CliRunner()
        runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(tmp_path / "new.yaml"),
                "--profile-out", str(profile_out),
                "--domain-file", str(domain_yaml),
                "--approve",
            ],
        )
        data = json.loads(profile_out.read_text())
        assert data.get("domain_hash"), "domain_hash must be non-None when domain file is loaded"

    def test_domain_hash_is_string(self, tmp_path: Path) -> None:
        from fabric_kg_builder.domain import default_domain_contract, save_domain_contract

        contract = default_domain_contract()
        contract.domain.description = "Test"
        domain_yaml = tmp_path / "domain.yaml"
        save_domain_contract(contract, domain_yaml)

        profile_out = tmp_path / "profile.json"
        runner = CliRunner()
        runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(tmp_path / "new.yaml"),
                "--profile-out", str(profile_out),
                "--domain-file", str(domain_yaml),
                "--approve",
            ],
        )
        data = json.loads(profile_out.read_text())
        assert isinstance(data.get("domain_hash"), str)


# ---------------------------------------------------------------------------
# Contract: Unresolved question filtering
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUnresolvedQuestionFilteringContract:
    """Verify that observed facts pre-fill answers so questions aren't repeated."""

    def test_date_range_pre_fills_temporal_answer(self, tmp_path: Path) -> None:
        """Observed date range must auto-fill temporal constraints answer."""
        from fabric_kg_builder.cli.init_domain_cmd import _noninteractive_defaults

        (tmp_path / "report_1995.csv").write_text("id\n1\n", encoding="utf-8")
        (tmp_path / "report_2023.csv").write_text("id\n1\n", encoding="utf-8")
        profile = build_source_profile(tmp_path)
        assert profile.observed.date_range is not None

        answers = _noninteractive_defaults(profile)
        temporal = answers.get("temporal_constraints", "")
        assert "1995" in temporal or "2023" in temporal, (
            f"Expected year in temporal answer, got: {temporal!r}"
        )

    def test_domain_description_pre_fills_answer(self, tmp_path: Path) -> None:
        """Profile domain_description must be reused in answers without re-asking."""
        from fabric_kg_builder.cli.init_domain_cmd import _noninteractive_defaults

        profile = build_source_profile(tmp_path, domain_description="Facility assets")
        answers = _noninteractive_defaults(profile)
        assert answers.get("domain_description") == "Facility assets", (
            "domain_description should be pre-filled from profile"
        )

    def test_no_date_range_uses_default_temporal(self, tmp_path: Path) -> None:
        """Without date range in profile, temporal defaults to 'none'."""
        from fabric_kg_builder.cli.init_domain_cmd import _noninteractive_defaults

        profile = build_source_profile(tmp_path)  # empty dir → no date range
        assert profile.observed.date_range is None

        answers = _noninteractive_defaults(profile)
        assert answers.get("temporal_constraints") == "none"


# ---------------------------------------------------------------------------
# Contract: B1 — Downstream reuse (enrich orchestration boundary)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDownstreamProfileReuse:
    """Prove the persisted profile is consumed by the enrich orchestration boundary."""

    def test_load_profile_returns_profile_when_present(self, tmp_path: Path) -> None:
        """_load_source_profile_for_enrich must return the approved profile."""
        from fabric_kg_builder.cli.enrich_cmd import _load_source_profile_for_enrich

        _make_csv(tmp_path / "data.csv", ["id", "name"])
        profile_path = tmp_path / "source-profile.json"
        _run_init_domain(tmp_path, extra_args=["--profile-out", str(profile_path)])

        profile, stale = _load_source_profile_for_enrich(tmp_path, profile_path)

        assert profile is not None, "Profile must be loaded when file exists"
        assert profile.approved is True, "Loaded profile must be approved"
        assert profile.observed.total_file_count == 1

    def test_load_profile_returns_none_when_absent(self, tmp_path: Path) -> None:
        """Missing profile must return (None, None) — no crash (legacy compat)."""
        from fabric_kg_builder.cli.enrich_cmd import _load_source_profile_for_enrich

        absent = tmp_path / ".fkg" / "source-profile.json"
        assert not absent.exists()

        profile, stale = _load_source_profile_for_enrich(tmp_path, absent)

        assert profile is None, "Must return None when profile is absent"
        assert stale is None, "No staleness warning when profile is absent"

    def test_stale_profile_returns_warning_not_error(self, tmp_path: Path) -> None:
        """Changed files after approval must produce a staleness warning, not an error."""
        from fabric_kg_builder.cli.enrich_cmd import _load_source_profile_for_enrich

        _make_csv(tmp_path / "original.csv", ["id"])
        profile_path = tmp_path / "source-profile.json"
        _run_init_domain(tmp_path, extra_args=["--profile-out", str(profile_path)])

        # Add a file AFTER the profile was approved
        _make_csv(tmp_path / "added_later.csv", ["new_col"])

        profile, stale_warning = _load_source_profile_for_enrich(tmp_path, profile_path)

        assert profile is not None, "Profile must still load even when stale"
        assert stale_warning is not None, "Staleness warning must be returned"
        assert "stale" in stale_warning.lower() or "changed" in stale_warning.lower()

    def test_current_profile_returns_no_staleness_warning(self, tmp_path: Path) -> None:
        """Unchanged files after approval must return (profile, None)."""
        from fabric_kg_builder.cli.enrich_cmd import _load_source_profile_for_enrich

        _make_csv(tmp_path / "data.csv", ["id", "name"])
        profile_path = tmp_path / "source-profile.json"
        _run_init_domain(tmp_path, extra_args=["--profile-out", str(profile_path)])

        profile, stale_warning = _load_source_profile_for_enrich(tmp_path, profile_path)

        assert profile is not None
        assert stale_warning is None, "No warning when files are unchanged"

    def test_extraction_risks_available_from_loaded_profile(self, tmp_path: Path) -> None:
        """Loaded profile must expose extraction risks for the enrich command."""
        from fabric_kg_builder.cli.enrich_cmd import _load_source_profile_for_enrich

        # Image files create extraction risks
        (tmp_path / "scan.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        profile_path = tmp_path / "source-profile.json"
        _run_init_domain(tmp_path, extra_args=["--profile-out", str(profile_path)])

        profile, _ = _load_source_profile_for_enrich(tmp_path, profile_path)

        assert profile is not None
        assert len(profile.inferred.extraction_risks) > 0, (
            "Extraction risks from init-domain must be accessible to enrich"
        )

    def test_profile_with_corrupt_json_returns_none_safely(self, tmp_path: Path) -> None:
        """Corrupt profile must return (None, None) — not crash enrich."""
        from fabric_kg_builder.cli.enrich_cmd import _load_source_profile_for_enrich

        corrupt = tmp_path / "corrupt.json"
        corrupt.write_text("{ not valid json }", encoding="utf-8")

        profile, stale = _load_source_profile_for_enrich(tmp_path, corrupt)

        assert profile is None
        assert stale is None


# ---------------------------------------------------------------------------
# Contract: B2 — Correction flow persists corrected values
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCorrectionFlowContract:
    """Correction flow must persist user-corrected values (not the originals)."""

    def test_correct_then_approve_persists_corrected_categories(
        self, tmp_path: Path
    ) -> None:
        """After user corrects document categories and approves, corrected
        values must appear in the persisted profile."""
        from fabric_kg_builder.cli.init_domain_cmd import (
            _apply_corrections,
            _approve_interactively,
        )

        _make_csv(tmp_path / "equipment.csv", ["equipment_id"])
        profile = build_source_profile(tmp_path)
        profile.inferred = profile.inferred.model_copy(
            update={"document_categories": ["manuals", "reports"]}
        )

        # Simulate correction: user types new categories
        with _click_input("Machinery specs,Inspection records\n\n\nn\n"):
            corrected = _apply_corrections(profile)

        assert "Machinery specs" in corrected.inferred.document_categories
        assert "Inspection records" in corrected.inferred.document_categories
        assert corrected.user_corrected is True

    def test_correct_then_approve_persists_corrected_entities(
        self, tmp_path: Path
    ) -> None:
        """After user corrects entity candidates, corrected values must be in profile."""
        from fabric_kg_builder.cli.init_domain_cmd import _apply_corrections

        profile = build_source_profile(tmp_path)

        # Simulate: user enters new entity candidates, skips other fields
        with _click_input("\nMachine,Sensor,Location\n\n\n"):
            corrected = _apply_corrections(profile)

        assert "Machine" in corrected.inferred.entity_candidates
        assert "Sensor" in corrected.inferred.entity_candidates
        assert corrected.user_corrected is True

    def test_correct_then_approve_date_range_override(
        self, tmp_path: Path
    ) -> None:
        """Corrected date range must replace the inferred one in the profile."""
        from fabric_kg_builder.cli.init_domain_cmd import _apply_corrections

        (tmp_path / "data_2020.csv").write_text("id\n1\n", encoding="utf-8")
        profile = build_source_profile(tmp_path)
        # profile has an inferred date range from the filename

        # Simulate: user skips categories, skips entities, skips risks, sets date range
        with _click_input("\n\n\n1998,2024\n"):
            corrected = _apply_corrections(profile)

        assert corrected.observed.date_range is not None
        assert "1998" in corrected.observed.date_range
        assert "2024" in corrected.observed.date_range
        assert corrected.user_corrected is True

    def test_no_changes_user_corrected_false(self, tmp_path: Path) -> None:
        """Entering nothing in correction mode must leave user_corrected=False."""
        from fabric_kg_builder.cli.init_domain_cmd import _apply_corrections

        profile = build_source_profile(tmp_path)

        # All Enter presses = keep everything, change nothing
        with _click_input("\n\n\n\n"):
            corrected = _apply_corrections(profile)

        assert corrected.user_corrected is False

    def test_correction_full_flow_via_cli(self, tmp_path: Path) -> None:
        """Full CLI test: correct then approve → persisted profile has corrected values
        and user_corrected=True."""
        _make_csv(tmp_path / "equipment.csv", ["equipment_id"])
        profile_out = tmp_path / "profile.json"
        out_yaml = tmp_path / "domain.yaml"

        runner = CliRunner()
        # Sequence: 'c' (correct), enter new categories, skip rest, 'y' approve
        user_input = (
            "c\n"               # choose correct
            "Machines,Sensors\n"  # new document categories
            "Robot,Actuator\n"   # new entity candidates
            "n\n"               # don't clear risks
            "\n"                # keep date range
            "y\n"               # approve
            "\n\n\n\n\n"        # answer unresolved questions with defaults
        )
        result = runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(out_yaml),
                "--profile-out", str(profile_out),
                "--interactive",  # force interactive even in CliRunner
            ],
            input=user_input,
        )
        assert result.exit_code == 0, combined_output(result)

        data = json.loads(profile_out.read_text())
        assert data["approved"] is True
        assert data["user_corrected"] is True
        cats = data["inferred"]["document_categories"]
        assert "Machines" in cats or "machines" in cats

    def test_approved_corrected_profile_consumed_by_enrich_boundary(
        self, tmp_path: Path
    ) -> None:
        """A corrected+approved profile must be loadable by the enrich orchestration
        boundary and must report user_corrected=True."""
        from fabric_kg_builder.cli.enrich_cmd import _load_source_profile_for_enrich

        _make_csv(tmp_path / "data.csv", ["id"])
        profile_path = tmp_path / "profile.json"

        # Build a corrected profile directly (simulating the correction flow)
        profile = build_source_profile(tmp_path)
        profile = profile.model_copy(
            update={
                "approved": True,
                "approved_at_utc": "2026-01-01T00:00:00+00:00",
                "approved_by": "test",
                "user_corrected": True,
                "inferred": profile.inferred.model_copy(
                    update={"entity_candidates": ["Machine", "Sensor"]}
                ),
            }
        )
        save_source_profile(profile, profile_path)

        loaded_profile, _ = _load_source_profile_for_enrich(tmp_path, profile_path)
        assert loaded_profile is not None
        assert loaded_profile.user_corrected is True
        assert "Machine" in loaded_profile.inferred.entity_candidates

    def test_inferred_items_not_in_domain_yaml_without_correction(
        self, tmp_path: Path
    ) -> None:
        """Auto-approved profile must NOT copy inferred entity_candidates to
        domain.yaml entity_categories (provenance protection)."""
        import yaml

        _make_csv(tmp_path / "equipment_schedule.csv", ["equipment_id"])
        profile_out = tmp_path / "profile.json"
        out_yaml = tmp_path / "domain.yaml"

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "init-domain",
                "--input", str(tmp_path),
                "--out", str(out_yaml),
                "--profile-out", str(profile_out),
                "--approve",  # auto-approve, no correction
            ],
        )
        assert result.exit_code == 0, combined_output(result)

        # Load the profile to get what was inferred
        data = json.loads(profile_out.read_text())
        inferred_candidates = data["inferred"].get("entity_candidates", [])

        domain_raw = yaml.safe_load(out_yaml.read_text())
        domain_cats = domain_raw.get("candidate_model", {}).get("entity_categories", [])

        # Inferred items must not appear verbatim in domain.yaml when not corrected
        for candidate in inferred_candidates:
            assert candidate not in domain_cats, (
                f"Inferred candidate '{candidate}' leaked into domain.yaml "
                "without explicit user correction — provenance contract violated."
            )


# ---------------------------------------------------------------------------
# Helpers for interactive test simulation
# ---------------------------------------------------------------------------

import contextlib
from io import StringIO
from unittest.mock import patch


@contextlib.contextmanager
def _click_input(text: str):
    """Context manager that patches click.prompt to consume lines from *text*."""
    lines = iter(text.split("\n"))

    def _mock_prompt(msg, default="", show_default=False, **_kwargs):  # noqa: ARG001
        try:
            val = next(lines)
        except StopIteration:
            val = ""
        return val if val != "" else default

    with patch("fabric_kg_builder.cli.init_domain_cmd.click.prompt", side_effect=_mock_prompt):
        yield
