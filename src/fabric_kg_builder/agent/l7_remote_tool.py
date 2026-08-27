"""Strict FastAPI host for canonical L6 evidence tools."""

from __future__ import annotations

import asyncio
import time
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
from fabric_kg_builder.app.auth import AuthError, InboundAuthVerifier


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
) -> Any:
    """Create a fail-closed RemoteTool host with exact L6 OpenAPI schemas."""
    try:
        from fastapi import Depends, FastAPI, HTTPException, Request
        from fastapi.responses import JSONResponse
    except ImportError as exc:
        raise ImportError(
            "fastapi is required for serve-l6; install fabric-kg-builder[app]"
        ) from exc
    if max_body_bytes < 1024:
        raise ValueError("max_body_bytes must be at least 1024")
    if timeout_seconds <= 0 or timeout_seconds > 300:
        raise ValueError("timeout_seconds must be in (0, 300]")

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

    @app.middleware("http")
    async def _limit_body(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.method in {"POST", "PUT", "PATCH"}:
            declared = request.headers.get("content-length")
            if declared:
                try:
                    if int(declared) > max_body_bytes:
                        return JSONResponse(
                            status_code=413,
                            content={"detail": "request body exceeds configured limit"},
                        )
                except ValueError:
                    return JSONResponse(
                        status_code=400,
                        content={"detail": "invalid content-length header"},
                    )
            body = await request.body()
            if len(body) > max_body_bytes:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "request body exceeds configured limit"},
                )
        return await call_next(request)

    def _authorize(request: Request) -> dict[str, Any]:
        try:
            claims = auth_verifier.verify(request.headers.get("Authorization"))
        except AuthError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail="request authorization failed",
            ) from exc
        if not isinstance(claims, dict):
            raise HTTPException(
                status_code=401,
                detail="request authorization failed",
            )
        return claims

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

    @app.get("/ready", include_in_schema=False)
    def ready() -> dict[str, Any]:
        is_ready = bool(handler.ready())
        if not is_ready:
            raise HTTPException(status_code=503, detail="L6 authorities are not ready")
        return {"status": "ready", "zero_synthesis": True}

    @app.post(
        f"/tools/{L6_TOOL_RESOLVE_SCOPE}",
        operation_id=L6_TOOL_RESOLVE_SCOPE,
        response_model=L6ResolvedScopes,
    )
    async def resolve_scope(
        value: L6ScopeResolutionInput,
        _claims: dict[str, Any] = Depends(_authorize),
    ) -> L6ResolvedScopes:
        return await _invoke(L6_TOOL_RESOLVE_SCOPE, value, L6ResolvedScopes)

    @app.post(
        f"/tools/{L6_TOOL_EXECUTE_GRAPH}",
        operation_id=L6_TOOL_EXECUTE_GRAPH,
        response_model=L6GraphToolOutput,
    )
    async def execute_graph(
        value: L6GraphToolInput,
        _claims: dict[str, Any] = Depends(_authorize),
    ) -> L6GraphToolOutput:
        return await _invoke(L6_TOOL_EXECUTE_GRAPH, value, L6GraphToolOutput)

    @app.post(
        f"/tools/{L6_TOOL_RETRIEVE_EVIDENCE}",
        operation_id=L6_TOOL_RETRIEVE_EVIDENCE,
        response_model=L6EvidenceToolOutput,
    )
    async def retrieve_evidence(
        value: L6EvidenceToolInput,
        _claims: dict[str, Any] = Depends(_authorize),
    ) -> L6EvidenceToolOutput:
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
        value: L6CitationToolInput,
        _claims: dict[str, Any] = Depends(_authorize),
    ) -> L6CitationPresentationCollection:
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
        value: L6ReadinessToolInput,
        _claims: dict[str, Any] = Depends(_authorize),
    ) -> L6ReadinessReport:
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
