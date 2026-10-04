"""knowledge.search_kb -- idempotent CRUD for Search knowledge sources and knowledge bases.

AGK-003: Manages the lifecycle of two top-level Azure AI Search resources
introduced by the agentic-retrieval feature:

  * **Knowledge sources** (``/knowledgesources/{name}``) -- data connections.
    The GA ``searchIndex`` kind wraps an existing index or alias.
    The preview ``fabricDataAgent`` and ``fabricOntology`` kinds are preview-only.

  * **Knowledge bases** (``/knowledgebases/{name}``) -- retrieval units that
    reference one or more knowledge sources.

Both resources use PUT for idempotent create-or-update; a prior GET is issued
so callers know whether the resource was created (HTTP 201-equivalent) or
updated (HTTP 200-equivalent).

Wire bodies (official REST contracts)
--------------------------------------
Search-index knowledge source (GA 2026-04-01)::

    {
        "name": "my-ks",
        "kind": "searchIndex",
        "description": "...",
        "searchIndexParameters": {
            "searchIndexName": "my-index",
            "semanticConfigurationName": "my-semantic-config",
            "sourceDataFields": [{"name": "description"}, {"name": "category"}],
            "searchFields": [{"name": "id"}]
        }
    }

Fabric Data Agent knowledge source (preview 2026-05-01-preview)::

    {
        "name": "my-fabric-da-ks",
        "kind": "fabricDataAgent",
        "fabricDataAgentParameters": {"workspaceId": "...", "dataAgentId": "..."}
    }

Fabric Ontology knowledge source (preview 2026-05-01-preview)::

    {
        "name": "my-onto-ks",
        "kind": "fabricOntology",
        "fabricOntologyParameters": {"workspaceId": "...", "ontologyId": "..."}
    }

Knowledge base (GA 2026-04-01)::

    {
        "name": "my-kb",
        "description": "...",
        "knowledgeSources": [{"name": "ks1"}, {"name": "ks2"}]
    }

Note: preview-only KB fields (``retrievalInstructions``, ``answerInstructions``,
``outputMode``, ``retrievalReasoningEffort``, ``corsOptions``) must NEVER be
included in GA requests.

API versions
------------
  GA:      2026-04-01 (searchIndex kind only)
  Preview: 2026-05-01-preview (adds fabricDataAgent, fabricOntology)

Permissions
-----------
  * Search Service Contributor -- create / manage knowledge sources and bases.
  * Search Index Data Contributor -- if the KB retrieval touches index content.
  * Cognitive Services User on the Search managed identity -- if KB uses an LLM.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from .models import (
    AgentFeature,
    CapabilityResult,
    FeatureNotAvailable,
    SearchAuth,
    _GA_VERSION,
    _PREVIEW_VERSION,
    _SEARCH_TOKEN_SCOPE,
    pinned_headers,
)
from .transport import HttpError, HttpRequest, HttpTransport
from .validation import SourceSpec, validate_sources
from . import lineage_adapter as _lin

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models for knowledge resources
# ---------------------------------------------------------------------------


@dataclass
class SearchIndexKnowledgeSourceSpec:
    """Specification for a GA search-index knowledge source (``kind: searchIndex``).

    Attributes
    ----------
    name : str
        Unique knowledge source name (no spaces, alphanumeric + hyphens).
    search_index_name : str
        Name of the existing AI Search index or alias to wrap.
    semantic_configuration_name : str | None
        Optional semantic configuration name to use.
    source_data_fields : list[str]
        Field names to return as source data.
    search_fields : list[str]
        Field names to search over.
    description : str
        Optional human-readable description.
    """

    name: str
    search_index_name: str
    semantic_configuration_name: str | None = None
    source_data_fields: list[str] = field(default_factory=list)
    search_fields: list[str] = field(default_factory=list)
    description: str = ""

    def to_body(self) -> dict[str, Any]:
        """Return the official GA REST body for PUT /knowledgesources/{name}."""
        params: dict[str, Any] = {
            "searchIndexName": self.search_index_name,
        }
        if self.semantic_configuration_name:
            params["semanticConfigurationName"] = self.semantic_configuration_name
        params["sourceDataFields"] = [{"name": f} for f in self.source_data_fields]
        params["searchFields"] = [{"name": f} for f in self.search_fields]

        body: dict[str, Any] = {
            "name": self.name,
            "kind": "searchIndex",
            "searchIndexParameters": params,
        }
        if self.description:
            body["description"] = self.description
        return body


# Backward-compatible alias
KnowledgeSourceSpec = SearchIndexKnowledgeSourceSpec


@dataclass
class FabricDataAgentKnowledgeSourceSpec:
    """Specification for a preview Fabric Data Agent knowledge source.

    Kind: ``fabricDataAgent``  (preview 2026-05-01-preview only).

    Attributes
    ----------
    name : str
        Unique knowledge source name.
    workspace_id : str
        Fabric workspace GUID.
    data_agent_id : str
        Fabric Data Agent item GUID.
    description : str
        Optional human-readable description.
    """

    name: str
    workspace_id: str
    data_agent_id: str
    description: str = ""

    def to_body(self) -> dict[str, Any]:
        """Return the official preview REST body for PUT /knowledgesources/{name}."""
        body: dict[str, Any] = {
            "name": self.name,
            "kind": "fabricDataAgent",
            "fabricDataAgentParameters": {
                "workspaceId": self.workspace_id,
                "dataAgentId": self.data_agent_id,
            },
        }
        if self.description:
            body["description"] = self.description
        return body


@dataclass
class FabricOntologyKnowledgeSourceSpec:
    """Specification for a preview Fabric Ontology knowledge source.

    Kind: ``fabricOntology``  (preview 2026-05-01-preview only).

    Attributes
    ----------
    name : str
        Unique knowledge source name.
    workspace_id : str
        Fabric workspace GUID.
    ontology_id : str
        Fabric Ontology item GUID.
    description : str
        Optional human-readable description.
    """

    name: str
    workspace_id: str
    ontology_id: str
    description: str = ""

    def to_body(self) -> dict[str, Any]:
        """Return the official preview REST body for PUT /knowledgesources/{name}."""
        body: dict[str, Any] = {
            "name": self.name,
            "kind": "fabricOntology",
            "fabricOntologyParameters": {
                "workspaceId": self.workspace_id,
                "ontologyId": self.ontology_id,
            },
        }
        if self.description:
            body["description"] = self.description
        return body


# Backward-compatible alias (RemoteKnowledgeSourceSpec is deprecated; use specific classes)
class RemoteKnowledgeSourceSpec:
    """Deprecated: use :class:`FabricDataAgentKnowledgeSourceSpec` or
    :class:`FabricOntologyKnowledgeSourceSpec` instead.
    """

    def __init__(
        self,
        name: str,
        source_type: str,
        workspace_id: str,
        item_id: str,
        description: str = "",
    ) -> None:
        self.name = name
        self.source_type = source_type
        self.workspace_id = workspace_id
        self.item_id = item_id
        self.description = description

    def to_body(self) -> dict[str, Any]:
        """Return the preview REST body using the correct discriminated shape."""
        if self.source_type == "fabricDataAgent":
            spec = FabricDataAgentKnowledgeSourceSpec(
                name=self.name,
                workspace_id=self.workspace_id,
                data_agent_id=self.item_id,
                description=self.description,
            )
            return spec.to_body()
        if self.source_type == "fabricOntology":
            spec = FabricOntologyKnowledgeSourceSpec(
                name=self.name,
                workspace_id=self.workspace_id,
                ontology_id=self.item_id,
                description=self.description,
            )
            return spec.to_body()
        raise ValueError(f"RemoteKnowledgeSourceSpec: unknown source_type {self.source_type!r}")


@dataclass
class KnowledgeBaseSpec:
    """Specification for a knowledge base.

    Attributes
    ----------
    name : str
        Unique knowledge base name.
    knowledge_source_names : list[str]
        Names of knowledge sources to include (max :data:`MAX_SOURCES` = 5).
    description : str
        Optional human-readable description.
    uses_llm : bool
        Set to ``True`` if the KB will invoke an LLM for answer synthesis.
        When ``True``, the Search managed identity needs the
        *Cognitive Services User* role (surfaced in :meth:`permission_notes`).

    Preview-only fields (only sent when api_version == _PREVIEW_VERSION):
        retrieval_instructions, answer_instructions, output_mode,
        retrieval_reasoning_effort, cors_origins.
    """

    name: str
    knowledge_source_names: list[str] = field(default_factory=list)
    description: str = ""
    uses_llm: bool = False
    # Preview-only fields -- never sent for GA requests
    retrieval_instructions: str | None = None
    answer_instructions: str | None = None
    output_mode: str | None = None
    retrieval_reasoning_effort: str | None = None
    cors_origins: list[str] = field(default_factory=list)

    # Legacy alias: source_names maps to knowledge_source_names
    @property
    def source_names(self) -> list[str]:
        return self.knowledge_source_names

    def to_body(self, *, api_version: str = _GA_VERSION) -> dict[str, Any]:
        """Return the REST body for PUT /knowledgebases/{name}.

        Parameters
        ----------
        api_version:
            The API version to build the body for.  Preview-only fields are
            omitted unless ``api_version == "2026-05-01-preview"``.
        """
        body: dict[str, Any] = {
            "name": self.name,
            "knowledgeSources": [{"name": n} for n in self.knowledge_source_names],
        }
        if self.description:
            body["description"] = self.description

        # Preview-only fields -- never included for GA
        if api_version == _PREVIEW_VERSION:
            if self.retrieval_instructions:
                body["retrievalInstructions"] = self.retrieval_instructions
            if self.answer_instructions:
                body["answerInstructions"] = self.answer_instructions
            if self.output_mode:
                body["outputMode"] = self.output_mode
            if self.retrieval_reasoning_effort:
                body["retrievalReasoningEffort"] = self.retrieval_reasoning_effort
            if self.cors_origins:
                body["corsOptions"] = {"allowedOrigins": self.cors_origins}

        return body

    def permission_notes(self) -> list[str]:
        """Return human-readable permission requirements for this KB."""
        notes = [
            "Search Service Contributor -- create/manage knowledge sources and bases.",
            "Search Index Data Contributor -- if KB retrieval touches index content.",
        ]
        if self.uses_llm:
            notes.append(
                "Cognitive Services User on Search managed identity -- required when KB uses an LLM."
            )
        return notes


# ---------------------------------------------------------------------------
# Upsert result
# ---------------------------------------------------------------------------


@dataclass
class UpsertResult:
    """Outcome of a knowledge source or knowledge base upsert operation.

    Attributes
    ----------
    name : str
        The resource name.
    created : bool
        ``True`` if the resource was newly created; ``False`` if it was updated.
    status_code : int
        The HTTP status code returned by the PUT call.
    body : dict
        The parsed response body.
    etag : str | None
        ETag from the response, if present.
    """

    name: str
    created: bool
    status_code: int
    body: dict[str, Any] = field(default_factory=dict)
    etag: str | None = None


# ---------------------------------------------------------------------------
# SearchKbClient
# ---------------------------------------------------------------------------


class SearchKbClient:
    """Client for idempotent create/update/get of Search knowledge resources.

    Parameters
    ----------
    capability : CapabilityResult
        Pinned capability result from :func:`discover_capabilities`.
    transport : HttpTransport
        Injectable transport (use ``FakeTransport`` in tests).
    token : str | None
        Pre-obtained bearer token.  If ``None``, *token_provider* is called.
    api_key : str | None
        API key for Search auth.  Mutually exclusive with *token*.
    token_provider : Callable[[], str] | None
        Callable that returns a bearer token.
    """

    def __init__(
        self,
        capability: CapabilityResult,
        transport: HttpTransport,
        token: str | None = None,
        api_key: str | None = None,
        token_provider: Callable[[], str] | None = None,
    ) -> None:
        if capability.api_version is None:
            raise FeatureNotAvailable(
                feature=AgentFeature.KNOWLEDGE_SOURCES,
                required_version="2026-04-01",
                available_version=None,
            )
        self._cap = capability
        self._transport = transport
        self._token = token
        self._api_key = api_key
        self._token_provider = token_provider
        self._ep = capability.endpoint.rstrip("/")
        self._api = capability.api_version

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_auth(self) -> SearchAuth:
        """Return a :class:`SearchAuth` for the configured credential."""
        if self._api_key:
            return SearchAuth(api_key=self._api_key)
        if self._token:
            return SearchAuth(token=self._token)
        if self._token_provider:
            self._token = self._token_provider()
            return SearchAuth(token=self._token)
        from azure.identity import DefaultAzureCredential  # noqa: PLC0415

        cred = DefaultAzureCredential()
        self._token = cred.get_token(_SEARCH_TOKEN_SCOPE).token
        return SearchAuth(token=self._token)

    def _url(self, path: str) -> str:
        return f"{self._ep}/{path.lstrip('/')}?api-version={self._api}"

    def _headers(self) -> dict[str, str]:
        return self._get_auth().to_headers()

    def _get(self, resource_path: str) -> tuple[int, dict[str, Any]]:
        """GET *resource_path* and return ``(status_code, body)``."""
        resp = self._transport.send(
            HttpRequest(
                method="GET",
                url=self._url(resource_path),
                headers=self._headers(),
            )
        )
        if resp.status_code not in (200, 404):
            raise HttpError(resp.status_code, resp.body)
        body = resp.body if isinstance(resp.body, dict) else {}
        return resp.status_code, body

    def _put(self, resource_path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any], str | None]:
        """PUT *resource_path* with *body* and return ``(status_code, response_body, etag)``."""
        resp = self._transport.send(
            HttpRequest(
                method="PUT",
                url=self._url(resource_path),
                headers=self._headers(),
                body=body,
            )
        )
        if resp.status_code >= 400:
            raise HttpError(resp.status_code, resp.body)
        rb = resp.body if isinstance(resp.body, dict) else {}
        etag = resp.headers.get("ETag") if resp.headers else None
        return resp.status_code, rb, etag

    # ------------------------------------------------------------------
    # Public API -- Knowledge Sources
    # ------------------------------------------------------------------

    def get_knowledge_source(self, name: str) -> dict[str, Any] | None:
        """Return the existing knowledge source dict, or ``None`` if not found."""
        self._cap.require(AgentFeature.KNOWLEDGE_SOURCES)
        status, body = self._get(f"knowledgesources/{name}")
        return body if status == 200 else None

    def upsert_knowledge_source(
        self,
        spec: SearchIndexKnowledgeSourceSpec
             | FabricDataAgentKnowledgeSourceSpec
             | FabricOntologyKnowledgeSourceSpec
             | RemoteKnowledgeSourceSpec,
    ) -> UpsertResult:
        """Idempotently create or update a knowledge source.

        For Fabric sources, the method calls :meth:`CapabilityResult.require`
        which raises :class:`FeatureNotAvailable` before any mutating call if
        the service does not support preview.
        """
        self._cap.require(AgentFeature.KNOWLEDGE_SOURCES)

        if isinstance(spec, FabricDataAgentKnowledgeSourceSpec) or (
            isinstance(spec, RemoteKnowledgeSourceSpec) and spec.source_type == "fabricDataAgent"
        ):
            self._cap.require(AgentFeature.FABRIC_DATA_AGENT_SOURCE)
        elif isinstance(spec, FabricOntologyKnowledgeSourceSpec) or (
            isinstance(spec, RemoteKnowledgeSourceSpec) and spec.source_type == "fabricOntology"
        ):
            self._cap.require(AgentFeature.FABRIC_ONTOLOGY_SOURCE)

        existing = self.get_knowledge_source(spec.name)
        existed_before = existing is not None

        status, body, etag = self._put(f"knowledgesources/{spec.name}", spec.to_body())
        created = not existed_before and status in (200, 201)
        logger.info(
            "[search_kb] knowledge source '%s' %s (HTTP %s)",
            spec.name,
            "created" if created else "updated",
            status,
        )
        _lin.record(
            operation="knowledge_source",
            action="upsert",
            api_version=self._api,
            capability_mode="preview" if self._cap.is_preview else "ga",
            resource_name=spec.name,
            status="created" if created else "updated",
            endpoint=self._ep,
            remote_id=etag,
        )
        return UpsertResult(name=spec.name, created=created, status_code=status, body=body, etag=etag)

    # ------------------------------------------------------------------
    # Public API -- Knowledge Bases
    # ------------------------------------------------------------------

    def get_knowledge_base(self, name: str) -> dict[str, Any] | None:
        """Return the existing knowledge base dict, or ``None`` if not found."""
        self._cap.require(AgentFeature.KNOWLEDGE_BASES)
        status, body = self._get(f"knowledgebases/{name}")
        return body if status == 200 else None

    def upsert_knowledge_base(
        self,
        spec: KnowledgeBaseSpec,
        source_specs: list[SourceSpec] | None = None,
    ) -> UpsertResult:
        """Idempotently create or update a knowledge base.

        Validates the source count cap (<=5) and source type availability.
        Preview-only fields in *spec* are automatically gated by the current
        API version -- they will not appear in the request body when using GA.
        """
        self._cap.require(AgentFeature.KNOWLEDGE_BASES)

        from .validation import MAX_SOURCES, SourceCapError  # noqa: PLC0415

        if len(spec.knowledge_source_names) > MAX_SOURCES:
            raise SourceCapError(len(spec.knowledge_source_names))

        if source_specs is not None:
            validate_sources(source_specs, api_version=self._api)

        existing = self.get_knowledge_base(spec.name)
        existed_before = existing is not None

        # Pass api_version so preview-only fields are correctly gated
        status, body, etag = self._put(
            f"knowledgebases/{spec.name}",
            spec.to_body(api_version=self._api),
        )
        created = not existed_before and status in (200, 201)
        logger.info(
            "[search_kb] knowledge base '%s' %s (HTTP %s) -- notes: %s",
            spec.name,
            "created" if created else "updated",
            status,
            "; ".join(spec.permission_notes()),
        )
        _lin.record(
            operation="knowledge_base",
            action="upsert",
            api_version=self._api,
            capability_mode="preview" if self._cap.is_preview else "ga",
            resource_name=spec.name,
            status="created" if created else "updated",
            endpoint=self._ep,
            remote_id=etag,
            extra={"source_count": len(spec.knowledge_source_names)},
        )
        return UpsertResult(name=spec.name, created=created, status_code=status, body=body, etag=etag)
