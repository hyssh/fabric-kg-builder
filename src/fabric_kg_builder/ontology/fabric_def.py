"""fabric_def.py — build Fabric Ontology REST parts in the REAL Fabric format.

Produces the exact part structure decoded from the working on_finance ontology:
  - definition.json (root)             → {}
  - .platform                          → Ontology metadata
  - EntityTypes/{entityTypeId}/definition.json
  - EntityTypes/{entityTypeId}/DataBindings/{guid}.json
  - RelationshipTypes/{relTypeId}/definition.json
  - RelationshipTypes/{relTypeId}/Contextualizations/{guid}.json

IDs (entityTypeId, propertyId, relTypeId) are BigInt strings: deterministic,
positive, unique, derived from SHA-256. DataBinding/Contextualization ids are
deterministic UUIDs (also from SHA-256).

This module is used by deploy-ontology to populate the Fabric graph via
updateDefinition. The OLD compiler.py (InlineBase64 parts in our model-yaml
format) is kept for the build/ontology artifact and compile-ontology CLI.

Per coordinator-fabric-ontology-real-format.md decision.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

# ---------------------------------------------------------------------------
# Schema URL constants (item/* path — matches working on_finance ontology)
# ---------------------------------------------------------------------------

_PLATFORM_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/"
    "platformProperties/2.0.0/schema.json"
)
_ENTITY_TYPE_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/ontology/"
    "entityType/1.0.0/schema.json"
)
_DATA_BINDING_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/ontology/"
    "dataBinding/1.0.0/schema.json"
)
_RELATIONSHIP_TYPE_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/ontology/"
    "relationshipType/1.0.0/schema.json"
)
_CONTEXTUALIZATION_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/ontology/"
    "contextualization/1.0.0/schema.json"
)

# Used as all-zeros logicalId for the .platform file (matches working ontology)
_ZERO_LOGICAL_ID = "00000000-0000-0000-0000-000000000000"

# BigInt range: use upper 63 bits of SHA-256 chunk to stay positive int64
_BIGINT_MODULUS = 2**62  # safe positive int64 range


# ---------------------------------------------------------------------------
# Deterministic ID helpers
# ---------------------------------------------------------------------------


def _bigint_id(seed: str) -> str:
    """Return a deterministic positive BigInt string derived from *seed*.

    Uses first 8 bytes of SHA-256(seed), interpreted as an unsigned int,
    then modulo 2^62 so the value fits safely in a positive int64.
    Stable across Python versions and OS (SHA-256 is platform-independent).
    """
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    raw = int.from_bytes(digest[:8], "big")
    return str(raw % _BIGINT_MODULUS)


def _guid_id(seed: str) -> str:
    """Return a deterministic UUID string (not BigInt) derived from *seed*.

    Used for DataBinding and Contextualization ids (Fabric uses guid there).
    """
    ns = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # NAMESPACE_URL
    return str(uuid.uuid5(ns, f"fabric-kg:{seed}"))


# ---------------------------------------------------------------------------
# Per-entity/property seeds (unique within the ontology)
# ---------------------------------------------------------------------------

# Entity type seed
_KGENTITY_SEED = "KGEntity:entityType"

# Property seeds — must be distinct from each other and from entity type seed
_PROP_ENTITY_ID_SEED = "KGEntity:prop:entity_id"
_PROP_ENTITY_TYPE_SEED = "KGEntity:prop:entity_type"
_PROP_DISPLAY_NAME_SEED = "KGEntity:prop:display_name"
_PROP_CANONICAL_KEY_SEED = "KGEntity:prop:canonical_key"

# RelationshipType seed
_RELATED_TO_SEED = "related_to:relType"

# DataBinding / Contextualization guid seeds
_KGENTITY_BINDING_SEED = "KGEntity:DataBinding:dbo.entities"
_RELATED_TO_CTX_SEED = "related_to:Contextualization:dbo.relationships"


def _compute_ids() -> dict[str, str]:
    """Compute all stable IDs once and return as a dict."""
    return {
        "entity_type_id": _bigint_id(_KGENTITY_SEED),
        "prop_entity_id": _bigint_id(_PROP_ENTITY_ID_SEED),
        "prop_entity_type": _bigint_id(_PROP_ENTITY_TYPE_SEED),
        "prop_display_name": _bigint_id(_PROP_DISPLAY_NAME_SEED),
        "prop_canonical_key": _bigint_id(_PROP_CANONICAL_KEY_SEED),
        "rel_type_id": _bigint_id(_RELATED_TO_SEED),
        "binding_guid": _guid_id(_KGENTITY_BINDING_SEED),
        "ctx_guid": _guid_id(_RELATED_TO_CTX_SEED),
    }


# ---------------------------------------------------------------------------
# Part builders
# ---------------------------------------------------------------------------


def _platform_part(ontology_name: str) -> dict[str, Any]:
    return {
        "$schema": _PLATFORM_SCHEMA,
        "metadata": {
            "type": "Ontology",
            "displayName": ontology_name,
        },
        "config": {
            "version": "2.0",
            "logicalId": _ZERO_LOGICAL_ID,
        },
    }


def _entity_type_definition(ids: dict[str, str]) -> dict[str, Any]:
    """Build KGEntity EntityType definition.json payload."""
    return {
        "$schema": _ENTITY_TYPE_SCHEMA,
        "id": ids["entity_type_id"],
        "namespace": "usertypes",
        "namespaceType": "Imported",
        "baseEntityTypeId": None,
        "name": "KGEntity",
        "entityIdParts": [ids["prop_entity_id"]],
        "displayNamePropertyId": ids["prop_display_name"],
        "visibility": "Visible",
        "properties": [
            {
                "id": ids["prop_entity_id"],
                "name": "entity_id",
                "redefines": None,
                "baseTypeNamespaceType": None,
                "valueType": "String",
            },
            {
                "id": ids["prop_entity_type"],
                "name": "entity_type",
                "redefines": None,
                "baseTypeNamespaceType": None,
                "valueType": "String",
            },
            {
                "id": ids["prop_display_name"],
                "name": "display_name",
                "redefines": None,
                "baseTypeNamespaceType": None,
                "valueType": "String",
            },
            {
                "id": ids["prop_canonical_key"],
                "name": "canonical_key",
                "redefines": None,
                "baseTypeNamespaceType": None,
                "valueType": "String",
            },
        ],
        "timeseriesProperties": [],
        "untypedProperties": [],
    }


def _entity_type_binding(
    ids: dict[str, str],
    workspace_id: str,
    lakehouse_item_id: str,
    schema: str,
) -> dict[str, Any]:
    """Build KGEntity DataBinding payload (binds dbo.entities columns)."""
    return {
        "$schema": _DATA_BINDING_SCHEMA,
        "id": ids["binding_guid"],
        "dataBindingConfiguration": {
            "dataBindingType": "NonTimeSeries",
            "propertyBindings": [
                {
                    "sourceColumnName": "entity_id",
                    "targetPropertyId": ids["prop_entity_id"],
                },
                {
                    "sourceColumnName": "entity_type",
                    "targetPropertyId": ids["prop_entity_type"],
                },
                {
                    "sourceColumnName": "display_name",
                    "targetPropertyId": ids["prop_display_name"],
                },
                {
                    "sourceColumnName": "canonical_key",
                    "targetPropertyId": ids["prop_canonical_key"],
                },
            ],
            "sourceTableProperties": {
                "sourceType": "LakehouseTable",
                "workspaceId": workspace_id,
                "itemId": lakehouse_item_id,
                "sourceTableName": "entities",
                "sourceSchema": schema,
            },
        },
    }


def _relationship_type_definition(ids: dict[str, str]) -> dict[str, Any]:
    """Build related_to RelationshipType definition.json payload."""
    return {
        "$schema": _RELATIONSHIP_TYPE_SCHEMA,
        "id": ids["rel_type_id"],
        "namespace": "usertypes",
        "namespaceType": "Imported",
        "name": "related_to",
        "source": {"entityTypeId": ids["entity_type_id"]},
        "target": {"entityTypeId": ids["entity_type_id"]},
    }


def _relationship_type_contextualization(
    ids: dict[str, str],
    workspace_id: str,
    lakehouse_item_id: str,
    schema: str,
) -> dict[str, Any]:
    """Build related_to Contextualization payload (binds dbo.relationships)."""
    return {
        "$schema": _CONTEXTUALIZATION_SCHEMA,
        "id": ids["ctx_guid"],
        "dataBindingTable": {
            "workspaceId": workspace_id,
            "itemId": lakehouse_item_id,
            "sourceTableName": "relationships",
            "sourceSchema": schema,
            "sourceType": "LakehouseTable",
        },
        "sourceKeyRefBindings": [
            {
                "sourceColumnName": "source_entity_id",
                "targetPropertyId": ids["prop_entity_id"],
            }
        ],
        "targetKeyRefBindings": [
            {
                "sourceColumnName": "target_entity_id",
                "targetPropertyId": ids["prop_entity_id"],
            }
        ],
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_ontology_parts(
    workspace_id: str,
    lakehouse_item_id: str,
    schema: str = "dbo",
    ontology_name: str = "kg_ontology",
) -> list[dict[str, Any]]:
    """Build the Fabric Ontology REST parts list in the EXACT Fabric format.

    Returns a list of dicts, each with:
        path         (str)  — relative path under the ontology root
        payload_json (dict) — the decoded JSON payload (caller base64-encodes)

    Six parts total:
        1. definition.json              → {}
        2. .platform                    → Ontology metadata
        3. EntityTypes/{id}/definition.json
        4. EntityTypes/{id}/DataBindings/{guid}.json
        5. RelationshipTypes/{id}/definition.json
        6. RelationshipTypes/{id}/Contextualizations/{guid}.json

    IDs are deterministic across calls — safe to call updateDefinition
    repeatedly (idempotent).

    Parameters
    ----------
    workspace_id:
        Fabric workspace GUID (from ontology/environments/{env}.json).
    lakehouse_item_id:
        Fabric Lakehouse item GUID (from ontology/environments/{env}.json).
    schema:
        Lakehouse schema name (default "dbo").
    ontology_name:
        Display name for the Ontology item (default "kg_ontology").
    """
    ids = _compute_ids()
    et_id = ids["entity_type_id"]
    rt_id = ids["rel_type_id"]
    binding_guid = ids["binding_guid"]
    ctx_guid = ids["ctx_guid"]

    return [
        # 1. Root manifest (always empty dict per Fabric spec)
        {
            "path": "definition.json",
            "payload_json": {},
        },
        # 2. Platform metadata
        {
            "path": ".platform",
            "payload_json": _platform_part(ontology_name),
        },
        # 3. KGEntity definition
        {
            "path": f"EntityTypes/{et_id}/definition.json",
            "payload_json": _entity_type_definition(ids),
        },
        # 4. KGEntity DataBinding → dbo.entities
        {
            "path": f"EntityTypes/{et_id}/DataBindings/{binding_guid}.json",
            "payload_json": _entity_type_binding(ids, workspace_id, lakehouse_item_id, schema),
        },
        # 5. related_to RelationshipType definition
        {
            "path": f"RelationshipTypes/{rt_id}/definition.json",
            "payload_json": _relationship_type_definition(ids),
        },
        # 6. related_to Contextualization → dbo.relationships
        {
            "path": f"RelationshipTypes/{rt_id}/Contextualizations/{ctx_guid}.json",
            "payload_json": _relationship_type_contextualization(
                ids, workspace_id, lakehouse_item_id, schema
            ),
        },
    ]


def get_stable_ids() -> dict[str, str]:
    """Expose computed IDs for inspection / testing."""
    return _compute_ids()


# ---------------------------------------------------------------------------
# Multi-type ontology (one EntityType per real domain type)
# ---------------------------------------------------------------------------
#
# The single-type model above renders one generic "KGEntity" box in the Fabric
# Ontology Explorer.  The functions below emit one EntityType per real domain
# type (Component, Procedure, Step, ...) bound to a per-type Lakehouse table, and
# one typed RelationshipType per (source_type -> target_type) pair bound to a
# per-pair edge table.  Consumed by deploy-ontology --multitype.
#
# Per-type tables are expected to share the same 4 bound columns as dbo.entities
# (entity_id / entity_type / display_name / canonical_key).  Per-pair edge tables
# share dbo.relationships' source_entity_id / target_entity_id columns.


def _mt_type_ids(type_name: str) -> dict[str, str]:
    """Deterministic IDs for one EntityType and its 4 properties."""
    return {
        "entity_type_id": _bigint_id(f"{type_name}:entityType"),
        "prop_entity_id": _bigint_id(f"{type_name}:prop:entity_id"),
        "prop_entity_type": _bigint_id(f"{type_name}:prop:entity_type"),
        "prop_display_name": _bigint_id(f"{type_name}:prop:display_name"),
        "prop_canonical_key": _bigint_id(f"{type_name}:prop:canonical_key"),
    }


def _mt_entity_type_definition(type_name: str, ids: dict[str, str]) -> dict[str, Any]:
    return {
        "$schema": _ENTITY_TYPE_SCHEMA,
        "id": ids["entity_type_id"],
        "namespace": "usertypes",
        "namespaceType": "Imported",
        "baseEntityTypeId": None,
        "name": type_name,
        "entityIdParts": [ids["prop_entity_id"]],
        "displayNamePropertyId": ids["prop_display_name"],
        "visibility": "Visible",
        "properties": [
            {"id": ids["prop_entity_id"], "name": "entity_id", "redefines": None,
             "baseTypeNamespaceType": None, "valueType": "String"},
            {"id": ids["prop_entity_type"], "name": "entity_type", "redefines": None,
             "baseTypeNamespaceType": None, "valueType": "String"},
            {"id": ids["prop_display_name"], "name": "display_name", "redefines": None,
             "baseTypeNamespaceType": None, "valueType": "String"},
            {"id": ids["prop_canonical_key"], "name": "canonical_key", "redefines": None,
             "baseTypeNamespaceType": None, "valueType": "String"},
        ],
        "timeseriesProperties": [],
        "untypedProperties": [],
    }


def _mt_entity_type_binding(
    ids: dict[str, str],
    workspace_id: str,
    lakehouse_item_id: str,
    schema: str,
    source_table_name: str,
) -> dict[str, Any]:
    return {
        "$schema": _DATA_BINDING_SCHEMA,
        "id": ids["binding_guid"],
        "dataBindingConfiguration": {
            "dataBindingType": "NonTimeSeries",
            "propertyBindings": [
                {"sourceColumnName": "entity_id", "targetPropertyId": ids["prop_entity_id"]},
                {"sourceColumnName": "entity_type", "targetPropertyId": ids["prop_entity_type"]},
                {"sourceColumnName": "display_name", "targetPropertyId": ids["prop_display_name"]},
                {"sourceColumnName": "canonical_key", "targetPropertyId": ids["prop_canonical_key"]},
            ],
            "sourceTableProperties": {
                "sourceType": "LakehouseTable",
                "workspaceId": workspace_id,
                "itemId": lakehouse_item_id,
                "sourceTableName": source_table_name,
                "sourceSchema": schema,
            },
        },
    }


def build_multitype_ontology_parts(
    workspace_id: str,
    lakehouse_item_id: str,
    entity_types: list[dict[str, Any]],
    relationship_pairs: list[dict[str, Any]],
    schema: str = "dbo",
    ontology_name: str = "kg_ontology",
) -> list[dict[str, Any]]:
    """Build Fabric Ontology parts for a multi-type graph.

    Parameters
    ----------
    entity_types:
        List of dicts with keys ``type_name`` and ``table_name`` (the per-type
        Lakehouse table, e.g. ``entities_component``).
    relationship_pairs:
        List of dicts with keys ``name``, ``source_type``, ``target_type`` and
        ``table_name`` (per-pair edge table, e.g. ``rel_procedure_step``).

    Returns the same ``[{path, payload_json}, ...]`` shape as
    :func:`build_ontology_parts`.  IDs are deterministic (idempotent
    updateDefinition).
    """
    parts: list[dict[str, Any]] = [
        {"path": "definition.json", "payload_json": {}},
        {"path": ".platform", "payload_json": _platform_part(ontology_name)},
    ]

    # Per-type EntityType definition + DataBinding.
    type_ids: dict[str, dict[str, str]] = {}
    for et in entity_types:
        type_name = et["type_name"]
        table_name = et["table_name"]
        ids = _mt_type_ids(type_name)
        ids["binding_guid"] = _guid_id(f"{type_name}:DataBinding:{schema}.{table_name}")
        type_ids[type_name] = ids
        et_id = ids["entity_type_id"]
        parts.append({
            "path": f"EntityTypes/{et_id}/definition.json",
            "payload_json": _mt_entity_type_definition(type_name, ids),
        })
        parts.append({
            "path": f"EntityTypes/{et_id}/DataBindings/{ids['binding_guid']}.json",
            "payload_json": _mt_entity_type_binding(
                ids, workspace_id, lakehouse_item_id, schema, table_name
            ),
        })

    # Per-pair RelationshipType definition + Contextualization.
    for rp in relationship_pairs:
        src = rp["source_type"]
        tgt = rp["target_type"]
        if src not in type_ids or tgt not in type_ids:
            continue  # endpoint type not modelled — skip defensively
        name = rp["name"]
        table_name = rp["table_name"]
        rt_id = _bigint_id(f"{name}:{src}:{tgt}:relType")
        ctx_guid = _guid_id(f"{name}:{src}:{tgt}:Contextualization:{schema}.{table_name}")
        src_ids = type_ids[src]
        tgt_ids = type_ids[tgt]
        parts.append({
            "path": f"RelationshipTypes/{rt_id}/definition.json",
            "payload_json": {
                "$schema": _RELATIONSHIP_TYPE_SCHEMA,
                "id": rt_id,
                "namespace": "usertypes",
                "namespaceType": "Imported",
                "name": name,
                "source": {"entityTypeId": src_ids["entity_type_id"]},
                "target": {"entityTypeId": tgt_ids["entity_type_id"]},
            },
        })
        parts.append({
            "path": f"RelationshipTypes/{rt_id}/Contextualizations/{ctx_guid}.json",
            "payload_json": {
                "$schema": _CONTEXTUALIZATION_SCHEMA,
                "id": ctx_guid,
                "dataBindingTable": {
                    "workspaceId": workspace_id,
                    "itemId": lakehouse_item_id,
                    "sourceTableName": table_name,
                    "sourceSchema": schema,
                    "sourceType": "LakehouseTable",
                },
                "sourceKeyRefBindings": [
                    {"sourceColumnName": "source_entity_id",
                     "targetPropertyId": src_ids["prop_entity_id"]},
                ],
                "targetKeyRefBindings": [
                    {"sourceColumnName": "target_entity_id",
                     "targetPropertyId": tgt_ids["prop_entity_id"]},
                ],
            },
        })

    return parts
