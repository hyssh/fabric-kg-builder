"""Tests for the ``fabric-kg app publish-structured`` command.

The command is the only shipped entry point that turns a sealed L4 run into an
L5a publication plan, so these tests exercise it through the CLI surface a
released wheel exposes rather than through the compiler helpers directly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli.app_cmd import app_cmd
from fabric_kg_builder.serving.lifecycle_projection import run_l4

from tests.unit.test_l5a_structured_publication import _l3_without_manifest

WORKSPACE_ID = "00000000-0000-0000-0000-0000000000ff"


@pytest.fixture
def sealed_run(tmp_path: Path) -> tuple[Path, Path]:
    l3_root = tmp_path / ".fkg" / "l3"
    l3 = _l3_without_manifest(tmp_path)
    run = run_l4(l3, state_root=tmp_path / ".fkg" / "l4")
    return run.sealed_source().root, l3_root


def _invoke(sealed_run: tuple[Path, Path], tmp_path: Path, *extra: str):
    run_root, l3_root = sealed_run
    return CliRunner().invoke(
        app_cmd,
        [
            "publish-structured",
            "--l4-run",
            str(run_root),
            "--l3-root",
            str(l3_root),
            "--workspace-id",
            WORKSPACE_ID,
            "--plan",
            str(tmp_path / "plan.json"),
            *extra,
        ],
    )


@pytest.mark.unit
def test_dry_run_is_the_default_and_seals_a_plan(sealed_run, tmp_path):
    """No flag must never mutate, and must still emit a hashed plan.

    A command whose safe mode produced no artifact would force operators to
    reach for ``--live`` to see what would happen.
    """

    result = _invoke(sealed_run, tmp_path)
    assert result.exit_code == 0, result.output
    assert "mode=dry-run" in result.output
    plan = json.loads((tmp_path / "plan.json").read_text("utf-8"))
    assert plan["release_version"] == "0.2.4"
    assert plan["workspace_id"] == WORKSPACE_ID
    assert plan["plan_hash"] in result.output


@pytest.mark.unit
def test_plan_reports_the_fabric_create_capability_as_blocked(
    sealed_run, tmp_path
):
    """The NO-GO verdict has to be in the sealed plan, not only in prose.

    Fabric item GET returns an empty ETag and DELETE ignores ``If-Match``, so
    the plan records create as unavailable for all four targets.
    """

    assert _invoke(sealed_run, tmp_path).exit_code == 0
    plan = json.loads((tmp_path / "plan.json").read_text("utf-8"))
    assert plan["live_publication_supported"] is False
    assert plan["blocked_capabilities"] == [
        "fabric.graph.create",
        "fabric.ontology.create",
        "fabric.parquet.create",
        "fabric.semantic_model.create",
    ]


@pytest.mark.unit
def test_live_without_the_exact_plan_hash_is_refused(sealed_run, tmp_path):
    """Approval is exact-match, so a stale or absent hash cannot pass."""

    result = _invoke(sealed_run, tmp_path, "--live", "--approve-live", "nope")
    assert result.exit_code != 0
    assert "exact plan hash" in result.output


@pytest.mark.unit
def test_live_with_the_exact_plan_hash_still_fails_closed(sealed_run, tmp_path):
    """Correct approval must not be mistaken for capability.

    This is the regression that matters most: an operator holding the right
    hash still cannot trigger an unfenced Fabric create.
    """

    assert _invoke(sealed_run, tmp_path).exit_code == 0
    plan_hash = json.loads((tmp_path / "plan.json").read_text("utf-8"))[
        "plan_hash"
    ]
    result = _invoke(
        sealed_run, tmp_path, "--live", "--approve-live", plan_hash
    )
    assert result.exit_code != 0
    assert "capability NO-GO" in result.output


@pytest.mark.unit
def test_plan_hash_is_stable_across_repeated_compilation(sealed_run, tmp_path):
    """A plan hash that drifts per invocation cannot authorise anything."""

    assert _invoke(sealed_run, tmp_path).exit_code == 0
    first = (tmp_path / "plan.json").read_text("utf-8")
    assert _invoke(sealed_run, tmp_path).exit_code == 0
    assert (tmp_path / "plan.json").read_text("utf-8") == first
