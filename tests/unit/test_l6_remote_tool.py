from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from fabric_kg_builder.agent.l6_remote_tool import (
    L6BlobReadinessAuthorityProvider,
    L6ReadinessAuthority,
    build_l6_openapi_spec,
    create_l6_remote_tool_app,
)
from fabric_kg_builder.app.auth import AuthError, InboundAuthVerifier
from fabric_kg_builder.contracts.base import canonical_sha256

_VALID_AUTH = "Bearer valid-token"
_TOOL_PATH = "/tools/fabric_kg_resolve_ontology_scope"


class _Verifier(InboundAuthVerifier):
    def verify(self, authorization_header):
        if authorization_header != _VALID_AUTH:
            raise AuthError("raw provider detail", status_code=401)
        return {
            "tid": "tenant",
            "aud": "api://l6",
            "oid": "caller-object-id",
            "roles": ["L6.Invoke"],
        }


class _Handler:
    def __init__(self):
        self.invocations = 0
        self.is_ready = True

    def ready(self):
        return self.is_ready

    async def invoke(self, tool_name, request, *, deadline_monotonic):
        self.invocations += 1
        raise RuntimeError("raw downstream secret")


class _AuthorityProvider:
    def __init__(self, authority=None):
        self.authority = authority or L6ReadinessAuthority(
            l6_definition_hash="d" * 64,
            backend_kind="azure_blob",
            backend_version="1",
        )

    def observe(self):
        return self.authority


def test_blob_readiness_adapter_requires_ready_durable_authority():
    class Authority:
        def __init__(self, ready):
            self.ready = ready

        def readiness_observation(self):
            return type("Observation", (), {"ready": self.ready})()

    unavailable = L6BlobReadinessAuthorityProvider(
        authority=Authority(False),
        l6_definition_hash="a" * 64,
    )
    assert unavailable.observe() is None
    available = L6BlobReadinessAuthorityProvider(
        authority=Authority(True),
        l6_definition_hash="a" * 64,
    )
    assert available.observe() == L6ReadinessAuthority(
        l6_definition_hash="a" * 64,
        backend_kind="azure_blob",
        backend_version="1",
    )


def _readiness_kwargs(**overrides):
    values = {
        "external_endpoint": "https://l6.example.test",
        "readiness_authority_provider": _AuthorityProvider(),
        "expected_tenant_id": "tenant",
        "expected_audience": "api://l6",
        "allowed_caller_object_ids": ("caller-object-id",),
        "required_app_role": "L6.Invoke",
        "expected_l6_definition_hash": "d" * 64,
        "expected_authority_backend": "azure_blob",
        "expected_authority_version": "1",
        "readiness_ttl_seconds": 20,
    }
    values.update(overrides)
    return values


def _client(handler=None, **kwargs):
    configured = _readiness_kwargs()
    configured.update(kwargs)
    auth_verifier = configured.pop("auth_verifier", _Verifier())
    return TestClient(
        create_l6_remote_tool_app(
            handler=handler or _Handler(),
            auth_verifier=auth_verifier,
            **configured,
        )
    )


def _raw_app(handler=None, **overrides):
    configured = _readiness_kwargs()
    configured.update(overrides)
    return create_l6_remote_tool_app(
        handler=handler or _Handler(),
        auth_verifier=_Verifier(),
        **configured,
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
    assert schema["info"]["version"] == "0.2.4"
    assert _raw_app().version == "0.2.4"


def test_auth_failure_is_sanitized_before_schema_validation():
    response = _client().post(
        _TOOL_PATH,
        headers={"Authorization": "Bearer invalid-token"},
        json={},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "request authorization failed"}
    assert "provider" not in response.text


def test_tool_ingress_enforces_caller_oid_and_app_role_before_body():
    class WrongRoleVerifier(InboundAuthVerifier):
        def verify(self, authorization_header):
            return {
                "tid": "tenant",
                "aud": "api://l6",
                "oid": "caller-object-id",
                "roles": ["Wrong.Role"],
            }

    response = _client(auth_verifier=WrongRoleVerifier()).post(
        _TOOL_PATH,
        headers={
            "Authorization": _VALID_AUTH,
            "Content-Length": str(10_000_000),
        },
        content=b"{}",
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "request authorization failed"}


def test_request_size_is_bounded():
    response = _client(max_body_bytes=1024).post(
        _TOOL_PATH,
        headers={"Authorization": _VALID_AUTH, "Content-Length": "2048"},
        content=b"{}",
    )
    assert response.status_code == 413


def test_health_is_non_authoritative_and_readiness_requires_authentication():
    client = _client()
    assert client.get("/health").json() == {"status": "ok"}
    response = client.get("/ready")
    assert response.status_code == 401
    assert response.json() == {"detail": "request authorization failed"}


def test_readiness_fails_closed_when_authority_is_absent_or_not_ready():
    response = _client(readiness_authority_provider=None).get(
        "/ready",
        headers={"Authorization": _VALID_AUTH},
    )
    assert response.status_code == 503

    handler = _Handler()
    handler.is_ready = False
    response = _client(handler, **_readiness_kwargs()).get(
        "/ready",
        headers={"Authorization": _VALID_AUTH},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "L6 authorities are not ready"


def test_readiness_rejects_mismatched_durable_authority():
    provider = _AuthorityProvider(
        L6ReadinessAuthority(
            l6_definition_hash="e" * 64,
            backend_kind="azure_blob",
            backend_version="1",
        )
    )
    response = _client(
        **_readiness_kwargs(readiness_authority_provider=provider)
    ).get("/ready", headers={"Authorization": _VALID_AUTH})
    assert response.status_code == 503
    assert response.json() == {"detail": "L6 authorities are not ready"}


def test_readiness_rejects_claims_that_do_not_match_live_authority():
    class WrongCallerVerifier(_Verifier):
        def verify(self, authorization_header):
            claims = super().verify(authorization_header)
            claims["oid"] = "different-caller"
            return claims

    response = _client(
        auth_verifier=WrongCallerVerifier(),
        **_readiness_kwargs(),
    ).get("/ready", headers={"Authorization": _VALID_AUTH})
    assert response.status_code == 503
    assert response.json() == {"detail": "L6 authorities are not ready"}


def test_authenticated_readiness_returns_sealed_preflight_observation():
    response = _client(**_readiness_kwargs()).get(
        "/ready",
        headers={"Authorization": _VALID_AUTH},
    )
    assert response.status_code == 200
    observation = response.json()
    assert observation["tenant_id"] == "tenant"
    assert observation["audience"] == "api://l6"
    assert observation["caller_object_id"] == "caller-object-id"
    assert observation["app_role"] == "L6.Invoke"
    assert observation["authority_backend"] == "azure_blob"
    assert observation["authority_version"] == "1"
    assert observation["l6_definition_hash"] == "d" * 64
    assert observation["openapi_schema_hash"] == canonical_sha256(
        build_l6_openapi_spec(
            endpoint="https://l6.example.test",
            max_body_bytes=1_048_576,
            timeout_seconds=30.0,
        )
    )
    sealed = dict(observation)
    readiness_hash = sealed.pop("readiness_hash")
    assert readiness_hash == canonical_sha256(sealed)


def test_invalid_schema_never_invokes_handler():
    handler = _Handler()
    response = _client(handler).post(
        _TOOL_PATH,
        headers={"Authorization": _VALID_AUTH},
        json={},
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "request failed canonical validation"}
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


async def _asgi_request(app, headers, messages, path=_TOOL_PATH):
    sent = []
    iterator = iter(messages)

    async def receive():
        message = next(iterator)
        if isinstance(message, BaseException):
            raise message
        if callable(message):
            return await message()
        return message

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 443),
    }
    await app(scope, receive, send)
    status = next(item["status"] for item in sent if item["type"] == "http.response.start")
    body = b"".join(
        item.get("body", b"")
        for item in sent
        if item["type"] == "http.response.body"
    )
    return status, json.loads(body)


def _headers(*extra):
    return [(b"authorization", _VALID_AUTH.encode()), *extra]


def test_auth_is_rejected_without_reading_body():
    app = _raw_app()
    status, body = asyncio.run(
        _asgi_request(
            app,
            [(b"authorization", b"Bearer invalid-token"), (b"content-length", b"2")],
            [AssertionError("body must not be read")],
        )
    )
    assert (status, body) == (401, {"detail": "request authorization failed"})


def test_oversized_declared_length_is_rejected_before_body():
    app = _raw_app(max_body_bytes=1024)
    status, body = asyncio.run(
        _asgi_request(
            app,
            _headers((b"content-length", b"1025")),
            [AssertionError("body must not be read")],
        )
    )
    assert (status, body) == (
        413,
        {"detail": "request body exceeds configured limit"},
    )


def test_chunked_body_aborts_as_soon_as_limit_is_crossed():
    app = _raw_app(max_body_bytes=1024)
    messages = [
        {"type": "http.request", "body": b"a" * 700, "more_body": True},
        {"type": "http.request", "body": b"b" * 400, "more_body": True},
        AssertionError("ingress must abort without reading another chunk"),
    ]
    status, body = asyncio.run(
        _asgi_request(
            app,
            _headers((b"transfer-encoding", b"chunked")),
            messages,
        )
    )
    assert (status, body) == (
        413,
        {"detail": "request body exceeds configured limit"},
    )


def test_declared_length_mismatch_aborts_immediately():
    app = _raw_app()
    status, body = asyncio.run(
        _asgi_request(
            app,
            _headers((b"content-length", b"1")),
            [
                {"type": "http.request", "body": b"{}", "more_body": True},
                AssertionError("ingress must abort after the mismatched chunk"),
            ],
        )
    )
    assert (status, body) == (
        400,
        {"detail": "request body length does not match content-length"},
    )


@pytest.mark.parametrize(
    ("headers", "expected_status", "expected_detail"),
    [
        ([], 411, "content-length or chunked transfer encoding required"),
        ([(b"content-length", b"nope")], 400, "invalid content-length header"),
        (
            [(b"content-length", b"2"), (b"content-length", b"3")],
            400,
            "invalid content-length header",
        ),
        (
            [(b"content-length", b"2"), (b"transfer-encoding", b"chunked")],
            400,
            "ambiguous request framing",
        ),
        (
            [(b"transfer-encoding", b"gzip, chunked")],
            400,
            "ambiguous transfer encoding",
        ),
        ([(b"content-encoding", b"gzip")], 415, "unsupported content encoding"),
    ],
)
def test_invalid_or_ambiguous_framing_is_static(
    headers, expected_status, expected_detail
):
    app = _raw_app()
    status, body = asyncio.run(
        _asgi_request(
            app,
            _headers(*headers),
            [AssertionError("rejected framing must not read the body")],
        )
    )
    assert (status, body) == (expected_status, {"detail": expected_detail})


def test_disconnect_is_sanitized_and_never_invokes_handler():
    handler = _Handler()
    app = _raw_app(handler)
    status, body = asyncio.run(
        _asgi_request(
            app,
            _headers((b"transfer-encoding", b"chunked")),
            [
                {"type": "http.request", "body": b"{", "more_body": True},
                {"type": "http.disconnect"},
            ],
        )
    )
    assert (status, body) == (
        408,
        {"detail": "request body was not completely received"},
    )
    assert handler.invocations == 0


def test_slow_trickle_is_cancelled_at_monotonic_ingress_deadline():
    handler = _Handler()
    receive_cancelled = False

    async def slow_chunk():
        nonlocal receive_cancelled
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            receive_cancelled = True
            raise
        return {"type": "http.request", "body": b"{}", "more_body": False}

    app = _raw_app(handler, timeout_seconds=0.01)
    status, body = asyncio.run(
        _asgi_request(
            app,
            _headers((b"transfer-encoding", b"chunked")),
            [slow_chunk],
        )
    )
    assert (status, body) == (
        408,
        {"detail": "request body exceeded its ingress deadline"},
    )
    assert receive_cancelled is True
    assert handler.invocations == 0


def test_ingress_cancellation_propagates_and_never_invokes_handler():
    handler = _Handler()
    app = _raw_app(handler)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            _asgi_request(
                app,
                _headers((b"transfer-encoding", b"chunked")),
                [asyncio.CancelledError()],
            )
        )
    assert handler.invocations == 0


def test_normal_bounded_request_reaches_handler_and_errors_are_static():
    handler = _Handler()
    payload = {
        "graph_execution_receipt_id": f"gxr-sha256:{'a' * 64}",
        "graph_execution_receipt_hash": "b" * 64,
    }
    response = _client(handler).post(
        "/tools/fabric_kg_report_coverage_readiness",
        headers={"Authorization": _VALID_AUTH},
        json=payload,
    )
    assert response.status_code == 502
    assert response.json() == {"detail": "tool execution failed"}
    assert "downstream" not in response.text
    assert "gxr-sha256" not in response.text
    assert handler.invocations == 1
