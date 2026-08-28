from __future__ import annotations

import json
import hashlib
from pathlib import Path
from datetime import datetime, timedelta, timezone

from click.testing import CliRunner

from fabric_kg_builder.agent.l6_integration import build_l6_agent_definition
from fabric_kg_builder.agent.l7_deployment import (
    L7DeploymentConfig,
    L7ObservedIdentity,
    L7OwnershipAuthorityObservation,
    L7ResourceReadback,
    L7RemoteReadinessObservation,
)
from fabric_kg_builder.cli.app_cmd import app_cmd


class _Probe:
    def __init__(self, config):
        self.config = config

    def current_identity(self):
        return L7ObservedIdentity(
            tenant_id=self.config.tenant_id,
            principal_id=self.config.expected_principal_id,
        )

    def probe_remote_readiness(self, *, config, definition):
        now = datetime.now(timezone.utc)
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
            "checked_at": now,
            "expires_at": now + timedelta(minutes=5),
        }
        from fabric_kg_builder.contracts.base import canonical_sha256

        return L7RemoteReadinessObservation(
            **values,
            readiness_hash=canonical_sha256(values),
        )

    def probe_ownership_authority(self, *, config):
        now = datetime.now(timezone.utc)
        values = {
            "backend": "azure_blob",
            "authority_id": config.fabric_connection_ownership_authority_id,
            "snapshot_version": 1,
            "checked_at": now,
            "expires_at": now + timedelta(minutes=5),
        }
        from fabric_kg_builder.contracts.base import canonical_sha256

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
        return L7ResourceReadback(
            resource_kind="fabric_item",
            stable_id=f"fabric:workspace/{workspace_id}/item/{item.item_id}",
            exists=True,
            resource_type=item.item_type,
            properties_hash="c" * 64,
            definition_hash=item.definition_hash,
        )

    def get_connection(self, *, resource_id):
        return L7ResourceReadback(
            resource_kind="foundry_connection",
            stable_id=resource_id,
            exists=False,
        )

    def get_agent(self, *, project_resource_id, agent_name):
        return L7ResourceReadback(
            resource_kind="foundry_agent",
            stable_id=f"{project_resource_id}/agents/{agent_name}",
            exists=False,
        )

    def desired_agent_hash(self, *, config, definition):
        from fabric_kg_builder.contracts.base import canonical_sha256

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


class _NoMutations:
    def apply(self, *args, **kwargs):
        raise AssertionError("dry-run must not mutate")

    def rollback(self, *args, **kwargs):
        raise AssertionError("dry-run must not rollback")

    def verify_postconditions(self, *args, **kwargs):
        raise AssertionError("dry-run must not verify mutation postconditions")

    def rollback_started(self, *args, **kwargs):
        raise AssertionError("dry-run must not rollback started mutations")


def _files(tmp_path: Path):
    fabric_definition_path = Path(
        "tests/fixtures/l7/data-agent-definition.json"
    )
    fabric_definition_bytes = fabric_definition_path.read_bytes()
    fabric_definition = json.loads(fabric_definition_bytes)
    from fabric_kg_builder.contracts.base import canonical_sha256

    config = {
        "tenant_id": "11111111-1111-4111-8111-111111111111",
        "subscription_id": "22222222-2222-4222-8222-222222222222",
        "resource_group": "rg-placeholder",
        "expected_principal_id": "33333333-3333-4333-8333-333333333333",
        "foundry_account_name": "foundry-placeholder",
        "foundry_project_name": "project-placeholder",
        "foundry_project_endpoint": (
            "https://foundry-placeholder.services.ai.azure.com/"
            "api/projects/project-placeholder"
        ),
        "model_deployment": "model-placeholder",
        "fabric_workspace_id": "44444444-4444-4444-8444-444444444444",
        "fabric_items": [
            {
                "item_id": "55555555-5555-4555-8555-555555555555",
                "item_type": "DataAgent",
                "definition_path": str(fabric_definition_path),
                "definition_hash": canonical_sha256(fabric_definition),
                "definition_bytes_hash": hashlib.sha256(
                    fabric_definition_bytes
                ).hexdigest(),
            }
        ],
        "fabric_connection_name": "fabric-agent",
        "remote_tool_connection_name": "l6-remote",
        "remote_tool_endpoint": "https://l6-placeholder.example",
        "remote_tool_audience": "api://l6-placeholder",
        "remote_tool_allowed_caller_object_ids": [
            "66666666-6666-4666-8666-666666666666"
        ],
        "remote_tool_required_app_role": "L6.Invoke",
        "fabric_connection_ownership_authority_id": (
            "gxra-sha256:" + "f" * 64
        ),
        "l6_authority_backend_version": "1",
        "l5a_definition_hash": "a" * 64,
        "l5b_definition_hash": "b" * 64,
        "plan_ttl_seconds": 900,
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    parsed = L7DeploymentConfig.model_validate_json(json.dumps(config))
    definition = build_l6_agent_definition(
        agent_name="Canonical L6 Agent",
        fabric_data_agent_connection_id=parsed.connection_resource_id(
            parsed.fabric_connection_name
        ),
        foundry_remote_tool_connection_id=parsed.connection_resource_id(
            parsed.remote_tool_connection_name
        ),
    )
    definition_path = tmp_path / "definition.json"
    definition_path.write_bytes(definition.canonical_bytes)
    return config, config_path, definition_path


def test_deploy_l6_dry_run_is_default_and_persists_plan(tmp_path, monkeypatch):
    config, config_path, definition_path = _files(tmp_path)
    monkeypatch.setattr(
        "fabric_kg_builder.agent.l7_adapters.build_default_azure_l7_adapters",
        lambda parsed: (_Probe(parsed), _NoMutations()),
    )
    plan_path = tmp_path / "plan.json"
    result = CliRunner().invoke(
        app_cmd,
        [
            "deploy-l6",
            "--config",
            str(config_path),
            "--definition",
            str(definition_path),
            "--plan",
            str(plan_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "mode=dry-run; mutations=0" in result.output
    assert plan_path.exists()


def test_deploy_l6_live_rejects_missing_approval(tmp_path, monkeypatch):
    _, config_path, definition_path = _files(tmp_path)
    monkeypatch.setattr(
        "fabric_kg_builder.agent.l7_adapters.build_default_azure_l7_adapters",
        lambda parsed: (_Probe(parsed), _NoMutations()),
    )
    result = CliRunner().invoke(
        app_cmd,
        [
            "deploy-l6",
            "--config",
            str(config_path),
            "--definition",
            str(definition_path),
            "--live",
        ],
    )
    assert result.exit_code != 0
    assert "--live requires --approve-live" in result.output
