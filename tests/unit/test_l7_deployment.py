from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest
from azure.core.exceptions import ServiceRequestError

from fabric_kg_builder.agent.l6_integration import build_l6_agent_definition
from fabric_kg_builder.agent.l7_deployment import (
    L7DeploymentConfig,
    L7DeploymentError,
    L7DeploymentExecutor,
    L7DeploymentPlanner,
    L7DeploymentReceipt,
    L7ConnectionOwnershipReceipt,
    L7FabricItemTarget,
    L7ObservedIdentity,
    L7OwnershipAuthorityObservation,
    L7RemoteReadinessObservation,
    L7RemoteAccounting,
    L7ResourceReadback,
    L7ResourceResult,
    load_l7_plan,
    persist_l7_receipt,
    persist_l7_plan,
)
from fabric_kg_builder.agent.l7_deployment import _connection_desired_hash
from fabric_kg_builder.contracts.base import canonical_sha256


NOW = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)
TENANT = "11111111-1111-4111-8111-111111111111"
PRINCIPAL = "22222222-2222-4222-8222-222222222222"
SUBSCRIPTION = "33333333-3333-4333-8333-333333333333"
WORKSPACE = "44444444-4444-4444-8444-444444444444"
DATA_AGENT = "55555555-5555-4555-8555-555555555555"
FABRIC_DEFINITION_PATH = Path("tests/fixtures/l7/data-agent-definition.json")
FABRIC_DEFINITION_BYTES = FABRIC_DEFINITION_PATH.read_bytes()
FABRIC_DEFINITION = json.loads(FABRIC_DEFINITION_BYTES)


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
            L7FabricItemTarget(
                item_id=DATA_AGENT,
                item_type="DataAgent",
                definition_path=str(FABRIC_DEFINITION_PATH),
                definition_hash=canonical_sha256(FABRIC_DEFINITION),
                definition_bytes_hash=hashlib.sha256(
                    FABRIC_DEFINITION_BYTES
                ).hexdigest(),
            ),
        ),
        fabric_connection_name="fabric-agent",
        remote_tool_connection_name="l6-remote",
        remote_tool_endpoint="https://l6-placeholder.example/tools",
        remote_tool_audience="api://l6-placeholder",
        remote_tool_allowed_caller_object_ids=(PRINCIPAL,),
        remote_tool_required_app_role="L6.Invoke",
        fabric_connection_ownership_authority_id=(
            "gxra-sha256:" + "f" * 64
        ),
        l6_authority_backend_version="1",
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

    def probe_remote_readiness(self, *, config, definition):
        values = {
            "endpoint": config.remote_tool_endpoint,
            "tenant_id": config.tenant_id,
            "audience": config.remote_tool_audience,
            "caller_object_id": (
                config.remote_tool_allowed_caller_object_ids[0]
            ),
            "app_role": config.remote_tool_required_app_role,
            "openapi_schema_hash": "e" * 64,
            "l6_definition_hash": definition.definition_hash,
            "authority_backend": "azure_blob",
            "authority_version": "1",
            "checked_at": NOW,
            "expires_at": NOW + timedelta(minutes=5),
        }
        return L7RemoteReadinessObservation(
            **values,
            readiness_hash=canonical_sha256(values),
        )

    def probe_ownership_authority(self, *, config):
        values = {
            "backend": "azure_blob",
            "authority_id": config.fabric_connection_ownership_authority_id,
            "snapshot_version": 1,
            "checked_at": NOW,
            "expires_at": NOW + timedelta(minutes=5),
        }
        return L7OwnershipAuthorityObservation(
            **values,
            observation_hash=canonical_sha256(values),
        )

    def get_fabric_connection_ownership(
        self,
        *,
        config,
        readback,
        data_agent_id,
    ):
        return None

    def get_fabric_item(self, *, workspace_id, item):
        self.calls.append("fabric")
        return L7ResourceReadback(
            resource_kind="fabric_item",
            stable_id=f"fabric:workspace/{workspace_id}/item/{item.item_id}",
            exists=True,
            etag="fabric-etag",
            resource_type=item.item_type,
            properties_hash="c" * 64,
            definition_hash=item.definition_hash,
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

    def verify_postconditions(self, *, config, definition, results):
        return None

    def rollback_started(self, action, *, config, definition):
        return None


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
def test_live_requires_exact_plan_hash_before_mutation(approval: str, tmp_path):
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
            receipt_path=tmp_path / "receipt.json",
        )
    assert mutations.calls == []


def test_expired_plan_rejected_before_mutation(tmp_path):
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
            receipt_path=tmp_path / "receipt.json",
        )
    assert mutations.calls == []


def test_resource_drift_rejected_before_mutation(tmp_path):
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
            receipt_path=tmp_path / "receipt.json",
        )
    assert mutations.calls == []


def test_exact_approved_plan_executes_all_actions(tmp_path):
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
        receipt_path=tmp_path / "receipt.json",
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


def test_fabric_target_requires_exact_definition_bytes_and_hash():
    values = _config().model_dump(mode="python")
    target = dict(values["fabric_items"][0])
    target["definition_hash"] = None
    values["fabric_items"] = (target,)
    with pytest.raises(ValueError):
        L7DeploymentConfig(**values)


def test_fabric_definition_byte_hash_drift_blocks_readback():
    config = _config()
    target = config.fabric_items[0].model_copy(
        update={"definition_bytes_hash": "0" * 64}
    )
    drifted = config.model_copy(update={"fabric_items": (target,)})
    probe = _Probe(drifted)
    with pytest.raises(L7DeploymentError, match="bytes/hash"):
        L7DeploymentPlanner(probe, clock=lambda: NOW).build(
            config=drifted,
            definition=_definition(drifted),
        )
    assert "fabric" not in probe.calls


def test_config_rejects_foundry_endpoint_for_another_project():
    values = _config().model_dump(mode="python")
    values["foundry_project_endpoint"] = (
        "https://foundry-placeholder.services.ai.azure.com/"
        "api/projects/other-project"
    )
    with pytest.raises(ValueError, match="differs from configured"):
        L7DeploymentConfig(**values)


def test_rollback_attempts_all_resources_after_one_rollback_fails(tmp_path):
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
            receipt_path=tmp_path / "receipt.json",
        )
    assert mutations.rollback_calls == [
        "remote_tool_connection",
        "fabric_connection",
    ]


@pytest.mark.parametrize(
    "failure",
    [
        ConnectionError("network"),
        ServiceRequestError("azure transport"),
        ValueError("parser"),
    ],
)
def test_any_post_mutation_non_success_rolls_back_and_is_sanitized(
    failure,
    tmp_path,
):
    config = _config()
    probe = _Probe(config)
    planner = L7DeploymentPlanner(probe, clock=lambda: NOW)
    plan = planner.build(config=config, definition=_definition(config))

    class Mutations(_Mutations):
        def __init__(self):
            super().__init__()
            self.rollback_calls = []

        def verify_postconditions(self, *, config, definition, results):
            raise failure

        def rollback(self, action, result, *, config):
            self.rollback_calls.append(action.resource_kind)
            return super().rollback(action, result, config=config)

    mutations = Mutations()
    with pytest.raises(L7DeploymentError, match="rollback_failures=0"):
        L7DeploymentExecutor(
            planner=planner,
            mutations=mutations,
            clock=lambda: NOW + timedelta(seconds=1),
        ).execute(
            plan=plan,
            approve_live=plan.plan_hash,
            config=config,
            definition=_definition(config),
            receipt_path=tmp_path / "receipt.json",
        )
    assert mutations.rollback_calls == [
        "foundry_agent",
        "remote_tool_connection",
        "fabric_connection",
    ]


def test_keyboard_interrupt_after_mutation_rolls_back_then_reraises(tmp_path):
    config = _config()
    probe = _Probe(config)
    planner = L7DeploymentPlanner(probe, clock=lambda: NOW)
    plan = planner.build(config=config, definition=_definition(config))

    class Mutations(_Mutations):
        def __init__(self):
            super().__init__()
            self.rollback_calls = []

        def verify_postconditions(self, *, config, definition, results):
            raise KeyboardInterrupt()

        def rollback(self, action, result, *, config):
            self.rollback_calls.append(action.resource_kind)
            return super().rollback(action, result, config=config)

    mutations = Mutations()
    with pytest.raises(KeyboardInterrupt):
        L7DeploymentExecutor(
            planner=planner,
            mutations=mutations,
            clock=lambda: NOW + timedelta(seconds=1),
        ).execute(
            plan=plan,
            approve_live=plan.plan_hash,
            config=config,
            definition=_definition(config),
            receipt_path=tmp_path / "receipt.json",
        )
    assert mutations.rollback_calls == [
        "foundry_agent",
        "remote_tool_connection",
        "fabric_connection",
    ]


def _ownership_receipt(config, *, etag="fabric-connection-etag"):
    connection_id = config.connection_resource_id(
        config.fabric_connection_name
    )
    data_agent = next(
        item for item in config.fabric_items if item.item_type == "DataAgent"
    )
    values = {
        "connection_id": connection_id,
        "connection_etag": etag,
        "category": "CustomKeys",
        "target": "-",
        "audience": "",
        "workspace_id": config.fabric_workspace_id,
        "data_agent_id": data_agent.item_id,
        "authority_id": config.fabric_connection_ownership_authority_id,
        "authority_version": 1,
        "issued_at": NOW,
        "signature": "9" * 64,
    }
    hash_values = {
        **values,
        "issued_at": NOW.isoformat().replace("+00:00", "Z"),
    }
    return L7ConnectionOwnershipReceipt(
        **values,
        receipt_hash=canonical_sha256(hash_values),
    )


class _ExistingFabricConnectionProbe(_Probe):
    def __init__(self, config, receipt):
        super().__init__(config)
        self.receipt = receipt

    def get_connection(self, *, resource_id):
        if resource_id.endswith(
            f"/connections/{self.config.fabric_connection_name}"
        ):
            data_agent = next(
                item
                for item in self.config.fabric_items
                if item.item_type == "DataAgent"
            )
            binding_hash = canonical_sha256(
                {
                    "workspace_id": self.config.fabric_workspace_id,
                    "data_agent_id": data_agent.item_id,
                }
            )
            return L7ResourceReadback(
                resource_kind="foundry_connection",
                stable_id=resource_id,
                exists=True,
                etag="fabric-connection-etag",
                resource_type="CustomKeys",
                properties_hash=_connection_desired_hash(
                    auth_type="CustomKeys",
                    category="CustomKeys",
                    target="-",
                    group="AzureAI",
                    metadata={
                        "type": "fabric_dataagent_preview",
                        "bindingHash": binding_hash,
                    },
                    binding_hash=binding_hash,
                ),
            )
        return super().get_connection(resource_id=resource_id)

    def get_fabric_connection_ownership(
        self,
        *,
        config,
        readback,
        data_agent_id,
    ):
        return self.receipt


def test_preexisting_redacted_customkeys_requires_signed_external_receipt():
    config = _config()
    plan = L7DeploymentPlanner(
        _ExistingFabricConnectionProbe(config, None),
        clock=lambda: NOW,
    ).build(config=config, definition=_definition(config))
    action = next(
        item for item in plan.actions if item.resource_kind == "fabric_connection"
    )
    assert action.action == "unsupported"
    assert "signed durable ownership receipt" in action.capability_reason


def test_valid_external_ownership_receipt_allows_exact_adoption():
    config = _config()
    plan = L7DeploymentPlanner(
        _ExistingFabricConnectionProbe(
            config,
            _ownership_receipt(config),
        ),
        clock=lambda: NOW,
    ).build(config=config, definition=_definition(config))
    action = next(
        item for item in plan.actions if item.resource_kind == "fabric_connection"
    )
    assert action.action == "adopt"


def test_stale_external_ownership_receipt_blocks_adoption():
    config = _config()
    plan = L7DeploymentPlanner(
        _ExistingFabricConnectionProbe(
            config,
            _ownership_receipt(config, etag="stale"),
        ),
        clock=lambda: NOW,
    ).build(config=config, definition=_definition(config))
    action = next(
        item for item in plan.actions if item.resource_kind == "fabric_connection"
    )
    assert action.action == "unsupported"


def test_unreachable_authenticated_readiness_blocks_plan_before_resource_gets():
    config = _config()

    class Probe(_Probe):
        def probe_remote_readiness(self, *, config, definition):
            raise L7DeploymentError(
                "authenticated RemoteTool readiness request failed"
            )

    probe = Probe(config)
    with pytest.raises(L7DeploymentError, match="authenticated"):
        L7DeploymentPlanner(probe, clock=lambda: NOW).build(
            config=config,
            definition=_definition(config),
        )
    assert probe.calls == ["identity"]


def test_readiness_drift_before_success_rolls_back_all_mutations(tmp_path):
    config = _config()

    class Probe(_Probe):
        def __init__(self, config):
            super().__init__(config)
            self.readiness_calls = 0

        def probe_remote_readiness(self, *, config, definition):
            self.readiness_calls += 1
            value = super().probe_remote_readiness(
                config=config,
                definition=definition,
            )
            if self.readiness_calls < 4:
                return value
            values = value.model_dump(
                mode="python",
                exclude={"readiness_hash"},
            )
            values["authority_version"] = "2"
            hash_values = {
                **values,
                "checked_at": values["checked_at"].isoformat().replace(
                    "+00:00",
                    "Z",
                ),
                "expires_at": values["expires_at"].isoformat().replace(
                    "+00:00",
                    "Z",
                ),
            }
            return L7RemoteReadinessObservation(
                **values,
                readiness_hash=canonical_sha256(hash_values),
            )

    class Mutations(_Mutations):
        def __init__(self):
            super().__init__()
            self.rollback_calls = []

        def rollback(self, action, result, *, config):
            self.rollback_calls.append(action.resource_kind)
            return super().rollback(action, result, config=config)

    probe = Probe(config)
    planner = L7DeploymentPlanner(probe, clock=lambda: NOW)
    plan = planner.build(config=config, definition=_definition(config))
    mutations = Mutations()
    with pytest.raises(L7DeploymentError, match="rollback_failures=0"):
        L7DeploymentExecutor(
            planner=planner,
            mutations=mutations,
            clock=lambda: NOW + timedelta(seconds=1),
        ).execute(
            plan=plan,
            approve_live=plan.plan_hash,
            config=config,
            definition=_definition(config),
            receipt_path=tmp_path / "receipt.json",
        )
    assert mutations.rollback_calls == [
        "foundry_agent",
        "remote_tool_connection",
        "fabric_connection",
    ]


def test_readiness_drift_immediately_before_mutation_is_zero_write(tmp_path):
    config = _config()

    class Probe(_Probe):
        def __init__(self, config):
            super().__init__(config)
            self.readiness_calls = 0

        def probe_remote_readiness(self, *, config, definition):
            self.readiness_calls += 1
            value = super().probe_remote_readiness(
                config=config,
                definition=definition,
            )
            if self.readiness_calls < 3:
                return value
            values = value.model_dump(
                mode="python",
                exclude={"readiness_hash"},
            )
            values["audience"] = "api://drifted"
            hash_values = {
                **values,
                "checked_at": values["checked_at"].isoformat().replace(
                    "+00:00",
                    "Z",
                ),
                "expires_at": values["expires_at"].isoformat().replace(
                    "+00:00",
                    "Z",
                ),
            }
            return L7RemoteReadinessObservation(
                **values,
                readiness_hash=canonical_sha256(hash_values),
            )

    probe = Probe(config)
    planner = L7DeploymentPlanner(probe, clock=lambda: NOW)
    plan = planner.build(config=config, definition=_definition(config))
    mutations = _Mutations()
    with pytest.raises(L7DeploymentError, match="before mutation"):
        L7DeploymentExecutor(
            planner=planner,
            mutations=mutations,
            clock=lambda: NOW + timedelta(seconds=1),
        ).execute(
            plan=plan,
            approve_live=plan.plan_hash,
            config=config,
            definition=_definition(config),
            receipt_path=tmp_path / "receipt.json",
        )
    assert mutations.calls == []


def test_started_mutation_is_reconciled_when_apply_never_returns(tmp_path):
    config = _config()
    probe = _Probe(config)
    planner = L7DeploymentPlanner(probe, clock=lambda: NOW)
    plan = planner.build(config=config, definition=_definition(config))

    class Mutations(_Mutations):
        def __init__(self):
            super().__init__()
            self.started_rollbacks = []

        def apply(self, action, *, config, definition):
            if action.resource_kind == "fabric_connection":
                raise ConnectionError("response lost after commit")
            return super().apply(
                action,
                config=config,
                definition=definition,
            )

        def rollback_started(self, action, *, config, definition):
            self.started_rollbacks.append(action.resource_kind)

    mutations = Mutations()
    with pytest.raises(L7DeploymentError, match="rollback_failures=0"):
        L7DeploymentExecutor(
            planner=planner,
            mutations=mutations,
            clock=lambda: NOW + timedelta(seconds=1),
        ).execute(
            plan=plan,
            approve_live=plan.plan_hash,
            config=config,
            definition=_definition(config),
            receipt_path=tmp_path / "receipt.json",
        )
    assert mutations.started_rollbacks == ["fabric_connection"]


def test_receipt_persistence_failure_rolls_back_all_mutations(
    tmp_path,
    monkeypatch,
):
    config = _config()
    probe = _Probe(config)
    planner = L7DeploymentPlanner(probe, clock=lambda: NOW)
    plan = planner.build(config=config, definition=_definition(config))

    class Mutations(_Mutations):
        def __init__(self):
            super().__init__()
            self.rollback_calls = []

        def rollback(self, action, result, *, config):
            self.rollback_calls.append(action.resource_kind)
            return super().rollback(action, result, config=config)

    def fail_persistence(path, receipt):
        del path, receipt
        raise OSError("disk full")

    monkeypatch.setattr(
        "fabric_kg_builder.agent.l7_deployment.persist_l7_receipt",
        fail_persistence,
    )
    mutations = Mutations()
    with pytest.raises(L7DeploymentError, match="rollback_failures=0"):
        L7DeploymentExecutor(
            planner=planner,
            mutations=mutations,
            clock=lambda: NOW + timedelta(seconds=1),
        ).execute(
            plan=plan,
            approve_live=plan.plan_hash,
            config=config,
            definition=_definition(config),
            receipt_path=tmp_path / "receipt.json",
        )
    assert mutations.rollback_calls == [
        "foundry_agent",
        "remote_tool_connection",
        "fabric_connection",
    ]


def test_receipt_persistence_is_atomic_create_if_absent(tmp_path):
    def receipt(seed):
        return L7DeploymentReceipt.seal(
            status="succeeded",
            plan_hash=seed * 64,
            config_hash="c" * 64,
            l6_definition_hash="d" * 64,
            started_at=NOW,
            completed_at=NOW + timedelta(seconds=1),
            resources=(),
            accounting=L7RemoteAccounting(
                calls=0,
                request_bytes=0,
                response_bytes=0,
                retries=0,
                waits=0,
            ),
        )

    receipts = (receipt("a"), receipt("b"))
    path = tmp_path / "receipt.json"

    def write(value):
        try:
            persist_l7_receipt(path, value)
            return "written"
        except L7DeploymentError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(write, receipts))
    assert sorted(outcomes) == ["rejected", "written"]
    assert L7DeploymentReceipt.model_validate_json(
        path.read_text("utf-8")
    ) in receipts
