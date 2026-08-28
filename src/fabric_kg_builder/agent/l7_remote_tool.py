"""Strict FastAPI host for canonical L6 evidence tools."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from fabric_kg_builder.agent.l6_integration import (
    L6CitationPresentationCollection,
    L6CitationToolInput,
    L6EvidenceToolInput,
    L6EvidenceToolOutput,
    L6GraphToolInput,
    L6GraphToolOutput,
    L6ReadinessReport,
    L6ReadinessToolInput,
    L6ResolvedScopes,
    L6ScopeResolutionInput,
    L6_TOOL_ASSEMBLE_CITATIONS,
    L6_TOOL_EXECUTE_GRAPH,
    L6_TOOL_REPORT_READINESS,
    L6_TOOL_RESOLVE_SCOPE,
    L6_TOOL_RETRIEVE_EVIDENCE,
)
from fabric_kg_builder.agent.l7_deployment import L7RemoteReadinessObservation
from fabric_kg_builder.app.auth import AuthError, InboundAuthVerifier
from fabric_kg_builder.contracts.base import canonical_sha256


class L6RemoteToolHandler(Protocol):
    """Runtime boundary that executes typed L6 tools without synthesis."""

    def ready(self) -> bool: ...

    async def invoke(
        self,
        tool_name: str,
        request: Any,
        *,
        deadline_monotonic: float,
    ) -> Any: ...


@dataclass(frozen=True)
class L6ReadinessAuthority:
    """Current identity of the durable L6 authority used by this host."""

    l6_definition_hash: str
    backend_kind: str
    backend_version: str


class L6ReadinessAuthorityProvider(Protocol):
    """Reads the durable L6 authority identity without mutating it."""

    def observe(self) -> L6ReadinessAuthority | None: ...


_TOOL_MODELS = (
    (
        L6_TOOL_RESOLVE_SCOPE,
        L6ScopeResolutionInput,
        L6ResolvedScopes,
    ),
    (
        L6_TOOL_EXECUTE_GRAPH,
        L6GraphToolInput,
        L6GraphToolOutput,
    ),
    (
        L6_TOOL_RETRIEVE_EVIDENCE,
        L6EvidenceToolInput,
        L6EvidenceToolOutput,
    ),
    (
        L6_TOOL_ASSEMBLE_CITATIONS,
        L6CitationToolInput,
        L6CitationPresentationCollection,
    ),
    (
        L6_TOOL_REPORT_READINESS,
        L6ReadinessToolInput,
        L6ReadinessReport,
    ),
)


def build_l6_openapi_spec(
    *,
    endpoint: str | None = None,
    max_body_bytes: int = 1_048_576,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Build the exact OpenAPI document used by both host and Foundry adapter."""
    components: dict[str, Any] = {}
    paths: dict[str, Any] = {}
    for tool_name, input_model, output_model in _TOOL_MODELS:
        for model in (input_model, output_model):
            schema = model.model_json_schema(
                ref_template="#/components/schemas/{model}"
            )
            components.update(schema.pop("$defs", {}))
            components[model.__name__] = schema
        paths[f"/tools/{tool_name}"] = {
            "post": {
                "operationId": tool_name,
                "summary": tool_name,
                "security": [{"entraBearer": []}],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "$ref": (
                                    f"#/components/schemas/{input_model.__name__}"
                                )
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Canonical L6 tool output",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": (
                                        "#/components/schemas/"
                                        f"{output_model.__name__}"
                                    )
                                }
                            }
                        },
                    }
                },
            }
        }
    spec: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {
            "title": "Fabric KG L6 RemoteTool",
            "version": "0.2.3",
            "description": "Canonical evidence-only L6 tools; zero synthesis.",
        },
        "paths": paths,
        "components": {
            "schemas": components,
            "securitySchemes": {
                "entraBearer": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "JWT",
                }
            },
        },
        "x-fabric-kg-definition": {
            "toolset": "canonical-l6",
            "zeroSynthesis": True,
            "maxRequestBytes": max_body_bytes,
            "deadlineSeconds": timeout_seconds,
        },
    }
    if endpoint:
        spec["servers"] = [{"url": endpoint.rstrip("/")}]
    return spec


def create_l6_remote_tool_app(
    *,
    handler: L6RemoteToolHandler,
    auth_verifier: InboundAuthVerifier,
    max_body_bytes: int = 1_048_576,
    timeout_seconds: float = 30.0,
    external_endpoint: str | None = None,
    readiness_authority_provider: L6ReadinessAuthorityProvider | None = None,
    expected_tenant_id: str | None = None,
    expected_audience: str | None = None,
    allowed_caller_object_ids: tuple[str, ...] = (),
    required_app_role: str | None = None,
    expected_l6_definition_hash: str | None = None,
    expected_authority_backend: str | None = None,
    expected_authority_version: str | None = None,
    readiness_ttl_seconds: float = 30.0,
) -> Any:
    """Create a fail-closed RemoteTool host with exact L6 OpenAPI schemas."""
    try:
        from fastapi import Depends, FastAPI, HTTPException, Request
        from fastapi.exceptions import RequestValidationError
        from fastapi.responses import JSONResponse
        from starlette.requests import ClientDisconnect
    except ImportError as exc:
        raise ImportError(
            "fastapi is required for serve-l6; install fabric-kg-builder[app]"
        ) from exc
    if max_body_bytes < 1024:
        raise ValueError("max_body_bytes must be at least 1024")
    if timeout_seconds <= 0 or timeout_seconds > 300:
        raise ValueError("timeout_seconds must be in (0, 300]")
    if readiness_ttl_seconds <= 0 or readiness_ttl_seconds > 300:
        raise ValueError("readiness_ttl_seconds must be in (0, 300]")

    app = FastAPI(
        title="Fabric KG L6 RemoteTool",
        description=(
            "Canonical evidence-only L6 tools. This host performs no synthesis."
        ),
        version="0.2.3",
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    def _error(status_code: int, detail: str) -> JSONResponse:
        return JSONResponse(status_code=status_code, content={"detail": detail})

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        _request: Request,
        _exc: RequestValidationError,
    ) -> JSONResponse:
        return _error(422, "request failed canonical validation")

    @app.middleware("http")
    async def _secure_tool_ingress(  # type: ignore[no-untyped-def]
        request: Request,
        call_next,
    ):
        if request.method != "POST" or not request.url.path.startswith("/tools/"):
            return await call_next(request)

        # Authorization is deliberately completed before consuming attacker input.
        try:
            claims = auth_verifier.verify(request.headers.get("Authorization"))
        except AuthError as exc:
            status_code = exc.status_code if exc.status_code in {401, 403} else 401
            return _error(status_code, "request authorization failed")
        except Exception:
            return _error(401, "request authorization failed")
        if not isinstance(claims, dict):
            return _error(401, "request authorization failed")
        request.state.auth_claims = claims

        raw_headers = request.scope.get("headers", ())
        content_lengths = [
            value.decode("latin-1")
            for name, value in raw_headers
            if name.lower() == b"content-length"
        ]
        transfer_encodings = [
            value.decode("latin-1")
            for name, value in raw_headers
            if name.lower() == b"transfer-encoding"
        ]
        content_encodings = [
            value.decode("latin-1")
            for name, value in raw_headers
            if name.lower() == b"content-encoding"
        ]

        if len(content_encodings) > 1:
            return _error(415, "unsupported content encoding")
        if content_encodings and content_encodings[0].lower() != "identity":
            return _error(415, "unsupported content encoding")

        transfer_tokens = [
            token.strip().lower()
            for value in transfer_encodings
            for token in value.split(",")
        ]
        if transfer_encodings and transfer_tokens != ["chunked"]:
            return _error(400, "ambiguous transfer encoding")
        if transfer_encodings and content_lengths:
            return _error(400, "ambiguous request framing")

        declared_length: int | None = None
        if content_lengths:
            length_tokens = [
                token.strip()
                for value in content_lengths
                for token in value.split(",")
            ]
            if (
                not length_tokens
                or any(not token.isascii() or not token.isdecimal() for token in length_tokens)
            ):
                return _error(400, "invalid content-length header")
            normalized_lengths = {token.lstrip("0") or "0" for token in length_tokens}
            if len(normalized_lengths) != 1:
                return _error(400, "invalid content-length header")
            normalized_length = normalized_lengths.pop()
            maximum = str(max_body_bytes)
            if len(normalized_length) > len(maximum) or (
                len(normalized_length) == len(maximum)
                and normalized_length > maximum
            ):
                return _error(413, "request body exceeds configured limit")
            declared_length = int(normalized_length)
        elif not transfer_encodings:
            return _error(411, "content-length or chunked transfer encoding required")

        body = bytearray()
        ingress_deadline = time.monotonic() + timeout_seconds
        stream = request.stream().__aiter__()
        try:
            while True:
                remaining = ingress_deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                try:
                    chunk = await asyncio.wait_for(
                        stream.__anext__(),
                        timeout=remaining,
                    )
                except StopAsyncIteration:
                    break
                if len(body) + len(chunk) > max_body_bytes:
                    return _error(413, "request body exceeds configured limit")
                if (
                    declared_length is not None
                    and len(body) + len(chunk) > declared_length
                ):
                    return _error(
                        400,
                        "request body length does not match content-length",
                    )
                body.extend(chunk)
        except asyncio.TimeoutError:
            return _error(408, "request body exceeded its ingress deadline")
        except ClientDisconnect:
            return _error(408, "request body was not completely received")
        if declared_length is not None and len(body) != declared_length:
            return _error(400, "request body length does not match content-length")

        request.scope["_l6_bounded_body"] = bytes(body)
        return await call_next(request)

    def _authorize(request: Request) -> tuple[dict[str, Any], bytes]:
        claims = getattr(request.state, "auth_claims", None)
        if not isinstance(claims, dict):
            raise HTTPException(
                status_code=401,
                detail="request authorization failed",
            )
        return claims, request.scope["_l6_bounded_body"]

    # Request is imported lazily, so resolve its postponed local annotation.
    _authorize.__annotations__["request"] = Request

    def _authenticate_readiness(request: Request) -> dict[str, Any]:
        try:
            claims = auth_verifier.verify(request.headers.get("Authorization"))
        except AuthError as exc:
            status_code = exc.status_code if exc.status_code in {401, 403} else 401
            raise HTTPException(
                status_code=status_code,
                detail="request authorization failed",
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=401,
                detail="request authorization failed",
            ) from exc
        if not isinstance(claims, dict):
            raise HTTPException(
                status_code=401,
                detail="request authorization failed",
            )
        return claims

    _authenticate_readiness.__annotations__["request"] = Request

    def _parse(body: bytes, input_model: type[Any]) -> Any:
        try:
            return input_model.model_validate_json(body)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail="request failed canonical validation",
            ) from exc

    async def _invoke(
        tool_name: str,
        value: Any,
        output_model: type[Any],
    ) -> Any:
        try:
            result = await asyncio.wait_for(
                handler.invoke(
                    tool_name,
                    value,
                    deadline_monotonic=time.monotonic() + timeout_seconds,
                ),
                timeout=timeout_seconds,
            )
            return output_model.model_validate(result)
        except asyncio.TimeoutError as exc:
            raise HTTPException(
                status_code=504,
                detail="tool execution exceeded its deadline",
            ) from exc
        except HTTPException:
            raise
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail="tool request or response failed canonical validation",
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail="tool execution failed",
            ) from exc

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(
        "/ready",
        include_in_schema=False,
        response_model=L7RemoteReadinessObservation,
    )
    def ready(
        claims: dict[str, Any] = Depends(_authenticate_readiness),
    ) -> L7RemoteReadinessObservation:
        unavailable = HTTPException(
            status_code=503,
            detail="L6 authorities are not ready",
        )
        required_text = (
            external_endpoint,
            expected_tenant_id,
            expected_audience,
            required_app_role,
            expected_l6_definition_hash,
            expected_authority_backend,
            expected_authority_version,
        )
        if (
            readiness_authority_provider is None
            or any(not isinstance(value, str) or not value for value in required_text)
            or not allowed_caller_object_ids
            or any(
                not isinstance(value, str) or not value
                for value in allowed_caller_object_ids
            )
        ):
            raise unavailable
        try:
            authority = readiness_authority_provider.observe()
            is_ready = bool(handler.ready())
        except Exception as exc:
            raise unavailable from exc
        if not isinstance(authority, L6ReadinessAuthority) or not is_ready:
            raise unavailable
        if (
            authority.l6_definition_hash != expected_l6_definition_hash
            or authority.backend_kind != expected_authority_backend
            or authority.backend_version != expected_authority_version
        ):
            raise unavailable

        tenant_id = claims.get("tid")
        audience = claims.get("aud")
        caller_object_id = claims.get("oid")
        roles = claims.get("roles")
        if (
            tenant_id != expected_tenant_id
            or audience != expected_audience
            or not isinstance(caller_object_id, str)
            or caller_object_id not in allowed_caller_object_ids
            or not isinstance(roles, (list, tuple))
            or required_app_role not in roles
        ):
            raise unavailable

        checked_at = datetime.now(timezone.utc)
        values = {
            "endpoint": external_endpoint.rstrip("/"),
            "tenant_id": tenant_id,
            "audience": audience,
            "caller_object_id": caller_object_id,
            "app_role": required_app_role,
            "openapi_schema_hash": canonical_sha256(
                build_l6_openapi_spec(
                    endpoint=external_endpoint,
                    max_body_bytes=max_body_bytes,
                    timeout_seconds=timeout_seconds,
                )
            ),
            "l6_definition_hash": authority.l6_definition_hash,
            "authority_backend": authority.backend_kind,
            "authority_version": authority.backend_version,
            "checked_at": checked_at,
            "expires_at": checked_at + timedelta(seconds=readiness_ttl_seconds),
        }
        values["readiness_hash"] = canonical_sha256(values)
        try:
            return L7RemoteReadinessObservation.model_validate(values)
        except (TypeError, ValueError) as exc:
            raise unavailable from exc

    @app.post(
        f"/tools/{L6_TOOL_RESOLVE_SCOPE}",
        operation_id=L6_TOOL_RESOLVE_SCOPE,
        response_model=L6ResolvedScopes,
    )
    async def resolve_scope(
        authorized: tuple[dict[str, Any], bytes] = Depends(_authorize),
    ) -> L6ResolvedScopes:
        value = _parse(authorized[1], L6ScopeResolutionInput)
        return await _invoke(L6_TOOL_RESOLVE_SCOPE, value, L6ResolvedScopes)

    @app.post(
        f"/tools/{L6_TOOL_EXECUTE_GRAPH}",
        operation_id=L6_TOOL_EXECUTE_GRAPH,
        response_model=L6GraphToolOutput,
    )
    async def execute_graph(
        authorized: tuple[dict[str, Any], bytes] = Depends(_authorize),
    ) -> L6GraphToolOutput:
        value = _parse(authorized[1], L6GraphToolInput)
        return await _invoke(L6_TOOL_EXECUTE_GRAPH, value, L6GraphToolOutput)

    @app.post(
        f"/tools/{L6_TOOL_RETRIEVE_EVIDENCE}",
        operation_id=L6_TOOL_RETRIEVE_EVIDENCE,
        response_model=L6EvidenceToolOutput,
    )
    async def retrieve_evidence(
        authorized: tuple[dict[str, Any], bytes] = Depends(_authorize),
    ) -> L6EvidenceToolOutput:
        value = _parse(authorized[1], L6EvidenceToolInput)
        return await _invoke(
            L6_TOOL_RETRIEVE_EVIDENCE,
            value,
            L6EvidenceToolOutput,
        )

    @app.post(
        f"/tools/{L6_TOOL_ASSEMBLE_CITATIONS}",
        operation_id=L6_TOOL_ASSEMBLE_CITATIONS,
        response_model=L6CitationPresentationCollection,
    )
    async def assemble_citations(
        authorized: tuple[dict[str, Any], bytes] = Depends(_authorize),
    ) -> L6CitationPresentationCollection:
        value = _parse(authorized[1], L6CitationToolInput)
        return await _invoke(
            L6_TOOL_ASSEMBLE_CITATIONS,
            value,
            L6CitationPresentationCollection,
        )

    @app.post(
        f"/tools/{L6_TOOL_REPORT_READINESS}",
        operation_id=L6_TOOL_REPORT_READINESS,
        response_model=L6ReadinessReport,
    )
    async def report_readiness(
        authorized: tuple[dict[str, Any], bytes] = Depends(_authorize),
    ) -> L6ReadinessReport:
        value = _parse(authorized[1], L6ReadinessToolInput)
        return await _invoke(
            L6_TOOL_REPORT_READINESS,
            value,
            L6ReadinessReport,
        )

    def _openapi() -> dict[str, Any]:
        return build_l6_openapi_spec(
            endpoint=external_endpoint,
            max_body_bytes=max_body_bytes,
            timeout_seconds=timeout_seconds,
        )

    app.openapi = _openapi
    return app
