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
        self.attempt_id = None

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "PUT":
            metadata = kwargs.get("json", {}).get("properties", {}).get(
                "metadata",
                {},
            )
            self.attempt_id = metadata.get("l7AttemptId")
        response = self.responses.pop(0)
        if (
            method == "GET"
            and self.attempt_id
            and isinstance(response._body, dict)
            and isinstance(response._body.get("properties"), dict)
        ):
            properties = dict(response._body["properties"])
            metadata = dict(properties.get("metadata") or {})
            metadata["l7AttemptId"] = self.attempt_id
            properties["metadata"] = metadata
            response._body = {**response._body, "properties": properties}
        return response


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


@pytest.mark.parametrize(
    ("failure", "expected_exception"),
    [
        (ValueError("parser"), ProjectConnectionError),
        (KeyboardInterrupt(), KeyboardInterrupt),
    ],
)
def test_any_post_put_non_success_conditionally_rolls_back(
    failure,
    expected_exception,
):
    class BrokenResponse(_Response):
        def json(self):
            raise failure

    request = _Requests(
        [
            _Response(404),
            BrokenResponse(201, headers={"ETag": "created-etag"}),
            _Response(204),
        ]
    )
    with pytest.raises(expected_exception):
        _client(request).upsert_remote_tool(
            name="remote",
            target="https://example.test/l6",
            audience="api://l6",
            create_only=True,
        )
    assert [call[0] for call in request.calls] == ["GET", "PUT", "DELETE"]
    assert request.calls[-1][2]["headers"]["If-Match"] == "created-etag"


def test_transport_connection_error_is_sanitized():
    def request(method, url, **kwargs):
        del method, url, kwargs
        raise ConnectionError("provider endpoint and body")

    with pytest.raises(ProjectConnectionError, match="transport failed"):
        _client(request).get("remote")


def test_commit_then_connection_transport_error_reconciles_and_deletes():
    resource_id = (
        "/subscriptions/sub/resourceGroups/rg/providers/"
        "Microsoft.CognitiveServices/accounts/account/projects/project/"
        "connections/remote"
    )

    class Transport:
        def __init__(self):
            self.value = None
            self.calls = []

        def __call__(self, method, url, **kwargs):
            self.calls.append(method)
            if method == "GET" and self.value is None:
                return _Response(404)
            if method == "PUT":
                self.value = {
                    "id": resource_id,
                    "etag": "committed-etag",
                    "properties": kwargs["json"]["properties"],
                }
                raise ConnectionError("response lost after commit")
            if method == "GET":
                return _Response(200, self.value)
            if method == "DELETE":
                self.value = None
                return _Response(204)
            raise AssertionError(method)

    transport = Transport()
    with pytest.raises(ProjectConnectionError, match="reconciled"):
        _client(transport).upsert_remote_tool(
            name="remote",
            target="https://example.test/l6",
            audience="api://l6",
            create_only=True,
        )
    assert transport.calls == ["GET", "PUT", "GET", "DELETE"]
    assert transport.value is None


def test_commit_then_http_5xx_reconciles_and_deletes():
    resource_id = (
        "/subscriptions/sub/resourceGroups/rg/providers/"
        "Microsoft.CognitiveServices/accounts/account/projects/project/"
        "connections/remote"
    )

    class Transport:
        def __init__(self):
            self.value = None
            self.calls = []

        def __call__(self, method, url, **kwargs):
            self.calls.append(method)
            if method == "GET" and self.value is None:
                return _Response(404)
            if method == "PUT":
                self.value = {
                    "id": resource_id,
                    "etag": "committed-etag",
                    "properties": kwargs["json"]["properties"],
                }
                return _Response(503)
            if method == "GET":
                return _Response(200, self.value)
            if method == "DELETE":
                self.value = None
                return _Response(204)
            raise AssertionError(method)

    transport = Transport()
    with pytest.raises(ProjectConnectionError, match="HTTP 503"):
        _client(transport).upsert_remote_tool(
            name="remote",
            target="https://example.test/l6",
            audience="api://l6",
            create_only=True,
        )
    assert transport.calls == ["GET", "PUT", "GET", "DELETE"]
    assert transport.value is None


def test_delayed_connection_commit_is_observed_before_absence_is_accepted():
    resource_id = (
        "/subscriptions/sub/resourceGroups/rg/providers/"
        "Microsoft.CognitiveServices/accounts/account/projects/project/"
        "connections/remote"
    )

    class Transport:
        def __init__(self):
            self.value = None
            self.calls = []
            self.reconcile_gets = 0

        def __call__(self, method, url, **kwargs):
            self.calls.append(method)
            if method == "GET" and self.value is None:
                if "PUT" not in self.calls:
                    return _Response(404)
                self.reconcile_gets += 1
                if self.reconcile_gets == 1:
                    return _Response(404)
                self.value = {
                    "id": resource_id,
                    "etag": "delayed-etag",
                    "properties": kwargs.get("json", {}),
                }
                # Use the exact request captured by the preceding PUT.
                put = next(
                    call
                    for call in reversed(self.recorded)
                    if call[0] == "PUT"
                )
                self.value["properties"] = put[1]["json"]["properties"]
                return _Response(200, self.value)
            if method == "PUT":
                self.recorded = getattr(self, "recorded", [])
                self.recorded.append((method, kwargs))
                return _Response(503)
            if method == "DELETE":
                self.value = None
                return _Response(204)
            raise AssertionError(method)

    transport = Transport()
    with pytest.raises(ProjectConnectionError, match="HTTP 503"):
        FoundryProjectConnectionClient(
            subscription_id="sub",
            resource_group="rg",
            account_name="account",
            project_name="project",
            tenant_id="tenant",
            credential=SimpleNamespace(
                tenant_id="tenant",
                get_token=lambda scope: SimpleNamespace(token="opaque-token"),
            ),
            transport=transport,
            reconciliation_timeout_seconds=1,
            reconciliation_poll_seconds=0.001,
        ).upsert_remote_tool(
            name="remote",
            target="https://example.test/l6",
            audience="api://l6",
            create_only=True,
        )
    assert transport.reconcile_gets == 2
    assert transport.value is None


def test_delayed_connection_update_is_restored_after_consistency_window():
    resource_id = (
        "/subscriptions/sub/resourceGroups/rg/providers/"
        "Microsoft.CognitiveServices/accounts/account/projects/project/"
        "connections/remote"
    )
    previous = {
        "authType": "ProjectManagedIdentity",
        "category": "RemoteTool",
        "target": "https://old.example/l6",
        "isSharedToAll": True,
        "audience": "api://old",
        "metadata": {"ApiType": "Azure"},
    }

    class Transport:
        def __init__(self):
            self.value = {
                "id": resource_id,
                "etag": "old-etag",
                "properties": previous,
            }
            self.put_count = 0
            self.reconcile_gets = 0

        def __call__(self, method, url, **kwargs):
            if method == "GET":
                if self.put_count == 1:
                    self.reconcile_gets += 1
                    if self.reconcile_gets == 2:
                        self.value = {
                            "id": resource_id,
                            "etag": "new-etag",
                            "properties": self.attempted,
                        }
                return _Response(200, self.value)
            if method == "PUT":
                self.put_count += 1
                if self.put_count == 1:
                    self.attempted = kwargs["json"]["properties"]
                    return _Response(503)
                self.value = {
                    "id": resource_id,
                    "etag": "restored-etag",
                    "properties": kwargs["json"]["properties"],
                }
                return _Response(200, self.value)
            raise AssertionError(method)

    transport = Transport()
    with pytest.raises(ProjectConnectionError, match="HTTP 503"):
        FoundryProjectConnectionClient(
            subscription_id="sub",
            resource_group="rg",
            account_name="account",
            project_name="project",
            tenant_id="tenant",
            credential=SimpleNamespace(
                tenant_id="tenant",
                get_token=lambda scope: SimpleNamespace(token="opaque-token"),
            ),
            transport=transport,
            reconciliation_timeout_seconds=1,
            reconciliation_poll_seconds=0.001,
        ).upsert_remote_tool(
            name="remote",
            target="https://new.example/l6",
            audience="api://new",
            expected_etag="old-etag",
        )
    assert transport.reconcile_gets == 2
    assert transport.value["properties"] == previous


def test_uncertain_update_requires_continuously_stable_previous_state():
    resource_id = (
        "/subscriptions/sub/resourceGroups/rg/providers/"
        "Microsoft.CognitiveServices/accounts/account/projects/project/"
        "connections/remote"
    )
    previous = {
        "authType": "ProjectManagedIdentity",
        "category": "RemoteTool",
        "target": "https://old.example/l6",
        "isSharedToAll": True,
        "audience": "api://old",
        "metadata": {"ApiType": "Azure"},
    }

    class Transport:
        def __init__(self):
            self.value = {
                "id": resource_id,
                "etag": "old-etag",
                "properties": previous,
            }
            self.put_seen = False
            self.reconcile_gets = 0

        def __call__(self, method, url, **kwargs):
            if method == "GET":
                if self.put_seen:
                    self.reconcile_gets += 1
                    if self.reconcile_gets == 2:
                        raise ConnectionError("transient gap")
                return _Response(200, self.value)
            if method == "PUT":
                self.put_seen = True
                return _Response(503)
            raise AssertionError(method)

    with pytest.raises(
        ProjectConnectionError,
        match="continuously stable",
    ):
        FoundryProjectConnectionClient(
            subscription_id="sub",
            resource_group="rg",
            account_name="account",
            project_name="project",
            tenant_id="tenant",
            credential=SimpleNamespace(
                tenant_id="tenant",
                get_token=lambda scope: SimpleNamespace(token="opaque-token"),
            ),
            transport=Transport(),
            reconciliation_timeout_seconds=0.01,
            reconciliation_poll_seconds=0.001,
        ).upsert_remote_tool(
            name="remote",
            target="https://new.example/l6",
            audience="api://new",
            expected_etag="old-etag",
        )
