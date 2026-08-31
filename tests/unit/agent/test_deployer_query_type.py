"""tests/unit/agent/test_deployer_query_type.py

Regression tests for issue #121: the Azure AI Search grounding tool's
``query_type`` must never be hardcoded to ``vector_semantic_hybrid`` (which
requires an integrated vectorizer and fails at invocation time on indexes
that don't have one). ``deploy_agent`` must resolve it via, in order:

  1. an explicit ``environments.<env>.knowledge.searchQueryType`` override
  2. a live probe of the target index (``client.index_has_integrated_vectorizer``)
  3. a safe default of ``"semantic"`` when neither is available/conclusive

These tests use a minimal fake client wrapping ``FakeAgentTransport`` (as
``deploy_agent`` expects a ``FoundryAgentClient``-shaped object) with an
injected ``index_has_integrated_vectorizer`` to exercise all three branches
without any network access.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from fabric_kg_builder.agent.deployer import (
    _ALLOWED_QUERY_TYPES,
    DeploymentError,
    deploy_agent,
)
from fabric_kg_builder.agent.foundry_agent_client import FakeAgentTransport


class _ProbeClient:
    """Wraps FakeAgentTransport and adds an injectable vectorizer probe.

    Mirrors the public surface of ``FoundryAgentClient`` that ``deploy_agent``
    calls: validate_schema / create_or_update_agent / check_ready / invoke /
    index_has_integrated_vectorizer.
    """

    def __init__(
        self,
        *,
        transport: FakeAgentTransport,
        vectorizer_probe_result: bool | None,
    ) -> None:
        self._transport = transport
        self._vectorizer_probe_result = vectorizer_probe_result
        self.probe_calls: list[tuple[str, str]] = []

    def validate_schema(self, agent_name: str) -> dict[str, Any]:
        existing = self._transport.get_agent(agent_name)
        return {
            "valid": True,
            "agent_id": (existing or {}).get("id", ""),
            "agent_version": (existing or {}).get("version", ""),
            "existing": existing,
            "errors": [],
        }

    def create_or_update_agent(self, definition: dict[str, Any]) -> dict[str, Any]:
        result = self._transport.create_or_update_agent(definition)
        result.setdefault("version_id", result.get("id", ""))
        return result

    def check_ready(self, agent_name: str) -> bool:
        existing = self._transport.get_agent(agent_name)
        return bool(existing)

    def invoke(self, agent_name: str, prompt: str) -> dict[str, Any]:
        return self._transport.invoke_agent(agent_name, prompt, 60)

    def index_has_integrated_vectorizer(
        self, connection_id: str, index_name: str
    ) -> bool | None:
        self.probe_calls.append((connection_id, index_name))
        return self._vectorizer_probe_result


def _write_metadata(
    tmp_path: Path, *, search_query_type_override: str | None = None
) -> Path:
    knowledge: dict[str, Any] = {"searchIndexName": "surface-tech-kg-chunks"}
    if search_query_type_override is not None:
        knowledge["searchQueryType"] = search_query_type_override

    metadata = {
        "schemaVersion": "1.0",
        "agentName": "test-grounded-agent",
        "defaultEnvironment": "dev",
        "model": {"deploymentName": "gpt-4o"},
        "environments": {
            "dev": {
                "projectEndpoint": "https://fake.services.ai.azure.com/api/projects/fake",
                "connections": {
                    "search": "fake-search-conn",
                    "fabricDataAgent": "fake-fabric-conn",
                },
                "knowledge": knowledge,
            }
        },
    }
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "agent-metadata.yaml"
    path.write_text(yaml.safe_dump(metadata), encoding="utf-8")
    return path


def _create_calls(transport: FakeAgentTransport) -> list[dict[str, Any]]:
    return [c for c in transport.calls if c["method"] == "create_or_update_agent"]


def _search_tool_spec(create_calls: list[dict[str, Any]]) -> dict[str, Any]:
    definition = create_calls[-1]["definition"]
    (search_spec,) = [t for t in definition["tools"] if t["type"] == "azure_ai_search"]
    return search_spec


def test_query_type_explicit_override_wins(tmp_path: Path) -> None:
    """An explicit knowledge.searchQueryType override is always honored,
    even when a live probe would say otherwise."""
    md_path = _write_metadata(tmp_path, search_query_type_override="vector_simple_hybrid")
    transport = FakeAgentTransport()
    client = _ProbeClient(transport=transport, vectorizer_probe_result=False)

    deploy_agent(environment="dev", metadata_path=md_path, _client=client)

    spec = _search_tool_spec(_create_calls(transport))
    assert spec["query_type"] == "vector_simple_hybrid"
    # Override present => the live probe must not even be consulted.
    assert client.probe_calls == []


def test_query_type_falls_back_to_semantic_when_no_vectorizer(tmp_path: Path) -> None:
    """No override + probe says no integrated vectorizer => safe 'semantic' default."""
    md_path = _write_metadata(tmp_path)
    transport = FakeAgentTransport()
    client = _ProbeClient(transport=transport, vectorizer_probe_result=False)

    deploy_agent(environment="dev", metadata_path=md_path, _client=client)

    spec = _search_tool_spec(_create_calls(transport))
    assert spec["query_type"] == "semantic"
    assert client.probe_calls == [("fake-search-conn", "surface-tech-kg-chunks")]


def test_query_type_uses_hybrid_when_vectorizer_detected(tmp_path: Path) -> None:
    """No override + probe confirms an integrated vectorizer => hybrid is safe to use."""
    md_path = _write_metadata(tmp_path)
    transport = FakeAgentTransport()
    client = _ProbeClient(transport=transport, vectorizer_probe_result=True)

    deploy_agent(environment="dev", metadata_path=md_path, _client=client)

    spec = _search_tool_spec(_create_calls(transport))
    assert spec["query_type"] == "vector_semantic_hybrid"


def test_query_type_falls_back_to_semantic_when_probe_undeterminable(
    tmp_path: Path,
) -> None:
    """No override + probe cannot determine (None) => safe 'semantic' default,
    never silently assume a vectorizer exists."""
    md_path = _write_metadata(tmp_path)
    transport = FakeAgentTransport()
    client = _ProbeClient(transport=transport, vectorizer_probe_result=None)

    deploy_agent(environment="dev", metadata_path=md_path, _client=client)

    spec = _search_tool_spec(_create_calls(transport))
    assert spec["query_type"] == "semantic"


def test_query_type_defensive_default_without_probe_support(tmp_path: Path) -> None:
    """If the injected client has no index_has_integrated_vectorizer at all
    (e.g. an older/minimal test double), deploy_agent must not crash and must
    still fall back to the safe 'semantic' default."""
    md_path = _write_metadata(tmp_path)
    transport = FakeAgentTransport()

    class _NoProbeClient:
        def validate_schema(self, agent_name: str) -> dict[str, Any]:
            return {
                "valid": True,
                "agent_id": "",
                "agent_version": "",
                "existing": None,
                "errors": [],
            }

        def create_or_update_agent(self, definition: dict[str, Any]) -> dict[str, Any]:
            return transport.create_or_update_agent(definition)

        def check_ready(self, agent_name: str) -> bool:
            return True

        def invoke(self, agent_name: str, prompt: str) -> dict[str, Any]:
            return transport.invoke_agent(agent_name, prompt, 60)

    deploy_agent(environment="dev", metadata_path=md_path, _client=_NoProbeClient())

    spec = _search_tool_spec(_create_calls(transport))
    assert spec["query_type"] == "semantic"


def test_invalid_query_type_override_is_rejected(tmp_path: Path) -> None:
    """A typo in knowledge.searchQueryType is rejected up front rather than
    being forwarded to the service as an opaque tool spec value."""
    md_path = _write_metadata(tmp_path, search_query_type_override="vector_hybrid")
    transport = FakeAgentTransport()
    client = _ProbeClient(transport=transport, vectorizer_probe_result=True)

    with pytest.raises(DeploymentError) as excinfo:
        deploy_agent(environment="dev", metadata_path=md_path, _client=client)

    assert "searchQueryType" in str(excinfo.value)
    assert "vector_hybrid" in str(excinfo.value)
    # Rejected before anything was created.
    assert _create_calls(transport) == []


def test_invalid_query_type_override_is_rejected_in_dry_run(tmp_path: Path) -> None:
    """The same typo is caught by --dry-run, so it never reaches a live run."""
    md_path = _write_metadata(tmp_path, search_query_type_override="semantic-hybrid")

    with pytest.raises(DeploymentError):
        deploy_agent(environment="dev", metadata_path=md_path, dry_run=True)


def test_every_allowed_query_type_override_is_accepted(tmp_path: Path) -> None:
    """The allowed set is the contract; each member must deploy unchanged."""
    for query_type in sorted(_ALLOWED_QUERY_TYPES):
        md_path = _write_metadata(
            tmp_path / query_type, search_query_type_override=query_type
        )
        transport = FakeAgentTransport()
        client = _ProbeClient(transport=transport, vectorizer_probe_result=True)

        deploy_agent(environment="dev", metadata_path=md_path, _client=client)

        assert _search_tool_spec(_create_calls(transport))["query_type"] == query_type
