"""Release 0.2.4 gates for strict L7 planning and installed CLI inventory."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import requests
from click.testing import CliRunner

import fabric_kg_builder.agent.l7_release as l7_release_module
from fabric_kg_builder import __version__
from fabric_kg_builder.agent.l7_release import (
    ArtifactBinding,
    AzureL7Backend,
    DeploymentAction,
    L7Backend,
    L7DeploymentPlan,
    L7Executor,
    L7Observation,
    L7Planner,
    L7ReleaseConfig,
    L7ReleaseError,
    ObservedIdentity,
    ObservationBackend,
    ResourceReadback,
    _ReceiptReservation,
    _search_document_batches,
    _search_scope,
    _validated_service_url,
    _write_immutable,
    load_l7_config,
    load_plan,
)
from fabric_kg_builder.agent.project_connections import (
    FoundryProjectConnectionClient,
    fabric_data_agent_connection_properties,
    normalize_connection_properties,
    search_connection_properties,
)
from fabric_kg_builder.cli import cli
from fabric_kg_builder.contracts.base import canonical_sha256


def _artifact(path: Path, value: dict[str, Any]) -> dict[str, str]:
    payload = (json.dumps(value, sort_keys=True) + "\n").encode()
    path.write_bytes(payload)
    return {
        "path": path.name,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "canonical_hash": canonical_sha256(value),
    }


def _ownership(
    path: Path,
    *,
    item_id: str,
    item_type: str,
    name: str,
    definition_hash: str,
) -> dict[str, str]:
    value = {
        "release": "0.2.4",
        "attempt_id": "op-" + "a" * 64,
        "authority_hash": "1" * 64,
        "item_id": item_id,
        "item_type": item_type,
        "name": name,
        "definition_hash": definition_hash,
        "etag": "\"etag-1\"",
        "created_at": "2026-08-27T00:00:00Z",
    }
    value["receipt_hash"] = canonical_sha256(value)
    return _artifact(path, value)


def _inputs(tmp_path: Path) -> tuple[Path, Path, L7ReleaseConfig]:
    l6 = _artifact(tmp_path / "l6.json", {"definition_hash": "a" * 64})
    data_agent = _artifact(
        tmp_path / "data-agent.json", {"definition": {"parts": []}}
    )
    ontology = _artifact(
        tmp_path / "ontology.json", {"definition": {"parts": [{"path": "x"}]}}
    )
    schema = _artifact(
        tmp_path / "index.json",
        {"name": "ignored", "fields": [{"name": "id", "type": "Edm.String"}]},
    )
    docs = _artifact(tmp_path / "docs.json", {"documents": []})
    data_agent_ownership = _ownership(
        tmp_path / "data-agent-ownership.json",
        item_id="data-agent-1",
        item_type="DataAgent",
        name="fabric-kg-024-data-agent",
        definition_hash=canonical_sha256({"parts": []}),
    )
    ontology_ownership = _ownership(
        tmp_path / "ontology-ownership.json",
        item_id="ontology-1",
        item_type="Ontology",
        name="fabric-kg-024-ontology",
        definition_hash=canonical_sha256({"parts": [{"path": "x"}]}),
    )
    ownership_registry = {
        "version": "1",
        "receipts": {
            "tenant-1/workspace-1/dataagent/data-agent-1": json.loads(
                (tmp_path / "data-agent-ownership.json").read_text(
                    encoding="utf-8"
                )
            )["receipt_hash"],
            "tenant-1/workspace-1/ontology/ontology-1": json.loads(
                (tmp_path / "ontology-ownership.json").read_text(
                    encoding="utf-8"
                )
            )["receipt_hash"],
            "tenant-1/workspace-1/ontology/unrelated-item": "f" * 64,
        },
    }
    registry_path = tmp_path / "ownership-registry.json"
    registry_bytes = (
        json.dumps(ownership_registry, sort_keys=True) + "\n"
    ).encode()
    registry_path.write_bytes(registry_bytes)
    registry_path.chmod(0o444)
    os.environ["FABRIC_KG_OWNERSHIP_REGISTRY"] = str(registry_path)
    os.environ["FABRIC_KG_OWNERSHIP_REGISTRY_SHA256"] = hashlib.sha256(
        registry_bytes
    ).hexdigest()
    config = {
        "release": "0.2.4",
        "tenant_id": "tenant-1",
        "subscription_id": "subscription-1",
        "resource_group": "resource-group-1",
        "expected_principal_id": "principal-1",
        "fabric_workspace_id": "workspace-1",
        "ownership_registry_output": str(
            tmp_path / "next-ownership-registry.json"
        ),
        "authority_hash": "1" * 64,
        "l5a_definition_hash": "2" * 64,
        "l5b_definition_hash": "3" * 64,
        "l6_definition": l6,
        "fabric_definitions": [
            {
                "mode": "managed",
                "name": "fabric-kg-024-data-agent",
                "item_id": "data-agent-1",
                "item_type": "DataAgent",
                "artifact": data_agent,
                "ownership_receipt": data_agent_ownership,
                "ownership_receipt_output": str(
                    tmp_path / "next-data-agent-ownership.json"
                ),
            },
            {
                "mode": "managed",
                "name": "fabric-kg-024-ontology",
                "item_id": "ontology-1",
                "item_type": "Ontology",
                "artifact": ontology,
                "ownership_receipt": ontology_ownership,
                "ownership_receipt_output": str(
                    tmp_path / "next-ontology-ownership.json"
                ),
            },
        ],
        "search": {
            "endpoint": "https://example.search.windows.net",
            "index_name": "fabric-kg-024-index",
            "index_schema": schema,
            "documents": docs,
            "knowledge_source_name": "fabric-kg-024-source",
            "knowledge_base_name": "fabric-kg-024-kb",
            "api_version": "2025-11-01-preview",
        },
        "foundry": {
            "account_name": "account",
            "project_name": "project",
            "search_connection_name": "fabric-kg-024-search-connection",
            "fabric_connection_name": "fabric-kg-024-fabric-connection",
            "data_agent_id": "data-agent-1",
            "deploy_builtin_agent": False,
        },
        "plan_ttl_seconds": 900,
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    parsed = load_l7_config(config_path)
    resources = []
    for target in parsed.fabric_definitions:
        collection = {
            "DataAgent": "dataAgents",
            "Ontology": "ontologies",
        }[target.item_type]
        resources.append(
            {
                "resource_id": (
                    f"/workspaces/workspace-1/{collection}/{target.item_id}"
                ),
                "exists": True,
                "resource_type": target.item_type,
                "name": target.name,
                "etag": '"etag-1"',
                "definition_hash": canonical_sha256(
                    json.loads(
                        (tmp_path / target.artifact.path).read_text(
                            encoding="utf-8"
                        )
                    )["definition"]
                ),
            }
        )
    observation = {
        "identity": {"tenant_id": "tenant-1", "principal_id": "principal-1"},
        "resources": resources,
        "capabilities": {
            "fabric.DataAgent.definition": True,
            "fabric.Ontology.definition": True,
            "search.index": True,
            "search.knowledge-source": True,
            "search.knowledge-base": True,
            "foundry.project-connections": True,
        },
        "observed_at": datetime(2026, 8, 27, tzinfo=timezone.utc).isoformat(),
    }
    observation_path = tmp_path / "observation.json"
    observation_path.write_text(json.dumps(observation), encoding="utf-8")
    return config_path, observation_path, parsed


def _create_inputs(
    tmp_path: Path,
) -> tuple[Path, L7ReleaseConfig, L7Observation]:
    config_path, _, _ = _inputs(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["ownership_registry_output"] = str(
        tmp_path / "created-ownership-registry.json"
    )
    resources = []
    capabilities: dict[str, bool] = {
        "search.index": True,
        "search.knowledge-source": True,
        "search.knowledge-base": True,
        "foundry.project-connections": False,
    }
    collections = {
        "DataAgent": "dataAgents",
        "Ontology": "ontologies",
    }
    for target in raw["fabric_definitions"]:
        target["mode"] = "create"
        target.pop("item_id")
        target.pop("ownership_receipt")
        target["ownership_receipt_output"] = str(
            tmp_path
            / f"created-{target['item_type'].casefold()}-ownership.json"
        )
        resources.append(
            {
                "resource_id": (
                    f"/workspaces/workspace-1/{collections[target['item_type']]}/"
                    f"by-name/{target['name']}"
                ),
                "exists": False,
                "resource_type": target["item_type"],
                "name": target["name"],
            }
        )
        capabilities[f"fabric.{target['item_type']}.create"] = True
    raw["foundry"]["data_agent_id"] = ""
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = load_l7_config(config_path)
    observation = L7Observation.model_validate(
        {
            "identity": {
                "tenant_id": "tenant-1",
                "principal_id": "principal-1",
            },
            "resources": resources,
            "capabilities": capabilities,
            "observed_at": datetime(2026, 8, 27, tzinfo=timezone.utc),
        }
    )
    return config_path, config, observation


@pytest.mark.unit
def test_release_version_and_36_top_level_commands() -> None:
    assert __version__ == "0.2.4"
    assert len(cli.commands) == 36
    assert "app" in cli.commands
    assert "deploy-l7" in cli.commands["app"].commands


@pytest.mark.unit
def test_cli_compiles_canonical_l6_definition(tmp_path: Path) -> None:
    output = tmp_path / "l6-definition.json"
    result = CliRunner().invoke(
        cli,
        [
            "app",
            "compile-l6",
            "--agent-name",
            "fabric-kg-024-agent",
            "--fabric-connection-id",
            "connection:fabric-data-agent",
            "--out",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    assert output.exists()
    definition = json.loads(output.read_text(encoding="utf-8"))
    assert len(definition["tools"]) == 5
    assert definition["definition_hash"] in result.output
    help_result = CliRunner().invoke(
        cli, ["deploy-data-agent", "--help"]
    )
    assert help_result.exit_code == 0
    assert "--definition-out" in help_result.output


@pytest.mark.unit
def test_cli_dry_run_is_default_and_writes_hashed_immutable_plan(
    tmp_path: Path,
) -> None:
    config_path, observation_path, _ = _inputs(tmp_path)
    plan_path = tmp_path / "plan.json"
    result = CliRunner().invoke(
        cli,
        [
            "app",
            "deploy-l7",
            "--config",
            str(config_path),
            "--observation",
            str(observation_path),
            "--plan",
            str(plan_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "mode=dry-run; mutations=0" in result.output
    assert "l6_hosting=generated-local-deferred" in result.output
    plan = load_plan(plan_path)
    assert plan.plan_hash in result.output
    assert plan.l6_hosting == "generated-local-deferred"
    assert [item.action for item in plan.actions].count("deferred") >= 1
    assert not [item for item in plan.actions if item.action == "no-go"]
    assert plan_path.stat().st_mode & 0o222 == 0


@pytest.mark.unit
def test_live_requires_exact_hash_and_rejects_local_observation(
    tmp_path: Path,
) -> None:
    config_path, observation_path, _ = _inputs(tmp_path)
    result = CliRunner().invoke(
        cli,
        [
            "app",
            "deploy-l7",
            "--config",
            str(config_path),
            "--observation",
            str(observation_path),
            "--live",
            "--approve-live",
            "wrong",
        ],
    )
    assert result.exit_code != 0
    assert "--observation is forbidden in live mode" in result.output


@pytest.mark.unit
def test_artifact_drift_fails_before_plan(tmp_path: Path) -> None:
    config_path, observation_path, _ = _inputs(tmp_path)
    (tmp_path / "docs.json").write_text('{"documents":[{"id":"drift"}]}')
    result = CliRunner().invoke(
        cli,
        [
            "app",
            "deploy-l7",
            "--config",
            str(config_path),
            "--observation",
            str(observation_path),
        ],
    )
    assert result.exit_code != 0
    assert "artifact byte hash mismatch" in result.output


class _FailingBackend(L7Backend):
    def __init__(self, observation: L7Observation) -> None:
        self.observation = observation
        self.applied: list[str] = []
        self.rolled_back: list[str] = []
        self.rollback_finalized = False

    def observe(self, config: L7ReleaseConfig) -> L7Observation:
        return self.observation

    def apply(
        self, config: L7ReleaseConfig, action: DeploymentAction
    ) -> ResourceReadback:
        if self.applied:
            raise RuntimeError("expected mutation failure")
        self.applied.append(action.component)
        return ResourceReadback(
            resource_id=action.resource_id,
            exists=True,
            resource_type=action.resource_type,
            name=action.name,
            etag='"after"',
            properties_hash=action.desired_hash,
        )

    def rollback(
        self, config: L7ReleaseConfig, action: DeploymentAction
    ) -> ResourceReadback:
        self.rolled_back.append(action.component)
        return ResourceReadback(
            resource_id=action.resource_id,
            exists=False,
            resource_type=action.resource_type,
            name=action.name,
        )

    def finalize_rollback(self) -> None:
        self.rollback_finalized = True


@pytest.mark.unit
def test_failure_after_mutation_persists_sanitized_rollback_receipt(
    tmp_path: Path,
) -> None:
    config_path, observation_path, config = _inputs(tmp_path)
    observation = L7Observation.model_validate_json(
        observation_path.read_text(encoding="utf-8")
    )
    backend = _FailingBackend(observation)
    planner = L7Planner(backend)
    plan = planner.build(config, config_path=config_path)
    receipt_path = tmp_path / "receipt.json"
    with pytest.raises(L7ReleaseError, match="rollback completed"):
        L7Executor(planner, backend).execute(
            config=config,
            config_path=config_path,
            plan=plan,
            approval=plan.plan_hash,
            receipt_path=receipt_path,
        )
    assert backend.applied
    assert backend.rolled_back[-1] == backend.applied[0]
    assert set(backend.applied).issubset(set(backend.rolled_back))
    assert backend.rollback_finalized is True
    receipt_text = receipt_path.with_name(
        "receipt.json.failure.json"
    ).read_text(encoding="utf-8")
    assert "Authorization" not in receipt_text
    assert "rollback-after" in receipt_text


@pytest.mark.unit
def test_plan_tampering_is_rejected(tmp_path: Path) -> None:
    config_path, observation_path, config = _inputs(tmp_path)
    observation = L7Observation.model_validate_json(
        observation_path.read_text(encoding="utf-8")
    )
    plan = L7Planner(ObservationBackend(observation)).build(
        config, config_path=config_path
    )
    raw = plan.model_dump(mode="json")
    raw["tenant_id"] = "tampered"
    with pytest.raises(ValueError, match="plan hash mismatch"):
        L7DeploymentPlan.model_validate(raw)


@pytest.mark.unit
def test_search_endpoint_cannot_exfiltrate_search_token(tmp_path: Path) -> None:
    config_path, _, _ = _inputs(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["search"]["endpoint"] = "https://attacker.example"
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(L7ReleaseError, match="invalid L7 configuration"):
        load_l7_config(config_path)


@pytest.mark.unit
def test_fabric_update_without_etag_is_capability_no_go(tmp_path: Path) -> None:
    config_path, observation_path, config = _inputs(tmp_path)
    raw = json.loads(observation_path.read_text(encoding="utf-8"))
    raw["resources"][0]["definition_hash"] = "0" * 64
    raw["resources"][0]["etag"] = ""
    observation = L7Observation.model_validate(raw)
    plan = L7Planner(ObservationBackend(observation)).build(
        config, config_path=config_path
    )
    target = next(item for item in plan.actions if item.component == "fabric-dataagent")
    assert target.action == "no-go"
    assert "ETag" in target.reason


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    [
        "surface-tech-ontology",
        "ks3001-ontology",
        "ontology",
        "fabric-kg-024-trailing-",
    ],
)
def test_protected_or_arbitrary_fabric_name_fails_before_network(
    tmp_path: Path,
    name: str,
) -> None:
    config_path, _, _ = _inputs(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["fabric_definitions"][0]["name"] = name
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(L7ReleaseError, match="invalid L7 configuration"):
        load_l7_config(config_path)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("section", "field", "name"),
    [
        ("search", "index_name", "surface-tech-index"),
        ("search", "knowledge_base_name", "ks3001-kb"),
        ("foundry", "search_connection_name", "legacy-search"),
    ],
)
def test_all_cloud_names_share_release_ownership_policy(
    tmp_path: Path,
    section: str,
    field: str,
    name: str,
) -> None:
    config_path, _, _ = _inputs(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw[section][field] = name
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(L7ReleaseError, match="invalid L7 configuration"):
        load_l7_config(config_path)


class _SpyBackend(ObservationBackend):
    def __init__(self, observation: L7Observation) -> None:
        super().__init__(observation)
        self.observe_calls = 0

    def observe(self, config: L7ReleaseConfig) -> L7Observation:
        self.observe_calls += 1
        return super().observe(config)


@pytest.mark.unit
def test_fabric_ownership_mismatch_fails_before_observation(
    tmp_path: Path,
) -> None:
    config_path, observation_path, _ = _inputs(tmp_path)
    wrong = _ownership(
        tmp_path / "wrong-ownership.json",
        item_id="different-item",
        item_type="DataAgent",
        name="fabric-kg-024-data-agent",
        definition_hash=canonical_sha256({"parts": []}),
    )
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["fabric_definitions"][0]["ownership_receipt"] = wrong
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    config = load_l7_config(config_path)
    observation = L7Observation.model_validate_json(
        observation_path.read_text(encoding="utf-8")
    )
    backend = _SpyBackend(observation)
    with pytest.raises(L7ReleaseError, match="ownership receipt binding"):
        L7Planner(backend).build(config, config_path=config_path)
    assert backend.observe_calls == 0


class _SuccessBackend(_FailingBackend):
    def apply(
        self, config: L7ReleaseConfig, action: DeploymentAction
    ) -> ResourceReadback:
        self.applied.append(action.component)
        return ResourceReadback(
            resource_id=action.resource_id,
            exists=True,
            resource_type=action.resource_type,
            name=action.name,
            etag='"after"',
            properties_hash=action.desired_hash,
        )


class _CreateBackend(AzureL7Backend):
    def __init__(self, observation: L7Observation) -> None:
        self.observation = observation
        self.applied: list[str] = []
        self.rolled_back: list[str] = []
        self._ownership_outputs = {}

    def observe(self, config: L7ReleaseConfig) -> L7Observation:
        return self.observation

    def apply(
        self, config: L7ReleaseConfig, action: DeploymentAction
    ) -> ResourceReadback:
        self.applied.append(action.component)
        if action.component.startswith("fabric-"):
            return ResourceReadback(
                resource_id=action.resource_id,
                stable_id=f"created-{action.order}",
                exists=True,
                resource_type=action.resource_type,
                name=action.name,
                etag=f'"etag-{action.order}"',
                definition_hash=action.desired_hash,
            )
        return ResourceReadback(
            resource_id=action.resource_id,
            exists=True,
            resource_type=action.resource_type,
            name=action.name,
            etag=f'"etag-{action.order}"',
            properties_hash=action.desired_hash,
        )

    def rollback(
        self, config: L7ReleaseConfig, action: DeploymentAction
    ) -> ResourceReadback:
        self.rolled_back.append(action.component)
        return ResourceReadback(
            resource_id=action.resource_id,
            exists=False,
            resource_type=action.resource_type,
            name=action.name,
        )


@pytest.mark.unit
def test_preexisting_different_receipt_blocks_all_mutations(tmp_path: Path) -> None:
    config_path, observation_path, config = _inputs(tmp_path)
    observation = L7Observation.model_validate_json(
        observation_path.read_text(encoding="utf-8")
    )
    backend = _SuccessBackend(observation)
    plan = L7Planner(backend).build(config, config_path=config_path)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text('{"different":"transaction"}', encoding="utf-8")
    with pytest.raises(L7ReleaseError, match="preexisting receipt"):
        L7Executor(L7Planner(backend), backend).execute(
            config=config,
            config_path=config_path,
            plan=plan,
            approval=plan.plan_hash,
            receipt_path=receipt_path,
        )
    assert backend.applied == []


@pytest.mark.unit
def test_empty_workspace_create_plan_and_ownership_outputs(
    tmp_path: Path,
) -> None:
    config_path, config, observation = _create_inputs(tmp_path)
    backend = _CreateBackend(observation)
    planner = L7Planner(backend)
    plan = planner.build(config, config_path=config_path)
    fabric_actions = [
        item for item in plan.actions if item.component.startswith("fabric-")
    ]
    assert fabric_actions
    assert all(item.action == "create" for item in fabric_actions)
    assert not [item for item in plan.actions if item.action == "no-go"]
    receipt = L7Executor(planner, backend).execute(
        config=config,
        config_path=config_path,
        plan=plan,
        approval=plan.plan_hash,
        receipt_path=tmp_path / "deployment-receipt.json",
    )
    assert receipt.status == "succeeded"
    assert Path(str(config.ownership_registry_output)).exists()
    for target in config.fabric_definitions:
        assert Path(str(target.ownership_receipt_output)).exists()


@pytest.mark.unit
def test_successful_global_rollback_removes_ownership_outputs(
    tmp_path: Path,
) -> None:
    ownership_path = tmp_path / "ownership.json"
    payload = b'{"attempt":"release-owned"}\n'
    backend = object.__new__(AzureL7Backend)
    backend._ownership_outputs = {}
    publication = _write_immutable(
        ownership_path, payload, retain_descriptors=True
    )
    assert publication is not None
    assert not isinstance(publication, tuple)
    backend._ownership_outputs[ownership_path] = (
        payload,
        publication.device,
        publication.inode,
        publication.directory,
        publication.descriptor,
    )

    backend.finalize_rollback()

    assert not ownership_path.exists()
    assert backend._ownership_outputs == {}


@pytest.mark.unit
def test_identical_preexisting_ownership_output_is_not_transaction_owned(
    tmp_path: Path,
) -> None:
    ownership_path = tmp_path / "ownership.json"
    payload = b'{"attempt":"preexisting"}\n'
    ownership_path.write_bytes(payload)
    ownership_path.chmod(0o400)
    backend = object.__new__(AzureL7Backend)
    backend._ownership_outputs = {}

    identity = _write_immutable(ownership_path, payload)
    if identity is not None:
        backend._ownership_outputs[ownership_path] = (payload, *identity)
    backend.finalize_rollback()

    assert ownership_path.read_bytes() == payload
    assert backend._ownership_outputs == {}


@pytest.mark.unit
def test_writable_preexisting_ownership_output_is_rejected(
    tmp_path: Path,
) -> None:
    ownership_path = tmp_path / "ownership.json"
    payload = b'{"attempt":"preexisting"}\n'
    ownership_path.write_bytes(payload)

    with pytest.raises(L7ReleaseError, match="unsafe ownership or mode"):
        _write_immutable(ownership_path, payload)


@pytest.mark.unit
def test_managed_replacement_registry_preserves_unrelated_entries(
    tmp_path: Path,
) -> None:
    config_path, observation_path, config = _inputs(tmp_path)
    observation = L7Observation.model_validate_json(
        observation_path.read_text(encoding="utf-8")
    )
    plan = L7Planner(ObservationBackend(observation)).build(
        config, config_path=config_path
    )
    action = next(
        item for item in plan.actions if item.component == "fabric-dataagent"
    ).model_copy(update={"action": "update"})
    target = config.fabric_definitions[0]
    backend = object.__new__(AzureL7Backend)
    backend.artifact_base = config_path.parent
    backend._ownership_outputs = {}
    backend.finalize_ownership(
        config,
        plan,
        [
            (
                action,
                ResourceReadback(
                    resource_id=action.resource_id,
                    stable_id=str(target.item_id),
                    exists=True,
                    resource_type=target.item_type,
                    name=target.name,
                    etag='"updated"',
                    definition_hash=action.desired_hash,
                ),
            )
        ],
    )
    registry = json.loads(
        Path(str(config.ownership_registry_output)).read_text(
            encoding="utf-8"
        )
    )
    assert (
        registry["receipts"][
            "tenant-1/workspace-1/ontology/unrelated-item"
        ]
        == "f" * 64
    )


@pytest.mark.unit
@pytest.mark.parametrize("invalid_mix", ["create-with-id", "managed-without-receipt"])
def test_fabric_intent_rejects_mixed_ownership_fields(
    tmp_path: Path,
    invalid_mix: str,
) -> None:
    config_path, _, _ = _inputs(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    target = raw["fabric_definitions"][0]
    if invalid_mix == "create-with-id":
        target["mode"] = "create"
        target["ownership_receipt_output"] = str(tmp_path / "ownership.json")
    else:
        target.pop("ownership_receipt")
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(L7ReleaseError, match="invalid L7 configuration"):
        load_l7_config(config_path)


@pytest.mark.unit
def test_success_receipt_disk_failure_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, observation_path, config = _inputs(tmp_path)
    observation = L7Observation.model_validate_json(
        observation_path.read_text(encoding="utf-8")
    )
    backend = _SuccessBackend(observation)
    plan = L7Planner(backend).build(config, config_path=config_path)
    receipt_path = tmp_path / "receipt.json"

    def fail_commit(
        self: _ReceiptReservation, receipt: object
    ) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(_ReceiptReservation, "commit_success", fail_commit)
    with pytest.raises(L7ReleaseError, match="rollback completed"):
        L7Executor(L7Planner(backend), backend).execute(
            config=config,
            config_path=config_path,
            plan=plan,
            approval=plan.plan_hash,
            receipt_path=receipt_path,
        )
    assert backend.rolled_back == list(reversed(backend.applied))
    failure = json.loads(
        receipt_path.with_name("receipt.json.failure.json").read_text(
            encoding="utf-8"
        )
    )
    assert failure["status"] == "rolled-back"


@pytest.mark.unit
def test_directory_fsync_error_after_link_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, observation_path, config = _inputs(tmp_path)
    observation = L7Observation.model_validate_json(
        observation_path.read_text(encoding="utf-8")
    )
    backend = _SuccessBackend(observation)
    plan = L7Planner(backend).build(config, config_path=config_path)
    receipt_path = tmp_path / "receipt.json"
    original = l7_release_module._fsync_parent
    calls = 0

    def fail_after_link(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory fsync failed")
        original(path)

    monkeypatch.setattr(l7_release_module, "_fsync_parent", fail_after_link)
    with pytest.raises(L7ReleaseError, match="rollback completed"):
        L7Executor(L7Planner(backend), backend).execute(
            config=config,
            config_path=config_path,
            plan=plan,
            approval=plan.plan_hash,
            receipt_path=receipt_path,
        )
    assert backend.rolled_back == list(reversed(backend.applied))
    assert not receipt_path.exists()
    assert receipt_path.with_name("receipt.json.failure.json").exists()


@pytest.mark.unit
def test_crash_reservation_blocks_retry_without_mutation(tmp_path: Path) -> None:
    config_path, observation_path, config = _inputs(tmp_path)
    observation = L7Observation.model_validate_json(
        observation_path.read_text(encoding="utf-8")
    )
    backend = _SuccessBackend(observation)
    plan = L7Planner(backend).build(config, config_path=config_path)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.with_name(".receipt.json.pending").write_text(
        '{"status":"reserved"}', encoding="utf-8"
    )
    with pytest.raises(L7ReleaseError, match="already reserved"):
        L7Executor(L7Planner(backend), backend).execute(
            config=config,
            config_path=config_path,
            plan=plan,
            approval=plan.plan_hash,
            receipt_path=receipt_path,
        )
    assert backend.applied == []


@pytest.mark.unit
@pytest.mark.parametrize("failure_destination", [False, True])
def test_dangling_receipt_symlink_blocks_mutation(
    tmp_path: Path,
    failure_destination: bool,
) -> None:
    config_path, observation_path, config = _inputs(tmp_path)
    observation = L7Observation.model_validate_json(
        observation_path.read_text(encoding="utf-8")
    )
    backend = _SuccessBackend(observation)
    plan = L7Planner(backend).build(config, config_path=config_path)
    receipt_path = tmp_path / "receipt.json"
    blocked = (
        receipt_path.with_name("receipt.json.failure.json")
        if failure_destination
        else receipt_path
    )
    blocked.symlink_to(tmp_path / "does-not-exist")
    with pytest.raises(L7ReleaseError, match="preexisting receipt"):
        L7Executor(L7Planner(backend), backend).execute(
            config=config,
            config_path=config_path,
            plan=plan,
            approval=plan.plan_hash,
            receipt_path=receipt_path,
        )
    assert backend.applied == []


class _Token:
    token = "real-access-token"


class _Credential:
    def get_token(self, scope: str) -> _Token:
        assert scope == "https://management.azure.com/.default"
        return _Token()


class _ScopedCredential:
    def __init__(self) -> None:
        self.scopes: list[str] = []

    def get_token(self, scope: str) -> _Token:
        self.scopes.append(scope)
        token = _Token()
        token.token = f"sentinel::{scope}"
        return token


class _Response:
    def __init__(
        self,
        status_code: int,
        body: dict[str, Any] | None = None,
        *,
        etag: str = "",
    ) -> None:
        self.status_code = status_code
        self._body = body or {}
        self.headers = {"ETag": etag} if etag else {}

    def json(self) -> dict[str, Any]:
        return self._body


@pytest.mark.unit
def test_project_connection_uses_real_token_and_independent_readback() -> None:
    calls: list[tuple[str, dict[str, str]]] = []
    created: dict[str, Any] | None = None

    def request(
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any] | None = None,
        timeout: int,
        allow_redirects: bool,
    ) -> _Response:
        nonlocal created
        calls.append((method, headers))
        assert timeout == 60
        assert allow_redirects is False
        if method == "GET" and created is None:
            return _Response(404)
        if method == "PUT":
            created = dict(json or {})
            return _Response(
                201,
                {
                    "id": url.split("?")[0].removeprefix(
                        "https://management.azure.com"
                    ),
                    "properties": created["properties"],
                },
                etag='"created"',
            )
        assert created is not None
        readback = json_module_roundtrip(created["properties"])
        if "credentials" in readback:
            readback["credentials"] = {
                "keys": {
                    "workspace-id": None,
                    "artifact-id": None,
                }
            }
        return _Response(
            200,
            {
                "id": url.split("?")[0].removeprefix(
                    "https://management.azure.com"
                ),
                "properties": readback,
            },
            etag='"created"',
        )

    client = FoundryProjectConnectionClient(
        subscription_id="subscription",
        resource_group="group",
        account_name="account",
        project_name="project",
        tenant_id="tenant",
        credential=_Credential(),
        request=request,
    )
    result = client.upsert_fabric_data_agent(
        name="fabric-kg-024-fabric",
        workspace_id="workspace",
        data_agent_id="agent",
        create_only=True,
    )
    assert result.binding_hash
    assert [method for method, _ in calls] == ["GET", "PUT", "GET"]
    assert all(
        headers["Authorization"] == "Bearer real-access-token"
        for _, headers in calls
    )


@pytest.mark.unit
def test_connection_readback_drift_is_not_deleted_without_exact_binding() -> None:
    created: dict[str, Any] | None = None
    methods: list[str] = []

    def request(
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any] | None = None,
        timeout: int,
        allow_redirects: bool,
    ) -> _Response:
        nonlocal created
        methods.append(method)
        if method == "GET" and created is None:
            return _Response(404)
        if method == "PUT":
            created = dict(json or {})
            return _Response(
                201,
                {
                    "id": url.split("?")[0].removeprefix(
                        "https://management.azure.com"
                    ),
                    "properties": created["properties"],
                },
            )
        if method == "DELETE":
            raise AssertionError("concurrently changed connection was deleted")
        assert created is not None
        drifted = json_module_roundtrip(created["properties"])
        drifted["target"] = "https://concurrent.example"
        return _Response(
            200,
            {
                "id": url.split("?")[0].removeprefix(
                    "https://management.azure.com"
                ),
                "properties": drifted,
            },
            etag='"concurrent"',
        )

    client = FoundryProjectConnectionClient(
        subscription_id="subscription",
        resource_group="group",
        account_name="account",
        project_name="project",
        credential=_Credential(),
        request=request,
    )
    with pytest.raises(Exception, match="readback mismatch"):
        client.upsert_search(
            name="fabric-kg-024-search",
            endpoint="https://example.search.windows.net",
            create_only=True,
            attempt_id="op-" + "a" * 64,
        )
    assert "DELETE" not in methods


@pytest.mark.unit
def test_legacy_url_first_connection_transport_remains_supported() -> None:
    calls: list[str] = []

    def legacy_put(url: str, **kwargs: Any) -> _Response:
        calls.append(url)
        return _Response(201, kwargs["json"], etag='"legacy"')

    client = FoundryProjectConnectionClient(
        subscription_id="subscription",
        resource_group="group",
        account_name="account",
        project_name="project",
        credential=_Credential(),
        request=legacy_put,
    )
    result = client.upsert_search(
        name="fabric-kg-024-search",
        endpoint="https://example.search.windows.net",
    )
    assert result.etag == '"legacy"'
    assert len(calls) == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    ("endpoint", "scope"),
    [
        (
            "https://example.search.windows.net",
            "https://search.azure.com/.default",
        ),
        (
            "https://example.search.azure.us",
            "https://search.azure.us/.default",
        ),
        (
            "https://example.search.azure.cn",
            "https://search.azure.cn/.default",
        ),
    ],
)
def test_search_scope_matches_endpoint_cloud(endpoint: str, scope: str) -> None:
    assert _search_scope(endpoint) == scope


@pytest.mark.unit
def test_fabric_search_and_arm_tokens_reach_transport_but_not_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = object.__new__(AzureL7Backend)
    credential = _ScopedCredential()
    backend.credential = credential
    captured: list[str] = []

    def transport(
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any] | None,
        timeout: int,
        allow_redirects: bool,
    ) -> _Response:
        assert allow_redirects is False
        captured.append(headers["Authorization"])
        return _Response(200, {})

    monkeypatch.setattr(requests, "request", transport)
    scopes = [
        "https://api.fabric.microsoft.com/.default",
        "https://search.azure.com/.default",
        "https://management.azure.com/.default",
    ]
    for scope in scopes:
        token = backend._token(scope)
        backend._request(
            "GET",
            "https://example.search.windows.net",
            token=token,
        )
    assert credential.scopes == scopes
    assert captured == [f"Bearer sentinel::{scope}" for scope in scopes]

    sentinel = "sentinel-never-log"

    def fail_transport(*args: object, **kwargs: object) -> _Response:
        raise requests.RequestException(sentinel)

    monkeypatch.setattr(requests, "request", fail_transport)
    with pytest.raises(L7ReleaseError) as captured_error:
        backend._request(
            "GET",
            "https://example.search.windows.net",
            token=sentinel,
        )
    assert sentinel not in str(captured_error.value)


def json_module_roundtrip(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value))


@pytest.mark.unit
@pytest.mark.parametrize(
    "candidate",
    [
        "https://evil.example/operation",
        "//evil.example/operation",
        "http://api.fabric.microsoft.com/operation",
        "https://user@api.fabric.microsoft.com/operation",
        "https://api.fabric.microsoft.com:444/operation",
    ],
)
def test_operation_url_rejects_token_origin_escape(candidate: str) -> None:
    with pytest.raises(L7ReleaseError, match="origin validation"):
        _validated_service_url(
            candidate,
            expected_origin="https://api.fabric.microsoft.com",
            base_url="https://api.fabric.microsoft.com/v1/workspaces/w/items/i",
        )


@pytest.mark.unit
def test_operation_url_accepts_only_relative_or_exact_absolute_origin() -> None:
    expected = "https://example.search.windows.net"
    base = f"{expected}/indexes/fabric-kg-024-index"
    assert _validated_service_url(
        "/operations/1", expected_origin=expected, base_url=base
    ) == f"{expected}/operations/1"
    assert _validated_service_url(
        f"{expected}/operations/1", expected_origin=expected, base_url=base
    ) == f"{expected}/operations/1"


@pytest.mark.unit
@pytest.mark.parametrize(
    "expected_origin",
    [
        "https://api.fabric.microsoft.com",
        "https://example.search.windows.net",
        "https://management.azure.com",
        "https://account.services.ai.azure.com",
    ],
)
def test_malicious_lro_location_is_rejected_before_token_attachment(
    expected_origin: str,
) -> None:
    backend = object.__new__(AzureL7Backend)
    calls = 0

    def should_not_send(*args: object, **kwargs: object) -> _Response:
        nonlocal calls
        calls += 1
        return _Response(200, {"status": "Succeeded"})

    backend._request = should_not_send
    with pytest.raises(L7ReleaseError, match="origin validation"):
        backend._wait_lro(
            "https://evil.example/operations/1?sig=secret",
            "audience-token",
            expected_origin=expected_origin,
            base_url=f"{expected_origin}/resource",
        )
    assert calls == 0


@pytest.mark.unit
def test_redirect_is_not_followed_with_search_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, str]] = []

    def redirect(
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any] | None,
        timeout: int,
        allow_redirects: bool,
    ) -> _Response:
        sent.append((url, headers["Authorization"]))
        response = _Response(302)
        response.headers["Location"] = "https://evil.example/steal"
        return response

    monkeypatch.setattr(requests, "request", redirect)
    with pytest.raises(L7ReleaseError, match="origin validation"):
        AzureL7Backend._request(
            "GET",
            "https://example.search.windows.net/operations/1",
            token="search-token",
            expected_origin="https://example.search.windows.net",
        )
    assert sent == [
        (
            "https://example.search.windows.net/operations/1",
            "Bearer search-token",
        )
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("authType", "ApiKey"),
        ("category", "UnexpectedCategory"),
        ("target", "https://other.search.windows.net"),
        ("audience", "unexpected-audience"),
        ("isSharedToAll", False),
        ("group", "Other"),
    ],
)
def test_connection_security_field_drift_changes_actual_hash(
    field: str,
    value: Any,
) -> None:
    expected = search_connection_properties(
        endpoint="https://example.search.windows.net",
        attempt_id="op-" + "a" * 64,
    )
    changed = dict(expected)
    changed[field] = value
    assert canonical_sha256(normalize_connection_properties(changed)) != (
        canonical_sha256(normalize_connection_properties(expected))
    )


@pytest.mark.unit
def test_connection_extra_property_and_custom_key_name_fail_closed() -> None:
    search = search_connection_properties(
        endpoint="https://example.search.windows.net",
        attempt_id="op-" + "a" * 64,
    )
    with pytest.raises(Exception, match="unexpected properties"):
        normalize_connection_properties({**search, "unexpected": True})

    fabric = fabric_data_agent_connection_properties(
        workspace_id="workspace",
        data_agent_id="agent",
        attempt_id="op-" + "a" * 64,
    )
    fabric["credentials"]["keys"]["wrong"] = "value"
    with pytest.raises(Exception, match="CustomKeys names"):
        normalize_connection_properties(fabric)

    mixed = fabric_data_agent_connection_properties(
        workspace_id="workspace",
        data_agent_id="agent",
        attempt_id="op-" + "a" * 64,
    )
    mixed["credentials"]["keys"]["workspace-id"] = None
    with pytest.raises(Exception, match="mixed visible and redacted"):
        normalize_connection_properties(mixed)


@pytest.mark.unit
@pytest.mark.parametrize("outcome", [500, 409, 429, "timeout"])
def test_ambiguous_connection_create_reconciles_and_deletes_own_attempt(
    outcome: int | str,
) -> None:
    stored: dict[str, Any] | None = None
    methods: list[str] = []

    def request(
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any] | None = None,
        timeout: int,
        allow_redirects: bool,
    ) -> _Response:
        nonlocal stored
        methods.append(method)
        if method == "GET":
            if stored is None:
                return _Response(404)
            return _Response(
                200,
                {
                    "id": url.split("?")[0].removeprefix(
                        "https://management.azure.com"
                    ),
                    "properties": stored,
                },
                etag='"owned"',
            )
        if method == "PUT":
            stored = dict((json or {})["properties"])
            if outcome == "timeout":
                raise TimeoutError("commit then timeout")
            return _Response(int(outcome))
        if method == "DELETE":
            assert headers["If-Match"] == '"owned"'
            stored = None
            return _Response(204)
        raise AssertionError(method)

    client = FoundryProjectConnectionClient(
        subscription_id="subscription",
        resource_group="group",
        account_name="account",
        project_name="project",
        credential=_Credential(),
        request=request,
    )
    with pytest.raises(Exception, match="rolled back|transport failed"):
        client.upsert_search(
            name="fabric-kg-024-search",
            endpoint="https://example.search.windows.net",
            create_only=True,
            attempt_id="op-" + "a" * 64,
        )
    assert "DELETE" in methods
    assert stored is None


@pytest.mark.unit
def test_connection_202_lro_uses_validated_arm_location() -> None:
    stored: dict[str, Any] | None = None

    def request(
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any] | None = None,
        timeout: int,
        allow_redirects: bool,
    ) -> _Response:
        nonlocal stored
        if method == "PUT":
            stored = dict((json or {})["properties"])
            response = _Response(202)
            response.headers["Location"] = "/operations/connection-1"
            return response
        if "/operations/" in url:
            return _Response(200, {"status": "Succeeded"})
        if stored is None:
            return _Response(404)
        return _Response(
            200,
            {
                "id": url.split("?")[0].removeprefix(
                    "https://management.azure.com"
                ),
                "properties": stored,
            },
            etag='"owned"',
        )

    client = FoundryProjectConnectionClient(
        subscription_id="subscription",
        resource_group="group",
        account_name="account",
        project_name="project",
        credential=_Credential(),
        request=request,
    )
    result = client.upsert_search(
        name="fabric-kg-024-search",
        endpoint="https://example.search.windows.net",
        create_only=True,
        attempt_id="op-" + "a" * 64,
    )
    assert result.attempt_id == "op-" + "a" * 64


@pytest.mark.unit
def test_default_foundry_transport_uses_requests_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def request(
        method: str,
        url: str,
        **kwargs: Any,
    ) -> _Response:
        calls.append(method)
        return _Response(404)

    monkeypatch.setattr(requests, "request", request)
    client = FoundryProjectConnectionClient(
        subscription_id="subscription",
        resource_group="group",
        account_name="account",
        project_name="project",
        credential=_Credential(),
    )
    assert client.get("fabric-kg-024-search") is None
    assert calls == ["GET"]


@pytest.mark.unit
def test_failed_search_create_lro_reconciles_owned_resource(
    tmp_path: Path,
) -> None:
    config_path, observation_path, config = _inputs(tmp_path)
    observation = L7Observation.model_validate_json(
        observation_path.read_text(encoding="utf-8")
    )
    action = next(
        item
        for item in L7Planner(ObservationBackend(observation)).build(
            config, config_path=config_path
        ).actions
        if item.component == "search-index"
    )
    backend = object.__new__(AzureL7Backend)
    backend.credential = _ScopedCredential()
    backend._mutation_confirmed = set()
    backend._created_etags = {}
    stored: dict[str, Any] | None = None
    deleted = False

    def transport(
        method: str,
        url: str,
        **kwargs: Any,
    ) -> _Response:
        nonlocal stored, deleted
        if method == "PUT":
            stored = dict(kwargs["body"])
            response = _Response(202)
            response.headers["Location"] = "/operations/search-1"
            return response
        if "/operations/" in url:
            return _Response(200, {"status": "Failed"})
        if method == "DELETE":
            deleted = True
            return _Response(204)
        if deleted:
            return _Response(404)
        return _Response(200, stored, etag='"owned"')

    backend._request = transport
    with pytest.raises(L7ReleaseError, match="rolled back"):
        backend._search_create_put(
            config,
            action,
            f"indexes/{config.search.index_name}",
            {"name": config.search.index_name},
            "search-token",
        )
    assert deleted is True


@pytest.mark.unit
def test_mismatched_owned_search_create_is_conditionally_deleted(
    tmp_path: Path,
) -> None:
    config_path, observation_path, config = _inputs(tmp_path)
    observation = L7Observation.model_validate_json(
        observation_path.read_text(encoding="utf-8")
    )
    action = next(
        item
        for item in L7Planner(ObservationBackend(observation)).build(
            config, config_path=config_path
        ).actions
        if item.component == "search-index"
    )
    backend = object.__new__(AzureL7Backend)
    backend.credential = _ScopedCredential()
    backend._mutation_confirmed = set()
    backend._created_etags = {}
    deleted = False

    def transport(method: str, url: str, **kwargs: Any) -> _Response:
        nonlocal deleted
        if method == "DELETE":
            deleted = True
            return _Response(204)
        if deleted:
            return _Response(404)
        return _Response(
            200,
            {
                "name": config.search.index_name,
                "description": action.ownership_marker,
                "unexpected": "binding-drift",
            },
            etag='"owned"',
        )

    backend._request = transport
    with pytest.raises(L7ReleaseError, match="foreign attempt or binding"):
        backend._reconcile_search_create(
            config,
            action,
            f"indexes/{config.search.index_name}",
            {
                "name": config.search.index_name,
                "description": action.ownership_marker,
            },
            "search-token",
            keep=False,
        )
    assert deleted is True


@pytest.mark.unit
def test_search_document_batches_respect_service_count_limit() -> None:
    batches = _search_document_batches(
        [{"id": str(index)} for index in range(1001)]
    )
    assert [len(batch) for batch in batches] == [1000, 1]
    assert all(
        item["@search.action"] == "upload"
        for batch in batches
        for item in batch
    )
    payload_batches = _search_document_batches(
        [{"id": str(index), "content": "x" * 16_000} for index in range(1000)]
    )
    assert all(
        len(
            json.dumps(
                {"value": batch}, ensure_ascii=True, allow_nan=False
            ).encode()
        )
        <= 15 * 1024 * 1024
        for batch in payload_batches
    )


@pytest.mark.unit
@pytest.mark.parametrize("supplied_result_location", [True, False])
def test_fabric_get_definition_uses_result_url_exactly_once(
    tmp_path: Path,
    supplied_result_location: bool,
) -> None:
    _, _, config = _inputs(tmp_path)
    target = config.fabric_definitions[0]
    backend = object.__new__(AzureL7Backend)
    backend._rollback_definitions = {}
    backend._token = lambda scope: "fabric-token"
    urls: list[str] = []

    def transport(
        method: str,
        url: str,
        **kwargs: Any,
    ) -> _Response:
        urls.append(url)
        if "/items/" in url:
            return _Response(
                200,
                {"id": target.item_id, "type": "DataAgent", "displayName": target.name},
                etag='"item"',
            )
        if url.endswith("/getDefinition"):
            response = _Response(202)
            response.headers["Location"] = "/operations/definition-1"
            return response
        if url.endswith("/operations/definition-1"):
            response = _Response(200, {"status": "Succeeded"})
            if supplied_result_location:
                response.headers["Location"] = (
                    "https://api.fabric.microsoft.com/operations/"
                    "definition-1/result"
                )
            return response
        if url.endswith("/operations/definition-1/result"):
            return _Response(
                200,
                {"definition": {"parts": []}},
                etag='"definition"',
            )
        raise AssertionError(url)

    backend._request = transport
    observed = backend._fabric_definition(config, target)
    assert observed.definition_hash == canonical_sha256({"parts": []})
    assert not any(url.endswith("/result/result") for url in urls)
    assert urls.count(
        "https://api.fabric.microsoft.com/operations/definition-1/result"
    ) == 1


@pytest.mark.unit
def test_fabric_lro_rejects_external_final_result_location() -> None:
    backend = object.__new__(AzureL7Backend)
    calls: list[str] = []

    def transport(
        method: str,
        url: str,
        **kwargs: Any,
    ) -> _Response:
        calls.append(url)
        response = _Response(200, {"status": "Succeeded"})
        response.headers["Location"] = "https://evil.example/result?sig=secret"
        return response

    backend._request = transport
    with pytest.raises(L7ReleaseError, match="origin validation"):
        backend._wait_lro(
            "/operations/definition-1",
            "fabric-token",
            expected_origin="https://api.fabric.microsoft.com",
            base_url="https://api.fabric.microsoft.com/v1/getDefinition",
        )
    assert calls == [
        "https://api.fabric.microsoft.com/operations/definition-1"
    ]


@pytest.mark.unit
def test_preflight_failure_event_reports_no_mutation_and_no_phantom_receipt(
    tmp_path: Path,
) -> None:
    """A pre-mutation failure must not imply a rollback or a written receipt."""
    config_path, observation_path, _ = _inputs(tmp_path)
    (tmp_path / "docs.json").write_text('{"documents":[{"id":"drift"}]}')
    events_path = tmp_path / "events.jsonl"
    receipt_path = tmp_path / "receipt.json"

    result = CliRunner().invoke(
        cli,
        [
            "app",
            "deploy-l7",
            "--config",
            str(config_path),
            "--observation",
            str(observation_path),
            "--log",
            str(events_path),
            "--out",
            str(receipt_path),
        ],
    )

    assert result.exit_code != 0
    failures = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("event") == "failure"
    ]
    assert len(failures) == 1
    failure = failures[0]
    assert failure["causal_stage"] == "preflight"
    assert failure["mutation_possible"] is False
    assert "receipt_path" not in failure
    assert "failure_receipt_path" not in failure
    assert not receipt_path.exists()
    assert not receipt_path.with_name("receipt.json.failure.json").exists()


@pytest.mark.unit
def test_search_authorization_failure_names_the_required_roles(
    tmp_path: Path,
) -> None:
    """A 403 readback must tell the operator exactly what access is missing."""
    config_path, _, config = _inputs(tmp_path)
    backend = AzureL7Backend(config_path.parent)

    class _Denied:
        status_code = 403
        headers: dict[str, str] = {}
        text = ""

        @staticmethod
        def json() -> dict[str, Any]:
            return {}

    claims = {"tid": config.tenant_id, "oid": config.expected_principal_id}
    token = ".".join(
        [
            "header",
            base64.urlsafe_b64encode(
                json.dumps(claims).encode("utf-8")
            ).decode("ascii"),
            "signature",
        ]
    )
    backend._token = lambda scope: token  # type: ignore[method-assign]
    backend._request = (  # type: ignore[method-assign]
        lambda method, url, **kwargs: _Denied()
    )

    with pytest.raises(L7ReleaseError) as excinfo:
        backend.observe(config)

    message = str(excinfo.value)
    assert "HTTP 403" in message
    assert "Search Index Data Contributor" in message
    assert config.search.endpoint in message


def _agentic_capability_inputs(
    tmp_path: Path, *, mode: str | None
) -> tuple[Path, Path]:
    """Inputs where the Search managed identity lacks its Foundry role."""
    config_path, observation_path, _ = _inputs(tmp_path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if mode is not None:
        raw["search"]["agentic_components"] = mode
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    observed = json.loads(observation_path.read_text(encoding="utf-8"))
    observed["capabilities"]["search.knowledge-source"] = False
    observed["capabilities"]["search.knowledge-base"] = False
    observation_path.write_text(json.dumps(observed), encoding="utf-8")
    return config_path, observation_path


def _plan_for(config_path: Path, observation_path: Path, plan_path: Path):
    result = CliRunner().invoke(
        cli,
        [
            "app",
            "deploy-l7",
            "--config",
            str(config_path),
            "--observation",
            str(observation_path),
            "--plan",
            str(plan_path),
        ],
    )
    assert result.exit_code == 0, result.output
    return load_plan(plan_path)


@pytest.mark.unit
def test_missing_search_managed_identity_role_is_a_no_go_by_default(
    tmp_path: Path,
) -> None:
    config_path, observation_path = _agentic_capability_inputs(
        tmp_path, mode=None
    )
    plan = _plan_for(config_path, observation_path, tmp_path / "plan.json")
    blocked = {
        item.component for item in plan.actions if item.action == "no-go"
    }
    assert blocked == {"search-knowledge-source", "search-knowledge-base"}


@pytest.mark.unit
def test_explicit_deferral_proves_the_direct_index_without_claiming_preview(
    tmp_path: Path,
) -> None:
    config_path, observation_path = _agentic_capability_inputs(
        tmp_path, mode="deferred"
    )
    plan = _plan_for(config_path, observation_path, tmp_path / "plan.json")
    by_component = {item.component: item for item in plan.actions}

    # The preview components are deferred, never created and never a success.
    for component in ("search-knowledge-source", "search-knowledge-base"):
        assert by_component[component].action == "deferred"
        assert "deferred" in by_component[component].reason
    assert not [item for item in plan.actions if item.action == "no-go"]

    # The direct index path is still actually deployed and verified.
    assert by_component["search-index"].action == "create"


@pytest.mark.unit
def test_deferral_never_masks_a_release_owned_search_name_collision(
    tmp_path: Path,
) -> None:
    config_path, observation_path = _agentic_capability_inputs(
        tmp_path, mode="deferred"
    )
    observed = json.loads(observation_path.read_text(encoding="utf-8"))
    observed["capabilities"]["search.knowledge-source"] = True
    observed["resources"].append(
        {
            "resource_id": (
                "https://example.search.windows.net/knowledgesources/"
                "fabric-kg-024-source"
            ),
            "exists": True,
            "resource_type": "SearchKnowledgeSource",
            "name": "fabric-kg-024-source",
        }
    )
    observation_path.write_text(json.dumps(observed), encoding="utf-8")

    plan = _plan_for(config_path, observation_path, tmp_path / "plan.json")
    by_component = {item.component: item for item in plan.actions}
    assert by_component["search-knowledge-source"].action == "no-go"
    assert "collision" in by_component["search-knowledge-source"].reason
    # The still-unavailable knowledge base remains deferred, not created.
    assert by_component["search-knowledge-base"].action == "deferred"


@pytest.mark.unit
def test_search_index_is_never_deferrable(tmp_path: Path) -> None:
    config_path, observation_path = _agentic_capability_inputs(
        tmp_path, mode="deferred"
    )
    observed = json.loads(observation_path.read_text(encoding="utf-8"))
    observed["capabilities"]["search.index"] = False
    observation_path.write_text(json.dumps(observed), encoding="utf-8")

    plan = _plan_for(config_path, observation_path, tmp_path / "plan.json")
    by_component = {item.component: item for item in plan.actions}
    assert by_component["search-index"].action == "no-go"
