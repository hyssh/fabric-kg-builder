"""agent/tools/kb_tool.py — Knowledge-Base tool adapter.

Wraps the AI Search index so the grounded agent can retrieve relevant
document chunks for search-route queries.

No network calls are made when a ``_client`` is injected; the adapter is
fully testable offline.

Secrets (connection strings, API keys) are NEVER stored in instances of
this class.  Auth is always via an injected credentials object (managed
identity or a mock for tests).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class KnowledgeBaseError(RuntimeError):
    """Raised when configured knowledge-base retrieval fails."""


@dataclass(frozen=True)
class KBResult:
    """A single retrieved chunk from the knowledge base."""

    chunk_id: str
    source_id: str
    text: str
    score: float = 0.0
    metadata: dict[str, str] = field(default_factory=dict)

    def to_citation_dict(self) -> dict[str, Any]:
        """Return a dict suitable for passing to Citation.model_validate()."""
        return {
            "source_type": "search",
            "source_id": self.source_id,
            "chunk_id": self.chunk_id,
            "display_text": self.text[:500],
            "score": self.score,
            "metadata": {k: str(v) for k, v in self.metadata.items()},
        }


@runtime_checkable
class SearchClientProtocol(Protocol):
    """Minimal protocol for AI Search clients (real or mock)."""

    def search(
        self,
        search_text: str,
        *,
        top: int = 5,
        select: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        ...


class KnowledgeBaseTool:
    """Knowledge-base retrieval adapter backed by Azure AI Search.

    Parameters
    ----------
    index_name:
        The AI Search index to query.
    _client:
        Injected search client.  If None, the tool works in "no-op" mode
        and returns an empty result list (useful for tests that do not
        exercise retrieval).
    top_k:
        Maximum number of results to return.
    """

    def __init__(
        self,
        index_name: str,
        *,
        _client: SearchClientProtocol | None = None,
        top_k: int = 5,
        fail_on_error: bool = False,
    ) -> None:
        self.index_name = index_name
        self._client = _client
        self.top_k = top_k
        self.fail_on_error = fail_on_error

    def retrieve(self, query: str, *, top_k: int | None = None) -> list[KBResult]:
        """Retrieve the top-k chunks relevant to *query*.

        Returns an empty list when no client is injected (offline/test mode).
        """
        if self._client is None:
            return []
        try:
            raw_results = self._client.search(
                query,
                top=top_k or self.top_k,
                select=["chunk_id", "content", "source_path", "asset_id"],
            )
            results: list[KBResult] = []
            for item in raw_results:
                # Handle both dict-like and attribute-access result objects.
                if isinstance(item, dict):
                    chunk_id = item.get("chunk_id", "")
                    source_id = (
                        item.get("source_id")
                        or item.get("source_path")
                        or item.get("asset_id")
                        or self.index_name
                    )
                    text = item.get("content", "")
                    score = float(item.get("@search.score", item.get("score", 0.0)))
                else:
                    chunk_id = getattr(item, "chunk_id", "")
                    source_id = (
                        getattr(item, "source_id", "")
                        or getattr(item, "source_path", "")
                        or getattr(item, "asset_id", "")
                        or self.index_name
                    )
                    text = getattr(item, "content", "")
                    score = float(
                        getattr(item, "@search.score", getattr(item, "score", 0.0))
                    )
                results.append(
                    KBResult(
                        chunk_id=str(chunk_id),
                        source_id=str(source_id),
                        text=str(text)[:2000],
                        score=score,
                    )
                )
            return results
        except Exception as exc:
            if self.fail_on_error:
                raise KnowledgeBaseError(
                    "Azure AI Search retrieval failed."
                ) from exc
            return []

    def check_ready(self) -> bool:
        """Verify that the configured index can be queried."""
        if self._client is None:
            return False
        try:
            list(self._client.search("*", top=1, select=["chunk_id"]))
            return True
        except Exception as exc:
            if self.fail_on_error:
                raise KnowledgeBaseError(
                    "Azure AI Search readiness check failed."
                ) from exc
            return False

    @property
    def is_available(self) -> bool:
        """True when an actual search client is wired."""
        return self._client is not None
