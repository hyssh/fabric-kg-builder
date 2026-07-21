"""agent/citation.py — normalized citation model for M8 grounded responses.

A Citation captures the provenance of one piece of evidence used in the
agent's answer.  Two source types exist:
  - "search"   — a chunk from AI Search (document-grounded)
  - "ontology" — an entity or relationship from the graph

Secrets and raw credentials are NEVER included in citations.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class CitationSource(str, Enum):
    SEARCH = "search"
    ONTOLOGY = "ontology"
    MIXED = "mixed"


class Citation(BaseModel):
    """A single grounded citation backing part of the agent's answer.

    Fields
    ------
    source_type:
        Where the evidence was retrieved from.
    source_id:
        For search: the AI Search index name or document ID.
        For ontology: the ontology/graph name.
    chunk_id:
        For search: the chunk identifier.  None for ontology.
    entity_type:
        For ontology: the entity type (e.g. "Component").  None for search.
    entity_id:
        For ontology: the entity ID (e.g. "ent_abc123").  None for search.
    display_text:
        Human-readable excerpt shown in the UI (max 500 chars, no raw secrets).
    score:
        Retrieval relevance score [0, 1] if available.
    metadata:
        Optional non-secret provenance metadata (e.g. page number, section).
        Must not contain connection strings, keys, or tokens.
    """

    source_type: CitationSource
    source_id: str = ""
    chunk_id: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    display_text: str = ""
    score: float | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_no_secrets(self) -> "Citation":
        """Reject citations containing obvious secret patterns."""
        _check_no_secrets(self.source_id, "source_id")
        _check_no_secrets(self.display_text, "display_text")
        for key, val in self.metadata.items():
            _check_no_secrets(val, f"metadata.{key}")
        return self

    def to_safe_dict(self) -> dict[str, Any]:
        """Return a dict safe to serialize in API responses."""
        return {k: v for k, v in self.model_dump().items() if v is not None}


_SECRET_PATTERNS = (
    "AccountKey=",
    "SharedAccessSignature",
    "api-key",
    "password=",
    "secret=",
    "token=",
    "-----BEGIN",
    "client_secret",
)


def _check_no_secrets(value: str, field: str) -> None:
    """Raise ValueError if *value* looks like it contains a credential."""
    lowered = value.lower()
    for pattern in _SECRET_PATTERNS:
        if pattern.lower() in lowered:
            raise ValueError(
                f"Citation field '{field}' appears to contain a secret "
                f"(pattern '{pattern}' detected). Redact before including in citations."
            )


def normalize_citations(raw: list[dict[str, Any]]) -> list[Citation]:
    """Convert raw dicts (from search/graph results) to validated Citation objects.

    Filters out any entries that fail secret validation rather than raising,
    so a partial citation list is always returned.
    """
    citations: list[Citation] = []
    for item in raw:
        try:
            citations.append(Citation.model_validate(item))
        except Exception:
            # Skip malformed or secret-containing citations.
            pass
    return citations
