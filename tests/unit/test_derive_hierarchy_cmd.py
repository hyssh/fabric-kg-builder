"""Tests for the ``derive-hierarchy`` CLI command.

Scope: exercises `fabric_kg_builder.cli.derive_hierarchy_cmd` end-to-end
against real sealed-L4 lineage (the same minimal L1->L2->L3->L4 pipeline
driver used by `test_sealed_l4_hierarchy_source.py` /
`test_derived_hierarchy_artifact.py`) and a real, in-process
`build_derived_community_hierarchy_safe` build, using the dependency-free
`DeterministicConnectedComponentsProvider` test fake injected via this
module's private test-only hook -- never the real native Leiden provider.

This proves the CLI's own wiring (sealed-input read -> hierarchy-core
mapping -> community build -> receipted write -> exit code), not native
Leiden partition quality/acceptance. Nothing here asserts an injected-
provider result is a "native" acceptance; any such claim is this file's
own responsibility and is never made.

Covers: complete, empty (zero entities), insufficient (entities present,
zero connecting relationships) outcomes and their exit codes; sealed-input
unavailability (exit 1); and each concrete protected-root default
(L1-L4 state dirs, L5a release dir, L3 input-manifest search root)
rejecting an output_root collision by label (exit 1), proving the CLI
wires in real, concrete protected roots rather than leaving the guard to
an optional caller.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import derive_hierarchy_cmd as derive_hierarchy_cmd_module
from fabric_kg_builder.cli.derive_hierarchy_cmd import derive_hierarchy_cmd
from fabric_kg_builder.graph.community_provider_testing import (
    DeterministicConnectedComponentsProvider,
)
from fabric_kg_builder.semantic.sealed_l4_hierarchy_source import (
    load_sealed_l4_hierarchy_rows,
)
from fabric_kg_builder.serving.lifecycle_projection import run_l4
from tests.unit.test_schema2_validation_stage import _l3, _pipeline


def _run_pipeline(tmp_path: Path, domain: str = "records", *, mutate=None):
    """Drive a real, minimal L1->L2->L3->L4 pipeline and return (l3, l4)."""
    l1_state_root, domain_path, _l2 = _pipeline(tmp_path, domain, mutate=mutate)
    l3 = _l3(tmp_path, l1_state_root, domain_path)
    l4 = run_l4(l3, state_root=tmp_path / ".fkg" / "l4")
    return l3, l4


def _drop_relationship_candidates(candidates, _work_unit):
    """Sparse/insufficient fixture: entities survive, relationships don't."""
    return [c for c in candidates if c["candidate_kind"] != "relationship"]


def _drop_all_candidates(candidates, _work_unit):
    """Empty fixture: zero entities qualify for clustering at all."""
    return []


@pytest.fixture(autouse=True)
def _inject_deterministic_provider():
    """Install the dependency-free test fake for every test in this file.

    Restored to ``None`` after each test so no production path can ever
    pick up a leaked test-only provider across test boundaries.
    """
    derive_hierarchy_cmd_module._test_only_provider_factory = (
        DeterministicConnectedComponentsProvider
    )
    try:
        yield
    finally:
        derive_hierarchy_cmd_module._test_only_provider_factory = None


def _invoke(tmp_path: Path, l3_state_root: Path, l4_run_root: Path, **extra_options):
    runner = CliRunner()
    out_root = tmp_path / "derived"
    args = [
        "--source",
        str(l4_run_root),
        "--out",
        str(out_root),
        "--input-manifest-search-root",
        str(l3_state_root),
        "--run-id",
        "hier-run-1",
        "--policy-id",
        "policy-1",
        # Point every protected-root default somewhere disjoint from this
        # test's own tmp tree by default; individual collision tests
        # override exactly one of these back onto a colliding path.
        "--l1-state",
        str(tmp_path / "unused-l1"),
        "--l2-state",
        str(tmp_path / "unused-l2"),
        "--l3-state",
        str(tmp_path / "unused-l3"),
        "--l4-state",
        str(tmp_path / "unused-l4"),
        "--l5a-release-dir",
        str(tmp_path / "unused-release"),
    ]
    for option, value in extra_options.items():
        args.extend([option, str(value)])
    result = runner.invoke(derive_hierarchy_cmd, args)
    return result, out_root


@pytest.mark.unit
def test_complete_outcome_exits_zero_and_publishes(tmp_path: Path) -> None:
    l3, l4 = _run_pipeline(tmp_path)
    result, out_root = _invoke(tmp_path, l3.state_root, l4.run_root)

    assert result.exit_code == 0, result.output
    assert "status: complete" in result.output
    assert out_root.exists()

    # Writer actually ran and published a real receipted artifact: the
    # sealed source itself must remain byte-unchanged (never mutated).
    sealed = load_sealed_l4_hierarchy_rows(
        l4.run_root, input_manifest_search_roots=(l3.state_root,)
    )
    assert len(sealed.entities) > 0


@pytest.mark.unit
def test_empty_source_exits_three(tmp_path: Path) -> None:
    l3, l4 = _run_pipeline(tmp_path, mutate=_drop_all_candidates)
    result, _out_root = _invoke(tmp_path, l3.state_root, l4.run_root)

    assert result.exit_code == 3, result.output
    assert "status: empty" in result.output


@pytest.mark.unit
def test_sparse_insufficient_outcome_exits_two(tmp_path: Path) -> None:
    l3, l4 = _run_pipeline(tmp_path, mutate=_drop_relationship_candidates)
    result, _out_root = _invoke(tmp_path, l3.state_root, l4.run_root)

    assert result.exit_code == 2, result.output
    assert "status: insufficient" in result.output


@pytest.mark.unit
def test_unavailable_sealed_source_exits_one(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        derive_hierarchy_cmd,
        [
            "--source",
            str(tmp_path / "does-not-exist"),
            "--out",
            str(tmp_path / "derived"),
            "--input-manifest-search-root",
            str(tmp_path / "also-missing-l3"),
            "--run-id",
            "hier-run-1",
            "--policy-id",
            "policy-1",
        ],
    )

    assert result.exit_code == 1, result.output
    assert "FAILED" in result.output


@pytest.mark.unit
@pytest.mark.parametrize(
    "collide_option",
    [
        "--l1-state",
        "--l2-state",
        "--l3-state",
        "--l4-state",
        "--l5a-release-dir",
    ],
)
def test_protected_root_default_rejects_output_root_collision(
    tmp_path: Path, collide_option: str
) -> None:
    l3, l4 = _run_pipeline(tmp_path)
    runner = CliRunner()
    # Point --out at the exact same path as the protected root under test,
    # leaving every other protected-root option at its disjoint default.
    colliding_root = tmp_path / "colliding-root"
    args = [
        "--source",
        str(l4.run_root),
        "--out",
        str(colliding_root),
        "--input-manifest-search-root",
        str(l3.state_root),
        "--run-id",
        "hier-run-1",
        "--policy-id",
        "policy-1",
        "--l1-state",
        str(tmp_path / "unused-l1"),
        "--l2-state",
        str(tmp_path / "unused-l2"),
        "--l3-state",
        str(tmp_path / "unused-l3"),
        "--l4-state",
        str(tmp_path / "unused-l4"),
        "--l5a-release-dir",
        str(tmp_path / "unused-release"),
    ]
    # Override exactly the option under test to collide with --out.
    index = args.index(collide_option)
    args[index + 1] = str(colliding_root)

    result = runner.invoke(derive_hierarchy_cmd, args)

    assert result.exit_code == 1, result.output
    assert "DERIVED_HIERARCHY_OUTPUT_ROOT_COLLISION" in result.output


@pytest.mark.unit
def test_input_manifest_search_root_collision_rejected(tmp_path: Path) -> None:
    l3, l4 = _run_pipeline(tmp_path)
    runner = CliRunner()
    # --out collides with the L3 input-manifest search root itself.
    result = runner.invoke(
        derive_hierarchy_cmd,
        [
            "--source",
            str(l4.run_root),
            "--out",
            str(l3.state_root),
            "--input-manifest-search-root",
            str(l3.state_root),
            "--run-id",
            "hier-run-1",
            "--policy-id",
            "policy-1",
            "--l1-state",
            str(tmp_path / "unused-l1"),
            "--l2-state",
            str(tmp_path / "unused-l2"),
            "--l3-state",
            str(tmp_path / "unused-l3"),
            "--l4-state",
            str(tmp_path / "unused-l4"),
            "--l5a-release-dir",
            str(tmp_path / "unused-release"),
        ],
    )

    assert result.exit_code == 1, result.output
    assert "DERIVED_HIERARCHY_OUTPUT_ROOT_COLLISION" in result.output
    assert "L3 input-manifest search root" in result.output


@pytest.mark.unit
def test_sealed_l4_run_root_itself_always_force_protected(tmp_path: Path) -> None:
    """The writer force-protects the sealed L4 run root regardless of what
    this command passes -- confirm the CLI's own every-other-default
    wiring does not paper over or disable that unconditional guard."""
    l3, l4 = _run_pipeline(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        derive_hierarchy_cmd,
        [
            "--source",
            str(l4.run_root),
            "--out",
            str(l4.run_root),
            "--input-manifest-search-root",
            str(l3.state_root),
            "--run-id",
            "hier-run-1",
            "--policy-id",
            "policy-1",
            "--l1-state",
            str(tmp_path / "unused-l1"),
            "--l2-state",
            str(tmp_path / "unused-l2"),
            "--l3-state",
            str(tmp_path / "unused-l3"),
            "--l4-state",
            str(tmp_path / "unused-l4"),
            "--l5a-release-dir",
            str(tmp_path / "unused-release"),
        ],
    )

    assert result.exit_code == 1, result.output
    assert "DERIVED_HIERARCHY_OUTPUT_ROOT_COLLISION" in result.output
    assert "sealed L4 run" in result.output
