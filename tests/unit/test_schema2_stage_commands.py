"""CLI activation tests for the schema-2 L3 and L4 stage commands."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli.main import cli

from .test_schema2_validation_stage import _pipeline

_STAGE_FAILURE_EXIT = 5


@pytest.mark.unit
def test_stage_commands_are_registered() -> None:
    assert "validate-evidence" in cli.commands
    assert "project-serving" in cli.commands


@pytest.mark.unit
def test_validate_evidence_fails_closed_without_an_l2_handoff(
    tmp_path: Path,
) -> None:
    """A missing L2 handoff must name its audit code, not raise a traceback."""
    result = CliRunner().invoke(
        cli,
        [
            "validate-evidence",
            "--state", str(tmp_path / ".fkg" / "l3"),
            "--l2-state", str(tmp_path / "missing-l2"),
            "--l1-state", str(tmp_path / "missing-l1"),
            "--domain", str(tmp_path / "domain.yaml"),
        ],
    )

    assert result.exit_code == _STAGE_FAILURE_EXIT
    assert "L3_INPUT_RECEIPT_INVALID" in result.output


@pytest.mark.unit
def test_project_serving_fails_closed_without_an_l2_handoff(
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        cli,
        [
            "project-serving",
            "--state", str(tmp_path / ".fkg" / "l4"),
            "--l3-state", str(tmp_path / ".fkg" / "l3"),
            "--l2-state", str(tmp_path / "missing-l2"),
            "--l1-state", str(tmp_path / "missing-l1"),
            "--domain", str(tmp_path / "domain.yaml"),
        ],
    )

    assert result.exit_code == _STAGE_FAILURE_EXIT
    assert "L3_INPUT_RECEIPT_INVALID" in result.output


@pytest.mark.unit
def test_validate_evidence_runs_l3_over_a_real_l2_handoff(tmp_path: Path) -> None:
    l1_state_root, domain_path, _ = _pipeline(tmp_path, "records")

    result = CliRunner().invoke(
        cli,
        [
            "validate-evidence",
            "--state", str(tmp_path / ".fkg" / "l3"),
            "--l2-state", str(tmp_path / ".fkg" / "l2"),
            "--l1-state", str(l1_state_root),
            "--domain", str(domain_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "status: succeeded" in result.output
    assert "evidence spans:" in result.output
    assert (tmp_path / ".fkg" / "l3").is_dir()


@pytest.mark.unit
def test_validate_evidence_reuses_leaf_checkpoints_on_rerun(tmp_path: Path) -> None:
    """Re-running must reuse completed leaves rather than revalidate them."""
    l1_state_root, domain_path, _ = _pipeline(tmp_path, "records")
    args = [
        "validate-evidence",
        "--state", str(tmp_path / ".fkg" / "l3"),
        "--l2-state", str(tmp_path / ".fkg" / "l2"),
        "--l1-state", str(l1_state_root),
        "--domain", str(domain_path),
    ]
    runner = CliRunner()

    first = runner.invoke(cli, args)
    assert first.exit_code == 0, first.output
    assert "reused=0" in first.output

    second = runner.invoke(cli, args)
    assert second.exit_code == 0, second.output
    assert "recomputed=0" in second.output


@pytest.mark.unit
def test_project_serving_writes_canonical_serving_tables(tmp_path: Path) -> None:
    l1_state_root, domain_path, _ = _pipeline(tmp_path, "records")

    result = CliRunner().invoke(
        cli,
        [
            "project-serving",
            "--state", str(tmp_path / ".fkg" / "l4"),
            "--l3-state", str(tmp_path / ".fkg" / "l3"),
            "--l2-state", str(tmp_path / ".fkg" / "l2"),
            "--l1-state", str(l1_state_root),
            "--domain", str(domain_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "status: succeeded" in result.output
    assert "semantic_asserted_entities:" in result.output
    parquet = list((tmp_path / ".fkg" / "l4").rglob("*.parquet"))
    assert parquet, "L4 must persist the canonical serving tables as Parquet"


@pytest.mark.unit
def test_project_serving_does_not_emit_schema_1_enriched_tables(
    tmp_path: Path,
) -> None:
    """L4 owns the schema-2 serving shape; it must not down-convert to schema-1."""
    l1_state_root, domain_path, _ = _pipeline(tmp_path, "records")

    result = CliRunner().invoke(
        cli,
        [
            "project-serving",
            "--state", str(tmp_path / ".fkg" / "l4"),
            "--l3-state", str(tmp_path / ".fkg" / "l3"),
            "--l2-state", str(tmp_path / ".fkg" / "l2"),
            "--l1-state", str(l1_state_root),
            "--domain", str(domain_path),
        ],
    )

    assert result.exit_code == 0, result.output
    names = {path.stem for path in (tmp_path / ".fkg" / "l4").rglob("*.parquet")}
    assert "semantic_asserted_entities" in names
    # The schema-1 compile-data tables carry no assertion state and must not
    # appear here; emitting them would silently discard schema-2 semantics.
    assert not names & {"entities", "relationships", "chunks", "claims"}
