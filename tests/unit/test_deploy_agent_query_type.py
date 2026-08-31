"""Regression tests for the Azure AI Search grounding tool query type.

The deployer used to hardcode ``vector_semantic_hybrid`` while the L5b
publication creates indexes without vector fields, so a grounded agent was
always configured with a query mode its index could not serve. The mismatch
only surfaced at the smoke prompt, i.e. after the agent had been created.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fabric_kg_builder.agent.deployer import DeploymentError, deploy_agent

_METADATA = """\
schemaVersion: '1.0'
agentName: query-type-agent
defaultEnvironment: dev
model:
  deploymentName: gpt-4-1-mini
environments:
  dev:
    projectEndpoint: https://example.services.ai.azure.com/api/projects/p
    deployments:
      chat: gpt-4-1-mini
    connections:
      search: /subscriptions/s/connections/search-conn
      fabricDataAgent: /subscriptions/s/connections/fabric-conn
    knowledge:
      searchIndexName: kg-index
"""


class _CapturingClient:
    """Minimal fake client that records the agent definition it receives."""

    def __init__(self) -> None:
        self.definition: dict[str, Any] | None = None

    def validate_schema(self, _name: str) -> dict[str, Any]:
        return {"valid": True}

    def create_or_update_agent(self, definition: dict[str, Any]) -> dict[str, Any]:
        self.definition = definition
        return {"id": "agent-1", "version": "1"}

    def check_ready(self, _name: str) -> bool:
        return True

    def invoke(self, _name: str, _prompt: str) -> dict[str, Any]:
        return {"output_text": "ready"}


def _metadata_file(tmp_path: Path, *, knowledge_extra: str = "") -> Path:
    text = _METADATA
    if knowledge_extra:
        text += knowledge_extra
    path = tmp_path / "agent-metadata.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _search_tool(client: _CapturingClient) -> dict[str, Any]:
    assert client.definition is not None
    tools = [
        tool
        for tool in client.definition["tools"]
        if tool["type"] == "azure_ai_search"
    ]
    assert len(tools) == 1
    return tools[0]


def _deploy(path: Path, client: _CapturingClient, **kwargs: Any) -> Any:
    return deploy_agent(
        environment="dev",
        _client=client,
        metadata_path=path,
        require_grounding_tools=True,
        **kwargs,
    )


def test_query_type_defaults_to_semantic_not_vector(tmp_path: Path) -> None:
    """The default must match what the L5b publication actually creates."""
    client = _CapturingClient()
    _deploy(_metadata_file(tmp_path), client)
    assert _search_tool(client)["query_type"] == "semantic"


def test_query_type_is_configurable_from_metadata(tmp_path: Path) -> None:
    client = _CapturingClient()
    _deploy(_metadata_file(tmp_path, knowledge_extra="      queryType: full\n"), client)
    assert _search_tool(client)["query_type"] == "full"


def test_top_k_is_configurable_from_metadata(tmp_path: Path) -> None:
    client = _CapturingClient()
    _deploy(_metadata_file(tmp_path, knowledge_extra="      topK: 9\n"), client)
    assert _search_tool(client)["top_k"] == 9


def test_unsupported_query_type_is_rejected(tmp_path: Path) -> None:
    client = _CapturingClient()
    with pytest.raises(DeploymentError, match="Unsupported .*queryType"):
        _deploy(_metadata_file(tmp_path, knowledge_extra="      queryType: nonsense\n"), client)
    assert client.definition is None


def test_vector_query_type_rejected_when_index_has_no_vector_field(
    tmp_path: Path,
) -> None:
    """This is the live defect: fail before creating the agent, not at smoke."""
    client = _CapturingClient()
    index = {"name": "kg-index", "fields": [{"name": "content", "searchable": True}]}
    with pytest.raises(DeploymentError, match="requires a vector field"):
        _deploy(
            _metadata_file(
                tmp_path, knowledge_extra="      queryType: vector_semantic_hybrid\n"
            ),
            client,
            _index_inspector=lambda _name: index,
        )
    # The guard must run before any mutation.
    assert client.definition is None


def test_vector_query_type_allowed_when_index_has_vector_field(
    tmp_path: Path,
) -> None:
    client = _CapturingClient()
    index = {
        "name": "kg-index",
        "fields": [
            {"name": "content", "searchable": True},
            {"name": "contentVector", "dimensions": 1536},
        ],
    }
    _deploy(
        _metadata_file(
            tmp_path, knowledge_extra="      queryType: vector_semantic_hybrid\n"
        ),
        client,
        _index_inspector=lambda _name: index,
    )
    assert _search_tool(client)["query_type"] == "vector_semantic_hybrid"


def test_index_inspection_failure_is_normalized(tmp_path: Path) -> None:
    client = _CapturingClient()

    def _boom(_name: str) -> dict[str, Any]:
        raise RuntimeError("connection reset")

    with pytest.raises(DeploymentError, match="Cannot verify"):
        _deploy(
            _metadata_file(
                tmp_path, knowledge_extra="      queryType: vector_semantic_hybrid\n"
            ),
            client,
            _index_inspector=_boom,
        )
    assert client.definition is None


def test_non_vector_query_type_never_inspects_the_index(tmp_path: Path) -> None:
    """The default path must make no network call."""
    client = _CapturingClient()

    def _never(_name: str) -> dict[str, Any]:
        raise AssertionError("index must not be inspected for a non-vector query")

    _deploy(_metadata_file(tmp_path), client, _index_inspector=_never)
    assert _search_tool(client)["query_type"] == "semantic"


def test_vector_query_type_skips_check_when_index_is_unreadable(
    tmp_path: Path,
) -> None:
    """No inspector and no searchEndpoint means the index cannot be read."""
    client = _CapturingClient()
    _deploy(
        _metadata_file(
            tmp_path, knowledge_extra="      queryType: vector_semantic_hybrid\n"
        ),
        client,
    )
    assert _search_tool(client)["query_type"] == "vector_semantic_hybrid"
