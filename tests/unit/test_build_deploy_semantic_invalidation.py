from __future__ import annotations

import importlib.util
from pathlib import Path

_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "fabric_kg_builder"
    / "cli"
    / "build_deploy_cmd.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "test_build_deploy_cmd_module",
    _MODULE_PATH,
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
_RunState = _MODULE._RunState
_input_fingerprint = _MODULE._input_fingerprint


def test_input_fingerprint_changes_with_projection_and_plan(tmp_path: Path) -> None:
    projection = tmp_path / "semantic-projection-receipt.json"
    plan = tmp_path / "materialization-plan.json"
    projection.write_text('{"hash":"one"}', encoding="utf-8")
    plan.write_text('{"hash":"plan"}', encoding="utf-8")

    first = _input_fingerprint(
        files={"projection": projection, "plan": plan}
    )
    projection.write_text('{"hash":"two"}', encoding="utf-8")
    second = _input_fingerprint(
        files={"projection": projection, "plan": plan}
    )

    assert first != second


def test_resume_skips_only_matching_semantic_fingerprint(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    state = _RunState(
        state_path,
        run_id="run-1",
        environment="test",
        resume=False,
    )
    calls: list[str] = []

    state.execute(
        "compile_graph",
        lambda: calls.append("first") or {"value": 1},
        resume=False,
        input_fingerprint="sha256:" + "a" * 64,
    )
    state.execute(
        "compile_graph",
        lambda: calls.append("skipped") or {"value": 2},
        resume=True,
        input_fingerprint="sha256:" + "a" * 64,
    )
    state.execute(
        "compile_graph",
        lambda: calls.append("rerun") or {"value": 3},
        resume=True,
        input_fingerprint="sha256:" + "b" * 64,
    )

    assert calls == ["first", "rerun"]
    assert state.data["stages"]["compile_graph"]["details"] == {"value": 3}
    assert state.data["stages"]["compile_graph"]["input_fingerprint"] == (
        "sha256:" + "b" * 64
    )


def test_legacy_stage_without_fingerprint_keeps_resume_behavior(
    tmp_path: Path,
) -> None:
    state = _RunState(
        tmp_path / "state.json",
        run_id="run-1",
        environment="test",
        resume=False,
    )
    calls: list[str] = []
    state.execute(
        "unrelated_stage",
        lambda: calls.append("first") or {},
        resume=False,
    )
    state.execute(
        "unrelated_stage",
        lambda: calls.append("second") or {},
        resume=True,
    )
    assert calls == ["first"]


def test_semantic_change_invalidates_all_downstream_artifact_stages(
    tmp_path: Path,
) -> None:
    authority = tmp_path / "semantic-manifest.json"
    authority.write_text('{"contract_hash":"one"}', encoding="utf-8")
    first_fingerprint = _input_fingerprint(
        files={"semantic_manifest": authority}
    )
    state = _RunState(
        tmp_path / "state.json",
        run_id="run-1",
        environment="test",
        resume=False,
    )
    stages = (
        "compile_agent",
        "compile_search",
        "package",
        "validate",
        "deployment_receipt",
    )
    calls: list[str] = []
    for stage in stages:
        state.execute(
            stage,
            lambda stage=stage: calls.append(f"first:{stage}") or {},
            resume=False,
            input_fingerprint=first_fingerprint,
        )
    for stage in stages:
        state.execute(
            stage,
            lambda stage=stage: calls.append(f"stale:{stage}") or {},
            resume=True,
            input_fingerprint=first_fingerprint,
        )

    authority.write_text('{"contract_hash":"two"}', encoding="utf-8")
    second_fingerprint = _input_fingerprint(
        files={"semantic_manifest": authority}
    )
    for stage in stages:
        state.execute(
            stage,
            lambda stage=stage: calls.append(f"rerun:{stage}") or {},
            resume=True,
            input_fingerprint=second_fingerprint,
        )

    assert [call for call in calls if call.startswith("stale:")] == []
    assert [call for call in calls if call.startswith("rerun:")] == [
        f"rerun:{stage}" for stage in stages
    ]


def test_each_sealed_authority_file_changes_downstream_fingerprint(
    tmp_path: Path,
) -> None:
    names = (
        "normalized-contract.json",
        "semantic-manifest.json",
        "semantic-model-manifest.json",
        "semantic-crosswalk.json",
        "materialization-plan.json",
        "model-quality-report.json",
        "dependency-graph.json",
        "semantic-projection-receipt.json",
    )
    files = {}
    for name in names:
        path = tmp_path / name
        path.write_text('{"version":1}', encoding="utf-8")
        files[name] = path
    baseline = _input_fingerprint(
        files=files,
        directories={"semantic": tmp_path},
    )

    for name in names:
        files[name].write_text('{"version":2}', encoding="utf-8")
        changed = _input_fingerprint(
            files=files,
            directories={"semantic": tmp_path},
        )
        assert changed != baseline, name
        files[name].write_text('{"version":1}', encoding="utf-8")
