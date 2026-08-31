"""tests/unit/agent/test_deploy_agent_cmd_grounding.py

The ``app deploy-agent`` command grounds the routing instructions from a
multitype-plan.json passed via ``--entity-types-file``.  Entity types were
already forwarded; relationship types were not, so the agent had no way to
learn the real edge names and could invent them (issue #112).

These tests pin the CLI-level wiring: both lists must be extracted from the
same plan file and handed to ``deploy_agent``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from click.testing import CliRunner

from fabric_kg_builder.cli import app_cmd as app_cmd_module


def _write_plan(tmp_path: Path) -> Path:
    plan = {
        "entity_types": [
            {"type_name": "surface_device"},
            {"type_name": "surface_component"},
        ],
        "relationship_pairs": [
            {"name": "device_has_component"},
            {"name": "procedure_requires_tool"},
        ],
    }
    path = tmp_path / "multitype-plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path


def _write_metadata(tmp_path: Path) -> Path:
    metadata = {
        "agentName": "test-agent",
        "defaultEnvironment": "dev",
        "environments": {
            "dev": {
                "projectEndpoint": "https://example.services.ai.azure.com/api/projects/p",
                "modelDeployment": "gpt-4o",
            }
        },
    }
    path = tmp_path / "agent-metadata.yaml"
    path.write_text(yaml.safe_dump(metadata), encoding="utf-8")
    return path


def _run(tmp_path: Path, monkeypatch: Any) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    class _Ctx:
        model_deployment = "gpt-4o"
        instructions_version = "v0"
        instructions_hash = "deadbeef"
        schema_valid = True
        agent_ready = True
        smoke_passed = True
        agent_version_id = None
        agent_version = None
        image_tag = None

    def _fake_deploy_agent(**kwargs: Any) -> _Ctx:
        captured.update(kwargs)
        return _Ctx()

    monkeypatch.setattr(app_cmd_module, "deploy_agent", _fake_deploy_agent)

    result = CliRunner().invoke(
        app_cmd_module.app_cmd,
        [
            "deploy-agent",
            "--env", "dev",
            "--dry-run",
            "--metadata", str(_write_metadata(tmp_path)),
            "--entity-types-file", str(_write_plan(tmp_path)),
        ],
        obj={},
    )
    assert result.exit_code == 0, result.output
    return captured


def test_cmd_forwards_entity_types(tmp_path: Path, monkeypatch: Any) -> None:
    captured = _run(tmp_path, monkeypatch)
    assert captured["entity_types"] == ["surface_device", "surface_component"]


def test_cmd_forwards_relationship_types(tmp_path: Path, monkeypatch: Any) -> None:
    """Without this the agent can invent edge names — see issue #112."""
    captured = _run(tmp_path, monkeypatch)
    assert captured["relationship_types"] == [
        "device_has_component",
        "procedure_requires_tool",
    ]
