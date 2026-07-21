"""knowledge.data_agent — Fabric Data Agent lifecycle management.

AGK-005: Manages the full lifecycle of a Fabric Data Agent item:

  * **Definition builder** — assembles InlineBase64 definition parts from a
    :class:`DataAgentSpec`, including:

      - ``Files/Config/data_agent.json``       — agent manifest
      - ``Files/Config/draft/stage_config.json`` — draft stage config
      - ``Files/Config/published/stage_config.json`` — published stage config
      - ``Files/Config/draft/{dsType}-{name}/datasource.json`` — per-source config

  * **Idempotent create-or-update** — LIST the workspace to check existence,
    then POST to create (201 sync | 202 LRO) or POST to ``updateDefinition``
    to update an existing item.

  * **LRO polling** — respects the ``Retry-After`` header; raises
    :class:`LROTimeoutError` after *lro_timeout_seconds*.

  * **Source cap** — enforced at build time (max 5 sources); raises
    :class:`~fabric_kg_builder.knowledge.validation.SourceCapError` if exceeded.

  * **Capability discovery** — ontology/search sources are treated as
    capability-discovered preview rather than assumed; the builder will include
    them only when the spec flags them as preview-confirmed.

Security
--------
  * Definition parts are JSON-encoded and base64-encoded before transmission.
    Connection-string values inside ``DataSourceSpec.connection_properties``
    are **not** logged — only the ``type`` and ``name`` fields are surfaced.
  * Token strings are never written to logs.

Permissions required (caller's identity or SPN)
------------------------------------------------
  * **Contributor** on the Fabric workspace (or ``Item.ReadWrite.All`` on the
    DataAgent item).

API
---
  ``POST https://api.fabric.microsoft.com/v1/workspaces/{workspaceId}/dataAgents``

Usage (test with FakeTransport)::

    from fabric_kg_builder.knowledge.transport import FakeTransport, HttpResponse
    from fabric_kg_builder.knowledge.data_agent import (
        DataAgentSpec, DataSourceSpec, FabricDataAgentClient
    )

    t = FakeTransport()
    # No existing agent
    t.register("GET", "/workspaces/ws-1/items", HttpResponse(200, body={"value": []}))
    # Create returns 201 sync
    t.register("POST", "/workspaces/ws-1/dataAgents",
        HttpResponse(201, body={"id": "agent-id-1", "displayName": "my-agent"}))

    spec = DataAgentSpec(
        display_name="my-agent",
        instruction="Answer questions about the knowledge graph.",
        sources=[DataSourceSpec(source_type="lakehouse", name="my-lakehouse")],
    )
    client = FabricDataAgentClient(workspace_id="ws-1", transport=t, token="fake")
    result = client.upsert(spec)
    assert result.item_id == "agent-id-1"
    assert result.created
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal
from urllib.parse import quote, urljoin

from .transport import HttpError, HttpRequest, HttpTransport
from .validation import MAX_SOURCES, SourceCapError
from . import lineage_adapter as _lin

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FABRIC_API_BASE = "https://api.fabric.microsoft.com/v1"
_FABRIC_API_VERSION = "v1"  # Fabric REST API base version for lineage recording
_FABRIC_TOKEN_SCOPE = "https://api.fabric.microsoft.com/.default"

# Datasource type enum — snake_case values from official Fabric docs
# https://learn.microsoft.com/rest/api/fabric/articles/item-management/definitions/data-agent-definition
_DOCUMENTED_DATASOURCE_TYPES: frozenset[str] = frozenset(
    {
        "unknown",
        "lakehouse_tables",
        "lakehouse",
        "data_warehouse",
        "kusto",
        "semantic_model",
        "graph",
        "mirrored_database",
        "mirrored_azure_databricks",
    }
)

# Public JSON schema identifiers for definition parts
# Source: https://learn.microsoft.com/en-us/rest/api/fabric/articles/item-management/definitions/data-agent-definition
_DATA_AGENT_SCHEMA_BASE = (
    "https://developer.microsoft.com/json-schemas/fabric/item/dataAgent/"
    "definition"
)
_STAGE_CONFIG_SCHEMA = (
    f"{_DATA_AGENT_SCHEMA_BASE}/stageConfiguration/1.0.0/schema.json"
)
_DATASOURCE_SCHEMA = (
    f"{_DATA_AGENT_SCHEMA_BASE}/dataSource/1.0.0/schema.json"
)
_FEWSHOTS_SCHEMA = (
    f"{_DATA_AGENT_SCHEMA_BASE}/fewShots/1.0.0/schema.json"
)
_DATA_AGENT_SCHEMA = (
    f"{_DATA_AGENT_SCHEMA_BASE}/dataAgent/2.1.0/schema.json"
)

# Graph datasource element type literals
ELEMENT_TYPE_NODE = "graph.nodeType"
ELEMENT_TYPE_EDGE = "graph.edgeType"
ELEMENT_TYPE_PROPERTY = "graph.property"

# Preview-only datasource types (capability-discovered)
_PREVIEW_DATASOURCE_TYPES: frozenset[str] = frozenset({"ontology", "search"})

_DEFAULT_LRO_POLL_INTERVAL = 5  # seconds
_DEFAULT_LRO_TIMEOUT = 300  # seconds

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LROTimeoutError(Exception):
    """Raised when a long-running operation does not complete within the timeout.

    Attributes
    ----------
    operation_url : str
        The LRO status URL being polled.
    elapsed_seconds : float
        Elapsed wall-clock seconds before giving up.
    """

    def __init__(self, operation_url: str, elapsed_seconds: float) -> None:
        self.operation_url = operation_url
        self.elapsed_seconds = elapsed_seconds
        super().__init__(
            f"LRO did not complete after {elapsed_seconds:.1f}s: {operation_url}"
        )


class UnsupportedDataSourceType(Exception):
    """Raised when a datasource type is unrecognised and not flagged as preview.

    Attributes
    ----------
    source_type : str
        The unrecognised type.
    """

    def __init__(self, source_type: str) -> None:
        self.source_type = source_type
        super().__init__(
            f"Datasource type {source_type!r} is not in the documented enum "
            f"{sorted(_DOCUMENTED_DATASOURCE_TYPES)} and is not flagged as "
            "capability-discovered preview. Set preview=True on the DataSourceSpec "
            "to allow it."
        )


class DataAgentDefinitionError(ValueError):
    """Raised when the deployed public definition cannot be verified."""


class DataAgentTargetError(ValueError):
    """Raised when an explicit Data Agent target mode cannot be honored."""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class DataSourceElement:
    """A single selectable element within a datasource (e.g. a Graph node/edge type).

    Fields match the documented ``DataSourceElement`` schema for the Fabric Data
    Agent definition.  Only explicitly selected elements (``is_selected=True``)
    are registered; do not silently select all available types.

    Attributes
    ----------
    id : str
        Stable UUID for this element (must be consistent across updates).
    display_name : str
        Human-readable name (matches the node/edge alias in the Graph Model).
    type : str
        Element category.  Use :data:`ELEMENT_TYPE_NODE` (``"graph.nodeType"``)
        or :data:`ELEMENT_TYPE_EDGE` (``"graph.edgeType"``).
    is_selected : bool
        Whether this element is active for the agent.  Must be set explicitly;
        defaults to ``False`` to prevent silently exposing all schema types.
    data_type : str | None
        Optional underlying data type hint.
    description : str | None
        Optional description surfaced in agent context.
    children : list | None
        Optional nested child elements.
    index_state : str | None
        Optional indexing state value.
    """

    id: str
    display_name: str
    type: str  # ELEMENT_TYPE_NODE or ELEMENT_TYPE_EDGE
    is_selected: bool = False
    data_type: str | None = None
    description: str | None = None
    children: list[Any] | None = None
    index_state: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the official element dict shape."""
        d: dict[str, Any] = {
            "id": self.id,
            "display_name": self.display_name,
            "type": self.type,
            "is_selected": self.is_selected,
        }
        if self.data_type is not None:
            d["data_type"] = self.data_type
        if self.description is not None:
            d["description"] = self.description
        if self.children is not None:
            d["children"] = self.children
        if self.index_state is not None:
            d["index_state"] = self.index_state
        return d


@dataclass
class FewShotExample:
    """One few-shot question/GQL pair derived from domain competency questions.

    Attributes
    ----------
    id : str
        Stable UUID for this example (must be consistent across updates).
    question : str
        Natural-language question from an approved competency question.
    query : str
        Bounded GQL query targeting actual node/edge aliases in the Graph Model.
        Must be non-empty; placeholder or raw query strings are rejected.
    """

    id: str
    question: str
    query: str

    def __post_init__(self) -> None:
        if not self.question.strip():
            raise ValueError("FewShotExample.question must be a non-empty string")
        if not self.query.strip():
            raise ValueError("FewShotExample.query must be a non-empty string")

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the official few-shot dict shape."""
        return {"id": self.id, "question": self.question, "query": self.query}


@dataclass
class DataSourceSpec:
    """A single data source configuration for a Fabric Data Agent.

    Attributes
    ----------
    source_type : str
        Datasource type using the official snake_case enum:
        ``graph``, ``lakehouse``, ``lakehouse_tables``, ``data_warehouse``,
        ``kusto``, ``semantic_model``, ``mirrored_database``,
        ``mirrored_azure_databricks``.  Use ``preview=True`` for
        capability-discovered types outside the documented enum.
    name : str
        Unique path-safe name used in the definition part path
        ``{type}-{name}/datasource.json`` (e.g. the safe display name of the
        Graph Model item).
    artifact_id : str
        Fabric item GUID of the source artifact (required in datasource.json).
    workspace_id : str
        Fabric workspace GUID owning the source artifact.
    display_name : str
        Human-readable display name for the datasource entry.
    instructions : str
        ``dataSourceInstructions`` injected into the datasource.json.
    description : str
        ``userDescription`` field in datasource.json.
    metadata : dict
        Additional metadata dict in datasource.json.
    elements : list[DataSourceElement]
        Explicitly selected elements (node/edge types for graph sources).
        Only elements with ``is_selected=True`` should be included.
        Do not auto-populate with all available types.
    few_shots : list[FewShotExample] | None
        Optional few-shot examples derived from domain competency questions.
        When provided, a ``fewshots.json`` part is emitted alongside
        ``datasource.json``.  Only include validated GQL against actual aliases.
    preview : bool
        Set to ``True`` for types outside the documented enum.  Prevents the
        builder from raising :class:`UnsupportedDataSourceType`.
    """

    source_type: str
    name: str
    artifact_id: str = ""
    workspace_id: str = ""
    display_name: str = ""
    instructions: str = ""
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    elements: list[DataSourceElement] = field(default_factory=list)
    few_shots: list[FewShotExample] | None = None
    preview: bool = False

    def datasource_path(self) -> str:
        """Return the definition part path for this source's datasource.json."""
        return f"Files/Config/draft/{self.source_type}-{self.name}/datasource.json"

    def fewshots_path(self) -> str:
        """Return the definition part path for this source's fewshots.json."""
        return f"Files/Config/draft/{self.source_type}-{self.name}/fewshots.json"

    def datasource_payload(self) -> dict[str, Any]:
        """Return the official datasource.json content dict.

        Conforms to the Fabric Data Agent datasource definition schema at:
        https://learn.microsoft.com/rest/api/fabric/articles/item-management/definitions/data-agent-definition
        """
        return {
            "$schema": _DATASOURCE_SCHEMA,
            "artifactId": self.artifact_id,
            "workspaceId": self.workspace_id,
            "displayName": self.display_name or self.name,
            "type": self.source_type,
            "dataSourceInstructions": self.instructions,
            "userDescription": self.description,
            "metadata": self.metadata,
            "elements": [e.to_dict() for e in self.elements],
        }

    def fewshots_payload(self) -> dict[str, Any]:
        """Return the official fewshots.json content dict.

        Only call this when :attr:`few_shots` is not ``None`` and non-empty.
        """
        examples = self.few_shots or []
        return {
            "$schema": _FEWSHOTS_SCHEMA,
            "fewShots": [fs.to_dict() for fs in examples],
        }


@dataclass
class DataAgentSpec:
    """Complete specification for a Fabric Data Agent item.

    Attributes
    ----------
    display_name : str
        The ``displayName`` of the Fabric item.
    instruction : str
        System instruction injected into the agent manifest.
    sources : list[DataSourceSpec]
        Data sources to include (max :data:`MAX_SOURCES` = 5).
    schema_version : str
        Manifest schema version string.
    """

    display_name: str
    instruction: str = ""
    sources: list[DataSourceSpec] = field(default_factory=list)
    schema_version: str = _DATA_AGENT_SCHEMA


# ---------------------------------------------------------------------------
# Definition builder (AGK-005 part builder)
# ---------------------------------------------------------------------------


def build_definition_parts(spec: DataAgentSpec) -> list[dict[str, str]]:
    """Build the ``InlineBase64`` definition parts for a Fabric Data Agent item.

    Validates the source count cap and source type legality before building.

    Parameters
    ----------
    spec : DataAgentSpec
        The agent specification to encode.

    Returns
    -------
    list[dict[str, str]]
        List of ``{"path": ..., "payload": ..., "payloadType": "InlineBase64"}``
        dicts ready to embed in the Fabric Items API ``definition.parts`` array.

    Raises
    ------
    SourceCapError
        More than :data:`MAX_SOURCES` sources.
    UnsupportedDataSourceType
        A source type is not in the documented enum and not marked
        ``preview=True``.
    """
    if len(spec.sources) > MAX_SOURCES:
        raise SourceCapError(len(spec.sources))

    for src in spec.sources:
        normalised = src.source_type.lower().replace(" ", "")
        if normalised not in _DOCUMENTED_DATASOURCE_TYPES and not src.preview:
            raise UnsupportedDataSourceType(src.source_type)

    parts: list[dict[str, str]] = []

    # 1. data_agent.json — contains only the schema version per official spec
    # https://learn.microsoft.com/rest/api/fabric/articles/item-management/definitions/data-agent-definition
    agent_manifest: dict[str, Any] = {"$schema": spec.schema_version}
    parts.append(_encode_part("Files/Config/data_agent.json", agent_manifest))

    # 2. stage_config.json (draft) — AI instructions for the draft stage
    stage_config: dict[str, Any] = {
        "$schema": _STAGE_CONFIG_SCHEMA,
        "aiInstructions": spec.instruction,
    }
    parts.append(
        _encode_part("Files/Config/draft/stage_config.json", stage_config)
    )

    # 3. Per-source datasource.json + optional fewshots.json
    for src in spec.sources:
        parts.append(_encode_part(src.datasource_path(), src.datasource_payload()))
        if src.few_shots:
            parts.append(_encode_part(src.fewshots_path(), src.fewshots_payload()))

    return parts


def _encode_part(path: str, payload: dict[str, Any]) -> dict[str, str]:
    """Base64-encode *payload* as JSON and return a definition part dict."""
    raw = json.dumps(payload, ensure_ascii=False, indent=2)
    b64 = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    return {
        "path": path,
        "payload": b64,
        "payloadType": "InlineBase64",
    }


# ---------------------------------------------------------------------------
# Upsert result
# ---------------------------------------------------------------------------


@dataclass
class DataAgentUpsertResult:
    """Outcome of a Fabric Data Agent upsert operation.

    Attributes
    ----------
    item_id : str
        Fabric item GUID (or ``"lro:<location>"`` if still provisioning).
    created : bool
        ``True`` if the item was newly created.
    status : str
        ``"created-201"``, ``"created-lro"``, ``"updated"``, or ``"mock"``.
    display_name : str
        The agent display name.
    note : str
        Human-readable status message.
    """

    item_id: str
    created: bool
    status: str
    display_name: str
    note: str = ""


@dataclass
class DataAgentPublishResult:
    """Outcome of promoting a Data Agent staging definition to production."""

    item_id: str
    published_description: str
    status: str = "published"


@dataclass(frozen=True)
class DataAgentStageSnapshot:
    """Decoded draft or published instruction and source selection."""

    stage: Literal["draft", "published"]
    instruction: str
    sources: tuple[dict[str, Any], ...]

    @property
    def instruction_hash(self) -> str:
        return _text_hash(self.instruction)

    @property
    def source_selection_hash(self) -> str:
        return _canonical_hash({
            "sources": [
                _normalized_source_selection(source)
                for source in self.sources
            ]
        })

    @property
    def selected_element_hash(self) -> str:
        return _canonical_hash({
            "elements": [
                {
                    "source_type": str(source.get("type") or ""),
                    "element": element,
                }
                for source in self.sources
                for element in _selected_elements(source)
            ]
        })

    @property
    def selected_element_count(self) -> int:
        return sum(
            len(_selected_elements(source)) for source in self.sources
        )

    @property
    def property_child_count(self) -> int:
        return sum(
            len(_selected_children(element))
            for source in self.sources
            for element in _selected_elements(source)
        )

    @property
    def agent_schema_sidecar(self) -> dict[str, Any] | None:
        sidecars = [
            metadata.get("fabricKgAgentSchema")
            for source in self.sources
            if isinstance((metadata := source.get("metadata")), dict)
            and isinstance(metadata.get("fabricKgAgentSchema"), dict)
        ]
        if not sidecars:
            return None
        first = sidecars[0]
        if any(sidecar != first for sidecar in sidecars[1:]):
            raise DataAgentDefinitionError(
                "Data Agent sources contain inconsistent semantic sidecars."
            )
        return first

    @property
    def agent_schema_reference(self) -> dict[str, str] | None:
        """Return the compact public-definition semantic metadata reference."""
        keys = (
            "fabricKgAgentSchemaHash",
            "fabricKgSemanticModelManifestHash",
            "fabricKgPersistedProjectionReceiptHash",
            "fabricKgOntologyItemId",
            "fabricKgGraphModelId",
            "fabricKgPropertyChildCoverage",
            "fabricKgExpectedPropertyCount",
        )
        references = [
            {
                key: str(metadata.get(key) or "")
                for key in keys
            }
            for source in self.sources
            if isinstance((metadata := source.get("metadata")), dict)
            and metadata.get("fabricKgAgentSchemaHash")
        ]
        if not references:
            return None
        first = references[0]
        if any(reference != first for reference in references[1:]):
            raise DataAgentDefinitionError(
                "Data Agent sources contain inconsistent semantic metadata "
                "references."
            )
        return first

    @property
    def agent_schema_sidecar_hash(self) -> str | None:
        sidecar = self.agent_schema_sidecar
        if sidecar is not None:
            return _canonical_hash(sidecar)
        reference = self.agent_schema_reference
        if reference is None:
            return None
        return reference["fabricKgAgentSchemaHash"] or None

    def source_receipts(self) -> list[dict[str, Any]]:
        """Return source identities and independently observed selection counts."""
        return [
            {
                "source_type": str(source.get("type") or ""),
                "source_name": str(source.get("_source_name") or ""),
                "workspace_id": str(source.get("workspaceId") or ""),
                "artifact_id": str(source.get("artifactId") or ""),
                "selected_element_count": len(
                    _selected_elements(source)
                ),
                "property_child_count": sum(
                    len(_selected_children(element))
                    for element in _selected_elements(source)
                ),
            }
            for source in self.sources
        ]


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _text_hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _selected_children(element: dict[str, Any]) -> list[dict[str, Any]]:
    children = element.get("children")
    if not isinstance(children, list):
        return []
    return sorted(
        (
            _normalized_data_source_element(child)
            for child in children
            if isinstance(child, dict)
            and child.get("is_selected") is True
        ),
        key=lambda child: str(child.get("id") or ""),
    )


def _normalized_data_source_element(
    element: dict[str, Any],
) -> dict[str, Any]:
    normalized = {
        "id": str(element.get("id") or ""),
        "display_name": str(element.get("display_name") or ""),
        "type": str(element.get("type") or ""),
        "is_selected": element.get("is_selected") is True,
    }
    for key in ("data_type", "description", "index_state"):
        value = element.get(key)
        if value is not None:
            normalized[key] = value
    children = _selected_children(element)
    if children:
        normalized["children"] = children
    return normalized


def _selected_elements(source: dict[str, Any]) -> list[dict[str, Any]]:
    elements = source.get("elements")
    if not isinstance(elements, list):
        return []
    return sorted(
        (
            _normalized_data_source_element(element)
            for element in elements
            if isinstance(element, dict)
            and element.get("is_selected") is True
        ),
        key=lambda element: str(element.get("id") or ""),
    )


def _normalized_source_selection(
    source: dict[str, Any],
) -> dict[str, Any]:
    return {
        "source_type": str(source.get("type") or ""),
        "workspace_id": str(source.get("workspaceId") or ""),
        "artifact_id": str(source.get("artifactId") or ""),
        "display_name": str(source.get("displayName") or ""),
        "metadata": source.get("metadata") or {},
        "elements": _selected_elements(source),
    }


def _decode_part_payload(part: dict[str, Any]) -> dict[str, Any]:
    if part.get("payloadType") != "InlineBase64":
        raise DataAgentDefinitionError(
            f"Definition part {part.get('path')!r} must use InlineBase64."
        )
    try:
        decoded = base64.b64decode(
            str(part.get("payload") or ""),
            validate=True,
        ).decode("utf-8")
        payload = json.loads(decoded)
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise DataAgentDefinitionError(
            f"Could not decode definition part {part.get('path')!r}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise DataAgentDefinitionError(
            f"Definition part {part.get('path')!r} must decode to an object."
        )
    return payload


def decode_stage_snapshot(
    definition: dict[str, Any],
    stage: Literal["draft", "published"],
) -> DataAgentStageSnapshot:
    """Decode one stage from an independently read-back definition."""
    parts = definition.get("parts")
    if not isinstance(parts, list):
        raise DataAgentDefinitionError(
            "Data Agent definition has no parts list."
        )
    stage_path = f"Files/Config/{stage}/stage_config.json"
    instruction: str | None = None
    sources: list[dict[str, Any]] = []
    prefix = f"Files/Config/{stage}/"
    suffix = "/datasource.json"
    for part in parts:
        if not isinstance(part, dict):
            continue
        path = str(part.get("path") or "")
        if path == stage_path:
            payload = _decode_part_payload(part)
            value = payload.get("aiInstructions")
            if not isinstance(value, str) or not value.strip():
                raise DataAgentDefinitionError(
                    f"{stage_path} has no non-empty aiInstructions string."
                )
            instruction = value
            continue
        if not path.startswith(prefix) or not path.endswith(suffix):
            continue
        payload = _decode_part_payload(part)
        source_type = str(payload.get("type") or "")
        directory = path[len(prefix):-len(suffix)]
        name_prefix = f"{source_type}-"
        source_name = (
            directory[len(name_prefix):]
            if source_type and directory.startswith(name_prefix)
            else directory
        )
        payload["_source_name"] = source_name
        sources.append(payload)
    if instruction is None:
        raise DataAgentDefinitionError(
            f"Data Agent definition is missing {stage_path}."
        )
    return DataAgentStageSnapshot(
        stage=stage,
        instruction=instruction,
        sources=tuple(sorted(
            sources,
            key=lambda source: (
                str(source.get("type") or ""),
                str(source.get("_source_name") or ""),
            ),
        )),
    )


def stage_snapshot_from_spec(spec: DataAgentSpec) -> DataAgentStageSnapshot:
    """Build the expected draft snapshot from the compiled Data Agent spec."""
    return decode_stage_snapshot(
        {"parts": build_definition_parts(spec)},
        "draft",
    )


# ---------------------------------------------------------------------------
# FabricDataAgentClient
# ---------------------------------------------------------------------------


class FabricDataAgentClient:
    """Client for idempotent create/update of Fabric Data Agent items.

    Parameters
    ----------
    workspace_id : str
        Fabric workspace GUID.
    transport : HttpTransport
        Injectable transport (use ``FakeTransport`` in tests).
    token : str | None
        Pre-obtained bearer token.
    token_provider : Callable[[], str] | None
        Token factory.  Defaults to ``DefaultAzureCredential`` when both
        *token* and *token_provider* are ``None``.
    lro_timeout_seconds : int
        Maximum seconds to wait for an LRO to complete (default 300).
    lro_poll_interval : int
        Seconds between LRO poll attempts (default 5; overridden by
        ``Retry-After`` header when present).
    """

    def __init__(
        self,
        workspace_id: str,
        transport: HttpTransport,
        token: str | None = None,
        token_provider: Callable[[], str] | None = None,
        lro_timeout_seconds: int = _DEFAULT_LRO_TIMEOUT,
        lro_poll_interval: int = _DEFAULT_LRO_POLL_INTERVAL,
    ) -> None:
        self._ws = workspace_id
        self._transport = transport
        self._token = token
        self._token_provider = token_provider
        self._lro_timeout = lro_timeout_seconds
        self._lro_poll = lro_poll_interval

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_token(self) -> str:
        if self._token:
            return self._token
        if self._token_provider:
            self._token = self._token_provider()
            return self._token
        from azure.identity import DefaultAzureCredential  # noqa: PLC0415

        cred = DefaultAzureCredential()
        self._token = cred.get_token(_FABRIC_TOKEN_SCOPE).token
        return self._token

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
        }

    def _base_url(self) -> str:
        return f"{_FABRIC_API_BASE}/workspaces/{self._ws}"

    # ------------------------------------------------------------------
    # List / get
    # ------------------------------------------------------------------

    def _list_items(self, *, max_pages: int = 100) -> list[dict[str, Any]]:
        """Return all items in the workspace (GET /workspaces/{ws}/items)."""
        url = f"{self._base_url()}/items"
        items: list[dict[str, Any]] = []
        for _page in range(max_pages):
            resp = self._transport.send(
                HttpRequest(
                    method="GET",
                    url=url,
                    headers=self._auth_headers(),
                )
            )
            if resp.status_code >= 400:
                raise HttpError(resp.status_code, resp.body)
            body = resp.body if isinstance(resp.body, dict) else {}
            values = body.get("value", [])
            if not isinstance(values, list):
                raise DataAgentTargetError(
                    "Fabric item listing returned a non-list value."
                )
            items.extend(
                value for value in values if isinstance(value, dict)
            )

            continuation_uri = body.get("continuationUri")
            continuation_token = body.get("continuationToken")
            if not continuation_uri and not continuation_token:
                return items
            if continuation_uri:
                candidate = urljoin(
                    f"{_FABRIC_API_BASE}/",
                    str(continuation_uri),
                )
                if not candidate.startswith(f"{_FABRIC_API_BASE}/"):
                    raise DataAgentTargetError(
                        "Fabric item listing returned an untrusted "
                        "continuation URI."
                    )
                url = candidate
            else:
                url = (
                    f"{self._base_url()}/items?continuationToken="
                    f"{quote(str(continuation_token), safe='')}"
                )

        raise DataAgentTargetError(
            f"Fabric item listing exceeded {max_pages} pages."
        )

    def get_data_agent(self, display_name: str) -> dict[str, Any] | None:
        """Return the existing DataAgent item dict matching *display_name*, or ``None``."""
        items = self._list_items()
        return next(
            (
                it
                for it in items
                if it.get("displayName") == display_name
                and it.get("type") == "DataAgent"
            ),
            None,
        )

    def get_data_agent_by_id(self, item_id: str) -> dict[str, Any] | None:
        """Return the exact configured DataAgent item, never a name fallback."""
        if not item_id:
            raise ValueError("Data Agent item ID must not be empty.")
        return next(
            (
                item
                for item in self._list_items()
                if item.get("id") == item_id
                and item.get("type") == "DataAgent"
            ),
            None,
        )

    def get_definition(self, item_id: str) -> dict[str, Any]:
        """Return the deployed public definition for a Data Agent item."""
        url = (
            f"{self._base_url()}/dataAgents/{item_id}/getDefinition"
        )
        resp = self._transport.send(
            HttpRequest(
                method="POST",
                url=url,
                headers=self._auth_headers(),
                body={},
            )
        )
        if resp.status_code >= 400:
            raise HttpError(resp.status_code, resp.body)
        body = resp.body if isinstance(resp.body, dict) else {}
        if resp.status_code == 202:
            location = (
                resp.headers.get("Location")
                or resp.headers.get("location")
                or ""
            )
            if not location:
                raise DataAgentDefinitionError(
                    "Data Agent getDefinition returned 202 without Location."
                )
            retry_after_text = (
                resp.headers.get("Retry-After")
                or resp.headers.get("retry-after")
                or str(self._lro_poll)
            )
            try:
                retry_after = int(retry_after_text)
            except ValueError:
                retry_after = self._lro_poll
            body = self._poll_lro(location, retry_after)
            definition = self._definition_from_body(body)
            if definition is None:
                result_resp = self._transport.send(
                    HttpRequest(
                        method="GET",
                        url=f"{location.rstrip('/')}/result",
                        headers=self._auth_headers(),
                    )
                )
                if result_resp.status_code >= 400:
                    raise HttpError(
                        result_resp.status_code,
                        result_resp.body,
                    )
                body = (
                    result_resp.body
                    if isinstance(result_resp.body, dict)
                    else {}
                )
        definition = self._definition_from_body(body)
        if definition is None:
            raise DataAgentDefinitionError(
                "Data Agent getDefinition response has no definition."
            )
        return definition

    def get_deployed_instruction(self, item_id: str) -> str:
        """Read the independently observed published instruction."""
        return self.get_stage_snapshot(item_id, "published").instruction

    def get_stage_snapshot(
        self,
        item_id: str,
        stage: Literal["draft", "published"],
    ) -> DataAgentStageSnapshot:
        """Read and decode one persisted Data Agent stage."""
        return decode_stage_snapshot(self.get_definition(item_id), stage)

    def get_stage_snapshots(
        self,
        item_id: str,
    ) -> tuple[DataAgentStageSnapshot, DataAgentStageSnapshot]:
        """Read draft and published stages from the same persisted definition."""
        definition = self.get_definition(item_id)
        return (
            decode_stage_snapshot(definition, "draft"),
            decode_stage_snapshot(definition, "published"),
        )

    @staticmethod
    def _definition_from_body(
        body: dict[str, Any],
    ) -> dict[str, Any] | None:
        definition = body.get("definition")
        if isinstance(definition, dict):
            return definition
        result = body.get("result")
        if isinstance(result, dict):
            definition = result.get("definition")
            if isinstance(definition, dict):
                return definition
        return None

    # ------------------------------------------------------------------
    # LRO polling
    # ------------------------------------------------------------------

    def _poll_lro(
        self,
        operation_url: str,
        retry_after: int,
    ) -> dict[str, Any]:
        """Poll *operation_url* until the LRO completes or times out.

        Parameters
        ----------
        operation_url : str
            The operation status URL from the ``Location`` / ``x-ms-operation-id``
            header.
        retry_after : int
            Initial poll interval in seconds (from ``Retry-After`` header;
            falls back to :attr:`_lro_poll`).

        Returns
        -------
        dict
            The completed operation result body.

        Raises
        ------
        LROTimeoutError
            If the LRO does not complete within :attr:`_lro_timeout` seconds.
        HttpError
            If a poll request returns an error status.
        """
        start = time.monotonic()
        interval = max(1, retry_after)

        while True:
            elapsed = time.monotonic() - start
            if elapsed > self._lro_timeout:
                raise LROTimeoutError(operation_url, elapsed)

            logger.debug(
                "[data_agent] LRO poll %s (elapsed %.1fs)", operation_url, elapsed
            )
            time.sleep(interval)

            resp = self._transport.send(
                HttpRequest(
                    method="GET",
                    url=operation_url,
                    headers=self._auth_headers(),
                )
            )
            if resp.status_code >= 400:
                raise HttpError(resp.status_code, resp.body)

            body = resp.body if isinstance(resp.body, dict) else {}
            status_str = body.get("status", "").lower()

            # Retry-After overrides the default poll interval
            ra = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
            if ra:
                try:
                    interval = int(ra)
                except ValueError:
                    pass

            if status_str in ("succeeded", "completed", ""):
                # Empty status or explicit success → done
                if status_str in ("succeeded", "completed") or resp.status_code == 200:
                    logger.info("[data_agent] LRO completed: %s", operation_url)
                    return body
            elif status_str in ("failed", "canceled", "cancelled"):
                error_detail = body.get("error") or body
                raise HttpError(resp.status_code or 500, error_detail)
            # else: still running — loop

    # ------------------------------------------------------------------
    # Create / update
    # ------------------------------------------------------------------

    def _create(self, spec: DataAgentSpec) -> DataAgentUpsertResult:
        """POST /workspaces/{ws}/dataAgents to create a new item."""
        parts = build_definition_parts(spec)
        payload: dict[str, Any] = {
            "displayName": spec.display_name,
            "definition": {"parts": parts},
        }
        url = f"{self._base_url()}/dataAgents"
        max_retries = 3
        for attempt in range(max_retries):
            resp = self._transport.send(
                HttpRequest(method="POST", url=url, headers=self._auth_headers(), body=payload)
            )
            if resp.status_code == 429:
                ra_str = resp.headers.get("Retry-After") or resp.headers.get("retry-after", "")
                wait = int(ra_str) if ra_str else 30
                logger.warning(
                    "[data_agent] 429 rate-limit on create (attempt %d/%d), "
                    "retrying after %ds",
                    attempt + 1,
                    max_retries,
                    wait,
                )
                if attempt < max_retries - 1:
                    time.sleep(wait)
                    continue
                raise HttpError(429, resp.body)
            break

        if resp.status_code == 201:
            body = resp.body if isinstance(resp.body, dict) else {}
            item_id = str(body.get("id") or "")
            if not item_id:
                created_item = self.get_data_agent(spec.display_name)
                item_id = str(
                    created_item.get("id", "")
                    if isinstance(created_item, dict)
                    else ""
                )
            if not item_id:
                raise DataAgentDefinitionError(
                    "Data Agent create returned HTTP 201 without an item ID, "
                    f"and '{spec.display_name}' could not be resolved."
                )
            logger.info(
                "[data_agent] created '%s' (id=%s, 201 sync)", spec.display_name, item_id
            )
            return DataAgentUpsertResult(
                item_id=item_id,
                created=True,
                status="created-201",
                display_name=spec.display_name,
                note=f"Created DataAgent '{spec.display_name}' (201 sync).",
            )

        if resp.status_code == 202:
            location = resp.headers.get("Location") or resp.headers.get("location", "")
            ra_str = resp.headers.get("Retry-After") or resp.headers.get("retry-after", "")
            retry_after = int(ra_str) if ra_str else self._lro_poll
            logger.info(
                "[data_agent] creating '%s' (202 LRO, location=%s)",
                spec.display_name,
                location,
            )
            lro_result = self._poll_lro(location, retry_after)
            nested_result = lro_result.get("result")
            item_id = str(
                lro_result.get("id")
                or lro_result.get("itemId")
                or (
                    nested_result.get("id")
                    if isinstance(nested_result, dict)
                    else ""
                )
                or (
                    nested_result.get("itemId")
                    if isinstance(nested_result, dict)
                    else ""
                )
                or ""
            )
            if not item_id:
                for attempt in range(3):
                    created_item = self.get_data_agent(spec.display_name)
                    item_id = str(
                        created_item.get("id", "")
                        if isinstance(created_item, dict)
                        else ""
                    )
                    if item_id:
                        break
                    if attempt < 2:
                        time.sleep(max(0, self._lro_poll))
            if not item_id:
                raise DataAgentDefinitionError(
                    "Data Agent create LRO succeeded but returned no item ID, "
                    f"and '{spec.display_name}' could not be resolved in the workspace."
                )
            return DataAgentUpsertResult(
                item_id=item_id,
                created=True,
                status="created-lro",
                display_name=spec.display_name,
                note=f"Created DataAgent '{spec.display_name}' via LRO.",
            )

        raise HttpError(resp.status_code, resp.body)

    def _update(
        self, spec: DataAgentSpec, item_id: str
    ) -> DataAgentUpsertResult:
        """POST the Data Agent-specific updateDefinition endpoint."""
        parts = build_definition_parts(spec)
        payload: dict[str, Any] = {"definition": {"parts": parts}}
        url = (
            f"{self._base_url()}/dataAgents/{item_id}/updateDefinition"
        )
        max_retries = 3
        for attempt in range(max_retries):
            resp = self._transport.send(
                HttpRequest(method="POST", url=url, headers=self._auth_headers(), body=payload)
            )
            if resp.status_code == 429:
                ra_str = resp.headers.get("Retry-After") or resp.headers.get("retry-after", "")
                wait = int(ra_str) if ra_str else 30
                logger.warning(
                    "[data_agent] 429 rate-limit on updateDefinition (attempt %d/%d), "
                    "retrying after %ds",
                    attempt + 1,
                    max_retries,
                    wait,
                )
                if attempt < max_retries - 1:
                    time.sleep(wait)
                    continue
                raise HttpError(429, resp.body)
            break

        if resp.status_code >= 400:
            raise HttpError(resp.status_code, resp.body)

        if resp.status_code == 202:
            location = resp.headers.get("Location") or resp.headers.get("location", "")
            ra_str = resp.headers.get("Retry-After") or resp.headers.get("retry-after", "")
            retry_after = int(ra_str) if ra_str else self._lro_poll
            self._poll_lro(location, retry_after)

        logger.info(
            "[data_agent] updated '%s' (id=%s)", spec.display_name, item_id
        )
        return DataAgentUpsertResult(
            item_id=item_id,
            created=False,
            status="updated",
            display_name=spec.display_name,
            note=f"Updated DataAgent '{spec.display_name}' (id={item_id}).",
        )

    def _delete(self, item_id: str) -> None:
        """Delete one exact Data Agent target before an approved replacement."""
        url = f"{self._base_url()}/dataAgents/{item_id}"
        resp = self._transport.send(
            HttpRequest(
                method="DELETE",
                url=url,
                headers=self._auth_headers(),
            )
        )
        if resp.status_code == 404:
            return
        if resp.status_code in {200, 204}:
            return
        if resp.status_code == 202:
            location = (
                resp.headers.get("Location")
                or resp.headers.get("location")
                or ""
            )
            if not location:
                raise DataAgentTargetError(
                    "Data Agent delete returned 202 without Location."
                )
            retry_after_text = (
                resp.headers.get("Retry-After")
                or resp.headers.get("retry-after")
                or str(self._lro_poll)
            )
            try:
                retry_after = int(retry_after_text)
            except ValueError:
                retry_after = self._lro_poll
            self._poll_lro(location, retry_after)
            return
        raise HttpError(
            resp.status_code,
            resp.body,
            response_headers=resp.headers,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def deploy_target(
        self,
        spec: DataAgentSpec,
        *,
        target_mode: Literal["update", "create", "replace"],
        configured_item_id: str | None = None,
        replace_approved: bool = False,
    ) -> DataAgentUpsertResult:
        """Honor one explicit Data Agent target mode without name fallback."""
        item_id = str(configured_item_id or "").strip()
        if target_mode == "create":
            if item_id:
                raise DataAgentTargetError(
                    "Create mode cannot be used with a configured item ID."
                )
            existing = self.get_data_agent(spec.display_name)
            if existing is not None:
                raise DataAgentTargetError(
                    f"Data Agent '{spec.display_name}' already exists; choose "
                    "update with its exact item ID or approved replace."
                )
            result = self._create(spec)
        elif target_mode in {"update", "replace"}:
            if not item_id:
                raise DataAgentTargetError(
                    f"{target_mode} mode requires an exact configured item ID."
                )
            existing = self.get_data_agent_by_id(item_id)
            if existing is None:
                raise DataAgentTargetError(
                    f"Configured Data Agent item '{item_id}' does not exist "
                    "or is not a DataAgent."
                )
            existing_name = str(existing.get("displayName") or "")
            if existing_name != spec.display_name:
                raise DataAgentTargetError(
                    f"Configured Data Agent '{item_id}' is named "
                    f"'{existing_name}', not '{spec.display_name}'."
                )
            if target_mode == "update":
                result = self._update(spec, item_id)
            else:
                if not replace_approved:
                    raise DataAgentTargetError(
                        "Replace mode requires explicit replacement approval."
                    )
                self._delete(item_id)
                created = self._create(spec)
                if created.item_id == item_id:
                    raise DataAgentTargetError(
                        "Approved replacement reused the deleted item ID; "
                        "Fabric did not create a distinct target."
                    )
                result = DataAgentUpsertResult(
                    item_id=created.item_id,
                    created=True,
                    status="replaced",
                    display_name=created.display_name,
                    note=(
                        f"Replaced DataAgent '{spec.display_name}' "
                        f"({item_id} -> {created.item_id})."
                    ),
                )
        else:
            raise DataAgentTargetError(
                f"Unsupported Data Agent target mode: {target_mode!r}."
            )
        _lin.record(
            operation="fabric_data_agent",
            action=target_mode,
            api_version=_FABRIC_API_VERSION,
            capability_mode="ga",
            resource_name=spec.display_name,
            status=result.status,
            endpoint=self._ws,
            remote_id=result.item_id,
        )
        return result

    def upsert(self, spec: DataAgentSpec) -> DataAgentUpsertResult:
        """Idempotently create or update a Fabric Data Agent item.

        1. Lists workspace items to detect an existing DataAgent with the same
           ``displayName``.
        2. If absent: POST to create (handles 201 sync and 202 LRO).
        3. If present: POST to ``updateDefinition`` (handles 200 and 202 LRO).

        Parameters
        ----------
        spec : DataAgentSpec
            Desired state for the agent.

        Returns
        -------
        DataAgentUpsertResult
            Contains the item GUID and whether the item was newly created.

        Raises
        ------
        SourceCapError
            If ``spec.sources`` exceeds five items.
        UnsupportedDataSourceType
            If a source type is unrecognised and not flagged preview.
        HttpError
            On non-success HTTP responses.
        LROTimeoutError
            If an LRO does not complete within *lro_timeout_seconds*.
        """
        existing = self.get_data_agent(spec.display_name)
        if existing is None:
            result = self._create(spec)
        else:
            result = self._update(spec, existing.get("id", ""))
        _lin.record(
            operation="fabric_data_agent",
            action="upsert",
            api_version=_FABRIC_API_VERSION,
            capability_mode="ga",
            resource_name=spec.display_name,
            status=result.status,
            endpoint=self._ws,
            remote_id=result.item_id or None,
        )
        return result

    def publish(
        self,
        item_id: str,
        *,
        description: str,
    ) -> DataAgentPublishResult:
        """Promote the current staging configuration to the live MCP agent."""
        if not item_id:
            raise ValueError("Data Agent publish requires an item ID.")
        published_description = description.strip()
        if not published_description:
            raise ValueError(
                "Data Agent publish requires a non-empty description."
            )
        url = (
            f"{self._base_url()}/dataAgents/{item_id}/staging/publish"
        )
        max_retries = 3
        for attempt in range(max_retries):
            resp = self._transport.send(
                HttpRequest(
                    method="POST",
                    url=url,
                    headers=self._auth_headers(),
                    body={
                        "publishedDescription": published_description,
                    },
                )
            )
            if resp.status_code != 429:
                break
            retry_after_text = (
                resp.headers.get("Retry-After")
                or resp.headers.get("retry-after")
                or "30"
            )
            try:
                retry_after = max(0, int(retry_after_text))
            except ValueError:
                retry_after = 30
            if attempt == max_retries - 1:
                raise HttpError(
                    resp.status_code,
                    resp.body,
                    response_headers=resp.headers,
                )
            time.sleep(retry_after)
        if resp.status_code >= 400:
            raise HttpError(
                resp.status_code,
                resp.body,
                response_headers=resp.headers,
            )
        if resp.status_code != 200:
            raise HttpError(
                resp.status_code,
                (
                    "Data Agent publish expected HTTP 200 but received "
                    f"{resp.status_code}."
                ),
                response_headers=resp.headers,
            )
        body = resp.body if isinstance(resp.body, dict) else {}
        observed_description = str(
            body.get("publishedDescription")
            or published_description
        )
        _lin.record(
            operation="fabric_data_agent",
            action="publish",
            api_version=_FABRIC_API_VERSION,
            capability_mode="preview",
            resource_name=item_id,
            status="published",
            endpoint=self._ws,
            remote_id=item_id,
        )
        return DataAgentPublishResult(
            item_id=item_id,
            published_description=observed_description,
        )
