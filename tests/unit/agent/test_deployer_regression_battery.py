"""tests/unit/agent/test_deployer_regression_battery.py

Regression coverage for wiring the issue #138 repeat-N test-case battery
into ``deploy_agent`` (Step 7.5, between smoke prompt and persisting
deploymentContext).

Asserts:
  - A live deploy with a REQUIRED testCase that fails/flakes the repeat-N
    gate aborts the deploy (DeploymentError) and never persists
    deploymentContext.
  - A non-required (known-flaky canary) testCase failing/flaking does NOT
    block the deploy, but is still recorded on the returned DeploymentContext
    for reporting.
  - ``skip_regression_battery=True`` bypasses the battery entirely.
  - Dry-run never runs the battery (it returns before a live client exists).
  - No testCases declared => battery is a no-op (backward compatible with
    every existing agent-metadata.yaml that predates this feature).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from fabric_kg_builder.agent.deployer import DeploymentError, deploy_agent
from fabric_kg_builder.agent.foundry_agent_client import FakeAgentTransport


class _QueueingClient:
    """Wraps FakeAgentTransport; ``invoke`` pops from a per-prompt response
    queue so each repeated call can return a different (flaky) result."""

    def __init__(self, transport: FakeAgentTransport, scripts: dict[str, list[dict[str, Any]]]) -> None:
        self._transport = transport
        self._scripts = {k: list(v) for k, v in scripts.items()}

    def validate_schema(self, agent_name: str) -> dict[str, Any]:
        existing = self._transport.get_agent(agent_name)
        return {"valid": True, "agent_id": (existing or {}).get("id", ""), "errors": []}

    def create_or_update_agent(self, definition: dict[str, Any]) -> dict[str, Any]:
        result = self._transport.create_or_update_agent(definition)
        result.setdefault("version_id", result.get("id", ""))
        return result

    def check_ready(self, agent_name: str) -> bool:
        return bool(self._transport.get_agent(agent_name))

    def invoke(self, agent_name: str, prompt: str) -> dict[str, Any]:
        if prompt not in self._scripts:
            # Smoke prompt / anything not explicitly scripted -> default OK.
            return self._transport.invoke_agent(agent_name, prompt, 60)
        responses = self._scripts[prompt]
        if not responses:
            raise AssertionError(f"script exhausted for {prompt!r}")
        return responses.pop(0)


def _resp(route_type: str) -> dict[str, Any]:
    text = f"route_type: {route_type}\nAnswer.\n"
    return {"answer": text, "output_text": text, "status": "completed"}


def _write_metadata(tmp_path: Path, *, test_cases: list[dict[str, Any]]) -> Path:
    metadata = {
        "schemaVersion": "1.0",
        "agentName": "test-grounded-agent",
        "defaultEnvironment": "dev",
        "model": {"deploymentName": "gpt-4o"},
        "environments": {
            "dev": {
                "projectEndpoint": "https://fake.services.ai.azure.com/api/projects/fake",
                "connections": {"fabricDataAgent": "fake-fabric-conn"},
                "knowledge": {},
            }
        },
        "testCases": test_cases,
    }
    path = tmp_path / "agent-metadata.yaml"
    path.write_text(yaml.safe_dump(metadata), encoding="utf-8")
    return path


def test_required_test_case_failure_blocks_deploy_and_does_not_persist(tmp_path: Path) -> None:
    md_path = _write_metadata(
        tmp_path,
        test_cases=[
            {"id": "tc1", "input": "q1", "expectedRouteType": "ontology"},
        ],
    )
    transport = FakeAgentTransport()
    client = _QueueingClient(transport, {"q1": [_resp("unsupported")] * 3})

    with pytest.raises(DeploymentError, match="Regression battery gate failed"):
        deploy_agent(
            environment="dev", metadata_path=md_path, _client=client, regression_repeat=3
        )

    # deploymentContext must NOT have been written to disk.
    raw = yaml.safe_load(md_path.read_text(encoding="utf-8"))
    assert not raw.get("deploymentContext")


def test_required_test_case_flaky_also_blocks_deploy(tmp_path: Path) -> None:
    md_path = _write_metadata(
        tmp_path,
        test_cases=[{"id": "surflink-screw", "input": "q1", "expectedRouteType": "mixed"}],
    )
    transport = FakeAgentTransport()
    # 1 pass, 4 fails — the exact ratio observed live for this bug class.
    scripted = [_resp("mixed")] + [_resp("unsupported")] * 4
    client = _QueueingClient(transport, {"q1": scripted})

    with pytest.raises(DeploymentError, match="flaky"):
        deploy_agent(
            environment="dev", metadata_path=md_path, _client=client, regression_repeat=5
        )


def test_non_required_flaky_case_does_not_block_but_is_recorded(tmp_path: Path) -> None:
    md_path = _write_metadata(
        tmp_path,
        test_cases=[
            {
                "id": "known-flaky-canary",
                "input": "q1",
                "expectedRouteType": "mixed",
                "required": False,
            }
        ],
    )
    transport = FakeAgentTransport()
    scripted = [_resp("mixed")] + [_resp("unsupported")] * 2
    client = _QueueingClient(transport, {"q1": scripted})

    ctx = deploy_agent(
        environment="dev", metadata_path=md_path, _client=client, regression_repeat=3
    )

    assert ctx.smoke_passed is True
    assert len(ctx.test_battery) == 1
    assert ctx.test_battery[0].classification == "flaky"
    assert ctx.test_battery[0].required is False


def test_all_pass_required_case_allows_deploy_to_proceed(tmp_path: Path) -> None:
    md_path = _write_metadata(
        tmp_path,
        test_cases=[{"id": "tc1", "input": "q1", "expectedRouteType": "ontology"}],
    )
    transport = FakeAgentTransport()
    client = _QueueingClient(transport, {"q1": [_resp("ontology")] * 3})

    ctx = deploy_agent(
        environment="dev", metadata_path=md_path, _client=client, regression_repeat=3
    )

    assert ctx.agent_version_id
    assert ctx.test_battery[0].classification == "pass"
    raw = yaml.safe_load(md_path.read_text(encoding="utf-8"))
    assert raw.get("deploymentContext", {}).get("dev", {}).get("agent_ready") is True


def test_skip_regression_battery_bypasses_gate_entirely(tmp_path: Path) -> None:
    md_path = _write_metadata(
        tmp_path,
        test_cases=[{"id": "tc1", "input": "q1", "expectedRouteType": "ontology"}],
    )
    transport = FakeAgentTransport()
    # Would fail the gate if the battery ran — must never be invoked.
    client = _QueueingClient(transport, {"q1": [_resp("unsupported")] * 3})

    ctx = deploy_agent(
        environment="dev",
        metadata_path=md_path,
        _client=client,
        skip_regression_battery=True,
    )

    assert ctx.agent_version_id
    assert ctx.test_battery == []


def test_no_test_cases_declared_is_a_no_op(tmp_path: Path) -> None:
    md_path = _write_metadata(tmp_path, test_cases=[])
    transport = FakeAgentTransport()
    client = _QueueingClient(transport, {})

    ctx = deploy_agent(environment="dev", metadata_path=md_path, _client=client)

    assert ctx.agent_version_id
    assert ctx.test_battery == []


def test_dry_run_never_runs_the_battery_even_with_failing_test_cases(tmp_path: Path) -> None:
    md_path = _write_metadata(
        tmp_path,
        test_cases=[{"id": "tc1", "input": "q1", "expectedRouteType": "ontology"}],
    )
    # No _client injected + dry_run=True => deployer short-circuits before
    # ever building a live client, so the battery must not run at all.
    ctx = deploy_agent(environment="dev", metadata_path=md_path, dry_run=True)

    assert ctx.test_battery == []
    assert ctx.agent_version_id == ""
