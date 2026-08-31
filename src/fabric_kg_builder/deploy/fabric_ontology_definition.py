"""Compile a Fabric-native Ontology item definition from an L5a definition.

The L5a ``ontology`` target definition is the release's canonical, hash-bound
description of the published graph. Fabric's Ontology item expects a different
shape entirely: a part-per-type tree of ``EntityTypes/<id>/definition.json``,
``DataBindings``, ``RelationshipTypes``, and ``Contextualizations``, each bound
to a Lakehouse table. This module performs that translation so the mapping is
reviewable and reproducible rather than hand-built at deploy time.

One deliberate widening is applied. A Fabric ``RelationshipType`` names exactly
one source entity type, but ``relationship-type:assertion-supported-by-evidence``
admits five. Narrowing it to the physical representative would silently claim
that only components carry evidence, so the source is widened to an abstract
base entity type instead. Widening keeps every published edge valid; narrowing
would misdescribe four fifths of them. The widening is reported by
:func:`compile_fabric_ontology_definition` rather than applied silently.
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from .lakehouse_schema import apply_source_schema, resolve_lakehouse_schema

BASE_ENTITY_TYPE_ID = "1000000"
BASE_ENTITY_TYPE_NAME = "surface_entity"
BASE_IDENTITY_PROPERTY_ID = "2000000"
BASE_ENTITY_TABLE = "l4_semantic_asserted_entities"
BASE_ENTITY_IDENTITY_COLUMN = "entity_id"
BASE_LABEL_PROPERTY_ID = "3000000"
BASE_ENTITY_LABEL_COLUMN = "label"
LABEL_PROPERTY_NAME = "label"
# Typed publication tables carry the derived mention as a structural column.
TYPED_LABEL_COLUMN = "__label"

_NAMESPACE = "usertypes"
_SCHEMA_ROOT = "https://developer.microsoft.com/json-schemas/fabric/item/ontology"
_ID_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def _stable_guid(kind: str, key: str) -> str:
    """Fabric rejects non-GUID binding ids, so derive one deterministically."""

    return str(uuid.uuid5(_ID_NAMESPACE, f"fabric-kg-024:{kind}:{key}"))


@dataclass(frozen=True)
class FabricOntologyCompilation:
    """The compiled parts plus every endpoint widening that was applied."""

    parts: tuple[dict[str, str], ...]
    widened_relationships: tuple[str, ...] = field(default=())


def _name(canonical_id: str) -> str:
    return canonical_id.split(":", 1)[-1].replace("-", "_")


def _part(path: str, payload: dict[str, Any]) -> dict[str, str]:
    """Encode one part, preserving key order.

    Keys are emitted in insertion order rather than sorted. Fabric deserializes
    ``sourceTableProperties`` polymorphically and requires its ``sourceType``
    discriminator to be the *first* property; sorting alphabetically moves it
    behind ``itemId`` and the import fails with ``ALMOperationImportFailed``.
    """

    encoded = base64.b64encode(
        json.dumps(payload, indent=2, sort_keys=False).encode("utf-8")
    ).decode("ascii")
    return {
        "path": path,
        "payload": encoded,
        "payloadType": "InlineBase64",
    }


def _property_payload(prop: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(prop["id"]),
        "name": _name(str(prop["canonical_property_id"])),
        "redefines": None,
        "baseTypeNamespaceType": None,
        "valueType": "String",
    }


def _entity_type_payload(
    entity_type: dict[str, Any],
    *,
    identity_property_id: str,
    label_property_id: str | None = None,
) -> dict[str, Any]:
    properties = [
        {
            "id": identity_property_id,
            "name": "id",
            "redefines": None,
            "baseTypeNamespaceType": None,
            "valueType": "String",
        }
    ]
    if label_property_id is not None:
        properties.append({
            "id": label_property_id,
            "name": LABEL_PROPERTY_NAME,
            "redefines": None,
            "baseTypeNamespaceType": None,
            "valueType": "String",
        })
    properties.extend(
        _property_payload(prop) for prop in entity_type.get("properties", ())
    )
    return {
        "$schema": f"{_SCHEMA_ROOT}/entityType/1.0.0/schema.json",
        "id": str(entity_type["id"]),
        "namespace": _NAMESPACE,
        "baseEntityTypeId": None,
        "name": _name(str(entity_type["canonical_semantic_type_id"])),
        "entityIdParts": [identity_property_id],
        "displayNamePropertyId": identity_property_id,
        "namespaceType": "Custom",
        "visibility": "Visible",
        "properties": properties,
        "timeseriesProperties": [],
        "semanticEnrichment": {
            "synonyms": [],
            "description": "",
            "customAttributes": {},
        },
        "untypedProperties": [],
    }


def _data_binding_payload(
    entity_type: dict[str, Any],
    *,
    identity_property_id: str,
    workspace_id: str,
    lakehouse_id: str,
    lakehouse_schema: str | None,
    table_name: str,
    identity_column: str,
    label_property_id: str | None = None,
    label_column: str | None = None,
) -> dict[str, Any]:
    bindings = [
        {
            "sourceColumnName": identity_column,
            "targetPropertyId": identity_property_id,
        }
    ]
    if label_property_id is not None and label_column is not None:
        bindings.append({
            "sourceColumnName": label_column,
            "targetPropertyId": label_property_id,
        })
    for prop in entity_type.get("properties", ()):
        bindings.append(
            {
                "sourceColumnName": str(prop["physical_column_id"]),
                "targetPropertyId": str(prop["id"]),
            }
        )
    return {
        "$schema": f"{_SCHEMA_ROOT}/dataBinding/1.0.0/schema.json",
        "id": _stable_guid("binding", str(entity_type["id"])),
        "dataBindingConfiguration": {
            "dataBindingType": "NonTimeSeries",
            "propertyBindings": bindings,
            "sourceTableProperties": apply_source_schema(
                {
                    "sourceType": "LakehouseTable",
                    "workspaceId": workspace_id,
                    "itemId": lakehouse_id,
                    "sourceTableName": table_name,
                },
                lakehouse_schema,
            ),
        },
    }


def compile_fabric_ontology_definition(
    l5a_ontology: dict[str, Any],
    *,
    workspace_id: str,
    lakehouse_id: str,
    display_name: str,
    description: str,
    lakehouse: Any,
) -> FabricOntologyCompilation:
    """Translate the L5a ontology definition into Fabric item parts.

    ``lakehouse`` identifies the table schema of the target Lakehouse.  Pass the
    schema name, the ``GET /lakehouses/{id}`` payload, or ``None`` for a
    Lakehouse created without schemas.  It is **not** optional in practice: a
    mismatch produces a definition that imports and reads back cleanly while
    binding to a OneLake path that does not exist, which surfaces only later as
    an unrefreshable, empty graph.
    """

    lakehouse_schema = resolve_lakehouse_schema(lakehouse)
    parts: list[dict[str, str]] = [_part("definition.json", {})]
    entity_types = list(l5a_ontology["entity_types"])
    identity_by_type: dict[str, str] = {}

    base_type = {
        "$schema": f"{_SCHEMA_ROOT}/entityType/1.0.0/schema.json",
        "id": BASE_ENTITY_TYPE_ID,
        "namespace": _NAMESPACE,
        "baseEntityTypeId": None,
        "name": BASE_ENTITY_TYPE_NAME,
        "entityIdParts": [BASE_IDENTITY_PROPERTY_ID],
        "displayNamePropertyId": BASE_IDENTITY_PROPERTY_ID,
        "namespaceType": "Custom",
        "visibility": "Visible",
        "properties": [
            {
                "id": BASE_IDENTITY_PROPERTY_ID,
                "name": "id",
                "redefines": None,
                "baseTypeNamespaceType": None,
                "valueType": "String",
            },
            {
                "id": BASE_LABEL_PROPERTY_ID,
                "name": LABEL_PROPERTY_NAME,
                "redefines": None,
                "baseTypeNamespaceType": None,
                "valueType": "String",
            },
        ],
        "timeseriesProperties": [],
        "semanticEnrichment": {
            "synonyms": [],
            "description": "Any published surface entity, of any type.",
            "customAttributes": {},
        },
        "untypedProperties": [],
    }
    parts.append(
        _part(f"EntityTypes/{BASE_ENTITY_TYPE_ID}/definition.json", base_type)
    )
    parts.append(
        _part(
            f"EntityTypes/{BASE_ENTITY_TYPE_ID}/DataBindings/"
            f"{_stable_guid('binding', BASE_ENTITY_TYPE_ID)}.json",
            _data_binding_payload(
                {"id": BASE_ENTITY_TYPE_ID, "properties": ()},
                identity_property_id=BASE_IDENTITY_PROPERTY_ID,
                workspace_id=workspace_id,
                lakehouse_id=lakehouse_id,
                lakehouse_schema=lakehouse_schema,
                table_name=BASE_ENTITY_TABLE,
                identity_column=BASE_ENTITY_IDENTITY_COLUMN,
                label_property_id=BASE_LABEL_PROPERTY_ID,
                label_column=BASE_ENTITY_LABEL_COLUMN,
            ),
        )
    )

    for entity_type in entity_types:
        type_id = str(entity_type["id"])
        identity_property_id = f"9{type_id}"
        label_property_id = f"8{type_id}"
        identity_by_type[type_id] = identity_property_id
        parts.append(
            _part(
                f"EntityTypes/{type_id}/definition.json",
                _entity_type_payload(
                    entity_type,
                    identity_property_id=identity_property_id,
                    label_property_id=label_property_id,
                ),
            )
        )
        parts.append(
            _part(
                f"EntityTypes/{type_id}/DataBindings/"
                f"{_stable_guid('binding', type_id)}.json",
                _data_binding_payload(
                    entity_type,
                    identity_property_id=identity_property_id,
                    workspace_id=workspace_id,
                    lakehouse_id=lakehouse_id,
                    lakehouse_schema=lakehouse_schema,
                    table_name=str(entity_type["physical_table_id"]),
                    identity_column=str(
                        entity_type["physical_identity_column"]
                    ),
                    label_property_id=label_property_id,
                    label_column=TYPED_LABEL_COLUMN,
                ),
            )
        )

    widened: list[str] = []
    for relationship in l5a_ontology["relationship_types"]:
        rel_id = str(relationship["id"])
        sources = list(relationship["allowed_source_semantic_type_ids"])
        targets = list(relationship["allowed_target_semantic_type_ids"])
        by_canonical = {
            str(item["canonical_semantic_type_id"]): str(item["id"])
            for item in entity_types
        }
        if len(sources) == 1:
            source_type_id = by_canonical[sources[0]]
            source_identity = identity_by_type[source_type_id]
        else:
            source_type_id = BASE_ENTITY_TYPE_ID
            source_identity = BASE_IDENTITY_PROPERTY_ID
            widened.append(
                str(relationship["canonical_semantic_relationship_id"])
            )
        if len(targets) == 1:
            target_type_id = by_canonical[targets[0]]
            target_identity = identity_by_type[target_type_id]
        else:
            target_type_id = BASE_ENTITY_TYPE_ID
            target_identity = BASE_IDENTITY_PROPERTY_ID
            widened.append(
                str(relationship["canonical_semantic_relationship_id"])
            )

        parts.append(
            _part(
                f"RelationshipTypes/{rel_id}/definition.json",
                {
                    "$schema": (
                        f"{_SCHEMA_ROOT}/relationshipType/1.0.0/schema.json"
                    ),
                    "namespace": _NAMESPACE,
                    "id": rel_id,
                    "name": _name(
                        str(relationship["canonical_semantic_relationship_id"])
                    ),
                    "namespaceType": "Custom",
                    "source": {"entityTypeId": source_type_id},
                    "target": {"entityTypeId": target_type_id},
                },
            )
        )
        parts.append(
            _part(
                f"RelationshipTypes/{rel_id}/Contextualizations/"
                f"{_stable_guid('ctx', rel_id)}.json",
                {
                    "$schema": (
                        f"{_SCHEMA_ROOT}/contextualization/1.0.0/schema.json"
                    ),
                    "id": _stable_guid("ctx", rel_id),
                    "dataBindingTable": apply_source_schema(
                        {
                            "workspaceId": workspace_id,
                            "itemId": lakehouse_id,
                            "sourceTableName": str(
                                relationship["physical_table_id"]
                            ),
                            "sourceType": "LakehouseTable",
                        },
                        lakehouse_schema,
                    ),
                    "sourceKeyRefBindings": [
                        {
                            "sourceColumnName": str(
                                relationship["source_identity_column"]
                            ),
                            "targetPropertyId": source_identity,
                        }
                    ],
                    "targetKeyRefBindings": [
                        {
                            "sourceColumnName": str(
                                relationship["target_identity_column"]
                            ),
                            "targetPropertyId": target_identity,
                        }
                    ],
                },
            )
        )

    parts.append(
        _part(
            ".platform",
            {
                "$schema": (
                    "https://developer.microsoft.com/json-schemas/fabric/"
                    "gitIntegration/platformProperties/2.0.0/schema.json"
                ),
                "metadata": {
                    "type": "Ontology",
                    "displayName": display_name,
                    "description": description,
                },
                "config": {
                    "version": "2.0",
                    "logicalId": "00000000-0000-0000-0000-000000000000",
                },
            },
        )
    )
    return FabricOntologyCompilation(
        parts=tuple(parts),
        widened_relationships=tuple(sorted(set(widened))),
    )
