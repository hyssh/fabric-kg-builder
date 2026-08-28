"""Release 0.2.4 gates for strict L7 planning and installed CLI inventory."""

from __future__ import annotations

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
    load_l7_config,
    load_plan,
)
from fabric_kg_builder.agent.project_connections import (
    FoundryProjectConnectionClient,
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
        "authority_hash": "1" * 64,
        "l5a_definition_hash": "2" * 64,
        "l5b_definition_hash": "3" * 64,
        "l6_definition": l6,
        "fabric_definitions": [
            {
                "name": "fabric-kg-024-data-agent",
                "item_id": "data-agent-1",
                "item_type": "DataAgent",
                "artifact": data_agent,
                "ownership_receipt": data_agent_ownership,
            },
            {
                "name": "fabric-kg-024-ontology",
                "item_id": "ontology-1",
                "item_type": "Ontology",
                "artifact": ontology,
                "ownership_receipt": ontology_ownership,
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


@pytest.mark.unit
def test_release_version_and_36_top_level_commands() -> None:
    assert __version__ == "0.2.4"
    assert len(cli.commands) == 36
    assert "app" in cli.commands
    assert "deploy-l7" in cli.commands["app"].commands


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
    assert [item.action for item in plan.actions].count("deferred") == 1
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
    ) -> _Response:
        nonlocal created
        calls.append((method, headers))
        assert timeout == 60
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
            readback["credentials"] = {"keys": None}
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
    ) -> _Response:
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
