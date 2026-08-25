from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from fabric_kg_builder.infra.runner import FakeCommandRunner
from fabric_kg_builder.infra.schema import InfraManifest

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
_AuthorityBoundCredential = _MODULE._AuthorityBoundCredential
_build_resolved_mutation_snapshot = _MODULE._build_resolved_mutation_snapshot
_canonical_authority_key = _MODULE._canonical_authority_key
_canonical_json_hash = _MODULE._canonical_json_hash
_load_authority_document = _MODULE._load_authority_document
_load_deployment_authority = _MODULE._load_deployment_authority
_identity_authority_environment = _MODULE._identity_authority_environment
_infrastructure_authority_source = _MODULE._infrastructure_authority_source
_import_infrastructure_state = _MODULE._import_infrastructure_state
_load_infrastructure_outputs_authority = (
    _MODULE._load_infrastructure_outputs_authority
)
_merge_authoritative_runtime_outputs = (
    _MODULE._merge_authoritative_runtime_outputs
)
_resolve_live_identity_authority = _MODULE._resolve_live_identity_authority
_runtime_environment = _MODULE._runtime_environment
_secret_free_authority = _MODULE._secret_free_authority
BuildDeployError = _MODULE.BuildDeployError


def _mutation_authority(
    **overrides,
) -> tuple[dict, str]:
    values = {
        "infrastructure_manifest": {
            "azure": {
                "subscription_id": "sub-1",
                "resource_group": {"name": "rg-1"},
            },
            "fabric": {
                "workspace": {"item_id": "workspace-1"},
                "lakehouse": {"item_id": "lakehouse-1"},
            },
        },
        "infrastructure_plan": {
            "items": [{"action": "adopt", "resource_name": "search-1"}]
        },
        "infrastructure_baseline_state": {"environment": "test"},
        "imported_outputs": {
            "value": {
                "searchEndpoint": "https://search.example.test",
                "fabricWorkspaceId": "workspace-1",
            }
        },
        "authoritative_arm_outputs": {
            "searchEndpoint": "https://search.example.test"
        },
        "deployment_manifest": {
            "value": {"workspace_id": "workspace-1"}
        },
        "densify_configuration": {
            "value": {"top_k": 3, "relationship_types": ["related_to"]}
        },
        "enabled_stages": ["deploy_lakehouse", "deploy_serving"],
        "behavior_flags": {"deploy_serving": True, "provision": False},
        "identity_authority": {
            "tenant_id": "00000000-0000-4000-8000-000000000001",
            "audiences": {
                "fabric": "https://api.fabric.microsoft.com/.default",
                "search": "https://search.azure.com/.default",
            },
        },
        "package_version": "0.2.4",
        "implementation_fingerprint": "sha256:" + "d" * 64,
    }
    values.update(overrides)
    return _build_resolved_mutation_snapshot(**values)


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


def test_before_start_rejection_does_not_mutate_reviewed_run_state(
    tmp_path: Path,
) -> None:
    state = _RunState(
        tmp_path / "state.json",
        run_id="run-1",
        environment="test",
        resume=False,
    )
    state.complete(dry_run=True)

    with pytest.raises(BuildDeployError, match="authority drift"):
        state.execute(
            "infrastructure",
            lambda: pytest.fail("live action must not run"),
            resume=True,
            before_start=lambda: (_ for _ in ()).throw(
                BuildDeployError("authority drift")
            ),
        )

    persisted = _RunState(
        tmp_path / "state.json",
        run_id="run-1",
        environment="test",
        resume=True,
    )
    assert persisted.data["status"] == "planned"
    assert "infrastructure" not in persisted.data["stages"]


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
    authority, authority_hash = _mutation_authority()
    state.data["resolved_mutation_authority"] = authority
    state.data["resolved_mutation_authority_hash"] = authority_hash
    state.complete(dry_run=True)
    state.data["status"] = "running"
    state.save()

    _approve_schema2_live_plan(
        state,
        plan_fingerprint=plan_fingerprint,
        managed_baseline_fingerprint=baseline_fingerprint,
        mutation_authority_snapshot=authority,
        mutation_authority_hash=authority_hash,
    )
    assert state.data["approved_plan_fingerprint"] == plan_fingerprint

    state.data["status"] = "failed"
    state.save()
    with pytest.raises(BuildDeployError, match="managed semantic baseline"):
        _approve_schema2_live_plan(
            state,
            plan_fingerprint=plan_fingerprint,
            managed_baseline_fingerprint="sha256:" + "c" * 64,
            mutation_authority_snapshot=authority,
            mutation_authority_hash=authority_hash,
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
        mutation_authority_snapshot=authority,
        mutation_authority_hash=authority_hash,
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
    authority, authority_hash = _mutation_authority()
    state.data["resolved_mutation_authority"] = authority
    state.data["resolved_mutation_authority_hash"] = authority_hash
    state.data.pop("approved_plan_fingerprint")
    state.complete(dry_run=True)

    _approve_schema2_live_plan(
        state,
        plan_fingerprint=new_plan,
        managed_baseline_fingerprint=old_baseline,
        mutation_authority_snapshot=authority,
        mutation_authority_hash=authority_hash,
    )

    assert state.data["approved_plan_fingerprint"] == new_plan


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        (
            "imported_outputs",
            {"value": {"fabricWorkspaceId": "workspace-2"}},
        ),
        (
            "authoritative_arm_outputs",
            {"searchEndpoint": "https://changed.example.test"},
        ),
        (
            "densify_configuration",
            {"value": {"top_k": 5, "relationship_types": ["related_to"]}},
        ),
        ("implementation_fingerprint", "sha256:" + "e" * 64),
        (
            "identity_authority",
            {
                "tenant_id": "00000000-0000-4000-8000-000000000002",
                "audiences": {
                    "fabric": "https://api.fabric.microsoft.com/.default",
                    "search": "https://search.azure.com/.default",
                },
            },
        ),
    ],
)
def test_resolved_mutation_authority_detects_required_drift(
    field: str,
    changed,
) -> None:
    _, baseline_hash = _mutation_authority()
    _, changed_hash = _mutation_authority(**{field: changed})
    assert changed_hash != baseline_hash


def test_strict_snapshot_keeps_required_named_mutation_authority() -> None:
    snapshot, _ = _mutation_authority()

    infrastructure = snapshot["infrastructure"]
    assert infrastructure["manifest"]["azure"]["subscription_id"] == "sub-1"
    assert infrastructure["manifest"]["fabric"]["workspace"]["item_id"] == (
        "workspace-1"
    )
    assert infrastructure["plan"]["items"][0] == {
        "action": "adopt",
        "resource_name": "search-1",
    }
    assert infrastructure["imported_outputs"]["value"][
        "searchEndpoint"
    ] == "https://search.example.test"
    assert snapshot["pipeline"]["identity_authority"]["tenant_id"].endswith(
        "0001"
    )
    assert snapshot["pipeline"]["identity_authority"]["audiences"][
        "fabric"
    ].endswith("/.default")


def test_strict_snapshot_preserves_complete_typed_manifest_and_outputs() -> None:
    manifest = InfraManifest.model_validate({
        "environment": "dev",
        "azure": {
            "subscription_id": "00000000-0000-4000-8000-000000000001",
            "resource_group": {"mode": "connect", "name": "rg-authority"},
        },
        "resources": {
            "storage": {
                "mode": "connect",
                "name": "storageauthority",
                "container": "kg-authority",
            },
            "search": {
                "mode": "connect",
                "name": "search-authority",
            },
        },
        "fabric": {
            "workspace": {
                "mode": "connect",
                "item_id": "00000000-0000-4000-8000-000000000002",
            },
            "lakehouse": {
                "mode": "connect",
                "item_id": "00000000-0000-4000-8000-000000000003",
            },
        },
    })
    snapshot, _ = _mutation_authority(
        infrastructure_manifest=manifest.model_dump(mode="json"),
        imported_outputs={
            "value": {
                "containerName": "kg-authority",
                "fabricWorkspaceId": (
                    "00000000-0000-4000-8000-000000000002"
                ),
                "fabricLakehouseId": (
                    "00000000-0000-4000-8000-000000000003"
                ),
                "searchEndpoint": "https://search-authority.example.test",
            }
        },
    )

    saved_manifest = snapshot["infrastructure"]["manifest"]
    assert saved_manifest["resources"]["storage"]["container"] == (
        "kg-authority"
    )
    assert saved_manifest["resources"]["search"]["name"] == (
        "search-authority"
    )
    assert saved_manifest["fabric"]["workspace"]["item_id"].endswith(
        "0002"
    )
    imported = snapshot["infrastructure"]["imported_outputs"]["value"]
    assert imported["containerName"] == "kg-authority"
    assert imported["fabricLakehouseId"].endswith("0003")


def test_resolved_mutation_authority_excludes_secrets_and_source_content() -> None:
    snapshot, _ = _mutation_authority(
        imported_outputs={
            "value": {
                "token": "unlabeled credential " + "A" * 32,
                "source_text": "Customer Jane jane@example.test 123-45-6789",
                "searchEndpoint": "https://search.example.test",
            }
        }
    )
    serialized = str(snapshot)
    assert "A" * 32 not in serialized
    assert "Customer Jane" not in serialized
    assert "jane@example.test" not in serialized
    assert "123-45-6789" not in serialized
    imported = snapshot["infrastructure"]["imported_outputs"]["value"]
    assert "token" not in imported
    assert "source_text" not in imported


def test_authority_key_normalization_covers_case_and_separators() -> None:
    assert _canonical_authority_key("clientSecret") == "clientsecret"
    assert _canonical_authority_key("ClientSecret") == "clientsecret"
    assert _canonical_authority_key("CLIENT_SECRET") == "clientsecret"
    assert _canonical_authority_key("client-secret") == "clientsecret"
    assert _canonical_authority_key("client.secret") == "clientsecret"


def test_live_identity_authority_binds_tenant_and_app_audience(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FABRIC_KG_TENANT_ID", raising=False)
    monkeypatch.delenv("FABRIC_KG_API_SCOPE", raising=False)
    monkeypatch.setenv(
        "AZURE_TENANT_ID",
        "00000000-0000-4000-8000-0000000000AA",
    )
    monkeypatch.setenv(
        "FABRIC_KG_AUDIENCE",
        "api://00000000-0000-4000-8000-0000000000BB",
    )

    authority = _resolve_live_identity_authority({
        "provision": True,
        "deploy_serving": True,
        "deploy_knowledge": True,
        "deploy_agent": True,
        "deploy_app": True,
    })

    assert authority["tenant_id"] == (
        "00000000-0000-4000-8000-0000000000aa"
    )
    assert authority["audiences"]["application"] == (
        "api://00000000-0000-4000-8000-0000000000bb"
    )
    assert authority["audiences"]["application_scope"] == (
        "api://00000000-0000-4000-8000-0000000000bb/.default"
    )
    assert authority["audiences"]["azure_management"].endswith(
        "/.default"
    )
    assert authority["audiences"]["fabric"].endswith("/.default")
    assert authority["audiences"]["foundry_ai"] == (
        "https://ai.azure.com/.default"
    )
    assert authority["audiences"]["storage"].endswith("/.default")
    assert authority["audiences"]["search"].endswith("/.default")
    snapshot, _ = _mutation_authority(identity_authority=authority)
    assert snapshot["pipeline"]["identity_authority"]["audiences"][
        "foundry_ai"
    ] == "https://ai.azure.com/.default"


def test_live_identity_authority_rejects_invalid_tenant_or_audience(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FABRIC_KG_TENANT_ID", raising=False)
    monkeypatch.delenv("FABRIC_KG_API_SCOPE", raising=False)
    monkeypatch.setenv("AZURE_TENANT_ID", "not-a-guid")
    with pytest.raises(BuildDeployError, match="GUID tenant ID"):
        _resolve_live_identity_authority({"deploy_serving": True})

    monkeypatch.setenv(
        "AZURE_TENANT_ID",
        "00000000-0000-4000-8000-000000000001",
    )
    monkeypatch.setenv(
        "FABRIC_KG_AUDIENCE",
        "https://app.example.test/audience?sig=opaque",
    )
    with pytest.raises(BuildDeployError, match="FABRIC_KG_AUDIENCE"):
        _resolve_live_identity_authority({"deploy_app": True})

    monkeypatch.setenv(
        "FABRIC_KG_AUDIENCE",
        "api://00000000-0000-4000-8000-000000000002",
    )
    monkeypatch.setenv(
        "FABRIC_KG_API_SCOPE",
        "https://management.azure.com/.default",
    )
    with pytest.raises(BuildDeployError, match="must equal"):
        _resolve_live_identity_authority({"deploy_app": True})


def test_live_identity_authority_rejects_tenant_ambiguity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "FABRIC_KG_TENANT_ID",
        "00000000-0000-4000-8000-000000000001",
    )
    monkeypatch.setenv(
        "AZURE_TENANT_ID",
        "00000000-0000-4000-8000-000000000002",
    )

    with pytest.raises(BuildDeployError, match="different tenants"):
        _resolve_live_identity_authority({"deploy_serving": True})


def test_live_identity_authority_uses_subscription_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FABRIC_KG_TENANT_ID", raising=False)
    monkeypatch.setenv(
        "AZURE_TENANT_ID",
        "00000000-0000-4000-8000-000000000010",
    )
    runner = FakeCommandRunner()
    runner.add_response(
        [
            "az",
            "account",
            "show",
            "--subscription",
            "subscription-1",
            "--output",
            "json",
        ],
        stdout=(
            '{"id":"subscription-1",'
            '"tenantId":"00000000-0000-4000-8000-000000000010"}'
        ),
    )

    authority = _resolve_live_identity_authority(
        {"deploy_serving": True},
        subscription_id="subscription-1",
        runner=runner,
    )

    assert authority["tenant_id"].endswith("0010")


def test_bound_credential_enforces_approved_tenant_and_scope() -> None:
    calls = []

    class Credential:
        def get_token(self, *scopes, **kwargs):
            calls.append((scopes, kwargs))
            return object()

    authority = {
        "tenant_id": "00000000-0000-4000-8000-000000000010",
        "audiences": {
            "fabric": "https://api.fabric.microsoft.com/.default",
            "application": "api://app",
        },
    }
    credential = _AuthorityBoundCredential(Credential(), authority)

    credential.get_token("https://api.fabric.microsoft.com/.default")

    assert calls[0][1]["tenant_id"] == authority["tenant_id"]
    with pytest.raises(BuildDeployError, match="scope differs"):
        credential.get_token("https://storage.azure.com/.default")
    with pytest.raises(BuildDeployError, match="tenant differs"):
        credential.get_token(
            "https://api.fabric.microsoft.com/.default",
            tenant_id="00000000-0000-4000-8000-000000000099",
        )


def test_identity_environment_seals_app_scope() -> None:
    environment = _identity_authority_environment({
        "tenant_id": "00000000-0000-4000-8000-000000000010",
        "audiences": {
            "fabric": "https://api.fabric.microsoft.com/.default",
            "application": "api://approved-app",
            "application_scope": "api://approved-app/.default",
        },
    })

    assert environment == {
        "AZURE_TENANT_ID": "00000000-0000-4000-8000-000000000010",
        "FABRIC_KG_APPROVED_TENANT_ID": (
            "00000000-0000-4000-8000-000000000010"
        ),
        "FABRIC_KG_APPROVED_TOKEN_SCOPES": (
            '["api://approved-app/.default",'
            '"https://api.fabric.microsoft.com/.default"]'
        ),
        "FABRIC_KG_TENANT_ID": (
            "00000000-0000-4000-8000-000000000010"
        ),
        "FABRIC_KG_FABRIC_SCOPE": (
            "https://api.fabric.microsoft.com/.default"
        ),
        "FABRIC_KG_AUDIENCE": "api://approved-app",
        "FABRIC_KG_API_SCOPE": "api://approved-app/.default",
    }


def test_runtime_environment_overrides_ambient_deployment_targets(
    tmp_path: Path,
) -> None:
    environment = _runtime_environment(
        outputs={
            "searchEndpoint": "https://search.example.test",
            "containerName": "approved-container",
            "fabricWorkspaceId": "workspace-approved",
            "identityPrincipalId": "principal-approved",
        },
        run_root=tmp_path,
        environment="dev",
        subscription_id="subscription-approved",
        resource_group="resource-group-approved",
    )

    assert environment["AZURE_SUBSCRIPTION_ID"] == "subscription-approved"
    assert environment["AZURE_RESOURCE_GROUP"] == "resource-group-approved"
    assert environment["FABRIC_KG_SEARCH_ENDPOINT"] == (
        "https://search.example.test"
    )
    assert environment["FABRIC_KG_BLOB_CONTAINER"] == "approved-container"
    assert environment["FABRIC_KG_FABRIC_WORKSPACE_ID"] == (
        "workspace-approved"
    )
    assert environment["FABRIC_KG_MANAGED_IDENTITY_PRINCIPAL_ID"] == (
        "principal-approved"
    )
    assert environment["ACR_LOGIN_SERVER"] == ""
    assert environment["FABRIC_KG_ACR_RESOURCE_ID"] == ""


def test_live_identity_authority_rejects_subscription_tenant_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FABRIC_KG_TENANT_ID", raising=False)
    monkeypatch.setenv(
        "AZURE_TENANT_ID",
        "00000000-0000-4000-8000-000000000010",
    )
    runner = FakeCommandRunner()
    runner.add_response(
        [
            "az",
            "account",
            "show",
            "--subscription",
            "subscription-1",
            "--output",
            "json",
        ],
        stdout=(
            '{"id":"subscription-1",'
            '"tenantId":"00000000-0000-4000-8000-000000000011"}'
        ),
    )

    with pytest.raises(BuildDeployError, match="subscription tenant"):
        _resolve_live_identity_authority(
            {"deploy_serving": True},
            subscription_id="subscription-1",
            runner=runner,
        )


def test_credential_containers_are_wholly_redacted_in_nested_lists() -> None:
    sanitized = _secret_free_authority({
        "details": [
            {
                "credentials": [
                    {"primaryKey": "short-one"},
                    {"secondary_key": "short-two"},
                ],
                "keys": {"secretAccessKey": "short-three"},
                "auth": {"sharedKey": "short-four"},
                "name": "safe-plan-name",
            }
        ]
    })

    details = sanitized["details"][0]
    assert "credentials" not in details
    assert "keys" not in details
    assert "auth" not in details
    assert details["name"] == "safe-plan-name"
    serialized = str(sanitized)
    for secret in ("short-one", "short-two", "short-three", "short-four"):
        assert secret not in serialized


def test_credential_containers_do_not_change_authority_hash() -> None:
    baseline = _secret_free_authority({
        "details": [{"name": "safe-plan-name"}],
    })
    with_credentials = _secret_free_authority({
        "details": [
            {
                "name": "safe-plan-name",
                "credentials": {"primaryKey": "short-one"},
                "keys": [{"secondaryKey": "short-two"}],
            }
        ],
    })

    assert with_credentials == baseline
    assert _canonical_json_hash(with_credentials) == (
        _canonical_json_hash(baseline)
    )


def test_strict_authority_schema_preserves_only_named_safe_fields() -> None:
    sanitized = _secret_free_authority({
        "tenantId": "00000000-0000-4000-8000-000000000001",
        "audiences": {
            "fabric": "https://api.fabric.microsoft.com/.default",
            "application": "api://00000000-0000-4000-8000-000000000002",
        },
        "fabricWorkspaceId": "00000000-0000-4000-8000-000000000003",
        "fabricLakehouseId": "00000000-0000-4000-8000-000000000004",
        "itemId": "00000000-0000-4000-8000-000000000005",
        "action": "adopt",
        "resourceType": "Microsoft.Search/searchServices",
        "resourceName": "search-safe",
        "version": "0.2.4",
        "sha256": "sha256:" + "a" * 64,
        "count": 4,
        "enabled": True,
        "unknownMapping": {
            "endpoint": "https://must-not-survive.example.test",
        },
    })

    assert sanitized["tenantId"].endswith("0001")
    assert sanitized["audiences"]["fabric"].endswith("/.default")
    assert sanitized["fabricWorkspaceId"].endswith("0003")
    assert sanitized["fabricLakehouseId"].endswith("0004")
    assert sanitized["itemId"].endswith("0005")
    assert sanitized["resourceName"] == "search-safe"
    assert sanitized["sha256"] == "sha256:" + "a" * 64
    assert sanitized["count"] == 4
    assert sanitized["enabled"] is True
    assert "unknownMapping" not in sanitized
    assert "must-not-survive" not in str(sanitized)


def test_opaque_credentials_are_recursively_excluded_from_snapshot_and_state(
    tmp_path: Path,
) -> None:
    credential_keys = (
        "clientSecret",
        "Client-Secret",
        "primary_client_secret",
        "accessToken",
        "oauth-access-token",
        "refresh_token",
        "IDToken",
        "storageAccountKey",
        "Storage-Account-Key",
        "account_key",
        "apiKey",
        "sas-token",
        "connectionString",
        "database-connection-string",
        "password",
        "serviceCredential",
        "Authorization",
        "privateKey",
        "shared_access_key",
        "signingKey",
        "clientCertificate",
        "certificateData",
        "privateMaterial",
        "primaryKey",
        "backupPrimaryKey",
        "primaryKeyValue",
        "BACKUPPRIMARYKEYVALUE",
        "secondaryKey",
        "secondary_key",
        "secretAccessKey",
        "accessKey",
        "sharedKey",
        "masterKey",
        "adminKey",
        "functionKey",
        "hostKey",
        "BACKUPPASSWORDVALUE",
        "BACKUPTOKENVALUE",
        "BACKUPSECRETVALUE",
        "BACKUPAUTHORIZATIONVALUE",
        "BACKUPCREDENTIALVALUE",
        "BACKUPCERTIFICATEVALUE",
        "BACKUPSUBSCRIPTIONKEYVALUE",
    )
    opaque_values = {
        key: f"opaque-value-{index:02d}"
        for index, key in enumerate(credential_keys)
    }
    authority = {
        "endpoint": "https://service.example.test",
        "resourceId": (
            "/subscriptions/sub/resourceGroups/rg/providers/"
            "Microsoft.Example/resources/item"
        ),
        "details": [
            {"name": "resource-name", **opaque_values},
            {
                "outputs": {
                    "endpoint": "https://nested.example.test",
                    "secondarySigningKey": "opaque-signing-material",
                }
            },
        ],
    }

    sanitized = _secret_free_authority(authority)
    snapshot, snapshot_hash = _mutation_authority(
        imported_outputs={"value": authority},
    )
    state = _RunState(
        tmp_path / "state.json",
        run_id="run-1",
        environment="test",
        resume=False,
    )
    state.data["resolved_mutation_authority"] = snapshot
    state.data["resolved_mutation_authority_hash"] = snapshot_hash
    state.save()
    persisted = (tmp_path / "state.json").read_text(encoding="utf-8")

    for secret in (*opaque_values.values(), "opaque-signing-material"):
        assert secret not in str(sanitized)
        assert secret not in str(snapshot)
        assert secret not in persisted
    assert sanitized["endpoint"] == "https://service.example.test"
    assert sanitized["resourceId"].endswith(
        "/Microsoft.Example/resources/item"
    )
    assert sanitized["details"][0]["name"] == "resource-name"
    assert set(snapshot_hash) <= set("sha256:0123456789abcdef")


def test_authority_document_hash_ignores_redacted_credential_changes(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    first_path.write_text(
        '{"endpoint":"https://safe.example","clientSecret":"opaque-one"}',
        encoding="utf-8",
    )
    second_path.write_text(
        '{"endpoint":"https://safe.example","clientSecret":"opaque-two"}',
        encoding="utf-8",
    )

    first = _load_authority_document(first_path)
    second = _load_authority_document(second_path)
    first_snapshot, first_hash = _mutation_authority(
        imported_outputs=first,
    )
    second_snapshot, second_hash = _mutation_authority(
        imported_outputs=second,
    )

    assert first["sha256"] == second["sha256"]
    assert first["value"] == second["value"]
    assert first_hash == second_hash
    assert "opaque-one" not in str(first_snapshot)
    assert "opaque-two" not in str(second_snapshot)


def test_run_local_outputs_must_match_external_authority(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external" / "outputs.json"
    external.parent.mkdir()
    external.write_text(
        '{"searchEndpoint":"https://search-one.example.test"}',
        encoding="utf-8",
    )
    run_root = tmp_path / "run"

    selected, _ = _infrastructure_authority_source(
        environment="dev",
        run_root=run_root,
        explicit_outputs_path=external,
    )
    assert selected == external.resolve()

    run_outputs = run_root / "infra" / "dev" / "outputs.json"
    run_outputs.parent.mkdir(parents=True)
    run_outputs.write_bytes(external.read_bytes())
    selected, _ = _infrastructure_authority_source(
        environment="dev",
        run_root=run_root,
        explicit_outputs_path=external,
    )
    assert selected == run_outputs.resolve()

    run_outputs.write_text(
        '{"searchEndpoint":"https://search-two.example.test"}',
        encoding="utf-8",
    )
    with pytest.raises(BuildDeployError, match="Run-local"):
        _infrastructure_authority_source(
            environment="dev",
            run_root=run_root,
            explicit_outputs_path=external,
        )
    selected, _ = _infrastructure_authority_source(
        environment="dev",
        run_root=run_root,
        explicit_outputs_path=external,
        authoritative_overrides={
            "searchEndpoint": "https://search-fresh.example.test"
        },
    )
    assert selected == run_outputs.resolve()


def test_imported_state_and_outputs_never_copy_nested_credentials(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source" / "outputs.json"
    source.parent.mkdir()
    output_secret = "opaque-output-secret"
    state_secret = "opaque-state-secret"
    source.write_text(
        json.dumps({
            "searchEndpoint": "https://search.example.test",
            "credentials": {
                "keys": [{"primaryKey": output_secret}]
            },
        }),
        encoding="utf-8",
    )
    source.with_name("state.json").write_text(
        json.dumps({
            "schema_version": "1.0",
            "environment": "dev",
            "last_operation": "apply",
            "last_operation_id": (
                "00000000-0000-4000-8000-000000000001"
            ),
            "last_operation_status": "succeeded",
            "managed_resource_ids": {},
            "adopted_resource_ids": {},
            "outputs": {
                "searchEndpoint": "https://search.example.test",
                "secrets": {"secondaryKey": state_secret},
            },
            "credentials": [{"secretAccessKey": state_secret}],
        }),
        encoding="utf-8",
    )
    run_root = tmp_path / "run"

    _import_infrastructure_state(
        environment="dev",
        run_root=run_root,
        explicit_outputs_path=source,
    )

    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            run_root / "infra" / "dev" / "outputs.json",
            run_root / "infra" / "dev" / "state.json",
        )
    )
    assert output_secret not in persisted
    assert state_secret not in persisted


def test_same_path_import_rewrites_run_local_state_credentials(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    target = run_root / "infra" / "dev"
    target.mkdir(parents=True)
    secret = "opaque-run-local-secret"
    outputs = target / "outputs.json"
    outputs.write_text(
        '{"searchEndpoint":"https://search.example.test"}',
        encoding="utf-8",
    )
    (target / "state.json").write_text(
        json.dumps({
            "schema_version": "1.0",
            "environment": "dev",
            "last_operation": "apply",
            "last_operation_id": (
                "00000000-0000-4000-8000-000000000001"
            ),
            "last_operation_status": "succeeded",
            "managed_resource_ids": {},
            "adopted_resource_ids": {},
            "outputs": {
                "searchEndpoint": "https://search.example.test",
                "credentials": {"primaryKey": secret},
            },
            "secrets": [{"secondaryKey": secret}],
        }),
        encoding="utf-8",
    )

    _import_infrastructure_state(
        environment="dev",
        run_root=run_root,
        explicit_outputs_path=outputs,
    )

    persisted = (target / "state.json").read_text(encoding="utf-8")
    assert secret not in persisted
    assert "credentials" not in persisted
    assert "secrets" not in persisted


def test_typed_infrastructure_outputs_preserve_model_targets_and_reject_unknown(
    tmp_path: Path,
) -> None:
    path = tmp_path / "outputs.json"
    path.write_text(
        json.dumps({
            "chatDeploymentId": (
                "/subscriptions/sub/resourceGroups/rg/providers/"
                "Microsoft.CognitiveServices/accounts/account/"
                "deployments/chat-one"
            ),
            "embeddingDeploymentId": (
                "/subscriptions/sub/resourceGroups/rg/providers/"
                "Microsoft.CognitiveServices/accounts/account/"
                "deployments/embed-one"
            ),
            "chatModelName": "gpt-4.1",
            "embeddingModelName": "text-embedding-3-large",
        }),
        encoding="utf-8",
    )

    first = _load_infrastructure_outputs_authority(path)
    assert first is not None
    assert first["value"]["chatModelName"] == "gpt-4.1"
    assert first["value"]["embeddingDeploymentId"].endswith("embed-one")

    path.write_text(
        '{"unknownTarget":"must-fail"}',
        encoding="utf-8",
    )
    with pytest.raises(BuildDeployError, match="Unknown infrastructure"):
        _load_infrastructure_outputs_authority(path)


def test_fresh_arm_endpoints_replace_all_no_provision_cached_values() -> None:
    imported = {
        "sha256": "sha256:" + "0" * 64,
        "value": {
            "blobEndpoint": "https://blob-stale.example.test",
            "documentIntelligenceEndpoint": (
                "https://documents-stale.example.test"
            ),
            "foundryEndpoint": "https://foundry-stale.example.test",
            "foundryOpenAIEndpoint": "https://openai-stale.example.test",
            "foundryProjectEndpoint": "https://project-stale.example.test",
            "searchEndpoint": "https://search-stale.example.test",
            "fabricWorkspaceId": "workspace-approved",
        },
    }
    fresh = {
        "runtime_outputs": {
            "blobEndpoint": "https://blob-fresh.example.test",
            "documentIntelligenceEndpoint": (
                "https://documents-fresh.example.test"
            ),
            "foundryEndpoint": "https://foundry-fresh.example.test",
            "foundryOpenAIEndpoint": "https://openai-fresh.example.test",
            "foundryProjectEndpoint": "https://project-fresh.example.test",
            "searchEndpoint": "https://search-fresh.example.test",
        }
    }

    merged = _merge_authoritative_runtime_outputs(imported, fresh)

    assert merged["value"]["fabricWorkspaceId"] == "workspace-approved"
    for key, value in fresh["runtime_outputs"].items():
        assert merged["value"][key] == value
    assert "stale" not in str(merged)


def test_deployment_authority_interpolates_and_preserves_all_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "deployment.yaml"
    path.write_text(
        """
workspace: ${FABRIC_WORKSPACE_ID}
items:
  ontology: {display_name: Ontology_A, prefix: ont, configured_id: ont-id}
  lakehouse: {display_name: Lakehouse_A, prefix: lh, configured_id: lh-id}
  semantic_model: {display_name: Model_A, prefix: sm, configured_id: sm-id}
  graph_model: {display_name: Graph_A, prefix: gm, configured_id: gm-id}
  data_agent: {display_name: Agent_A, prefix: da, configured_id: da-id}
  search_index: {display_name: Search_A, prefix: si, configured_id: si-id}
dependencies:
  - item: data_agent
    depends_on: [semantic_model, graph_model, search_index]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("FABRIC_WORKSPACE_ID", "workspace-one")

    first = _load_deployment_authority(path)

    assert first["value"]["workspace"] == "workspace-one"
    assert first["value"]["items"]["semantic_model"]["prefix"] == "sm"
    assert first["value"]["items"]["data_agent"]["configured_id"] == "da-id"
    assert first["value"]["items"]["search_index"]["display_name"] == (
        "Search_A"
    )
    assert first["value"]["dependencies"][0]["depends_on"] == [
        "semantic_model",
        "graph_model",
        "search_index",
    ]

    monkeypatch.setenv("FABRIC_WORKSPACE_ID", "workspace-two")
    second = _load_deployment_authority(path)
    assert first["sha256"] != second["sha256"]


def test_deployment_authority_accepts_typed_empty_optional_fields(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deployment.yaml"
    path.write_text(
        """
workspace: workspace-one
items:
  ontology:
    display_name: Ontology_A
""",
        encoding="utf-8",
    )

    authority = _load_deployment_authority(path)

    assert authority["value"]["items"]["ontology"]["display_name"] == (
        "Ontology_A"
    )
    assert authority["value"]["items"]["data_agent"] == {
        "display_name": "",
        "prefix": "",
        "configured_id": "",
    }


def test_safe_endpoint_and_arm_id_bypass_generic_secret_scanning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "fabric_kg_builder.release.redact.looks_like_secret",
        lambda _value: True,
    )
    resource_id = (
        "/subscriptions/00000000-0000-4000-8000-000000000001/"
        "resourceGroups/release-resource-group-with-a-long-name/providers/"
        "Microsoft.Search/searchServices/search-service-with-a-long-name"
    )

    sanitized = _secret_free_authority({
        "endpoint": (
            "https://Search-Service-With-A-Long-Name.Example.Test/"
            "api/projects/release"
        ),
        "resourceId": resource_id,
        "safeName": "ordinary-but-non-allowlisted",
    })

    assert sanitized["endpoint"] == (
        "https://search-service-with-a-long-name.example.test/"
        "api/projects/release"
    )
    assert sanitized["resourceId"] == resource_id
    assert "safeName" not in sanitized


@pytest.mark.parametrize(
    ("key", "value"),
    [
        (
            "endpoint",
            "https://user:password@service.example.test/path",
        ),
        (
            "endpoint",
            "https://service.example.test/path?sig=opaque#fragment",
        ),
        ("endpoint", "http://service.example.test/path"),
        (
            "endpoint",
            "https://service.example.test/AccountKey="
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        ),
        (
            "endpoint",
            "https://service.example.test/PrimaryKey/short",
        ),
        (
            "endpoint",
            "https://service.example.test/Secondary_Key=short",
        ),
        (
            "endpoint",
            "https://service.example.test/SecretAccessKey/short",
        ),
        (
            "endpoint",
            "https://service.example.test/FunctionKey/short",
        ),
        (
            "endpoint",
            "https://service.example.test/HostKey/short",
        ),
        (
            "endpoint",
            "https://service.example.test/SharedAccessKey/short",
        ),
        (
            "endpoint",
            "https://service.example.test/%50rimary%4Bey/short",
        ),
        (
            "endpoint",
            "https://service.example.test/api/PRIMARYKEY/opaque-short",
        ),
        (
            "endpoint",
            "https://service.example.test/"
            "%25252550rimary%2525254Bey/opaque-short",
        ),
        (
            "endpoint",
            "https://service.example.test/"
            "%252542ACKUPCONNECTIONSTRINGVALUE/opaque-short",
        ),
        (
            "endpoint",
            "https://service.example.test/"
            "%252542ACKUPPASSWORDVALUE/opaque-short",
        ),
        (
            "endpoint",
            "https://service.example.test/"
            "%252542ACKUPSUBSCRIPTIONKEYVALUE/opaque-short",
        ),
        (
            "resourceId",
            "/subscriptions/sub/resourceGroups/rg/providers/"
            "Microsoft.Example/resources/item?sig=opaque",
        ),
        (
            "resourceId",
            "/subscriptions/sub/resourceGroups/rg/resources/item",
        ),
    ],
)
def test_unsafe_endpoint_and_resource_authority_is_rejected(
    key: str,
    value: str,
) -> None:
    assert _secret_free_authority({key: value})[key] == (
        "[REDACTED_NON_AUTHORITY]"
    )


def test_invalid_typed_endpoint_aborts_snapshot_construction() -> None:
    with pytest.raises(BuildDeployError, match="invalid or unsafe"):
        _mutation_authority(
            imported_outputs={
                "value": {
                    "searchEndpoint": (
                        "https://search.example.test/path?sig=opaque"
                    )
                }
            },
        )


def test_distinct_long_endpoint_and_resource_ids_change_authority_hash() -> None:
    first = _secret_free_authority({
        "endpoint": "https://service-one-with-a-long-name.example.test/api",
        "resourceId": (
            "/subscriptions/sub/resourceGroups/rg/providers/"
            "Microsoft.Example/resources/"
            "resource-with-a-long-distinct-name-one"
        ),
    })
    second = _secret_free_authority({
        "endpoint": "https://service-two-with-a-long-name.example.test/api",
        "resourceId": (
            "/subscriptions/sub/resourceGroups/rg/providers/"
            "Microsoft.Example/resources/"
            "resource-with-a-long-distinct-name-two"
        ),
    })

    assert first["endpoint"] != second["endpoint"]
    assert first["resourceId"] != second["resourceId"]
    assert _canonical_json_hash(first) != _canonical_json_hash(second)


def test_dynamic_arm_state_maps_preserve_distinct_resource_authority() -> None:
    first_id = (
        "/subscriptions/sub/resourceGroups/rg/providers/"
        "Microsoft.Search/searchServices/"
        "search-service-with-a-long-distinct-name-one"
    )
    second_id = (
        "/subscriptions/sub/resourceGroups/rg/providers/"
        "Microsoft.Search/searchServices/"
        "search-service-with-a-long-distinct-name-two"
    )
    first, first_hash = _mutation_authority(
        infrastructure_baseline_state={
            "environment": "test",
            "managed_resource_ids": {
                "Microsoft.Search/searchServices": first_id,
            },
        },
    )
    second, second_hash = _mutation_authority(
        infrastructure_baseline_state={
            "environment": "test",
            "managed_resource_ids": {
                "Microsoft.Search/searchServices": second_id,
            },
        },
    )

    assert first_id in str(first)
    assert second_id in str(second)
    assert first_hash != second_hash


def test_foundry_search_connection_id_is_bound_to_authority_hash() -> None:
    prefix = (
        "/subscriptions/sub/resourceGroups/rg/providers/"
        "Microsoft.CognitiveServices/accounts/account/projects/project/"
        "connections/"
    )
    first_id = prefix + "search-connection-one"
    second_id = prefix + "search-connection-two"
    first, first_hash = _mutation_authority(
        imported_outputs={
            "value": {
                "foundrySearchConnectionId": first_id,
                "foundrySearchConnectionName": "search-connection-one",
            }
        },
    )
    second, second_hash = _mutation_authority(
        imported_outputs={
            "value": {
                "foundrySearchConnectionId": second_id,
                "foundrySearchConnectionName": "search-connection-two",
            }
        },
    )

    assert first_id in str(first)
    assert second_id in str(second)
    assert first_hash != second_hash


def test_secret_bearing_endpoint_path_never_enters_snapshot_state(
    tmp_path: Path,
) -> None:
    secret = "A" * 32
    endpoint = f"https://service.example.test/AccountKey={secret}"
    with pytest.raises(BuildDeployError, match="invalid or unsafe"):
        _mutation_authority(
            imported_outputs={"value": {"searchEndpoint": endpoint}},
        )

    assert not (tmp_path / "state.json").exists()


def test_approved_snapshot_retry_is_unchanged_after_partial_failure(
    tmp_path: Path,
) -> None:
    state = _RunState(
        tmp_path / "state.json",
        run_id="run-1",
        environment="test",
        resume=False,
    )
    plan = "sha256:" + "a" * 64
    baseline = "sha256:" + "b" * 64
    authority, authority_hash = _mutation_authority()
    state.data.update({
        "plan_fingerprint": plan,
        "reviewed_semantic_baseline_fingerprint": baseline,
        "resolved_mutation_authority": authority,
        "resolved_mutation_authority_hash": authority_hash,
    })
    state.complete(dry_run=True)
    _approve_schema2_live_plan(
        state,
        plan_fingerprint=plan,
        managed_baseline_fingerprint=baseline,
        mutation_authority_snapshot=authority,
        mutation_authority_hash=authority_hash,
    )
    state.data["status"] = "failed"
    state.save()
    _approve_schema2_live_plan(
        state,
        plan_fingerprint=plan,
        managed_baseline_fingerprint=baseline,
        mutation_authority_snapshot=authority,
        mutation_authority_hash=authority_hash,
    )


def test_approval_rejects_target_drift_without_auto_approving(
    tmp_path: Path,
) -> None:
    state = _RunState(
        tmp_path / "state.json",
        run_id="run-1",
        environment="test",
        resume=False,
    )
    plan = "sha256:" + "a" * 64
    baseline = "sha256:" + "b" * 64
    authority, authority_hash = _mutation_authority()
    drifted, drifted_hash = _mutation_authority(
        authoritative_arm_outputs={
            "searchEndpoint": "https://changed.example.test"
        }
    )
    state.data.update({
        "plan_fingerprint": plan,
        "reviewed_semantic_baseline_fingerprint": baseline,
        "resolved_mutation_authority": authority,
        "resolved_mutation_authority_hash": authority_hash,
    })
    state.complete(dry_run=True)
    with pytest.raises(BuildDeployError, match="authority drifted"):
        _approve_schema2_live_plan(
            state,
            plan_fingerprint=plan,
            managed_baseline_fingerprint=baseline,
            mutation_authority_snapshot=drifted,
            mutation_authority_hash=drifted_hash,
        )
    assert "approved_mutation_authority_hash" not in state.data


def test_approval_rejects_tenant_and_audience_drift(
    tmp_path: Path,
) -> None:
    state = _RunState(
        tmp_path / "state.json",
        run_id="run-1",
        environment="test",
        resume=False,
    )
    plan = "sha256:" + "a" * 64
    baseline = "sha256:" + "b" * 64
    authority, authority_hash = _mutation_authority()
    drifted, drifted_hash = _mutation_authority(
        identity_authority={
            "tenant_id": "00000000-0000-4000-8000-000000000099",
            "audiences": {
                "fabric": "https://api.fabric.microsoft.com/.default",
                "application": (
                    "api://00000000-0000-4000-8000-000000000098"
                ),
            },
        },
    )
    state.data.update({
        "plan_fingerprint": plan,
        "reviewed_semantic_baseline_fingerprint": baseline,
        "resolved_mutation_authority": authority,
        "resolved_mutation_authority_hash": authority_hash,
    })
    state.complete(dry_run=True)

    with pytest.raises(BuildDeployError, match="authority drifted"):
        _approve_schema2_live_plan(
            state,
            plan_fingerprint=plan,
            managed_baseline_fingerprint=baseline,
            mutation_authority_snapshot=drifted,
            mutation_authority_hash=drifted_hash,
        )


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
