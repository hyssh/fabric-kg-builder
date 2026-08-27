from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from fabric_kg_builder.agent.l7_remote_tool import create_l6_remote_tool_app
from fabric_kg_builder.app.auth import AuthError, InboundAuthVerifier


class _Verifier(InboundAuthVerifier):
    def verify(self, authorization_header):
        if authorization_header != "Bearer valid":
            raise AuthError("raw provider detail", status_code=401)
        return {"tid": "tenant", "aud": "api://l6"}


class _Handler:
    def __init__(self):
        self.invocations = 0
        self.is_ready = True

    def ready(self):
        return self.is_ready

    async def invoke(self, tool_name, request, *, deadline_monotonic):
        self.invocations += 1
        raise RuntimeError("raw downstream secret")


def _client(handler=None, **kwargs):
    return TestClient(
        create_l6_remote_tool_app(
            handler=handler or _Handler(),
            auth_verifier=_Verifier(),
            **kwargs,
        )
    )


def test_openapi_is_exactly_bound_to_five_canonical_tools():
    schema = _client().get("/openapi.json").json()
    operations = {
        operation["operationId"]
        for path in schema["paths"].values()
        for operation in path.values()
    }
    assert len(operations) == 5
    assert schema["x-fabric-kg-definition"]["zeroSynthesis"] is True


def test_auth_failure_is_sanitized_before_schema_validation():
    response = _client().post(
        "/tools/fabric_kg_resolve_ontology_scope",
        headers={"Authorization": "Bearer wrong"},
        json={},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "request authorization failed"}
    assert "provider" not in response.text


def test_request_size_is_bounded():
    response = _client(max_body_bytes=1024).post(
        "/tools/fabric_kg_resolve_ontology_scope",
        headers={
            "Authorization": "Bearer valid",
            "Content-Length": "2048",
        },
        content=b"{}",
    )
    assert response.status_code == 413


def test_readiness_fails_closed():
    handler = _Handler()
    handler.is_ready = False
    response = _client(handler).get("/ready")
    assert response.status_code == 503
    assert response.json()["detail"] == "L6 authorities are not ready"


def test_invalid_schema_never_invokes_handler():
    handler = _Handler()
    response = _client(handler).post(
        "/tools/fabric_kg_resolve_ontology_scope",
        headers={"Authorization": "Bearer valid"},
        json={},
    )
    assert response.status_code == 422
    assert handler.invocations == 0


def test_handler_contract_receives_cooperative_deadline():
    class Handler(_Handler):
        def __init__(self):
            super().__init__()
            self.deadline = None

        async def invoke(self, tool_name, request, *, deadline_monotonic):
            self.deadline = deadline_monotonic
            return await super().invoke(
                tool_name,
                request,
                deadline_monotonic=deadline_monotonic,
            )

    schema = _client(Handler()).get("/openapi.json").json()
    assert schema["x-fabric-kg-definition"]["deadlineSeconds"] == 30.0
