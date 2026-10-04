"""app/models.py — Pydantic request/response models for the FastAPI app.

These models form the OpenAPI contract between the front-end and the API.
No secrets are ever serialized in these models.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    """POST /chat and POST /stream request body."""

    question: str = Field(..., min_length=1, max_length=2000, description="User question.")
    session_id: str = Field(default="", max_length=128, description="Optional session correlation ID.")
    top_k: int = Field(default=5, ge=1, le=20, description="Max KB results per query.")
    include_citations: bool = Field(default=True)
    include_lineage: bool = Field(default=False)

    @field_validator("question")
    @classmethod
    def question_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("question must not be blank.")
        return v.strip()


class FeedbackRequest(BaseModel):
    """POST /feedback request body."""

    request_id: str = Field(..., min_length=1, max_length=128)
    thumbs: str = Field(..., description="'up' or 'down'")
    comment: str = Field(default="", max_length=1000)

    @field_validator("thumbs")
    @classmethod
    def valid_thumbs(cls, v: str) -> str:
        if v not in ("up", "down"):
            raise ValueError("thumbs must be 'up' or 'down'.")
        return v


class VisualSearchRequest(BaseModel):
    """POST /images/search request body."""

    query: str = Field(..., min_length=1, max_length=500)
    top_k: int = Field(default=8, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def query_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("query must not be blank.")
        return v.strip()


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class CitationResponse(BaseModel):
    """A single citation included in an answer."""

    source_type: str
    source_id: str = ""
    chunk_id: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    display_text: str = ""
    score: float | None = None


class ChatResponse(BaseModel):
    """Response from POST /chat."""

    model_config = {"json_schema_extra": {
        "example": {
            "request_id": "req_abc123",
            "answer": "The requested entity information is available in the knowledge base.",
            "route_type": "search",
            "citations": [
                {
                    "source_type": "search",
                    "source_id": "kg-dev-kg-chunks",
                    "chunk_id": "chunk_001",
                    "display_text": "Entity identifier: ITEM-0042, category: Assembly.",
                    "score": 0.92,
                }
            ],
            "refused": False,
            "latency_ms": 450,
        }
    }}

    request_id: str = Field(description="Unique request identifier for correlation.")
    answer: str
    route_type: str = Field(description="search | ontology | mixed | unsupported | safety")
    citations: list[CitationResponse] = Field(default_factory=list)
    refused: bool = False
    latency_ms: int | None = None


class StreamChunk(BaseModel):
    """One SSE chunk from GET /stream."""

    type: str = Field(description="'delta' | 'citation' | 'route' | 'done' | 'error'")
    content: str = Field(default="")
    citation: CitationResponse | None = None
    route_type: str | None = None
    request_id: str | None = None


class HealthResponse(BaseModel):
    """GET /health response."""

    status: str = "ok"
    version: str = ""
    environment: str = ""
    kb_available: bool = False
    visual_available: bool = False
    graph_available: bool = False
    ready: bool = False
    live_mode: bool = False
    kb_status: str = "not_configured"
    visual_status: str = "not_configured"
    graph_status: str = "not_configured"


class CitationDetailResponse(BaseModel):
    """GET /citations/{citation_id} response."""

    citation_id: str
    source_type: str
    source_id: str
    display_text: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class FeedbackResponse(BaseModel):
    """POST /feedback response."""

    accepted: bool = True
    request_id: str = ""


class VisualSearchItemResponse(BaseModel):
    """A protected visual-asset reference returned to the UI."""

    visual_id: str
    image_id: str
    description: str = ""
    source_path: str = ""
    asset_type: str = ""
    score: float = 0.0
    image_url: str


class VisualSearchResponse(BaseModel):
    """Response from POST /images/search."""

    results: list[VisualSearchItemResponse] = Field(default_factory=list)
