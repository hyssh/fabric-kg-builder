"""app/api.py — FastAPI reference application for the Fabric KG grounded agent.

Endpoints:
  GET  /health                  — liveness/readiness probe
  POST /chat                    — synchronous chat
  POST /stream                  — SSE streaming chat
  GET  /citations/{citation_id} — citation detail lookup
  POST /feedback                — thumbs-up/down feedback

Auth / Security:
  - Inbound: injectable InboundAuthVerifier (AllowAllVerifier for local dev,
    EntraAuthVerifier for production).
  - Outbound: ManagedIdentityAuthProvider for downstream service calls.
  - No embedded secrets anywhere.
  - Request body limited to MAX_BODY_BYTES.
  - Rate limiting via a simple in-memory token-bucket (injectable).
  - Request IDs assigned per-request.
  - Sensitive fields are redacted from logs.

This module defines the FastAPI ``app`` factory and exposes ``create_app()``.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncGenerator

from fabric_kg_builder.app.auth import AllowAllVerifier, AuthError, InboundAuthVerifier
from fabric_kg_builder.app.models import (
    ChatRequest,
    ChatResponse,
    CitationDetailResponse,
    CitationResponse,
    FeedbackRequest,
    FeedbackResponse,
    HealthResponse,
    StreamChunk,
    VisualSearchItemResponse,
    VisualSearchRequest,
    VisualSearchResponse,
)
from fabric_kg_builder.agent.instructions import ROUTE_SAFETY, ROUTE_UNSUPPORTED
from fabric_kg_builder.agent.tools.kb_tool import KnowledgeBaseError, KnowledgeBaseTool
from fabric_kg_builder.agent.tools.fabric_data import (
    FabricDataAgentAdapter,
    FabricDataError,
)
from fabric_kg_builder.knowledge.routing import classify_question
from fabric_kg_builder.app.visual_search import VisualSearchError, VisualSearchTool

try:
    from fastapi import FastAPI, HTTPException, Request, Depends  # type: ignore[import]
    from fastapi.responses import JSONResponse, Response, StreamingResponse  # type: ignore[import]
    from starlette.concurrency import run_in_threadpool  # type: ignore[import]
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False

MAX_BODY_BYTES = 64 * 1024  # 64 KB hard limit
_RATE_LIMIT_WINDOW_S = 60
_RATE_LIMIT_MAX_REQUESTS = 60
_READINESS_CACHE_SECONDS = 30.0


class DownstreamServiceError(RuntimeError):
    """Safe public error raised when a configured downstream call fails."""

    def __init__(
        self,
        message: str,
        *,
        execution_receipt: dict[str, Any] | None = None,
    ) -> None:
        self.execution_receipt = execution_receipt
        super().__init__(message)


class RateLimiter:
    """Simple in-memory token-bucket rate limiter.

    Injectable for tests — replace with a real distributed limiter in prod.
    """

    def __init__(self, max_requests: int = _RATE_LIMIT_MAX_REQUESTS, window_s: int = _RATE_LIMIT_WINDOW_S) -> None:
        self.max_requests = max_requests
        self.window_s = window_s
        self._buckets: dict[str, list[float]] = {}

    def is_allowed(self, key: str) -> bool:
        now = time.monotonic()
        window_start = now - self.window_s
        bucket = [t for t in self._buckets.get(key, []) if t > window_start]
        if len(bucket) >= self.max_requests:
            return False
        bucket.append(now)
        self._buckets[key] = bucket
        return True


def _readiness_passes(
    *,
    live_mode: bool,
    kb_ready: bool,
    visual_ready: bool,
    graph_required: bool,
    graph_ready: bool,
) -> bool:
    if not live_mode:
        return True
    return (
        kb_ready
        and visual_ready
        and (graph_ready if graph_required else True)
    )


def _redact_authorization(headers: dict[str, str]) -> dict[str, str]:
    """Return headers with Authorization value replaced by '[redacted]'."""
    return {k: ("[redacted]" if k.lower() == "authorization" else v) for k, v in headers.items()}


def _make_request_id() -> str:
    return "req_" + uuid.uuid4().hex[:12]


def create_app_from_env() -> "FastAPI":
    """Production factory: reads environment variables and builds a fail-closed app.

    This is the entry point for ``uvicorn --factory``.
    Raises ``AppConfigError`` at startup when auth config is missing in
    non-local-dev modes.

    To run locally without auth:
        FABRIC_KG_ENVIRONMENT=local FABRIC_KG_LOCAL_DEV=true uvicorn ...
    """
    from fabric_kg_builder.app.config import (
        AppConfigError,
        build_auth_verifier,
        build_runtime_dependencies,
        load_app_config,
    )

    try:
        config = load_app_config()
    except AppConfigError as exc:
        # Fail the process at startup — never silently fall back to AllowAll.
        raise RuntimeError(f"[STARTUP FAILURE] {exc}") from exc

    try:
        verifier = build_auth_verifier(config)
        kb_tool, visual_tool, graph_adapter = build_runtime_dependencies(config)
    except AppConfigError as exc:
        raise RuntimeError(f"[STARTUP FAILURE] {exc}") from exc
    return create_app(
        auth_verifier=verifier,
        kb_tool=kb_tool,
        visual_tool=visual_tool,
        graph_adapter=graph_adapter,
        environment=config.environment,
        version=config.version,
        require_downstreams=config.live_mode or config.local_live_mode,
    )


def create_app(
    *,
    auth_verifier: "InboundAuthVerifier | None" = None,
    kb_tool: "KnowledgeBaseTool | None" = None,
    visual_tool: "VisualSearchTool | None" = None,
    graph_adapter: "FabricDataAgentAdapter | None" = None,
    rate_limiter: "RateLimiter | None" = None,
    environment: str = "dev",
    version: str = "0.2.3",
    require_downstreams: bool = False,
    _allow_all_override: bool = False,
) -> "FastAPI":
    """Factory: create a configured FastAPI application.

    Injectable for testing.  When ``auth_verifier`` is None:
      - In test/offline mode (``_allow_all_override=True``): AllowAllVerifier.
      - Otherwise: defers to environment config via ``create_app_from_env()``.
        Use this only in tests; production must go through ``create_app_from_env``.

    Args:
        auth_verifier:       Inbound request verifier (injected for tests).
        kb_tool:             AI Search KB tool adapter.
        graph_adapter:       Fabric graph adapter.
        rate_limiter:        Rate limiter instance.
        environment:         Environment name echoed in /health.
        version:             API version echoed in /health.
        _allow_all_override: Allow AllowAllVerifier fallback (tests only).
    """
    if not _FASTAPI_AVAILABLE:
        raise ImportError(
            "fastapi is required. Install: pip install fastapi uvicorn"
        )

    if auth_verifier is None:
        if _allow_all_override:
            auth_verifier = AllowAllVerifier()
        else:
            # Production path — read config from environment.
            from fabric_kg_builder.app.config import (
                AppConfigError,
                build_auth_verifier,
                build_runtime_dependencies,
                load_app_config,
            )
            try:
                config = load_app_config()
                auth_verifier = build_auth_verifier(config)
                kb_tool, visual_tool, graph_adapter = build_runtime_dependencies(config)
                environment = config.environment
                version = config.version
                require_downstreams = config.live_mode or config.local_live_mode
            except AppConfigError as exc:
                raise RuntimeError(f"[STARTUP FAILURE] {exc}") from exc

    _verifier = auth_verifier
    _kb = kb_tool or KnowledgeBaseTool(index_name="", _client=None)
    _visual = visual_tool or VisualSearchTool(
        index_name="",
        blob_account_url="",
        blob_container="",
    )
    _graph = graph_adapter or FabricDataAgentAdapter(
        _client=None,
        schema_mode="schema1_compatibility",
    )
    _limiter = rate_limiter or RateLimiter()
    _live_mode = require_downstreams
    _graph_required = _graph.is_available
    if _live_mode and (not _kb.is_available or not _visual.is_available):
        raise RuntimeError(
            "[STARTUP FAILURE] Live mode requires configured Azure AI Search "
            "and visual asset clients."
        )
    _readiness_cache: dict[str, Any] = {
        "checked_at": float("-inf"),
        "kb_ready": not _live_mode,
        "visual_ready": not _live_mode,
        "graph_ready": not _live_mode,
    }

    app = FastAPI(
        title="Fabric KG Reference App",
        description="Grounded Q&A API backed by the knowledge graph and AI Search.",
        version=version,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    # ── Middleware: body size limit ─────────────────────────────────────────

    @app.middleware("http")
    async def _body_limit(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.method in ("POST", "PUT", "PATCH"):
            body = await request.body()
            if len(body) > MAX_BODY_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"detail": f"Request body exceeds {MAX_BODY_BYTES} bytes."},
                )
        return await call_next(request)

    # ── Auth dependency ─────────────────────────────────────────────────────

    def _require_auth(request: Request) -> dict[str, Any]:
        auth_header = request.headers.get("Authorization")
        try:
            return _verifier.verify(auth_header)
        except AuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc))

    # ── Rate limit dependency ───────────────────────────────────────────────

    def _check_rate_limit(request: Request) -> None:
        client_ip = request.client.host if request.client else "unknown"
        if not _limiter.is_allowed(client_ip):
            raise HTTPException(status_code=429, detail="Rate limit exceeded.")

    # ── GET /health ─────────────────────────────────────────────────────────

    def _health_response(*, probe: bool = False) -> HealthResponse:
        if probe and _live_mode:
            now = time.monotonic()
            if now - float(_readiness_cache["checked_at"]) >= _READINESS_CACHE_SECONDS:
                kb_ready = False
                visual_ready = False
                graph_ready = False
                try:
                    kb_ready = _kb.check_ready()
                except Exception:
                    kb_ready = False
                visual_ready = _visual.is_available
                try:
                    graph_ready = _graph.check_ready()
                except Exception:
                    graph_ready = False
                _readiness_cache.update(
                    checked_at=now,
                    kb_ready=kb_ready,
                    visual_ready=visual_ready,
                    graph_ready=graph_ready,
                )
        kb_ready = bool(_readiness_cache["kb_ready"])
        visual_ready = bool(_readiness_cache["visual_ready"])
        graph_ready = bool(_readiness_cache["graph_ready"])
        ready = _readiness_passes(
            live_mode=_live_mode,
            kb_ready=kb_ready,
            visual_ready=visual_ready,
            graph_required=_graph_required,
            graph_ready=graph_ready,
        )
        return HealthResponse(
            status="ok" if ready else "degraded",
            version=version,
            environment=environment,
            kb_available=_kb.is_available,
            visual_available=_visual.is_available,
            graph_available=_graph.is_available,
            ready=ready,
            live_mode=_live_mode,
            kb_status=(
                "ready" if kb_ready else ("configured" if _kb.is_available else "not_configured")
            ),
            visual_status=(
                "ready"
                if visual_ready
                else ("configured" if _visual.is_available else "not_configured")
            ),
            graph_status=(
                "ready"
                if graph_ready
                else ("configured" if _graph.is_available else "not_configured")
            ),
        )

    @app.get("/health", response_model=HealthResponse, tags=["ops"])
    async def health() -> HealthResponse:
        return _health_response()

    @app.get("/ready", response_model=HealthResponse, tags=["ops"])
    async def readiness() -> HealthResponse | JSONResponse:
        state = await run_in_threadpool(_health_response, probe=True)
        if not state.ready:
            return JSONResponse(status_code=503, content=state.model_dump())
        return state

    @app.get("/auth/ready", response_model=HealthResponse, tags=["ops"])
    async def authenticated_readiness(
        claims: dict = Depends(_require_auth),
    ) -> HealthResponse | JSONResponse:
        state = await run_in_threadpool(_health_response, probe=True)
        if not state.ready:
            return JSONResponse(status_code=503, content=state.model_dump())
        return state

    # ── Downstream execution helper ────────────────────────────────────────

    async def _run_answer(body: ChatRequest):
        receipt: dict[str, Any] = {}
        try:
            outcome = await run_in_threadpool(
                _answer_question,
                question=body.question,
                kb=_kb,
                graph=_graph,
                top_k=body.top_k,
                approved_plan_id=body.approved_plan_id,
                receipt_sink=receipt,
            )
            return (*outcome, receipt or None)
        except DownstreamServiceError as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "message": str(exc),
                    "execution_receipt": exc.execution_receipt,
                },
            ) from exc
        except (KnowledgeBaseError, FabricDataError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    # ── POST /chat ──────────────────────────────────────────────────────────

    @app.post("/chat", response_model=ChatResponse, tags=["chat"])
    async def chat(
        body: ChatRequest,
        request: Request,
        claims: dict = Depends(_require_auth),
        _rate: None = Depends(_check_rate_limit),
    ) -> ChatResponse:
        request_id = _make_request_id()
        t0 = time.monotonic()

        (
            answer,
            route_type,
            citations,
            refused,
            execution_receipt,
        ) = await _run_answer(body)

        latency_ms = int((time.monotonic() - t0) * 1000)
        citation_responses = (
            [CitationResponse(**c.to_safe_dict()) for c in citations]
            if body.include_citations
            else []
        )
        return ChatResponse(
            request_id=request_id,
            answer=answer,
            route_type=route_type,
            citations=citation_responses,
            refused=refused,
            latency_ms=latency_ms,
            execution_receipt=execution_receipt,
        )

    # ── POST /stream ─────────────────────────────────────────────────────────

    @app.post("/stream", tags=["chat"])
    async def stream_chat(
        body: ChatRequest,
        request: Request,
        claims: dict = Depends(_require_auth),
        _rate: None = Depends(_check_rate_limit),
    ) -> StreamingResponse:
        request_id = _make_request_id()
        try:
            (
                answer,
                route_type,
                citations,
                refused,
                execution_receipt,
            ) = await _run_answer(body)
        except HTTPException as exc:
            detail = exc.detail
            if isinstance(detail, dict):
                message = str(detail.get("message") or "Downstream failure.")
                execution_receipt = detail.get("execution_receipt")
            else:
                message = str(detail)
                execution_receipt = None

            async def _error_generator() -> AsyncGenerator[str, None]:
                error_chunk = StreamChunk(
                    type="error",
                    content=message,
                    request_id=request_id,
                    execution_receipt=execution_receipt,
                )
                yield f"data: {error_chunk.model_dump_json()}\n\n"

            return StreamingResponse(
                _error_generator(),
                media_type="text/event-stream",
                headers={
                    "X-Request-Id": request_id,
                    "Cache-Control": "no-cache",
                },
            )

        async def _event_generator() -> AsyncGenerator[str, None]:
            # Emit route activity
            route_chunk = StreamChunk(
                type="route",
                route_type=route_type,
                request_id=request_id,
            )
            yield f"data: {route_chunk.model_dump_json()}\n\n"

            # Stream answer in chunks (simulate streaming for offline mode)
            words = answer.split()
            buffer = []
            for word in words:
                buffer.append(word)
                if len(buffer) >= 5:
                    delta = StreamChunk(type="delta", content=" ".join(buffer) + " ")
                    yield f"data: {delta.model_dump_json()}\n\n"
                    buffer = []
            if buffer:
                delta = StreamChunk(type="delta", content=" ".join(buffer))
                yield f"data: {delta.model_dump_json()}\n\n"

            # Emit citations
            if body.include_citations:
                for cit in citations:
                    safe = cit.to_safe_dict()
                    citation_chunk = StreamChunk(
                        type="citation",
                        citation=CitationResponse(**safe),
                    )
                    yield f"data: {citation_chunk.model_dump_json()}\n\n"

            # Done
            done_chunk = StreamChunk(
                type="done",
                request_id=request_id,
                execution_receipt=execution_receipt,
            )
            yield f"data: {done_chunk.model_dump_json()}\n\n"

        return StreamingResponse(
            _event_generator(),
            media_type="text/event-stream",
            headers={
                "X-Request-Id": request_id,
                "Cache-Control": "no-cache",
            },
        )

    # ── GET /citations/{citation_id} ─────────────────────────────────────────

    @app.get("/citations/{citation_id}", response_model=CitationDetailResponse, tags=["citations"])
    async def citation_detail(
        citation_id: str,
        claims: dict = Depends(_require_auth),
    ) -> CitationDetailResponse:
        if _live_mode:
            raise HTTPException(
                status_code=501,
                detail=(
                    "Citation detail storage is not configured. "
                    "Use citations returned directly by /chat or /stream."
                ),
            )
        return CitationDetailResponse(
            citation_id=citation_id,
            source_type="search",
            source_id="",
            display_text="Citation detail not yet available in offline mode.",
            metadata={},
        )

    # ── POST /feedback ────────────────────────────────────────────────────────

    @app.post("/feedback", response_model=FeedbackResponse, tags=["feedback"])
    async def feedback(
        body: FeedbackRequest,
        claims: dict = Depends(_require_auth),
    ) -> FeedbackResponse:
        # In production, persist to storage.  For now, accept and acknowledge.
        return FeedbackResponse(accepted=True, request_id=body.request_id)

    # ── Visual asset search and protected image retrieval ──────────────────

    @app.post("/images/search", response_model=VisualSearchResponse, tags=["images"])
    async def search_images(
        body: VisualSearchRequest,
        claims: dict = Depends(_require_auth),
        _rate: None = Depends(_check_rate_limit),
    ) -> VisualSearchResponse:
        try:
            matches = await run_in_threadpool(
                _visual.search,
                body.query,
                top_k=body.top_k,
            )
        except VisualSearchError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return VisualSearchResponse(
            results=[
                VisualSearchItemResponse(
                    visual_id=item.visual_id,
                    image_id=item.image_id,
                    description=item.description,
                    source_path=item.source_path,
                    asset_type=item.asset_type,
                    score=item.score,
                    image_url=f"/images/{item.visual_id}",
                )
                for item in matches
            ]
        )

    @app.get("/images/{visual_id}", tags=["images"])
    async def get_image(
        visual_id: str,
        claims: dict = Depends(_require_auth),
    ):
        try:
            image_bytes, content_type = await run_in_threadpool(
                _visual.read_image,
                visual_id,
            )
        except VisualSearchError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(
            content=image_bytes,
            media_type=content_type,
            headers={"Cache-Control": "private, max-age=300"},
        )

    return app


def _answer_question(
    *,
    question: str,
    kb: KnowledgeBaseTool,
    graph: FabricDataAgentAdapter,
    top_k: int = 5,
    approved_plan_id: str | None = None,
    receipt_sink: dict[str, Any] | None = None,
) -> tuple[str, str, list, bool]:
    """Offline/grounded answer builder.

    Returns (answer, route_type, citations, refused).
    In production this would call the Foundry prompt-agent; here it demonstrates
    the routing logic deterministically without LLM calls.
    """
    from fabric_kg_builder.agent.citation import normalize_citations

    q_lower = question.lower()

    # Safety check
    safety_signals = (
        "ignore previous",
        "print your system prompt",
        "forget your instructions",
        "jailbreak",
        "bypass",
    )
    if any(s in q_lower for s in safety_signals):
        return (
            "I cannot comply with that request.",
            ROUTE_SAFETY,
            [],
            True,
        )

    raw_citations: list[dict] = []
    route_type = "search"
    graph_rows: list[dict[str, Any]] = []
    routing = classify_question(question)

    if approved_plan_id:
        graph_result = graph.execute_approved_plan(
            approved_plan_id,
            intent=question,
        )
        if receipt_sink is not None:
            receipt_sink.update(graph_result.execution_receipt)
        if graph_result.status == "error":
            raise DownstreamServiceError(
                "Fabric GraphModel approved-plan execution failed.",
                execution_receipt=graph_result.execution_receipt,
            )
        if graph_result.status == "unsupported":
            return (
                graph_result.error_message
                or "The requested approved Graph plan is unavailable.",
                ROUTE_UNSUPPORTED,
                [],
                True,
            )
        graph_rows = graph_result.rows
        raw_citations.extend(graph_result.to_citation_dicts())
        citations = normalize_citations(raw_citations)
        if not graph_rows:
            return (
                f"Approved plan `{approved_plan_id}` returned no verified rows.",
                "ontology",
                citations,
                False,
            )
        rendered_rows = [
            ", ".join(
                f"{key}={value}"
                for key, value in sorted(row.items())
                if value is not None and value != ""
            )
            for row in graph_rows[:5]
        ]
        return (
            f"Approved plan `{approved_plan_id}` returned "
            f"{len(graph_rows)} verified row(s): "
            + " | ".join(rendered_rows),
            "ontology",
            citations,
            False,
        )

    if (
        graph.schema_mode == "schema2_bounded"
        and bool(routing.graph_signals)
    ):
        if graph.is_unsupported_query_type(q_lower):
            return (
                "The requested Graph path is not supported by the approved "
                "bounded Graph authority.",
                ROUTE_UNSUPPORTED,
                [],
                True,
            )
        return (
            "No approved bounded Graph plan is mapped to this graph-intent "
            "request. Supply a sealed approved_plan_id or decompose the "
            "question into approved bounded subquestions.",
            ROUTE_UNSUPPORTED,
            [],
            True,
        )

    if (
        graph.schema_mode == "schema1_compatibility"
        and bool(routing.graph_signals)
        and graph.is_unsupported_query_type(q_lower)
    ):
        return (
            "This Graph query type is not supported.",
            ROUTE_UNSUPPORTED,
            [],
            True,
        )

    kb_results = kb.retrieve(question, top_k=top_k)
    for r in kb_results:
        raw_citations.append(r.to_citation_dict())

    if routing.graph_signals:
        route_type = "ontology"
        graph_result = graph.query_keyword(_graph_keyword(question))
        if graph_result.status == "error":
            raise DownstreamServiceError(
                "Fabric GraphModel query failed."
            )
        graph_rows = graph_result.rows
        for c in graph_result.to_citation_dicts():
            raw_citations.append(c)

    if kb_results and route_type == "ontology":
        route_type = "mixed"

    answer = _compose_offline_answer(question, kb_results, graph_rows=graph_rows)
    citations = normalize_citations(raw_citations)
    refused = route_type in (ROUTE_SAFETY, ROUTE_UNSUPPORTED)

    return answer, route_type, citations, refused


def _graph_keyword(question: str) -> str:
    """Choose a useful, domain-neutral graph search term from a question."""
    import re

    tokens = re.findall(r"[\w.-]+", question, flags=re.UNICODE)
    stop_words = {
        "about", "are", "connected", "directly", "entities", "graph", "hierarchy",
        "how", "in", "is", "knowledge", "related", "relationship", "the", "to",
        "what", "which", "with",
    }
    candidates = [token for token in tokens if token.lower() not in stop_words]
    structured = [
        token
        for token in candidates
        if any(character.isdigit() for character in token)
        or "-" in token
        or "_" in token
    ]
    pool = structured or candidates or tokens
    return max(pool, key=len) if pool else question


def _compose_offline_answer(
    question: str,
    kb_results: list,
    *,
    graph_rows: list[dict[str, Any]] | None = None,
) -> str:
    """Compose a deterministic grounded answer from retrieved data."""
    graph_rows = graph_rows or []
    if graph_rows and not kb_results:
        labels = [
            str(row.get("display_name") or row.get("entity_id") or "")
            for row in graph_rows[:5]
        ]
        labels = [label for label in labels if label]
        suffix = f": {', '.join(labels)}" if labels else ""
        return f"The knowledge graph returned {len(graph_rows)} matching entities{suffix}."
    if not kb_results:
        return (
            "I found no direct matches in the knowledge base for your question. "
            "Please refine your query or check that the relevant data has been ingested."
        )
    top = kb_results[0]
    if graph_rows:
        return (
            f"Based on the knowledge base: {top.text[:300]} "
            f"The graph also returned {len(graph_rows)} related entities."
        )
    return f"Based on the knowledge base: {top.text[:300]}"
