from __future__ import annotations

from types import SimpleNamespace

import pytest

from fabric_kg_builder.agent.project_connections import (
    FoundryProjectConnectionClient,
    ProjectConnectionError,
)


class _Response:
    def __init__(self, status_code, body=None, headers=None):
        self.status_code = status_code
        self._body = body or {}
        self.headers = headers or {}

    def json(self):
        return self._body


class _Requests:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def _client(request):
    credential = SimpleNamespace(
        tenant_id="tenant",
        get_token=lambda scope: SimpleNamespace(token="opaque-token"),
    )
    return FoundryProjectConnectionClient(
        subscription_id="sub",
        resource_group="rg",
        account_name="account",
        project_name="project",
        tenant_id="tenant",
        credential=credential,
        transport=request,
    )


def _legacy_client(request):
    credential = SimpleNamespace(
        tenant_id="tenant",
        get_token=lambda scope: SimpleNamespace(token="opaque-token"),
    )
    return FoundryProjectConnectionClient(
        subscription_id="sub",
        resource_group="rg",
        account_name="account",
        project_name="project",
        tenant_id="tenant",
        credential=credential,
        request=request,
    )


def test_authorization_uses_acquired_token_without_returning_it():
    request = _Requests([_Response(404)])
    assert _client(request).get("remote") is None
    headers = request.calls[0][2]["headers"]
    assert headers["Authorization"] == "Bearer opaque-token"
    assert "opaque-token" not in repr(_client(request).__dict__)


def test_remote_connection_create_uses_cas_and_exact_readback():
    resource_id = (
        "/subscriptions/sub/resourceGroups/rg/providers/"
        "Microsoft.CognitiveServices/accounts/account/projects/project/"
        "connections/remote"
    )
    properties = {
        "authType": "ProjectManagedIdentity",
        "category": "RemoteTool",
        "target": "https://example.test/l6",
        "isSharedToAll": True,
        "audience": "api://l6",
        "metadata": {"ApiType": "Azure"},
    }
    body = {
        "id": resource_id,
        "etag": "etag-new",
        "properties": properties,
    }
    request = _Requests(
        [_Response(404), _Response(201, body), _Response(200, body)]
    )
    result = _client(request).upsert_remote_tool(
        name="remote",
        target="https://example.test/l6",
        audience="api://l6",
    )
    assert result.action == "created"
    assert request.calls[1][2]["headers"]["If-None-Match"] == "*"
    assert all(
        call[2]["headers"]["Authorization"] == "Bearer opaque-token"
        for call in request.calls
    )


def test_update_rejects_etag_drift_without_put():
    resource_id = (
        "/subscriptions/sub/resourceGroups/rg/providers/"
        "Microsoft.CognitiveServices/accounts/account/projects/project/"
        "connections/remote"
    )
    body = {
        "id": resource_id,
        "etag": "live-etag",
        "properties": {
            "category": "RemoteTool",
            "target": "https://old.example/l6",
            "audience": "api://old",
        },
    }
    request = _Requests([_Response(200, body)])
    with pytest.raises(ProjectConnectionError, match="changed since planning"):
        _client(request).upsert_remote_tool(
            name="remote",
            target="https://example.test/l6",
            audience="api://l6",
            expected_etag="planned-etag",
        )
    assert [call[0] for call in request.calls] == ["GET"]


def test_planned_create_never_adopts_concurrent_matching_resource():
    resource_id = (
        "/subscriptions/sub/resourceGroups/rg/providers/"
        "Microsoft.CognitiveServices/accounts/account/projects/project/"
        "connections/remote"
    )
    body = {
        "id": resource_id,
        "etag": "other-etag",
        "properties": {
            "authType": "ProjectManagedIdentity",
            "category": "RemoteTool",
            "target": "https://example.test/l6",
            "isSharedToAll": True,
            "audience": "api://l6",
            "metadata": {"ApiType": "Azure"},
        },
    }
    request = _Requests([_Response(200, body)])
    with pytest.raises(ProjectConnectionError, match="appeared since planning"):
        _client(request).upsert_remote_tool(
            name="remote",
            target="https://example.test/l6",
            audience="api://l6",
            create_only=True,
        )
    assert [call[0] for call in request.calls] == ["GET"]


def test_failed_post_create_readback_conditionally_deletes_created_resource():
    resource_id = (
        "/subscriptions/sub/resourceGroups/rg/providers/"
        "Microsoft.CognitiveServices/accounts/account/projects/project/"
        "connections/remote"
    )
    desired = {
        "authType": "ProjectManagedIdentity",
        "category": "RemoteTool",
        "target": "https://example.test/l6",
        "isSharedToAll": True,
        "audience": "api://l6",
        "metadata": {"ApiType": "Azure"},
    }
    drifted = {
        **desired,
        "authType": "ApiKey",
    }
    request = _Requests(
        [
            _Response(404),
            _Response(201, {"id": resource_id, "etag": "new", "properties": desired}),
            _Response(
                200,
                {"id": resource_id, "etag": "new", "properties": drifted},
            ),
            _Response(204),
        ]
    )
    with pytest.raises(ProjectConnectionError, match="readback mismatch"):
        _client(request).upsert_remote_tool(
            name="remote",
            target="https://example.test/l6",
            audience="api://l6",
            create_only=True,
        )
    assert [call[0] for call in request.calls] == ["GET", "PUT", "GET", "DELETE"]
    assert request.calls[-1][2]["headers"]["If-Match"] == "new"


def test_legacy_url_first_request_injection_receives_all_methods():
    calls = []

    def request(url, **kwargs):
        calls.append((url, kwargs))
        return _Response(404)

    assert _legacy_client(request).get("remote") is None
    assert calls[0][1]["method"] == "GET"


def test_redacted_fabric_credentials_use_non_secret_binding_commitment():
    resource_id = (
        "/subscriptions/sub/resourceGroups/rg/providers/"
        "Microsoft.CognitiveServices/accounts/account/projects/project/"
        "connections/fabric"
    )
    binding_hash = (
        "751f9d8102a956924d6b25bd49c5f33ac36f9b48cc08147ccbc1bea4964a9546"
    )
    body = {
        "id": resource_id,
        "etag": "etag",
        "properties": {
            "authType": "CustomKeys",
            "category": "CustomKeys",
            "group": "AzureAI",
            "target": "-",
            "isSharedToAll": True,
            "metadata": {
                "type": "fabric_dataagent_preview",
                "bindingHash": binding_hash,
            },
        },
    }
    request = _Requests([_Response(200, body)])
    result = _client(request).get("fabric")
    assert result is not None
    assert result.binding_hash == binding_hash


def test_redacted_custom_keys_connection_cannot_be_updated_destructively():
    resource_id = (
        "/subscriptions/sub/resourceGroups/rg/providers/"
        "Microsoft.CognitiveServices/accounts/account/projects/project/"
        "connections/fabric"
    )
    body = {
        "id": resource_id,
        "etag": "etag",
        "properties": {
            "authType": "CustomKeys",
            "category": "CustomKeys",
            "group": "AzureAI",
            "target": "-",
            "isSharedToAll": True,
            "metadata": {
                "type": "fabric_dataagent_preview",
                "bindingHash": "a" * 64,
            },
        },
    }
    request = _Requests([_Response(200, body)])
    with pytest.raises(ProjectConnectionError, match="redacted credentials"):
        _client(request).upsert_fabric_data_agent(
            name="fabric",
            workspace_id="workspace",
            data_agent_id="different-agent",
            expected_etag="etag",
        )
    assert [call[0] for call in request.calls] == ["GET"]
