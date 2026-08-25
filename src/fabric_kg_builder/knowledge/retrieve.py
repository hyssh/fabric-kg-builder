"""knowledge.retrieve -- direct retrieve and citation probe with lineage callback.

AGK-004: Issues a POST to the official OData-style endpoint and normalises
the response into canonical :class:`Citation` objects.

Official retrieve endpoint (GA 2026-04-01)
------------------------------------------
  ``POST {endpoint}/knowledgebases('{kbName}')/retrieve?api-version=2026-04-01``

  Note the OData key syntax with parentheses and single-quoted name.

Request body (GA)::

    {
        "intents": [{"type": "semantic", "search": "<query>"}],
        "includeActivity": true,
        "maxRuntimeInSeconds": 60,
        "maxOutputSizeInTokens": 100000,
        "knowledgeSourceParams": [
            {
                "kind": "searchIndex",
                "knowledgeSourceName": "my-ks",
                "includeReferences": true,
                "includeReferenceSourceData": true
            }
        ]
    }

  ``query`` and ``maxDocs`` are not valid GA fields.

Response structure::

    {
        "response": [{"content": [{"type": "text", "text": "<answer>"}]}],
        "activity": [...],
        "references": [{
            "type": "searchIndex",
            "id": "ref-1",
            "activitySource": 1,
            "sourceData": {"id": "docKey1", "title": "...", "content": "..."},
            "rerankerScore": 3.5,
            "docKey": "myDocKey1"
        }]
    }

HTTP 206 means partial activity failure (some sources failed).  It is
explicitly raised as :class:`PartialRetrievalError`, not success-shaped.

Authentication
--------------
  * Primary Search auth: ``api-key: <key>`` OR ``Authorization: Bearer <token>``
    -- never both at the same time.
  * Fabric preview sources: additionally requires
    ``x-ms-query-source-authorization: Bearer <obo-token>`` -- this is
    separate from the Search auth header and must NOT substitute for it.

Citation normalisation
----------------------
  * ``citation_id``  = ``"{source_name}/{doc_key}"`` (stable, no PII)
  * ``content``      = truncated to 2 000 characters
  * ``score``        = float reranker score; ``None`` if not provided
  * ``source_name``  = knowledge source name
  * ``doc_key``      = ``docKey`` (searchIndex) or ``id`` (Fabric)
  * ``metadata``     = all remaining provider-specific fields (for lineage / audit)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from .models import SearchAuth, _GA_VERSION, _PREVIEW_VERSION, pinned_headers
from .transport import HttpError, HttpRequest, HttpTransport

logger = logging.getLogger(__name__)

_CONTENT_TRUNCATION = 2_000  # characters; avoid PII in logs / audit


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PartialRetrievalError(Exception):
    """Raised when the service returns HTTP 206 (partial activity failure).

    Some knowledge sources returned results but at least one source encountered
    an error.  The partial results and activity diagnostics are available via
    the attributes.

    Attributes
    ----------
    answer_text : str
        Partial answer text extracted from the response (may be empty).
    citations : list[Citation]
        Citations from sources that succeeded.
    activity : list[dict]
        Activity diagnostics showing which sources failed and why.
    raw_body : dict
        The full raw response body for caller inspection.
    """

    def __init__(
        self,
        answer_text: str,
        citations: list[Citation],
        activity: list[dict[str, Any]],
        raw_body: dict[str, Any],
        response_headers: dict[str, str] | None = None,
    ) -> None:
        self.answer_text = answer_text
        self.citations = citations
        self.activity = activity
        self.raw_body = raw_body
        self.response_headers = response_headers or {}
        super().__init__(
            f"Partial retrieval (HTTP 206): {len(citations)} citation(s), "
            f"{len(activity)} activity event(s). Check .activity for failure details."
        )


class LineageCallbackError(Exception):
    """Raised when a lineage callback fails and the operation requires it.

    Attributes
    ----------
    citation_id : str
        The citation that triggered the callback failure.
    original_error : Exception
        The original exception raised by the callback.
    """

    def __init__(self, citation_id: str, original_error: Exception) -> None:
        self.citation_id = citation_id
        self.original_error = original_error
        super().__init__(
            f"Lineage callback failed for citation {citation_id!r}: {original_error}"
        )


# ---------------------------------------------------------------------------
# Citation model
# ---------------------------------------------------------------------------


@dataclass
class Citation:
    """A single retrieved passage / citation from a knowledge base.

    Attributes
    ----------
    citation_id : str
        Stable identifier: ``"{source_name}/{doc_key}"``.
    source_name : str
        The knowledge source that provided this passage.
    doc_key : str
        Document or chunk key within the source index.
    content : str
        Passage content (truncated to :data:`_CONTENT_TRUNCATION` characters).
    score : float | None
        Reranker score; ``None`` if not provided by the service.
    metadata : dict
        All remaining provider-specific fields (for lineage / audit use).
    """

    citation_id: str
    source_name: str
    doc_key: str
    content: str
    score: float | None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Retrieval result
# ---------------------------------------------------------------------------


@dataclass
class RetrievalResult:
    """The full result of a knowledge base retrieve call.

    Attributes
    ----------
    answer_text : str
        Synthesised answer text extracted from ``response[].content[].text``.
    citations : list[Citation]
        Normalised citations from ``references[]``.
    activity : list[dict]
        Raw activity diagnostics from ``activity[]``.
    is_partial : bool
        ``True`` when the response was HTTP 206 (some sources failed).
        When ``is_partial=True`` the result should be treated as unreliable.
    """

    answer_text: str
    citations: list[Citation]
    activity: list[dict[str, Any]] = field(default_factory=list)
    is_partial: bool = False
    response_headers: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Citation normalisation
# ---------------------------------------------------------------------------

_SOURCE_DATA_LINEAGE_FIELDS = {
    "canonical_id": ("canonical_id", "canonicalId"),
    "linked_entity_ids": ("linked_entity_ids", "linkedEntityIds"),
    "evidence_id": ("evidence_id", "evidenceId"),
    "chunk_id": ("chunk_id", "chunkId"),
    "asset_version_id": ("asset_version_id", "assetVersionId"),
    "source_file_id": ("source_file_id", "sourceFileId"),
    "blob_url": ("blob_url", "blobUrl"),
    "source_locator_json": (
        "source_locator_json",
        "sourceLocatorJson",
        "source_locator",
        "sourceLocator",
    ),
}


def _source_data_lineage(source_data: dict[str, Any]) -> dict[str, Any]:
    """Retain only canonical identity and immutable lineage metadata."""
    metadata: dict[str, Any] = {}
    sources = [source_data]
    nested = source_data.get("metadata")
    if isinstance(nested, dict):
        sources.append(nested)
    for canonical_name, aliases in _SOURCE_DATA_LINEAGE_FIELDS.items():
        for source in sources:
            value = None
            for alias in aliases:
                candidate = source.get(alias)
                if candidate is not None and candidate != "":
                    value = candidate
                    break
            if value is not None:
                metadata[canonical_name] = value
                break
    return metadata


def _normalise_citation(raw: dict[str, Any], activity_sources: list[dict[str, Any]] | None = None) -> Citation:
    """Convert a raw reference dict from ``references[]`` into a :class:`Citation`.

    Supports both ``searchIndex`` references (with ``docKey``, ``rerankerScore``,
    ``sourceData``) and Fabric preview references (with ``workspaceId``,
    ``dataAgentId`` / ``ontologyId``, ``sourceData.fabricAnswer``).
    """
    ref_type: str = raw.get("type", "")
    ref_id: str = raw.get("id", "")

    if ref_type == "searchIndex":
        doc_key = raw.get("docKey") or ref_id
        source_data: dict[str, Any] = raw.get("sourceData") or {}
        content_raw = (
            source_data.get("content")
            or source_data.get("text")
            or ""
        )
        content = str(content_raw)[:_CONTENT_TRUNCATION]
        score_raw = raw.get("rerankerScore")
        score: float | None = float(score_raw) if score_raw is not None else None

        # Resolve source name from activitySources if available
        activity_source_idx = raw.get("activitySource")
        source_name = ""
        if activity_sources and activity_source_idx is not None:
            try:
                src_entry = activity_sources[int(activity_source_idx) - 1]
                source_name = src_entry.get("knowledgeSourceName", "")
            except (IndexError, ValueError, TypeError):
                pass
        if not source_name:
            source_name = source_data.get("sourceName", "") or ""

    elif ref_type in ("fabricDataAgent", "fabricOntology"):
        doc_key = ref_id
        source_data = raw.get("sourceData") or {}
        content_raw = source_data.get("fabricAnswer") or source_data.get("content") or ""
        content = str(content_raw)[:_CONTENT_TRUNCATION]
        score = None
        ws_id = raw.get("workspaceId", "")
        agent_id = raw.get("dataAgentId") or raw.get("ontologyId") or ""
        source_name = f"{ws_id}/{agent_id}" if ws_id else agent_id

    else:
        # Unknown type -- extract what we can
        doc_key = raw.get("docKey") or ref_id
        source_data = raw.get("sourceData") or {}
        content_raw = source_data.get("content") or raw.get("content") or ""
        content = str(content_raw)[:_CONTENT_TRUNCATION]
        score_raw = raw.get("rerankerScore") or raw.get("score")
        score = float(score_raw) if score_raw is not None else None
        source_name = source_data.get("sourceName", "")

    citation_id = f"{source_name}/{doc_key}" if source_name else doc_key

    # Collect metadata without duplicating already-mapped fields
    skip = {"id", "type", "docKey", "rerankerScore", "sourceData",
            "workspaceId", "dataAgentId", "ontologyId", "activitySource", "content"}
    metadata = {k: v for k, v in raw.items() if k not in skip}
    metadata.update(_source_data_lineage(source_data))

    return Citation(
        citation_id=citation_id,
        source_name=source_name,
        doc_key=doc_key,
        content=content,
        score=score,
        metadata=metadata,
    )


def _extract_answer(parsed: dict[str, Any]) -> str:
    """Extract the synthesised answer text from ``response[].content[]``."""
    response_list = parsed.get("response") or []
    for resp_item in response_list:
        if not isinstance(resp_item, dict):
            continue
        for content_item in (resp_item.get("content") or []):
            if isinstance(content_item, dict) and content_item.get("type") == "text":
                return str(content_item.get("text") or "")
    return ""


# ---------------------------------------------------------------------------
# Retriever
# ---------------------------------------------------------------------------


class KnowledgeBaseRetriever:
    """Issues retrieval requests against a knowledge base and returns normalised citations.

    Parameters
    ----------
    endpoint : str
        Search service endpoint (e.g. ``https://svc.search.windows.net``).
    kb_name : str
        Name of the knowledge base to query.
    api_version : str
        Pinned API version (from :class:`CapabilityResult`).
    transport : HttpTransport
        Injectable transport.
    token : str | None
        Pre-obtained bearer token.  Mutually exclusive with *api_key*.
    api_key : str | None
        Search API key.  Mutually exclusive with *token*.
    obo_token : str | None
        End-user OBO token for Fabric preview sources.  Sent as
        ``x-ms-query-source-authorization``.  Never used as primary auth.
    token_provider : Callable[[], str] | None
        Token factory (used when *token* is ``None`` and *api_key* is ``None``).
    lineage_callback : Callable[[Citation], None] | None
        Optional callback invoked for each citation after normalisation.
        If the callback raises, a :class:`LineageCallbackError` is raised.
    require_lineage_callback : bool
        When ``True`` (default ``False``), a lineage callback failure is a
        hard error (raises :class:`LineageCallbackError`).  When ``False``,
        the result is marked :attr:`RetrievalResult.is_partial` = ``True``.
    knowledge_source_params : list[dict] | None
        Explicit ``knowledgeSourceParams`` to include in the request body.
        If ``None``, an empty list is sent (service uses KB defaults).
    """

    def __init__(
        self,
        endpoint: str,
        kb_name: str,
        api_version: str,
        transport: HttpTransport,
        token: str | None = None,
        api_key: str | None = None,
        obo_token: str | None = None,
        token_provider: Callable[[], str] | None = None,
        lineage_callback: Callable[[Citation], None] | None = None,
        require_lineage_callback: bool = False,
        knowledge_source_params: list[dict[str, Any]] | None = None,
    ) -> None:
        self._ep = endpoint.rstrip("/")
        self._kb = kb_name
        self._api = api_version
        self._transport = transport
        self._token = token
        self._api_key = api_key
        self._obo_token = obo_token
        self._token_provider = token_provider
        self._lineage_callback = lineage_callback
        self._require_lineage = require_lineage_callback
        self._ks_params = knowledge_source_params

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_auth(self) -> SearchAuth:
        if self._api_key:
            return SearchAuth(api_key=self._api_key, obo_token=self._obo_token)
        if self._token:
            return SearchAuth(token=self._token, obo_token=self._obo_token)
        if self._token_provider:
            self._token = self._token_provider()
            return SearchAuth(token=self._token, obo_token=self._obo_token)
        from azure.identity import DefaultAzureCredential  # noqa: PLC0415
        from .models import _SEARCH_TOKEN_SCOPE  # noqa: PLC0415
        from fabric_kg_builder.azure_identity import default_azure_credential

        cred = default_azure_credential()
        self._token = cred.get_token(_SEARCH_TOKEN_SCOPE).token
        return SearchAuth(token=self._token, obo_token=self._obo_token)

    def _url(self) -> str:
        """Return the OData-style retrieve endpoint URL."""
        return (
            f"{self._ep}/knowledgebases('{self._kb}')/retrieve"
            f"?api-version={self._api}"
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def retrieve(
        self,
        query: str,
        max_runtime_seconds: int = 60,
        max_output_tokens: int = 100_000,
        max_output_docs: int = 50,
        reasoning_effort: str = "low",
        output_mode: str = "answerSynthesis",
    ) -> list[Citation]:
        """Retrieve passages for *query* from the knowledge base.

        Returns the citations from the ``references[]`` array, sorted by
        reranker score descending.

        Parameters
        ----------
        query : str
            The natural-language retrieval query.
        max_runtime_seconds : int
            Maximum seconds the service will spend on the request.
        max_output_tokens : int
            Maximum output token budget (GA: ``maxOutputSizeInTokens``;
            preview: ``maxOutputSize``).
        max_output_docs : int
            Maximum output document count (preview only).
        reasoning_effort : str
            Reasoning effort kind for preview (``"low"``, ``"medium"``,
            ``"high"``).  Ignored for GA.
        output_mode : str
            Output mode for preview (e.g. ``"answerSynthesis"``).  Ignored
            for GA.

        Returns
        -------
        list[Citation]
            Normalised citations sorted by score (descending, ``None`` last).

        Raises
        ------
        HttpError
            On non-2xx non-206 responses.
        PartialRetrievalError
            When the service returns HTTP 206.
        ValueError
            If *query* is blank.
        LineageCallbackError
            When lineage callback raises and ``require_lineage_callback=True``.
        """
        result = self.retrieve_full(
            query,
            max_runtime_seconds=max_runtime_seconds,
            max_output_tokens=max_output_tokens,
            max_output_docs=max_output_docs,
            reasoning_effort=reasoning_effort,
            output_mode=output_mode,
        )
        return result.citations

    def retrieve_full(
        self,
        query: str,
        max_runtime_seconds: int = 60,
        max_output_tokens: int = 100_000,
        max_output_docs: int = 50,
        reasoning_effort: str = "low",
        output_mode: str = "answerSynthesis",
    ) -> RetrievalResult:
        """Like :meth:`retrieve` but returns a full :class:`RetrievalResult`.

        Raises :class:`PartialRetrievalError` on HTTP 206.

        For the GA version (``2026-04-01``) the request body uses the
        ``intents`` shape.  For the preview version (``2026-05-01-preview``)
        the request body uses the ``messages`` shape with additional preview
        fields (``retrievalReasoningEffort``, ``outputMode``,
        ``maxOutputDocuments``).
        """
        if not query or not query.strip():
            raise ValueError("retrieve: query must be a non-empty string")

        if self._api == _PREVIEW_VERSION:
            # Preview body: messages format
            # https://learn.microsoft.com/rest/api/searchservice/knowledge-retrieval/retrieve?view=rest-searchservice-2026-05-01-preview
            body: dict[str, Any] = {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": query}],
                    }
                ],
                "maxRuntimeInSeconds": max_runtime_seconds,
                "maxOutputSize": max_output_tokens,
                "maxOutputDocuments": max_output_docs,
                "retrievalReasoningEffort": {"kind": reasoning_effort},
                "includeActivity": True,
                "outputMode": output_mode,
            }
        else:
            # GA body: intents format
            # https://learn.microsoft.com/rest/api/searchservice/knowledge-retrieval/retrieve?view=rest-searchservice-2026-04-01
            body = {
                "intents": [{"type": "semantic", "search": query}],
                "includeActivity": True,
                "maxRuntimeInSeconds": max_runtime_seconds,
                "maxOutputSizeInTokens": max_output_tokens,
            }
        if self._ks_params is not None:
            body["knowledgeSourceParams"] = self._ks_params

        auth = self._get_auth()
        resp = self._transport.send(
            HttpRequest(
                method="POST",
                url=self._url(),
                headers=auth.to_headers(),
                body=body,
            )
        )

        parsed: dict[str, Any] = resp.body if isinstance(resp.body, dict) else {}

        if resp.status_code == 206:
            # Partial activity failure -- build partial result and raise
            activity = parsed.get("activity") or []
            refs = parsed.get("references") or []
            activity_sources = _extract_activity_sources(activity)
            citations = [_normalise_citation(r, activity_sources) for r in refs]
            answer = _extract_answer(parsed)
            raise PartialRetrievalError(
                answer_text=answer,
                citations=citations,
                activity=activity,
                raw_body=parsed,
                response_headers=resp.headers,
            )

        if resp.status_code >= 400:
            raise HttpError(
                resp.status_code,
                resp.body,
                response_headers=resp.headers,
            )

        activity = parsed.get("activity") or []
        activity_sources = _extract_activity_sources(activity)
        refs = parsed.get("references") or []
        citations = [_normalise_citation(r, activity_sources) for r in refs]
        answer = _extract_answer(parsed)

        lineage_failed = False
        if self._lineage_callback is not None:
            for c in citations:
                try:
                    self._lineage_callback(c)
                except Exception as exc:  # noqa: BLE001
                    if self._require_lineage:
                        raise LineageCallbackError(c.citation_id, exc) from exc
                    logger.warning(
                        "[retrieve] lineage_callback raised for %s: %s",
                        c.citation_id,
                        exc,
                    )
                    lineage_failed = True

        # Sort by score descending (None -- last)
        citations.sort(key=lambda c: (c.score is None, -(c.score or 0.0)))

        logger.debug(
            "[retrieve] kb=%s query=%r -- %d citation(s)",
            self._kb,
            query[:80],
            len(citations),
        )
        return RetrievalResult(
            answer_text=answer,
            citations=citations,
            activity=activity,
            is_partial=lineage_failed,
            response_headers=resp.headers,
        )


def _extract_activity_sources(activity: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract source-info entries from activity for resolving activitySource indices."""
    sources = []
    for entry in activity:
        if isinstance(entry, dict) and entry.get("type") == "knowledgeSource":
            sources.append(entry)
    return sources
