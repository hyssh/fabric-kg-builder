from __future__ import annotations

from types import SimpleNamespace

from fabric_kg_builder.agent.l7_adapters import (
    AzureL7ReadOnlyProbe,
    SDKL7FoundryAgentBackend,
)
from fabric_kg_builder.contracts.base import canonical_sha256


def test_foundry_get_resolves_versions_latest_and_hashes_effective_definition():
    definition = {
        "model": "model",
        "instructions": "canonical",
        "tools": [{"type": "openapi"}],
    }
    agent = SimpleNamespace(
        name="Canonical L6 Agent",
        versions=SimpleNamespace(
            latest=SimpleNamespace(version="7"),
        ),
    )

    class Operations:
        def list(self):
            return [agent]

        def get_version(self, *, agent_name, agent_version):
            assert agent_name == "Canonical L6 Agent"
            assert agent_version == "7"
            return SimpleNamespace(
                definition=definition,
                metadata={"l6_definition_hash": "a" * 64},
            )

    backend = object.__new__(SDKL7FoundryAgentBackend)
    backend._project = SimpleNamespace(agents=Operations())
    readback = backend.get(
        project_resource_id="/subscriptions/sub/projects/project",
        agent_name="Canonical L6 Agent",
    )
    assert readback.etag == "7"
    assert readback.properties_hash == canonical_sha256(definition)
    assert readback.definition_hash == "a" * 64


class _Response:
    def __init__(self, status_code, body, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self):
        return self._body


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def _fabric_probe(session):
    probe = object.__new__(AzureL7ReadOnlyProbe)
    probe._credential = SimpleNamespace(
        get_token=lambda scope: SimpleNamespace(token="token")
    )
    probe._session = session
    return probe


def test_fabric_definition_readback_uses_post():
    session = _Session(
        [_Response(200, {"definition": {"parts": [{"path": "definition.json"}]}})]
    )
    result = _fabric_probe(session)._fabric_definition(
        workspace_id="workspace",
        item_id="item",
    )
    assert "definition" in result
    assert session.calls[0][0] == "POST"
    assert session.calls[0][2]["json"] == {}


def test_fabric_definition_readback_polls_202_result():
    session = _Session(
        [
            _Response(
                202,
                {},
                {
                    "Location": (
                        "https://api.fabric.microsoft.com/v1/operations/op"
                    )
                },
            ),
            _Response(
                200,
                {
                    "status": "Succeeded",
                    "result": {"definition": {"parts": [{"path": "definition.json"}]}},
                },
            ),
        ]
    )
    result = _fabric_probe(session)._fabric_definition(
        workspace_id="workspace",
        item_id="item",
    )
    assert result["definition"]["parts"][0]["path"] == "definition.json"
    assert [call[0] for call in session.calls] == ["POST", "GET"]
