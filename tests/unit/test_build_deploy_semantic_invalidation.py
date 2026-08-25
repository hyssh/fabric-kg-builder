from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

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
_STAGE_DEPENDENCIES = _MODULE._STAGE_DEPENDENCIES
_approve_schema2_live_plan = _MODULE._approve_schema2_live_plan
BuildDeployError = _MODULE.BuildDeployError


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
    assert state.data["stages"]["compile_graph"]["direct_input_fingerprint"] == (
        "sha256:" + "b" * 64
    )


def test_legacy_stage_without_fingerprint_is_conservatively_invalidated(
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
    del state.data["stages"]["unrelated_stage"]["input_fingerprint"]
    del state.data["stages"]["unrelated_stage"]["direct_input_fingerprint"]
    state.save()
    state.execute(
        "unrelated_stage",
        lambda: calls.append("second") or {},
        resume=True,
    )
    assert calls == ["first", "second"]


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


def test_changed_parent_fingerprint_invalidates_descendant_with_same_direct_input(
    tmp_path: Path,
) -> None:
    state = _RunState(
        tmp_path / "state.json",
        run_id="run-1",
        environment="test",
        resume=False,
    )
    calls: list[str] = []
    stable_child_input = "sha256:" + "c" * 64

    state.execute(
        "compile_data",
        lambda: calls.append("data:first") or {},
        resume=False,
        input_fingerprint="sha256:" + "a" * 64,
    )
    state.execute(
        "compile_semantic",
        lambda: calls.append("semantic:first") or {},
        resume=False,
        input_fingerprint=stable_child_input,
    )
    state.execute(
        "compile_data",
        lambda: calls.append("data:changed") or {},
        resume=True,
        input_fingerprint="sha256:" + "b" * 64,
    )
    state.execute(
        "compile_semantic",
        lambda: calls.append("semantic:rerun") or {},
        resume=True,
        input_fingerprint=stable_child_input,
    )

    assert calls == [
        "data:first",
        "semantic:first",
        "data:changed",
        "semantic:rerun",
    ]
    assert state.data["stages"]["compile_semantic"][
        "dependency_fingerprints"
    ]["compile_data"] == state.data["stages"]["compile_data"][
        "input_fingerprint"
    ]


def test_search_change_does_not_invalidate_ontology_sibling(
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
        "compile_semantic",
        lambda: {},
        resume=False,
        input_fingerprint="sha256:" + "a" * 64,
    )
    state.execute(
        "compile_ontology",
        lambda: calls.append("ontology:first") or {},
        resume=False,
        input_fingerprint="sha256:" + "o" * 64,
    )
    state.execute(
        "compile_search",
        lambda: calls.append("search:first") or {},
        resume=False,
        input_fingerprint="sha256:" + "s" * 64,
    )
    state.execute(
        "compile_search",
        lambda: calls.append("search:changed") or {},
        resume=True,
        input_fingerprint="sha256:" + "t" * 64,
    )
    state.execute(
        "compile_ontology",
        lambda: calls.append("ontology:unexpected") or {},
        resume=True,
        input_fingerprint="sha256:" + "o" * 64,
    )

    assert calls == ["ontology:first", "search:first", "search:changed"]


def test_dependency_graph_covers_full_pipeline_authority() -> None:
    expected = {
        "enrich",
        "densify",
        "compile_data",
        "compile_semantic",
        "compile_ontology",
        "compile_graph",
        "compile_agent",
        "compile_search",
        "package",
        "validate",
        "deploy_lakehouse",
        "deploy_ontology",
        "deploy_serving",
        "validate_projection",
        "deploy_knowledge",
        "deploy_agent",
        "deploy_app",
        "deployment_receipt",
        "runtime_config",
        "runtime_acceptance",
    }

    assert expected <= set(_STAGE_DEPENDENCIES)


def test_source_domain_semantic_prompt_model_and_config_inputs_are_fingerprinted(
    tmp_path: Path,
) -> None:
    names = (
        "source.csv",
        "domain.yaml",
        "semantic-contract.yaml",
        "mappings.yaml",
        "vocabulary.yaml",
        "ids.lock.json",
        "fabric-kg.yaml",
    )
    files = {}
    for name in names:
        path = tmp_path / name
        path.write_text(f"{name}:v1", encoding="utf-8")
        files[name] = path
    values = {
        "source_profile_hash": "profile:v1",
        "prompt_schema_fingerprint": "prompt:v1",
        "model_identity": "model:v1",
    }
    baseline = _input_fingerprint(files=files, values=values)

    for name, path in files.items():
        path.write_text(f"{name}:v2", encoding="utf-8")
        assert _input_fingerprint(files=files, values=values) != baseline, name
        path.write_text(f"{name}:v1", encoding="utf-8")

    for name in values:
        changed = {**values, name: f"{name}:v2"}
        assert _input_fingerprint(files=files, values=changed) != baseline, name


def test_stage_dependency_graph_is_acyclic_and_reaches_validation() -> None:
    def descendants(root: str) -> set[str]:
        found: set[str] = set()
        pending = [root]
        while pending:
            parent = pending.pop()
            for stage, dependencies in _STAGE_DEPENDENCIES.items():
                if parent in dependencies and stage not in found:
                    found.add(stage)
                    pending.append(stage)
        return found

    for stage in _STAGE_DEPENDENCIES:
        assert stage not in descendants(stage), stage

    enrich_descendants = descendants("enrich")
    assert {
        "compile_data",
        "compile_semantic",
        "compile_search",
        "package",
        "validate",
        "deploy_lakehouse",
        "deploy_ontology",
        "deploy_serving",
        "validate_projection",
        "deployment_receipt",
        "runtime_acceptance",
    } <= enrich_descendants


def test_invalidated_local_stage_clears_only_declared_run_output(
    tmp_path: Path,
) -> None:
    state = _RunState(
        tmp_path / "state.json",
        run_id="run-1",
        environment="test",
        resume=False,
    )
    output = tmp_path / "build" / "parquet"
    state.execute(
        "compile_data",
        lambda: output.mkdir(parents=True) or {},
        resume=False,
        input_fingerprint="sha256:" + "a" * 64,
        invalidate_paths=(output,),
    )
    stale = output / "stale.parquet"
    stale.write_text("stale", encoding="utf-8")

    def rebuild() -> dict:
        assert not stale.exists()
        output.mkdir(parents=True)
        (output / "current.parquet").write_text("current", encoding="utf-8")
        return {}

    state.execute(
        "compile_data",
        rebuild,
        resume=True,
        input_fingerprint="sha256:" + "b" * 64,
        invalidate_paths=(output,),
    )

    assert not stale.exists()
    assert (output / "current.parquet").is_file()


def test_deploy_stage_skips_when_only_stage_written_metadata_changes(
    tmp_path: Path,
) -> None:
    state = _RunState(
        tmp_path / "state.json",
        run_id="run-1",
        environment="test",
        resume=False,
    )
    authority = tmp_path / "source-agent-metadata.yaml"
    authority.write_text("agentName: stable\n", encoding="utf-8")
    runtime_metadata = tmp_path / "agent-metadata.yaml"
    registry = tmp_path / "registry.json"
    calls: list[str] = []
    direct = _input_fingerprint(files={"metadata_authority": authority})

    def deploy() -> dict:
        calls.append("deploy")
        runtime_metadata.write_text(
            "deploymentContext:\n  test:\n    version: random\n",
            encoding="utf-8",
        )
        registry.write_text('{"deployment_id":"random"}', encoding="utf-8")
        return {}

    state.execute(
        "deploy_agent",
        deploy,
        resume=False,
        input_fingerprint=direct,
        dependencies=(),
    )
    state.execute(
        "deploy_agent",
        deploy,
        resume=True,
        input_fingerprint=_input_fingerprint(
            files={"metadata_authority": authority}
        ),
        dependencies=(),
    )

    assert calls == ["deploy"]


def test_enrich_invalidation_preserves_shared_registry(
    tmp_path: Path,
) -> None:
    state = _RunState(
        tmp_path / "state.json",
        run_id="run-1",
        environment="test",
        resume=False,
    )
    enriched = tmp_path / "build" / "enriched"
    registry = tmp_path / "registry.json"
    registry.write_text('{"deployments":["existing"]}', encoding="utf-8")

    state.execute(
        "enrich",
        lambda: enriched.mkdir(parents=True) or {},
        resume=False,
        input_fingerprint="sha256:" + "a" * 64,
        dependencies=(),
        invalidate_paths=(enriched,),
    )

    def rerun() -> dict:
        assert registry.is_file()
        assert "existing" in registry.read_text(encoding="utf-8")
        enriched.mkdir(parents=True)
        return {}

    state.execute(
        "enrich",
        rerun,
        resume=True,
        input_fingerprint="sha256:" + "b" * 64,
        dependencies=(),
        invalidate_paths=(enriched,),
    )


def test_approved_schema2_plan_remains_resumable_after_failure(
    tmp_path: Path,
) -> None:
    state = _RunState(
        tmp_path / "state.json",
        run_id="run-1",
        environment="test",
        resume=False,
    )
    plan_fingerprint = "sha256:" + "a" * 64
    baseline_fingerprint = "sha256:" + "b" * 64
    state.data["plan_fingerprint"] = plan_fingerprint
    state.data["reviewed_semantic_baseline_fingerprint"] = (
        baseline_fingerprint
    )
    state.complete(dry_run=True)

    _approve_schema2_live_plan(
        state,
        plan_fingerprint=plan_fingerprint,
        managed_baseline_fingerprint=baseline_fingerprint,
    )
    assert state.data["approved_plan_fingerprint"] == plan_fingerprint

    state.data["status"] = "failed"
    state.save()
    with pytest.raises(BuildDeployError, match="managed semantic baseline"):
        _approve_schema2_live_plan(
            state,
            plan_fingerprint=plan_fingerprint,
            managed_baseline_fingerprint="sha256:" + "c" * 64,
        )

    state.data["stages"]["record_semantic_baseline"] = {
        "status": "succeeded",
        "details": {
            "semantic_baseline_fingerprint": "sha256:" + "c" * 64
        },
    }
    _approve_schema2_live_plan(
        state,
        plan_fingerprint=plan_fingerprint,
        managed_baseline_fingerprint="sha256:" + "c" * 64,
    )


def test_new_dry_run_can_review_contract_change_after_recorded_baseline(
    tmp_path: Path,
) -> None:
    state = _RunState(
        tmp_path / "state.json",
        run_id="run-1",
        environment="test",
        resume=False,
    )
    old_plan = "sha256:" + "a" * 64
    new_plan = "sha256:" + "b" * 64
    old_baseline = "sha256:" + "c" * 64
    state.data.update(
        {
            "status": "failed",
            "plan_fingerprint": old_plan,
            "approved_plan_fingerprint": old_plan,
            "reviewed_semantic_baseline_fingerprint": old_baseline,
            "stages": {
                "record_semantic_baseline": {
                    "status": "succeeded",
                    "details": {
                        "semantic_baseline_fingerprint": old_baseline
                    },
                }
            },
        }
    )
    state.data["plan_fingerprint"] = new_plan
    state.data["reviewed_semantic_baseline_fingerprint"] = old_baseline
    state.data.pop("approved_plan_fingerprint")
    state.complete(dry_run=True)

    _approve_schema2_live_plan(
        state,
        plan_fingerprint=new_plan,
        managed_baseline_fingerprint=old_baseline,
    )

    assert state.data["approved_plan_fingerprint"] == new_plan


def test_managed_baseline_write_does_not_invalidate_compatibility_stage(
    tmp_path: Path,
) -> None:
    state = _RunState(
        tmp_path / "state.json",
        run_id="run-1",
        environment="test",
        resume=False,
    )
    baseline = tmp_path / "managed-baseline.yaml"
    reviewed = _input_fingerprint(
        files={"managed_semantic_baseline": baseline}
    )
    direct = _input_fingerprint(
        values={"managed_baseline_fingerprint": reviewed}
    )
    calls: list[str] = []
    state.execute(
        "semantic_compatibility",
        lambda: calls.append("first") or {},
        resume=False,
        input_fingerprint=direct,
        dependencies=(),
    )
    baseline.write_text("contract: newly recorded\n", encoding="utf-8")
    state.execute(
        "semantic_compatibility",
        lambda: calls.append("unexpected") or {},
        resume=True,
        input_fingerprint=_input_fingerprint(
            values={"managed_baseline_fingerprint": reviewed}
        ),
        dependencies=(),
    )

    assert calls == ["first"]
