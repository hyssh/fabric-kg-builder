"""tests/unit/agent/test_deployer_visual_assets.py

Regression tests for v1.8: an optional
``environments.<env>.knowledge.visualAssetsIndexName`` config key wires a
second, INDEPENDENT ``azure_ai_search`` tool_spec (its own tool object) for
an image/visual-assets index, alongside the primary evidence index's
tool_spec. Each is resolved (query_type auto-detect) independently by its
own index_name.

Design note: the live Foundry service rejects more than one entry in a
single AzureAISearchToolResource.indexes list ("Array length 2 exceeds
maximum 1" — confirmed via a live dry-run/deploy attempt), so a second
index MUST be a second tool_spec/tool object, never a second entry under
one tool. See foundry_agent_client.py's SDKAgentTransport.create_or_update_agent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from fabric_kg_builder.agent.deployer import deploy_agent
from fabric_kg_builder.agent.foundry_agent_client import FakeAgentTransport


class _ProbeClient:
    """Mirrors FoundryAgentClient's public surface with a per-index probe."""

    def __init__(
        self,
        *,
        transport: FakeAgentTransport,
        vectorizer_by_index: dict[str, bool | None],
    ) -> None:
        self._transport = transport
        self._vectorizer_by_index = vectorizer_by_index
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
        return self._vectorizer_by_index.get(index_name)


def _write_metadata(tmp_path: Path, *, with_visual_assets: bool = True) -> Path:
    knowledge: dict[str, Any] = {"searchIndexName": "fabric-kg-024-seattle-hub-evidence"}
    if with_visual_assets:
        knowledge["visualAssetsIndexName"] = "fabric-kg-024-seattle-hub-visual-assets"
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


def _search_tool_specs(transport: FakeAgentTransport) -> list[dict[str, Any]]:
    create_calls = [c for c in transport.calls if c["method"] == "create_or_update_agent"]
    definition = create_calls[-1]["definition"]
    return [t for t in definition["tools"] if t["type"] == "azure_ai_search"]


def test_visual_assets_index_added_as_a_second_independent_tool_spec(tmp_path: Path) -> None:
    md_path = _write_metadata(tmp_path)
    transport = FakeAgentTransport()
    client = _ProbeClient(
        transport=transport,
        vectorizer_by_index={
            "fabric-kg-024-seattle-hub-evidence": False,
            "fabric-kg-024-seattle-hub-visual-assets": False,
        },
    )

    deploy_agent(environment="dev", metadata_path=md_path, _client=client)

    specs = _search_tool_specs(transport)
    assert len(specs) == 2
    index_names = {s["index_name"] for s in specs}
    assert index_names == {
        "fabric-kg-024-seattle-hub-evidence",
        "fabric-kg-024-seattle-hub-visual-assets",
    }
    for spec in specs:
        assert spec["query_type"] == "semantic"
        assert spec["project_connection_id"] == "fake-search-conn"
    # Both indexes must be probed independently — same connection, but a
    # different index name each time.
    assert ("fake-search-conn", "fabric-kg-024-seattle-hub-evidence") in client.probe_calls
    assert ("fake-search-conn", "fabric-kg-024-seattle-hub-visual-assets") in client.probe_calls


def test_visual_assets_index_resolves_query_type_independently(tmp_path: Path) -> None:
    """The primary and visual-assets indexes can resolve to DIFFERENT query
    types — one probe result must never be assumed for the other index."""
    md_path = _write_metadata(tmp_path)
    transport = FakeAgentTransport()
    client = _ProbeClient(
        transport=transport,
        vectorizer_by_index={
            "fabric-kg-024-seattle-hub-evidence": False,
            "fabric-kg-024-seattle-hub-visual-assets": True,
        },
    )

    deploy_agent(environment="dev", metadata_path=md_path, _client=client)

    specs = _search_tool_specs(transport)
    by_index = {s["index_name"]: s["query_type"] for s in specs}
    assert by_index["fabric-kg-024-seattle-hub-evidence"] == "semantic"
    assert by_index["fabric-kg-024-seattle-hub-visual-assets"] == "vector_semantic_hybrid"


def test_no_visual_assets_index_configured_yields_single_search_tool_spec(
    tmp_path: Path,
) -> None:
    """Without visualAssetsIndexName set, behavior is unchanged: exactly one
    azure_ai_search tool_spec (backward compatible with pre-v1.8 config)."""
    md_path = _write_metadata(tmp_path, with_visual_assets=False)
    transport = FakeAgentTransport()
    client = _ProbeClient(
        transport=transport,
        vectorizer_by_index={"fabric-kg-024-seattle-hub-evidence": False},
    )

    deploy_agent(environment="dev", metadata_path=md_path, _client=client)

    specs = _search_tool_specs(transport)
    assert len(specs) == 1
    assert specs[0]["index_name"] == "fabric-kg-024-seattle-hub-evidence"


def test_visual_assets_index_honors_explicit_query_type_override(tmp_path: Path) -> None:
    """An explicit knowledge.searchQueryType override applies to BOTH tool
    specs and the live probe must not be consulted for either."""
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
                "knowledge": {
                    "searchIndexName": "fabric-kg-024-seattle-hub-evidence",
                    "visualAssetsIndexName": "fabric-kg-024-seattle-hub-visual-assets",
                    "searchQueryType": "vector_simple_hybrid",
                },
            }
        },
    }
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "agent-metadata.yaml"
    path.write_text(yaml.safe_dump(metadata), encoding="utf-8")

    transport = FakeAgentTransport()
    client = _ProbeClient(transport=transport, vectorizer_by_index={})

    deploy_agent(environment="dev", metadata_path=path, _client=client)

    specs = _search_tool_specs(transport)
    assert len(specs) == 2
    assert all(s["query_type"] == "vector_simple_hybrid" for s in specs)
    assert client.probe_calls == []
