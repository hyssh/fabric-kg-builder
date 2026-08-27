from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from fabric_kg_builder.agent.l6_integration import build_l6_agent_definition
from fabric_kg_builder.agent.l7_deployment import (
    L7DeploymentConfig,
    L7DeploymentError,
    L7DeploymentExecutor,
    L7DeploymentPlanner,
    L7FabricItemTarget,
    L7ObservedIdentity,
    L7ResourceReadback,
    L7ResourceResult,
    load_l7_plan,
    persist_l7_plan,
)
from fabric_kg_builder.contracts.base import canonical_sha256


NOW = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)
TENANT = "11111111-1111-4111-8111-111111111111"
PRINCIPAL = "22222222-2222-4222-8222-222222222222"
SUBSCRIPTION = "33333333-3333-4333-8333-333333333333"
WORKSPACE = "44444444-4444-4444-8444-444444444444"
DATA_AGENT = "55555555-5555-4555-8555-555555555555"


def _config() -> L7DeploymentConfig:
    return L7DeploymentConfig(
        tenant_id=TENANT,
        subscription_id=SUBSCRIPTION,
        resource_group="rg-placeholder",
        expected_principal_id=PRINCIPAL,
        foundry_account_name="foundry-placeholder",
        foundry_project_name="project-placeholder",
        foundry_project_endpoint=(
            "https://foundry-placeholder.services.ai.azure.com/"
            "api/projects/project-placeholder"
        ),
        model_deployment="model-placeholder",
        fabric_workspace_id=WORKSPACE,
        fabric_items=(
            L7FabricItemTarget(item_id=DATA_AGENT, item_type="DataAgent"),
        ),
        fabric_connection_name="fabric-agent",
        remote_tool_connection_name="l6-remote",
        remote_tool_endpoint="https://l6-placeholder.example/tools",
        remote_tool_audience="api://l6-placeholder",
        remote_tool_allowed_caller_object_ids=(PRINCIPAL,),
        remote_tool_required_app_role="L6.Invoke",
        l5a_definition_hash="a" * 64,
        l5b_definition_hash="b" * 64,
        plan_ttl_seconds=900,
    )


def _definition(config: L7DeploymentConfig):
    return build_l6_agent_definition(
        agent_name="Canonical L6 Agent",
        fabric_data_agent_connection_id=config.connection_resource_id(
            config.fabric_connection_name
        ),
        foundry_remote_tool_connection_id=config.connection_resource_id(
            config.remote_tool_connection_name
        ),
    )


class _Probe:
    def __init__(self, config: L7DeploymentConfig) -> None:
        self.config = config
        self.calls: list[str] = []
        self.connection_etag = "etag-1"

    def current_identity(self):
        self.calls.append("identity")
        return L7ObservedIdentity(tenant_id=TENANT, principal_id=PRINCIPAL)

    def get_fabric_item(self, *, workspace_id, item):
        self.calls.append("fabric")
        return L7ResourceReadback(
            resource_kind="fabric_item",
            stable_id=f"fabric:workspace/{workspace_id}/item/{item.item_id}",
            exists=True,
            etag="fabric-etag",
            resource_type=item.item_type,
            properties_hash="c" * 64,
        )

    def get_connection(self, *, resource_id):
        self.calls.append("connection")
        return L7ResourceReadback(
            resource_kind="foundry_connection",
            stable_id=resource_id,
            exists=False,
        )

    def get_agent(self, *, project_resource_id, agent_name):
        self.calls.append("agent")
        return L7ResourceReadback(
            resource_kind="foundry_agent",
            stable_id=f"{project_resource_id}/agents/{agent_name}",
            exists=False,
        )

    def desired_agent_hash(self, *, config, definition):
        return canonical_sha256(
            {
                "definition_hash": definition.definition_hash,
                "model_deployment": config.model_deployment,
                "instructions": definition["instructions"],
                "tools": definition["tools"],
                "connections": definition["connections"],
                "limits": definition["limits"],
            }
        )


class _Mutations:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def apply(self, action, *, config, definition):
        self.calls.append(action.stable_id)
        return L7ResourceResult(
            stable_id=action.stable_id,
            action=(
                "verified"
                if action.action == "verify"
                else "created"
            ),
            after_etag="etag-after",
            readback_hash=canonical_sha256({"id": action.stable_id}),
            rollback_status=(
                "pending" if action.rollback.action != "none" else "not_required"
            ),
        )

    def rollback(self, action, result, *, config):
        return result.model_copy(update={"rollback_status": "succeeded"})


def test_plan_is_read_only_immutable_and_round_trips(tmp_path: Path):
    config = _config()
    probe = _Probe(config)
    plan = L7DeploymentPlanner(probe, clock=lambda: NOW).build(
        config=config,
        definition=_definition(config),
    )
    assert probe.calls == [
        "identity",
        "fabric",
        "connection",
        "connection",
        "agent",
    ]
    assert plan.code_version == "0.2.3"
    assert plan.hosting_prerequisite.startswith("RemoteTool endpoint")
    path = tmp_path / "plan.json"
    persist_l7_plan(path, plan)
    assert load_l7_plan(path) == plan


@pytest.mark.parametrize("approval", ["", "0" * 64, "wrong"])
def test_live_requires_exact_plan_hash_before_mutation(approval: str):
    config = _config()
    probe = _Probe(config)
    planner = L7DeploymentPlanner(probe, clock=lambda: NOW)
    plan = planner.build(config=config, definition=_definition(config))
    mutations = _Mutations()
    executor = L7DeploymentExecutor(
        planner=planner,
        mutations=mutations,
        clock=lambda: NOW + timedelta(seconds=1),
    )
    with pytest.raises(L7DeploymentError, match="exact plan hash"):
        executor.execute(
            plan=plan,
            approve_live=approval,
            config=config,
            definition=_definition(config),
        )
    assert mutations.calls == []


def test_expired_plan_rejected_before_mutation():
    config = _config()
    probe = _Probe(config)
    planner = L7DeploymentPlanner(probe, clock=lambda: NOW)
    plan = planner.build(config=config, definition=_definition(config))
    mutations = _Mutations()
    executor = L7DeploymentExecutor(
        planner=planner,
        mutations=mutations,
        clock=lambda: NOW + timedelta(hours=1),
    )
    with pytest.raises(L7DeploymentError, match="expired"):
        executor.execute(
            plan=plan,
            approve_live=plan.plan_hash,
            config=config,
            definition=_definition(config),
        )
    assert mutations.calls == []


def test_resource_drift_rejected_before_mutation():
    config = _config()
    probe = _Probe(config)
    planner = L7DeploymentPlanner(probe, clock=lambda: NOW)
    plan = planner.build(config=config, definition=_definition(config))
    original = probe.get_fabric_item

    def drifted(**kwargs):
        value = original(**kwargs)
        return value.model_copy(update={"etag": "fabric-etag-drift"})

    probe.get_fabric_item = drifted
    mutations = _Mutations()
    executor = L7DeploymentExecutor(
        planner=planner,
        mutations=mutations,
        clock=lambda: NOW + timedelta(seconds=1),
    )
    with pytest.raises(L7DeploymentError, match="drift"):
        executor.execute(
            plan=plan,
            approve_live=plan.plan_hash,
            config=config,
            definition=_definition(config),
        )
    assert mutations.calls == []


def test_exact_approved_plan_executes_all_actions():
    config = _config()
    probe = _Probe(config)
    planner = L7DeploymentPlanner(probe, clock=lambda: NOW)
    plan = planner.build(config=config, definition=_definition(config))
    mutations = _Mutations()
    receipt = L7DeploymentExecutor(
        planner=planner,
        mutations=mutations,
        clock=lambda: NOW + timedelta(seconds=1),
    ).execute(
        plan=plan,
        approve_live=plan.plan_hash,
        config=config,
        definition=_definition(config),
    )
    assert receipt.status == "succeeded"
    assert receipt.plan_hash == plan.plan_hash
    assert receipt.accounting.calls == len(plan.actions)
    assert len(mutations.calls) == len(plan.actions)


def test_config_rejects_signed_remote_url():
    values = _config().model_dump(mode="python")
    values["remote_tool_endpoint"] = "https://example.test/tool?sig=secret"
    with pytest.raises(ValueError, match="unsigned HTTPS"):
        L7DeploymentConfig(**values)


def test_config_rejects_foundry_endpoint_for_another_project():
    values = _config().model_dump(mode="python")
    values["foundry_project_endpoint"] = (
        "https://foundry-placeholder.services.ai.azure.com/"
        "api/projects/other-project"
    )
    with pytest.raises(ValueError, match="differs from configured"):
        L7DeploymentConfig(**values)


def test_rollback_attempts_all_resources_after_one_rollback_fails():
    config = _config()
    probe = _Probe(config)
    planner = L7DeploymentPlanner(probe, clock=lambda: NOW)
    plan = planner.build(config=config, definition=_definition(config))

    class Mutations(_Mutations):
        def __init__(self):
            super().__init__()
            self.rollback_calls = []

        def apply(self, action, *, config, definition):
            if action.resource_kind == "foundry_agent":
                raise L7DeploymentError("safe injected failure")
            return super().apply(
                action,
                config=config,
                definition=definition,
            )

        def rollback(self, action, result, *, config):
            self.rollback_calls.append(action.resource_kind)
            if action.resource_kind == "remote_tool_connection":
                raise L7DeploymentError("safe rollback failure")
            return super().rollback(action, result, config=config)

    mutations = Mutations()
    with pytest.raises(L7DeploymentError, match="rollback_failures=1"):
        L7DeploymentExecutor(
            planner=planner,
            mutations=mutations,
            clock=lambda: NOW + timedelta(seconds=1),
        ).execute(
            plan=plan,
            approve_live=plan.plan_hash,
            config=config,
            definition=_definition(config),
        )
    assert mutations.rollback_calls == [
        "remote_tool_connection",
        "fabric_connection",
    ]
