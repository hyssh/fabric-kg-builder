"""serving.graph_model — Fabric Graph Model definition builder + REST client.

M6 SRV-006/007 (SPEC-006 §6.4/§10.3)

Documented REST API (Preview, as of 2026-07):
  List:   GET  /v1/workspaces/{workspaceId}/graphModels
    https://learn.microsoft.com/en-us/rest/api/fabric/graphmodel/items/list-graph-models
  Create: POST /v1/workspaces/{workspaceId}/graphModels
    https://learn.microsoft.com/en-us/rest/api/fabric/graphmodel/items/create-graph-model
  Update: POST /v1/workspaces/{workspaceId}/graphModels/{graphModelId}/updateDefinition
    https://learn.microsoft.com/en-us/rest/api/fabric/graphmodel/items/update-graph-model-definition
  Definition schema:
    https://learn.microsoft.com/en-us/rest/api/fabric/articles/item-management/definitions/graph-model-definition
  Execute GQL query (beta):
    POST /v1/workspaces/{workspaceId}/graphModels/{graphModelId}/executeQuery?beta=true
    https://learn.microsoft.com/en-us/rest/api/fabric/graphmodel/items/execute-query(beta)
    https://learn.microsoft.com/en-us/fabric/graph/gql-query-api

These endpoints are PREVIEW.  When GET /graphModels returns 404 the module
returns action: "blocked-manual" with guided portal instructions.

Create request body: {"displayName": name} — do NOT include "type".
Definition format: {"format": "json", "parts": [<InlineBase64 parts>]}.

Part filenames and $schema URLs (per Microsoft Learn docs):
  dataSources.json       -> .../dataSources/1.1.0/schema.json
  graphDefinition.json   -> .../graphDefinition/1.0.0/schema.json
  graphType.json         -> .../graphType/1.0.0/schema.json
  stylingConfiguration   -> .../stylingConfiguration/1.0.0/schema.json
  .platform              -> git-integration platformProperties/2.0.0

dataSources.json content (DeltaTable, abfss:// OneLake path):
  {"$schema": ..., "dataSources": [
    {"name": "...", "type": "DeltaTable", "properties": {"path": "abfss://..."}}]}

graphDefinition.json content (nodeTables/edgeTables, NOT nodeTypes/edgeTypes):
  {"$schema": ..., "nodeTables": [{"id", "nodeTypeAlias", "dataSourceName",
    "propertyMappings": [...], "filter": {...}}],
   "edgeTables": [{"id", "edgeTypeAlias", "dataSourceName",
    "sourceNodeKeyColumns", "destinationNodeKeyColumns", "propertyMappings"}]}

graphType.json content (structural schema with aliases/labels):
  {"$schema": ..., "nodeTypes": [{"alias", "labels", "primaryKeyProperties",
    "properties": [...]}],
   "edgeTypes": [{"alias", "labels", "sourceNodeType": {"alias": ...},
    "destinationNodeType": {"alias": ...}, "properties": [...]}]}

stylingConfiguration.json content (modelLayout with positions/styles/pan/zoom):
  {"$schema": ..., "modelLayout": {"positions": {...}, "styles": {...},
    "pan": {"x": 0, "y": 0}, "zoomLevel": 1}}
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

logger = logging.getLogger(__name__)

_FABRIC_API_BASE = "https://api.fabric.microsoft.com/v1"
_FABRIC_TOKEN_SCOPE = "https://api.fabric.microsoft.com/.default"
_FABRIC_PORTAL_URL = "https://app.fabric.microsoft.com/"

# ── $schema URLs (Microsoft Learn definition docs, 2026-07) ─────────────────
# Source: https://learn.microsoft.com/en-us/rest/api/fabric/articles/item-management/definitions/graph-model-definition
_SCHEMA_DATA_SOURCES = (
    "https://developer.microsoft.com/json-schemas/fabric/item/"
    "graphIndex/definition/dataSources/1.1.0/schema.json"
)
_SCHEMA_GRAPH_DEF = (
    "https://developer.microsoft.com/json-schemas/fabric/item/"
    "graphIndex/definition/graphDefinition/1.0.0/schema.json"
)
_SCHEMA_GRAPH_TYPE = (
    "https://developer.microsoft.com/json-schemas/fabric/item/"
    "graphIndex/definition/graphType/1.0.0/schema.json"
)
_SCHEMA_STYLING = (
    "https://developer.microsoft.com/json-schemas/fabric/item/"
    "graphIndex/definition/stylingConfiguration/1.0.0/schema.json"
)
_SCHEMA_PLATFORM = (
    "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/"
    "platformProperties/2.0.0/schema.json"
)

# OneLake DeltaTable path template
# Source: dataSources.json example in Microsoft Learn definition docs
_ONELAKE_PATH_TEMPLATE = (
    "abfss://{workspace_id}@onelake.dfs.fabric.microsoft.com"
    "/{lakehouse_item_id}/Tables/{table_name}"
)


# ---------------------------------------------------------------------------
# Transport protocol
# ---------------------------------------------------------------------------


class GraphModelTransport(Protocol):
    def get(self, url: str, headers: dict[str, str]) -> "_GMResponse":
        ...

    def post(self, url: str, headers: dict[str, str], json: dict) -> "_GMResponse":
        ...

    def delete(self, url: str, headers: dict[str, str]) -> "_GMResponse":
        ...


class _GMResponse:
    def __init__(self, status_code: int, body: Any = None, headers: dict | None = None) -> None:
        self.status_code = status_code
        self.body = body
        self.headers: dict[str, str] = headers or {}

    @property
    def ok(self) -> bool:
        return self.status_code < 400


class FakeGraphModelTransport:
    """In-memory fake transport for testing.

    ``graph_model_capability`` defaults to False (Preview absent — the safe default).
    Set to True in tests that exercise the automation path.

    API shape matches documented contract:
      GET  /v1/workspaces/{ws}/graphModels -> 200 {"value": [...]} or 404
      POST /v1/workspaces/{ws}/graphModels -> 201 item (no "type" in request)
      POST /v1/workspaces/{ws}/graphModels/{id}/updateDefinition -> 200
    """

    def __init__(self, graph_model_capability: bool = False) -> None:
        self._items: dict[str, dict[str, Any]] = {}  # displayName -> item
        self._call_log: list[tuple[str, str]] = []
        self._stored_parts: list[dict[str, Any]] = []
        self.graph_model_capability = graph_model_capability
        self.force_create_202: bool = False
        self.force_update_202: bool = False
        # GQL query configuration
        self.gql_responses: dict[str, dict[str, Any]] = {}  # query_fragment -> body
        self.gql_default_count: int = 5
        self.force_gql_429: bool = False

    def get(self, url: str, headers: dict[str, str]) -> _GMResponse:
        self._call_log.append(("GET", url))
        if "/operations/" in url:
            # Extract item_id from Location "/operations/op-{item_id}" pattern (create LRO)
            op_segment = url.split("/operations/")[-1].rstrip("/")
            if op_segment.startswith("op-"):
                item_id_from_op = op_segment[3:]
                return _GMResponse(200, {"status": "Succeeded", "result": {"id": item_id_from_op}})
            # Generic LRO (updateDefinition, getDefinition)
            return _GMResponse(200, {"status": "Succeeded"})
        if "/graphModels" in url and "/updateDefinition" not in url:
            if not self.graph_model_capability:
                return _GMResponse(404, {"error": {"code": "FeatureNotAvailable"}})
            return _GMResponse(200, {"value": list(self._items.values())})
        return _GMResponse(200, {})

    def post(self, url: str, headers: dict[str, str], json_body: dict) -> _GMResponse:
        self._call_log.append(("POST", url))
        # executeQuery: POST /graphModels/{id}/executeQuery?beta=true
        if "/graphModels/" in url and "/executeQuery" in url:
            if self.force_gql_429:
                return _GMResponse(429, {}, {"Retry-After": "30"})
            # Require beta=true parameter (case-insensitive)
            if "beta=true" not in url.lower():
                return _GMResponse(
                    400,
                    {"error": {"code": "BadRequest", "message": "beta=true required for executeQuery"}},
                )
            query = json_body.get("query", "")
            for fragment, resp in self.gql_responses.items():
                if fragment in query:
                    return _GMResponse(200, resp)
            # Default success with gql_default_count
            return _GMResponse(200, {
                "status": {
                    "code": "00000",
                    "description": "note: successful completion",
                    "diagnostics": {"OPERATION": "query"},
                },
                "result": {
                    "kind": "TABLE",
                    "columns": [{"name": "count", "gqlType": "INT64", "jsonType": "number"}],
                    "data": [{"count": self.gql_default_count}],
                },
            })
        # getDefinition: POST /graphModels/{id}/getDefinition
        if "/graphModels/" in url and "/getDefinition" in url:
            if not self.graph_model_capability:
                return _GMResponse(404, {"error": {"code": "FeatureNotAvailable"}})
            return _GMResponse(200, {"definition": {"format": "json", "parts": self._stored_parts}})
        # updateDefinition uses /graphModels/{id}/updateDefinition
        if "/graphModels/" in url and "/updateDefinition" in url:
            if "definition" in json_body:
                self._stored_parts = list(json_body["definition"].get("parts", []))
            if self.force_update_202:
                return _GMResponse(202, {}, {"Location": "/operations/update-op-1", "Retry-After": "1"})
            return _GMResponse(200, {})
        # Create graphModel — body must NOT contain "type"
        if "/graphModels" in url:
            if not self.graph_model_capability:
                return _GMResponse(404, {"error": {"code": "FeatureNotAvailable"}})
            if "type" in json_body:
                return _GMResponse(
                    400,
                    {"error": {"message": "CreateGraphModelRequest does not accept 'type'"}},
                )
            name = json_body.get("displayName", "unnamed")
            item_id = str(uuid.uuid4())
            item = {
                "id": item_id,
                "displayName": name,
                "type": "GraphModel",
                "workspaceId": "fake-workspace",
            }
            self._items[name] = item
            if self.force_create_202:
                return _GMResponse(202, {}, {"Location": f"/operations/op-{item_id}", "Retry-After": "1"})
            return _GMResponse(201, item)
        return _GMResponse(200, {})

    def delete(self, url: str, headers: dict[str, str]) -> _GMResponse:
        self._call_log.append(("DELETE", url))
        item_id = url.rstrip("/").split("/")[-1]
        for name, item in list(self._items.items()):
            if item.get("id") == item_id:
                del self._items[name]
                return _GMResponse(204, {})
        return _GMResponse(404, {})


# ---------------------------------------------------------------------------
# Real transport
# ---------------------------------------------------------------------------


class _RequestsGMTransport:
    def __init__(self, timeout: int = 60) -> None:
        self._timeout = timeout

    def get(self, url: str, headers: dict[str, str]) -> _GMResponse:
        import requests  # type: ignore[import]

        r = requests.get(url, headers=headers, timeout=self._timeout)
        try:
            body = r.json()
        except ValueError:
            body = r.text
        return _GMResponse(r.status_code, body, dict(r.headers))

    def post(self, url: str, headers: dict[str, str], json: dict) -> _GMResponse:
        import requests  # type: ignore[import]

        r = requests.post(url, headers=headers, json=json, timeout=self._timeout)
        try:
            body = r.json()
        except ValueError:
            body = r.text
        return _GMResponse(r.status_code, body, dict(r.headers))

    def delete(self, url: str, headers: dict[str, str]) -> _GMResponse:
        import requests  # type: ignore[import]

        r = requests.delete(url, headers=headers, timeout=self._timeout)
        try:
            body = r.json()
        except ValueError:
            body = r.text
        return _GMResponse(r.status_code, body, dict(r.headers))


# ---------------------------------------------------------------------------
# OneLake path helper
# ---------------------------------------------------------------------------


def onelake_abfss_path(
    workspace_id: str,
    lakehouse_item_id: str,
    table_name: str,
    schema: str = "dbo",
) -> str:
    """Return an abfss:// DeltaTable path for a OneLake table.

    Format per Microsoft Learn dataSources.json example:
      abfss://{workspaceId}@onelake.dfs.fabric.microsoft.com/{lakehouseId}/Tables/{schema}/{tableName}
    """
    return _ONELAKE_PATH_TEMPLATE.format(
        workspace_id=workspace_id,
        lakehouse_item_id=lakehouse_item_id,
        table_name=f"{schema}/{table_name}",
    )


# ---------------------------------------------------------------------------
# Definition parts builder (SRV-006)
# ---------------------------------------------------------------------------


def build_graph_model_parts(
    *,
    entity_types: list[str],
    relationship_pairs: Optional[list[dict[str, Any]]] = None,
    node_labels: Optional[dict[str, str]] = None,
    workspace_id: str = "",
    lakehouse_item_id: str = "",
    schema: str = "dbo",
    model_name: str = "kg_graph_model",
    entity_table: str = "entities",
    relationship_table: str = "relationships",
    include_semantic_properties: bool = False,
    node_table_bindings: Optional[dict[str, dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Build Graph Model definition parts from observed entity/relationship types.

    Follows the exact documented Fabric definition schema (Microsoft Learn, 2026-07).
    Never samples or assumes defaults.

    Internal format: [{"path": str, "payload_json": dict}]
    Call encode_parts_for_api(parts) to get InlineBase64 API format.

    dataSources: one per canonical or manifest-owned materialization table.
    graphType:   nodeTypes use alias="{et}_nodeType"; edgeTypes reference source/dest
                 node aliases.
    graphDefinition: nodeTables with nodeTypeAlias + filter on entity_type;
                     edgeTables with edgeTypeAlias + sourceNodeKeyColumns/destinationNodeKeyColumns.
    stylingConfiguration: modelLayout.positions/styles keyed by alias; pan/zoomLevel defaults.
    """
    relationship_pairs = relationship_pairs or []
    node_labels = node_labels or {}
    has_manifest_bindings = node_table_bindings is not None
    node_table_bindings = node_table_bindings or {}
    node_property_names = [
        "entity_id", "display_name", "description", "entity_type", "canonical_key",
        "source_file_id", "project_id", "asset_id", "asset_version_id", "run_id",
        "parent_record_id", "source_locator_json", "schema_version", "domain_hash",
    ]
    edge_property_names = [
        "relationship_id", "relationship_type", "evidence_id", "properties_json",
        "confidence", "content_hash", "project_id", "asset_id", "asset_version_id",
        "run_id", "parent_record_id", "source_locator_json", "schema_version",
        "domain_hash",
    ]
    if include_semantic_properties:
        node_property_names.extend([
            "aliases_json", "action", "status", "event_date",
            "evidence_ids_json", "citation_json",
        ])
        edge_property_names.extend([
            "original_relationship_type", "evidence_ids_json", "citation_json",
            "assertion_status", "event_date",
        ])
    def graph_label(raw_label: str, used_labels: set[str]) -> str:
        """Return a Fabric-valid, collision-free graph label."""
        label = re.sub(r"[^A-Za-z0-9_]", "_", raw_label).strip("_") or "GraphItem"
        if label[0].isdigit():
            label = f"GraphItem_{label}"
        candidate = label
        suffix = 2
        while candidate in used_labels:
            candidate = f"{label}_{suffix}"
            suffix += 1
        used_labels.add(candidate)
        return candidate

    # ── dataSources.json ─────────────────────────────────────────────────────
    # Each Delta table is relative to the Lakehouse item reference. Fabric's
    # current Graph Model schema requires this instead of an abfss URI.
    lakehouse_reference = "lakehouse"
    if has_manifest_bindings:
        table_names = {
            str(
                node_table_bindings.get(entity_type, {}).get(
                    "table",
                    entity_table,
                )
            )
            for entity_type in entity_types
        }
        table_names.update(
            str(pair.get("table") or relationship_table)
            for pair in relationship_pairs
        )
    else:
        table_names = {entity_table, relationship_table}
    data_source_by_table: dict[str, str] = {}
    used_data_source_names: set[str] = set()
    for table_name in sorted(table_names):
        seed = _graph_alias(f"{table_name}_table")
        candidate = seed
        suffix = 2
        while candidate in used_data_source_names:
            candidate = f"{seed}_{suffix}"
            suffix += 1
        used_data_source_names.add(candidate)
        data_source_by_table[table_name] = candidate
    data_sources: list[dict[str, Any]] = [
        {
            "name": data_source_by_table[table_name],
            "type": "DeltaTable",
            "properties": {
                "referenceName": lakehouse_reference,
                "path": f"Tables/{schema}/{table_name}",
            },
        }
        for table_name in sorted(data_source_by_table)
    ]

    # ── graphType.json — structural schema ───────────────────────────────────
    node_type_aliases: dict[str, str] = {}
    node_type_labels: dict[str, str] = {}
    used_labels: set[str] = set()
    graph_node_types: list[dict[str, Any]] = []
    for et in entity_types:
        binding = node_table_bindings.get(et, {})
        property_columns = list(
            dict.fromkeys(
                binding.get("property_columns") or node_property_names
            )
        )
        entity_id_column = str(
            binding.get("entity_id_column") or "entity_id"
        )
        if entity_id_column not in property_columns:
            property_columns.insert(0, entity_id_column)
        requested_alias = binding.get("node_type_alias")
        alias = _graph_alias(
            str(requested_alias) if requested_alias else f"{et}_nodeType"
        )
        if requested_alias and alias != requested_alias:
            raise ValueError(
                f"Invalid contract-owned Graph node alias '{requested_alias}'."
            )
        node_type_aliases[et] = alias
        requested_label = node_labels.get(et)
        if requested_label:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", requested_label):
                raise ValueError(
                    f"Invalid contract-owned Graph node label '{requested_label}'."
                )
            if requested_label in used_labels:
                raise ValueError(
                    f"Duplicate contract-owned Graph label '{requested_label}'."
                )
            used_labels.add(requested_label)
            node_type_labels[et] = requested_label
        else:
            node_type_labels[et] = graph_label(et, used_labels)
        graph_node_types.append({
            "alias": alias,
            "labels": [node_type_labels[et]],
            "primaryKeyProperties": [entity_id_column],
            "properties": [
                {"name": name, "type": "STRING"}
                for name in property_columns
            ],
        })

    relationship_name_counts: dict[str, int] = {}
    for pair in relationship_pairs:
        pair_name = str(pair.get("name", ""))
        relationship_name_counts[pair_name] = (
            relationship_name_counts.get(pair_name, 0) + 1
        )

    edge_type_aliases: dict[tuple[str, str, str], str] = {}
    graph_edge_types: list[dict[str, Any]] = []
    for rp in relationship_pairs:
        src = rp.get("source_type", "")
        tgt = rp.get("target_type", "")
        name = rp.get("name", f"{src}_to_{tgt}")
        if src not in node_type_aliases or tgt not in node_type_aliases:
            logger.warning(
                "[graph_model] Skipping edge '%s': endpoint types %r/%r not in map",
                name, src, tgt,
            )
            continue
        pair_key = (name, src, tgt)
        alias_seed = (
            f"{src}_{name}_{tgt}_edgeType"
            if relationship_name_counts.get(name, 0) > 1
            else f"{name}_edgeType"
        )
        requested_alias = rp.get("graph_alias")
        alias = _graph_alias(
            str(requested_alias) if requested_alias else alias_seed
        )
        if requested_alias and alias != requested_alias:
            raise ValueError(
                f"Invalid contract-owned Graph edge alias '{requested_alias}'."
            )
        edge_type_aliases[pair_key] = alias
        label_seed = (
            f"{src}_{name}_{tgt}"
            if relationship_name_counts.get(name, 0) > 1
            else str(name)
        )
        requested_label = rp.get("graph_label")
        if requested_label:
            requested_label = str(requested_label)
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", requested_label):
                raise ValueError(
                    f"Invalid contract-owned Graph edge label '{requested_label}'."
                )
            if requested_label in used_labels:
                raise ValueError(
                    f"Duplicate contract-owned Graph label '{requested_label}'."
                )
            used_labels.add(requested_label)
            edge_label = requested_label
        else:
            edge_label = graph_label(label_seed, used_labels)
        property_columns = list(
            dict.fromkeys(rp.get("property_columns") or edge_property_names)
        )
        graph_edge_types.append({
            "alias": alias,
            "labels": [edge_label],
            "sourceNodeType": {"alias": node_type_aliases[src]},
            "destinationNodeType": {"alias": node_type_aliases[tgt]},
            "properties": [
                {
                    "name": name,
                    "type": "FLOAT" if name == "confidence" else "STRING",
                }
                for name in property_columns
            ],
        })

    # ── graphDefinition.json — data mapping ──────────────────────────────────
    node_tables: list[dict[str, Any]] = []
    for et in entity_types:
        binding = node_table_bindings.get(et, {})
        table_name = str(binding.get("table") or entity_table)
        property_columns = list(
            dict.fromkeys(
                binding.get("property_columns") or node_property_names
            )
        )
        node_table = {
            "id": _stable_id(f"graphmodel:nodetable:{et}"),
            "nodeTypeAlias": node_type_aliases[et],
            "dataSourceName": data_source_by_table[table_name],
            "propertyMappings": [
                {"propertyName": name, "sourceColumn": name}
                for name in property_columns
            ],
        }
        filter_column = binding.get("filter_column")
        if filter_column:
            node_table["filter"] = {
                "operator": "Equal",
                "columnName": str(filter_column),
                "value": binding.get("filter_value"),
            }
        node_tables.append(node_table)

    edge_tables: list[dict[str, Any]] = []
    for rp in relationship_pairs:
        name = rp.get("name", "")
        src = rp.get("source_type", "")
        tgt = rp.get("target_type", "")
        pair_key = (name, src, tgt)
        if pair_key not in edge_type_aliases:
            continue
        table_name = str(
            (rp.get("table") if has_manifest_bindings else None)
            or relationship_table
        )
        property_columns = list(
            dict.fromkeys(rp.get("property_columns") or edge_property_names)
        )
        edge_table = {
            "id": _stable_id(f"graphmodel:edgetable:{src}:{name}:{tgt}"),
            "edgeTypeAlias": edge_type_aliases[pair_key],
            "dataSourceName": data_source_by_table[table_name],
            "sourceNodeKeyColumns": [
                str(rp.get("source_entity_id_column") or "source_entity_id")
            ],
            "destinationNodeKeyColumns": [
                str(rp.get("target_entity_id_column") or "target_entity_id")
            ],
            "propertyMappings": [
                {"propertyName": name, "sourceColumn": name}
                for name in property_columns
            ],
        }
        filter_column = rp.get("type_filter_column")
        if filter_column:
            edge_table["filter"] = {
                "operator": "Equal",
                "columnName": str(filter_column),
                "value": rp.get("type_filter_value"),
            }
        edge_tables.append(edge_table)

    # ── stylingConfiguration.json ─────────────────────────────────────────────
    positions: dict[str, dict[str, int]] = {}
    styles: dict[str, dict[str, int]] = {}
    for i, et in enumerate(entity_types):
        alias = node_type_aliases[et]
        positions[alias] = {"x": (i % 4) * 200, "y": (i // 4) * 150}
        styles[alias] = {"size": 30}
    for rp in relationship_pairs:
        rname = rp.get("name", "")
        pair_key = (
            rname,
            rp.get("source_type", ""),
            rp.get("target_type", ""),
        )
        if pair_key in edge_type_aliases:
            styles[edge_type_aliases[pair_key]] = {"size": 20}

    parts: list[dict[str, Any]] = [
        {
            "path": "dataSources.json",
            "payload_json": {
                "$schema": _SCHEMA_DATA_SOURCES,
                "itemReferences": [
                    {
                        "name": lakehouse_reference,
                        "item": {
                            "workspaceId": workspace_id,
                            "itemId": lakehouse_item_id,
                        },
                    }
                ],
                "dataSources": data_sources,
            },
        },
        {
            "path": "graphType.json",
            "payload_json": {
                "$schema": _SCHEMA_GRAPH_TYPE,
                "nodeTypes": graph_node_types,
                "edgeTypes": graph_edge_types,
            },
        },
        {
            "path": "graphDefinition.json",
            "payload_json": {
                "$schema": _SCHEMA_GRAPH_DEF,
                "nodeTables": node_tables,
                "edgeTables": edge_tables,
            },
        },
        {
            "path": "stylingConfiguration.json",
            "payload_json": {
                "$schema": _SCHEMA_STYLING,
                "modelLayout": {
                    "positions": positions,
                    "styles": styles,
                    "pan": {"x": 0, "y": 0},
                    "zoomLevel": 1,
                },
            },
        },
        {
            "path": ".platform",
            "payload_json": {
                "$schema": _SCHEMA_PLATFORM,
                "metadata": {
                    "type": "GraphModel",
                    "displayName": model_name,
                },
                "config": {
                    "version": "2.0",
                    "logicalId": "00000000-0000-0000-0000-000000000000",
                },
            },
        },
    ]
    return parts


def encode_parts_for_api(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Encode internal parts (payload_json dicts) to InlineBase64 API format.

    Output shape per Fabric REST API docs:
      [{"path": "dataSources.json", "payload": "<base64>", "payloadType": "InlineBase64"}, ...]
    """
    encoded = []
    for part in parts:
        raw_json = json.dumps(part["payload_json"], ensure_ascii=False)
        b64 = base64.b64encode(raw_json.encode("utf-8")).decode("ascii")
        encoded.append({
            "path": part["path"],
            "payload": b64,
            "payloadType": "InlineBase64",
        })
    return encoded


def extract_entity_types_from_parquet(entities_rows: list[dict[str, Any]]) -> list[str]:
    """Extract distinct entity type names from observed entity rows (first-seen order)."""
    seen: dict[str, None] = {}
    for row in entities_rows:
        et = row.get("entity_type", "")
        if et:
            seen[et] = None
    return list(seen.keys())


def extract_relationship_pairs_from_parquet(
    relationships_rows: list[dict[str, Any]],
    entities_by_id: dict[str, dict[str, Any]],
    *,
    min_pair_count: int = 1,
    max_pairs: int | None = None,
) -> list[dict[str, Any]]:
    """Extract frequent (verb, source-type, target-type) graph edge bindings."""
    counts: dict[tuple[str, str, str], int] = {}
    order: list[tuple[str, str, str]] = []
    for row in relationships_rows:
        rt = row.get("relationship_type", "")
        src_id = row.get("source_entity_id", "")
        tgt_id = row.get("target_entity_id", "")
        src_type = (entities_by_id.get(src_id) or {}).get("entity_type", "")
        tgt_type = (entities_by_id.get(tgt_id) or {}).get("entity_type", "")
        if not (rt and src_type and tgt_type):
            continue
        key = (rt, src_type, tgt_type)
        if key not in counts:
            order.append(key)
            counts[key] = 0
        counts[key] += 1
    selected = [key for key in order if counts[key] >= min_pair_count]
    if max_pairs is not None:
        selected = sorted(selected, key=lambda key: (-counts[key], order.index(key)))[:max_pairs]
    return [
        {"name": name, "source_type": source_type, "target_type": target_type}
        for name, source_type, target_type in selected
    ]


# ---------------------------------------------------------------------------
# Mapping artifact (SRV-006 — always available, no API required)
# ---------------------------------------------------------------------------


def write_graph_mapping_artifact(
    output_path: "str | Path",
    parts: list[dict[str, Any]],
    *,
    workspace_id: str = "",
    lakehouse_item_id: str = "",
    schema: str = "dbo",
    model_name: str = "kg_graph_model",
    artifact_version: str = "1",
) -> Path:
    """Write domain-derived mapping artifact as a versioned JSON file.

    Stores unencoded payload_json content (human-readable).
    NOT the InlineBase64 API payload — call encode_parts_for_api() for that.

    REST API reference in artifact:
      https://learn.microsoft.com/en-us/rest/api/fabric/graphmodel/items/create-graph-model
    """
    out = Path(output_path)
    if out.is_dir() or not out.suffix:
        out.mkdir(parents=True, exist_ok=True)
        out = out / f"graph_mapping_v{artifact_version}.json"
    else:
        out.parent.mkdir(parents=True, exist_ok=True)

    artifact = {
        "_schema": "fabric-kg-graph-mapping/1.0",
        "_note": (
            "Domain-derived versioned mapping artifact (human-readable). "
            "NOT the InlineBase64 API payload. "
            "Call encode_parts_for_api() to produce the API payload."
        ),
        "_api_docs": {
            "create": "https://learn.microsoft.com/en-us/rest/api/fabric/graphmodel/items/create-graph-model",
            "list": "https://learn.microsoft.com/en-us/rest/api/fabric/graphmodel/items/list-graph-models",
            "update_definition": (
                "https://learn.microsoft.com/en-us/rest/api/fabric/"
                "graphmodel/items/update-graph-model-definition"
            ),
            "definition_schema": (
                "https://learn.microsoft.com/en-us/rest/api/fabric/articles/"
                "item-management/definitions/graph-model-definition"
            ),
        },
        "model_name": model_name,
        "artifact_version": artifact_version,
        "workspace_id": workspace_id,
        "lakehouse_item_id": lakehouse_item_id,
        "schema": schema,
        "parts": parts,
    }
    out.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("[graph_model] Wrote mapping artifact: %s", out)
    return out


# ---------------------------------------------------------------------------
# Transport helpers
# ---------------------------------------------------------------------------


def _make_headers(token_provider: Optional[Callable[[], str]]) -> dict[str, str]:
    """Build HTTP headers; include Bearer auth when token_provider is given."""
    if token_provider is not None:
        tok = token_provider()
        return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    return {"Content-Type": "application/json"}


def _poll_lro(
    tp: GraphModelTransport,
    headers: dict[str, str],
    location: str,
    *,
    max_retries: int = 10,
    initial_retry_after: int = 5,
    _sleep: Optional[Callable[[float], None]] = None,
) -> dict[str, Any]:
    """Poll a Fabric LRO until Succeeded or Failed.

    Per Fabric docs: GET {Location} returns {"status": "Running"|"Succeeded"|"Failed", ...}
    On Succeeded: body may contain {"result": {"id": ...}} for create operations.
    On Failed: body may contain {"error": {...}}.

    Raises RuntimeError on Failed, non-2xx, or max_retries exceeded.
    The injectable ``_sleep`` parameter allows tests to capture/skip real waits.
    """
    import time

    sleep_fn = _sleep if _sleep is not None else time.sleep
    wait = float(initial_retry_after)
    for attempt in range(max_retries):
        sleep_fn(wait)
        resp = tp.get(location, headers)
        if resp.status_code == 429:
            ra = resp.headers.get("Retry-After", str(initial_retry_after))
            try:
                wait = float(ra)
            except ValueError:
                wait = float(initial_retry_after)
            continue
        if not resp.ok:
            raise RuntimeError(
                f"LRO polling failed at attempt {attempt}: "
                f"HTTP {resp.status_code}: {resp.body}"
            )
        body = resp.body if isinstance(resp.body, dict) else {}
        status = body.get("status", "")
        if status == "Succeeded":
            return body
        if status == "Failed":
            raise RuntimeError(f"LRO operation failed: {body.get('error', body)}")
        # Running / NotStarted — honour Retry-After if present
        ra = resp.headers.get("Retry-After", "")
        if ra:
            try:
                wait = float(ra)
            except ValueError:
                pass
    raise RuntimeError(f"LRO did not complete after {max_retries} retries: {location!r}")


# ---------------------------------------------------------------------------
# create_or_get_graph_model — documented Preview REST API
# ---------------------------------------------------------------------------


def create_or_get_graph_model(
    workspace_id: str,
    name: str,
    parts: list[dict[str, Any]],
    *,
    graph_model_id: Optional[str] = None,
    token_provider: Optional[Callable[[], str]] = None,
    transport: Optional[GraphModelTransport] = None,
    lro_max_retries: int = 10,
    lro_initial_retry_after: int = 5,
    _lro_sleep: Optional[Callable[[float], None]] = None,
    _create_retry_timeout_s: float = 300,
    _create_retry_sleep: Optional[Callable[[float], None]] = None,
) -> dict[str, Any]:
    """Create or reuse a Fabric GraphModel; fall back to guided manual action.

    SPEC-006 §6.4/§10.3 + documented Preview REST API (Microsoft Learn, 2026-07):

    1. GET /v1/workspaces/{ws}/graphModels
       Capability discovery + list existing items.
       404 -> FeatureNotAvailable -> return action: "blocked-manual".

    2. Reuse existing item if displayName matches.

    3. Create: POST /v1/workspaces/{ws}/graphModels
       Body: {"displayName": name}  — do NOT include "type" (contract violation).
       Source: https://learn.microsoft.com/en-us/rest/api/fabric/graphmodel/items/create-graph-model

    4. Update definition: POST /v1/workspaces/{ws}/graphModels/{id}/updateDefinition?updateMetadata=true
       Body: {"definition": {"format": "json", "parts": [<InlineBase64>]}}
       Source: https://learn.microsoft.com/en-us/rest/api/fabric/graphmodel/items/update-graph-model-definition
    """
    tp = transport or _RequestsGMTransport()
    hdr = _make_headers(token_provider)

    # A configured ID is authoritative.  Do not list by name or create another
    # preview Graph Model when a deployment has already selected its target.
    item_id: Optional[str] = graph_model_id
    action = "configured" if graph_model_id else "reused"
    capability_state = "configured" if graph_model_id else "available"
    if not item_id:
        list_url = f"{_FABRIC_API_BASE}/workspaces/{workspace_id}/graphModels"
        cap_resp = tp.get(list_url, hdr)
        if cap_resp.status_code == 429:
            raise RuntimeError(
                f"Rate limited listing GraphModels for workspace '{workspace_id}' (429). "
                f"Retry-After: {cap_resp.headers.get('Retry-After', '60')}s"
            )
        if cap_resp.status_code == 404:
            return {
                "item_id": None, "status": "capability-absent",
                "capability_state": "absent", "action": "blocked-manual",
                "lro_location": None, "parts_count": len(parts),
                "note": (
                    f"GraphModel Preview unavailable for workspace {workspace_id!r}. "
                    f"Create '{name}' in Fabric and use graph_mapping_v*.json."
                ),
            }
        existing_items = (
            cap_resp.body.get("value", [])
            if cap_resp.ok and isinstance(cap_resp.body, dict) else []
        )
        existing = next((i for i in existing_items if i.get("displayName") == name), None)
        if existing:
            item_id = existing["id"]
            logger.info("[graph_model] Reusing GraphModel '%s' id=%s", name, item_id)
        else:
            retry_started = time.monotonic()
            retry_sleep = _create_retry_sleep or time.sleep
            while True:
                create_resp = tp.post(
                    list_url, hdr, {"displayName": name}
                )
                body = create_resp.body if isinstance(create_resp.body, dict) else {}
                error = body.get("error", {})
                error_code = str(
                    body.get("errorCode")
                    or (
                        error.get("errorCode", error.get("code", ""))
                        if isinstance(error, dict) else ""
                    )
                )
                if (
                    create_resp.status_code != 409
                    or error_code != "ItemDisplayNameNotAvailableYet"
                ):
                    break
                if time.monotonic() - retry_started >= _create_retry_timeout_s:
                    raise RuntimeError(
                        f"Timed out after {_create_retry_timeout_s:g}s waiting for "
                        f"GraphModel name '{name}' to become available."
                    )
                retry_after = create_resp.headers.get("Retry-After", "5")
                try:
                    wait = max(0.0, float(retry_after))
                except ValueError:
                    wait = 5.0
                logger.info(
                    "[graph_model] GraphModel name '%s' is pending deletion; "
                    "retrying create in %gs.",
                    name,
                    wait,
                )
                retry_sleep(wait)
            if create_resp.status_code == 201:
                item_id = (create_resp.body or {}).get("id", "")
                action = "created"
            elif create_resp.status_code == 202:
                succeeded = _poll_lro(
                    tp, hdr, create_resp.headers.get("Location", ""),
                    max_retries=lro_max_retries,
                    initial_retry_after=int(
                        create_resp.headers.get("Retry-After", str(lro_initial_retry_after))
                    ),
                    _sleep=_lro_sleep,
                )
                item_id = (succeeded.get("result") or {}).get("id", "")
                action = "created"
            else:
                raise RuntimeError(
                    f"Failed to create GraphModel '{name}': "
                    f"HTTP {create_resp.status_code}: {create_resp.body}"
                )
            if not item_id:
                # Some Fabric Preview responses accept the create but omit the
                # item ID; recover it from the authoritative collection.
                resolve_resp = tp.get(list_url, hdr)
                resolved_items = (
                    resolve_resp.body.get("value", [])
                    if resolve_resp.ok and isinstance(resolve_resp.body, dict)
                    else []
                )
                resolved = next(
                    (
                        item for item in resolved_items
                        if item.get("displayName") == name
                    ),
                    None,
                )
                item_id = str((resolved or {}).get("id", ""))
            if not item_id:
                raise RuntimeError(
                    f"GraphModel '{name}' creation returned no item ID and "
                    "the item could not be resolved from the Graph Models list."
                )

    # ── Step 4: Update definition ────────────────────────────────────────────
    # Source: https://learn.microsoft.com/en-us/rest/api/fabric/graphmodel/items/update-graph-model-definition
    # URL: POST /v1/workspaces/{ws}/graphModels/{id}/updateDefinition?updateMetadata=true
    # NOT /items/{id}/updateDefinition
    encoded_parts = encode_parts_for_api(parts)
    update_url = (
        f"{_FABRIC_API_BASE}/workspaces/{workspace_id}"
        f"/graphModels/{item_id}/updateDefinition?updateMetadata=true"
    )
    update_payload = {
        "definition": {
            "format": "json",
            "parts": encoded_parts,
        }
    }
    update_resp = tp.post(update_url, hdr, update_payload)

    if update_resp.status_code == 200:
        return {
            "item_id": item_id,
            "status": "ok-200",
            "capability_state": capability_state,
            "note": f"GraphModel '{name}' definition pushed (200 OK). {len(parts)} parts.",
            "action": action,
            "lro_location": None,
            "parts_count": len(parts),
        }
    if update_resp.status_code == 202:
        lro_location = update_resp.headers.get("Location", "")
        retry_after_secs = int(update_resp.headers.get("Retry-After", str(lro_initial_retry_after)))
        logger.info("[graph_model] updateDefinition '%s' returned 202; polling LRO", name)
        _poll_lro(
            tp, hdr, lro_location,
            max_retries=lro_max_retries,
            initial_retry_after=retry_after_secs,
            _sleep=_lro_sleep,
        )
        return {
            "item_id": item_id,
            "status": "ok-202",
            "capability_state": capability_state,
            "note": f"GraphModel '{name}' updateDefinition complete (polled 202 LRO). {len(parts)} parts.",
            "action": action,
            "lro_location": lro_location,
            "parts_count": len(parts),
        }

    err = (
        f"updateDefinition for GraphModel '{name}' failed: "
        f"HTTP {update_resp.status_code}: {update_resp.body}"
    )
    logger.error("[graph_model] %s", err)
    raise RuntimeError(err)


def delete_graph_model(
    workspace_id: str,
    graph_model_id: str,
    *,
    token_provider: Optional[Callable[[], str]] = None,
    transport: Optional[GraphModelTransport] = None,
    lro_max_retries: int = 60,
    lro_initial_retry_after: int = 5,
    _lro_sleep: Optional[Callable[[float], None]] = None,
) -> None:
    """Delete one Graph Model and wait for Fabric's long-running operation."""
    tp = transport or _RequestsGMTransport()
    hdr = _make_headers(token_provider)
    url = (
        f"{_FABRIC_API_BASE}/workspaces/{workspace_id}"
        f"/graphModels/{graph_model_id}"
    )
    response = tp.delete(url, hdr)
    if response.status_code == 404:
        return
    if response.status_code in {200, 204}:
        return
    if response.status_code == 202:
        _poll_lro(
            tp,
            hdr,
            response.headers.get("Location", ""),
            max_retries=lro_max_retries,
            initial_retry_after=int(
                response.headers.get("Retry-After", str(lro_initial_retry_after))
            ),
            _sleep=_lro_sleep,
        )
        return
    raise RuntimeError(
        f"Failed to delete GraphModel '{graph_model_id}': "
        f"HTTP {response.status_code}: {response.body}"
    )


# ---------------------------------------------------------------------------
# get_graph_model_definition — documented Preview REST API
# ---------------------------------------------------------------------------


def get_graph_model_definition(
    workspace_id: str,
    graph_model_id: str,
    *,
    token_provider: Optional[Callable[[], str]] = None,
    transport: Optional[GraphModelTransport] = None,
    lro_max_retries: int = 10,
    lro_initial_retry_after: int = 5,
    _lro_sleep: Optional[Callable[[float], None]] = None,
) -> dict[str, Any]:
    """Retrieve a GraphModel public definition.

    POST /v1/workspaces/{workspaceId}/graphModels/{graphModelId}/getDefinition

    Source: https://learn.microsoft.com/en-us/rest/api/fabric/graphmodel/items/get-graph-model-definition

    Returns the definition dict: {"format": "json", "parts": [...]}.
    202 LRO is polled until Succeeded.
    Raises RuntimeError on 429 or unexpected errors.
    """
    tp = transport or _RequestsGMTransport()
    hdr = _make_headers(token_provider)

    url = (
        f"{_FABRIC_API_BASE}/workspaces/{workspace_id}"
        f"/graphModels/{graph_model_id}/getDefinition"
    )
    resp = tp.post(url, hdr, {})

    if resp.status_code == 429:
        ra = resp.headers.get("Retry-After", "60")
        raise RuntimeError(
            f"Rate limited fetching definition for GraphModel '{graph_model_id}' (429). "
            f"Retry-After: {ra}s"
        )

    if resp.status_code == 200:
        body = resp.body if isinstance(resp.body, dict) else {}
        return body.get("definition", body)

    if resp.status_code == 202:
        location = resp.headers.get("Location", "")
        retry_after_secs = int(resp.headers.get("Retry-After", str(lro_initial_retry_after)))
        logger.info(
            "[graph_model] getDefinition '%s' returned 202; polling LRO at '%s'",
            graph_model_id, location,
        )
        succeeded = _poll_lro(
            tp, hdr, location,
            max_retries=lro_max_retries,
            initial_retry_after=retry_after_secs,
            _sleep=_lro_sleep,
        )
        if "definition" in succeeded:
            return succeeded["definition"]
        result = succeeded.get("result")
        if isinstance(result, dict):
            definition = result.get("definition", result)
            if isinstance(definition, dict) and definition.get("parts"):
                return definition

        result_location = (
            location
            if location.rstrip("/").endswith("/result")
            else location.rstrip("/") + "/result"
        )
        result_resp = tp.get(result_location, hdr)
        if result_resp.status_code == 429:
            raise RuntimeError(
                "Rate limited fetching the completed GraphModel definition "
                f"result (429). Retry-After: "
                f"{result_resp.headers.get('Retry-After', '60')}s"
            )
        if not result_resp.ok:
            raise RuntimeError(
                "GraphModel getDefinition LRO completed but its result "
                f"could not be read: HTTP {result_resp.status_code}: "
                f"{result_resp.body}"
            )
        result_body = (
            result_resp.body
            if isinstance(result_resp.body, dict)
            else {}
        )
        definition = result_body.get("definition", result_body)
        if not isinstance(definition, dict):
            raise RuntimeError(
                "GraphModel getDefinition result does not contain a definition."
            )
        return definition

    raise RuntimeError(
        f"getDefinition for GraphModel '{graph_model_id}' failed: "
        f"HTTP {resp.status_code}: {resp.body}"
    )


# ---------------------------------------------------------------------------
# GQL execute query — documented Beta REST API
# ---------------------------------------------------------------------------


def execute_gql_query(
    workspace_id: str,
    graph_model_id: str,
    query: str,
    *,
    token_provider: Optional[Callable[[], str]] = None,
    transport: Optional[GraphModelTransport] = None,
    beta_acknowledged: bool = False,
    continuation_token: Optional[str] = None,
) -> dict[str, Any]:
    """Execute a GQL query against a Fabric GraphModel.

    POST /v1/workspaces/{workspaceId}/graphModels/{graphModelId}/executeQuery?beta=true

    Source:
      https://learn.microsoft.com/en-us/rest/api/fabric/graphmodel/items/execute-query(beta)
      https://learn.microsoft.com/en-us/fabric/graph/gql-query-api

    ``beta_acknowledged`` must be True — this API is beta and may change.
    HTTP 200 is NOT sufficient: check ``status.code`` in the returned body.
    Status prefixes 00/01/02/03 = success; 04+ = application error.
    429 raises RuntimeError with Retry-After.
    Supports pagination via ``continuation_token`` parameter.
    """
    if not beta_acknowledged:
        raise RuntimeError(
            "execute_gql_query requires explicit beta acknowledgement: "
            "pass beta_acknowledged=True (this API is beta and subject to change). "
            "Source: https://learn.microsoft.com/en-us/rest/api/fabric/"
            "graphmodel/items/execute-query(beta)"
        )

    tp = transport or _RequestsGMTransport()
    hdr = _make_headers(token_provider)
    hdr["Accept"] = "application/json"

    url = (
        f"{_FABRIC_API_BASE}/workspaces/{workspace_id}"
        f"/graphModels/{graph_model_id}/executeQuery?beta=true"
    )
    if continuation_token:
        url += f"&continuationToken={continuation_token}"

    resp = tp.post(url, hdr, {"query": query})

    if resp.status_code == 429:
        ra = resp.headers.get("Retry-After", "60")
        raise RuntimeError(
            f"Rate limited executing GQL query on '{graph_model_id}' (429). "
            f"Retry-After: {ra}s"
        )

    if not resp.ok:
        raise RuntimeError(
            f"GQL query failed: HTTP {resp.status_code}: {resp.body}"
        )

    # HTTP 200 is not sufficient — application errors are in the body.
    # Return raw body; caller is responsible for checking status.code.
    return resp.body if isinstance(resp.body, dict) else {}


def collect_all_gql_pages(
    workspace_id: str,
    graph_model_id: str,
    query: str,
    *,
    token_provider: Optional[Callable[[], str]] = None,
    transport: Optional[GraphModelTransport] = None,
    beta_acknowledged: bool = False,
    max_pages: int = 10,
) -> dict[str, Any]:
    """Execute a GQL query collecting all Fabric pagination pages.

    Follows the Fabric REST pagination contract: checks ``continuationToken``
    in the response body for the next page token.
    Returns the final response dict with all data rows combined.

    Source: https://learn.microsoft.com/en-us/rest/api/fabric/articles/pagination
    """
    all_data: list[dict[str, Any]] = []
    continuation: Optional[str] = None
    last_resp: Optional[dict[str, Any]] = None

    for _ in range(max_pages):
        resp = execute_gql_query(
            workspace_id, graph_model_id, query,
            token_provider=token_provider,
            transport=transport,
            beta_acknowledged=beta_acknowledged,
            continuation_token=continuation,
        )
        last_resp = resp
        result = resp.get("result", {})
        if result.get("kind") == "TABLE":
            all_data.extend(result.get("data", []))
        continuation = resp.get("continuationToken") or resp.get("continuationUri")
        if not continuation:
            break

    if last_resp and all_data:
        last_resp = dict(last_resp)
        if last_resp.get("result", {}).get("kind") == "TABLE":
            last_resp["result"] = dict(last_resp["result"])
            last_resp["result"]["data"] = all_data

    return last_resp or {}


class GraphModelGQLClient:
    """Real GQL query client using the documented Fabric executeQuery beta API.

    Implements the ``GQLQueryClient`` protocol from ``serving.competency``.
    Wraps ``execute_gql_query`` and ``collect_all_gql_pages`` with caller-owned
    token_provider and transport injection.

    This client has ``beta_acknowledged=True`` baked in — it is intentionally
    committed to the beta API.  Use ``CompetencyVerifier(gql_beta_acknowledged=True)``
    to activate GQL competency gates.

    Source: https://learn.microsoft.com/en-us/rest/api/fabric/graphmodel/items/execute-query(beta)
    """

    def __init__(
        self,
        token_provider: Optional[Callable[[], str]] = None,
        transport: Optional[GraphModelTransport] = None,
        max_pages: int = 10,
        readiness_timeout_s: float = 300.0,
        readiness_poll_interval_s: float = 5.0,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ) -> None:
        self._token_provider = token_provider
        self._transport = transport
        self._max_pages = max_pages
        self._readiness_timeout_s = readiness_timeout_s
        self._readiness_poll_interval_s = readiness_poll_interval_s
        self._sleep = sleep_fn or time.sleep
        self._graph_ready = False

    def _execute_when_ready(
        self,
        operation: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        """Retry the post-update GraphIsNotLoaded transition within a bound."""
        if self._graph_ready:
            return operation()
        deadline = time.monotonic() + self._readiness_timeout_s
        while True:
            try:
                result = operation()
                self._graph_ready = True
                return result
            except RuntimeError as exc:
                message = str(exc)
                if (
                    "GraphIsNotLoaded" not in message
                    and "GraphNotQueryable" not in message
                ):
                    raise
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Graph Model did not become queryable within "
                        f"{self._readiness_timeout_s:g} seconds."
                    ) from exc
                self._sleep(self._readiness_poll_interval_s)

    def execute_query(
        self,
        workspace_id: str,
        graph_model_id: str,
        query: str,
    ) -> dict[str, Any]:
        """Execute GQL query; return raw response body (caller checks status.code)."""
        return self._execute_when_ready(
            lambda: execute_gql_query(
                workspace_id, graph_model_id, query,
                token_provider=self._token_provider,
                transport=self._transport,
                beta_acknowledged=True,
            )
        )

    def execute_query_all_pages(
        self,
        workspace_id: str,
        graph_model_id: str,
        query: str,
    ) -> dict[str, Any]:
        """Execute GQL query collecting all pagination pages."""
        return self._execute_when_ready(
            lambda: collect_all_gql_pages(
                workspace_id, graph_model_id, query,
                token_provider=self._token_provider,
                transport=self._transport,
                beta_acknowledged=True,
                max_pages=self._max_pages,
            )
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _stable_id(seed: str) -> str:
    """Deterministic short ID from SHA-256 of seed."""
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _graph_alias(value: str) -> str:
    """Return a deterministic GraphModel alias safe across arbitrary domains."""
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", value):
        return value
    alias = re.sub(r"[^A-Za-z0-9_]", "_", value)
    alias = re.sub(r"_+", "_", alias).strip("_")
    if not alias or not alias[0].isalpha():
        alias = f"Type_{alias}"
    return f"{alias[:96]}_{_stable_id(value)[:8]}"
