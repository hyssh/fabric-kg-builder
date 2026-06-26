"""Ontology compiler: model.yaml + ids.lock.json → Fabric definition parts.

Reads ``ontology/model.yaml`` and ``ontology/ids.lock.json`` and emits the
full Fabric Ontology directory structure:

    build/ontology/
      .platform
      definition.json                                  ← manifest; all parts with Base64 payloads
      EntityTypes/{typeId}/definition.json
      EntityTypes/{typeId}/DataBindings/{guid}.json
      RelationshipTypes/{typeId}/definition.json
      RelationshipTypes/{typeId}/Contextualizations/{guid}.json

All GUIDs are deterministic UUIDv5 values so re-runs are stable across
environments. IDs come exclusively from ids.lock.json — never regenerated.

Per SPEC-003 §6.
"""

from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Schema URL constants
# ---------------------------------------------------------------------------

_PLATFORM_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/"
    "platformProperties/2.0.0/schema.json"
)
_ENTITY_TYPE_DEF_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/ontology/entityType/1.0.0/schema.json"
)
_DATA_BINDING_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/ontology/dataBinding/1.0.0/schema.json"
)
_RELATIONSHIP_TYPE_DEF_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/ontology/relationshipType/1.0.0/schema.json"
)
_CONTEXTUALIZATION_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/ontology/contextualization/1.0.0/schema.json"
)

# ---------------------------------------------------------------------------
# Deterministic GUID namespace
# ---------------------------------------------------------------------------

# All GUIDs are derived from a single UUIDv5 namespace keyed to the ontology name.
_ONTOLOGY_NS: uuid.UUID = uuid.uuid5(uuid.NAMESPACE_DNS, "FabricKG")

# ---------------------------------------------------------------------------
# model.yaml property type → Fabric type name
# ---------------------------------------------------------------------------

_PROP_TYPE_MAP: dict[str, str] = {
    "string": "String",
    "int": "Int64",
    "double": "Double",
    "boolean": "Boolean",
    "timestamp": "DateTime",
    "blob_url": "String",  # emitted with "format": "uri" — see _build_property
}

# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class OntologyCompilerError(ValueError):
    """Raised when model.yaml or ids.lock.json fail validation."""


# ---------------------------------------------------------------------------
# Deterministic GUID helper
# ---------------------------------------------------------------------------


def _derive_guid(type_name: str, table: str) -> str:
    """Return a deterministic UUID v5 from type name + table name.

    Stable across environments and re-runs.  Two types binding to the same
    table will produce different GUIDs because ``type_name`` differs.
    """
    return str(uuid.uuid5(_ONTOLOGY_NS, f"{type_name}:{table}"))


# ---------------------------------------------------------------------------
# JSON / Base64 helper
# ---------------------------------------------------------------------------


def _b64(obj: dict[str, Any]) -> str:
    """Return the Base64-encoded UTF-8 JSON of *obj* (no trailing newline)."""
    return base64.b64encode(
        json.dumps(obj, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate(model: dict[str, Any], ids: dict[str, Any]) -> None:
    """Raise :class:`OntologyCompilerError` if the model or ID lock is invalid.

    Checks (per SPEC-003 §6.9 steps 3–5):
    1. Every entity type in model.yaml has an ID in ids.lock.json.
    2. Every relationship type in model.yaml has an ID in ids.lock.json.
    3. No duplicate IDs across entity and relationship type maps.
    4. Every relationship type references known entity type names.
    """
    entity_ids: dict[str, str] = ids.get("entityTypes", {})
    rel_ids: dict[str, str] = ids.get("relationshipTypes", {})

    known_entity_names: set[str] = {et["name"] for et in model.get("entityTypes", [])}

    # 1. Entity type IDs
    for et in model.get("entityTypes", []):
        name = et["name"]
        if name not in entity_ids:
            raise OntologyCompilerError(
                f"Entity type '{name}' in model.yaml has no matching ID in ids.lock.json"
            )

    # 2. Relationship type IDs
    for rt in model.get("relationshipTypes", []):
        name = rt["name"]
        if name not in rel_ids:
            raise OntologyCompilerError(
                f"Relationship type '{name}' in model.yaml has no matching ID in ids.lock.json"
            )

    # 3. No duplicate IDs
    all_ids = list(entity_ids.values()) + list(rel_ids.values())
    seen: set[str] = set()
    for id_val in all_ids:
        if id_val in seen:
            raise OntologyCompilerError(
                f"Duplicate type ID '{id_val}' detected in ids.lock.json"
            )
        seen.add(id_val)

    # 4. Relationship source / target types must exist as entity types
    for rt in model.get("relationshipTypes", []):
        src = rt.get("sourceType")
        tgt = rt.get("targetType")
        if src and src not in known_entity_names:
            raise OntologyCompilerError(
                f"Relationship type '{rt['name']}' references unknown sourceType '{src}'"
            )
        if tgt and tgt not in known_entity_names:
            raise OntologyCompilerError(
                f"Relationship type '{rt['name']}' references unknown targetType '{tgt}'"
            )


# ---------------------------------------------------------------------------
# Part builders
# ---------------------------------------------------------------------------


def _build_platform(ontology_name: str) -> dict[str, Any]:
    logical_id = str(uuid.uuid5(_ONTOLOGY_NS, "ontology:logicalId"))
    return {
        "$schema": _PLATFORM_SCHEMA,
        "metadata": {
            "type": "Ontology",
            "displayName": ontology_name,
        },
        "config": {
            "version": "2.0",
            "logicalId": logical_id,
        },
    }


def _build_property(prop: dict[str, Any]) -> dict[str, Any]:
    ptype = prop.get("type", "string")
    fabric_type = _PROP_TYPE_MAP.get(ptype, "String")
    out: dict[str, Any] = {
        "name": prop["name"],
        "type": fabric_type,
        "isRequired": bool(prop.get("required", False)),
    }
    if ptype == "blob_url":
        out["format"] = "uri"
    return out


def _build_entity_definition(et: dict[str, Any], type_id: str) -> dict[str, Any]:
    return {
        "$schema": _ENTITY_TYPE_DEF_SCHEMA,
        "typeId": type_id,
        "name": et["name"],
        "description": et.get("description", ""),
        "properties": [_build_property(p) for p in et.get("properties", [])],
    }


def _build_data_binding(
    et: dict[str, Any],
    binding_guid: str,
    lakehouse_id: str,
) -> dict[str, Any]:
    db = et.get("dataBinding", {})
    obj: dict[str, Any] = {
        "$schema": _DATA_BINDING_SCHEMA,
        "bindingId": binding_guid,
        "displayName": f"{et['name']} from {db.get('table', '')}",
        "dataSourceType": "Lakehouse",
        "lakehouseId": lakehouse_id,
        "tableName": db.get("table", ""),
        "entityIdColumn": db.get("entityIdColumn", ""),
        "displayNameColumn": db.get("displayNameColumn", ""),
    }
    if db.get("typeFilterColumn"):
        obj["typeFilterColumn"] = db["typeFilterColumn"]
        # Include typeFilterValue even if empty string — callers can rely on its presence
        # when typeFilterColumn is set (e.g., ImageAsset binds all rows by setting value "")
        obj["typeFilterValue"] = db.get("typeFilterValue", "")
    mappings = [
        {"propertyName": col["property"], "columnName": col["column"]}
        for col in db.get("additionalColumns", [])
    ]
    if mappings:
        obj["propertyMappings"] = mappings
    return obj


def _build_relationship_definition(
    rt: dict[str, Any],
    type_id: str,
    entity_ids: dict[str, str],
    rel_ids: dict[str, str],
) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "$schema": _RELATIONSHIP_TYPE_DEF_SCHEMA,
        "typeId": type_id,
        "name": rt["name"],
        "description": rt.get("description", ""),
        "sourceTypeId": entity_ids.get(rt.get("sourceType", ""), ""),
        "targetTypeId": entity_ids.get(rt.get("targetType", ""), ""),
    }
    # Emit inverseTypeId for materialize and alias policies
    inverse_policy = rt.get("inversePolicy", "none")
    inverse_name = rt.get("inverseName")
    if inverse_policy in ("materialize", "alias") and inverse_name:
        inverse_id = rel_ids.get(inverse_name)
        if inverse_id:
            obj["inverseTypeId"] = inverse_id
    return obj


def _build_contextualization(
    rt: dict[str, Any],
    ctx_guid: str,
    lakehouse_id: str,
) -> dict[str, Any]:
    db = rt.get("dataBinding", {})
    obj: dict[str, Any] = {
        "$schema": _CONTEXTUALIZATION_SCHEMA,
        "contextualizationId": ctx_guid,
        "displayName": f"{rt['name']} from {db.get('table', '')}",
        "dataSourceType": "Lakehouse",
        "lakehouseId": lakehouse_id,
        "tableName": db.get("table", ""),
        "relationshipIdColumn": db.get("relationshipIdColumn", ""),
        "sourceEntityIdColumn": db.get("sourceEntityIdColumn", ""),
        "targetEntityIdColumn": db.get("targetEntityIdColumn", ""),
    }
    if db.get("typeFilterColumn"):
        obj["typeFilterColumn"] = db["typeFilterColumn"]
        obj["typeFilterValue"] = db.get("typeFilterValue", "")
    if db.get("evidenceIdColumn"):
        obj["evidenceIdColumn"] = db["evidenceIdColumn"]
    return obj


# ---------------------------------------------------------------------------
# Main compiler class
# ---------------------------------------------------------------------------


class OntologyCompiler:
    """Compiles model.yaml + ids.lock.json into Fabric Ontology definition parts.

    Usage::

        compiler = OntologyCompiler(
            model_path="ontology/model.yaml",
            ids_lock_path="ontology/ids.lock.json",
            lakehouse_id="c1a44e9d-...",   # from env config; empty string OK for tests
        )

        # Write all files to disk
        compiler.compile("build/ontology")

        # Or get REST InlineBase64 parts without touching the filesystem
        parts = compiler.get_rest_parts()

    Raises :class:`OntologyCompilerError` immediately at construction if the
    model or ID lock fails validation.
    """

    def __init__(
        self,
        model_path: Path | str,
        ids_lock_path: Path | str,
        lakehouse_id: str = "",
    ) -> None:
        self.model_path = Path(model_path)
        self.ids_lock_path = Path(ids_lock_path)
        self.lakehouse_id = lakehouse_id

        with self.model_path.open(encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        # Support both `{ontology: {...}}` wrapper and bare dict
        self.model: dict[str, Any] = raw.get("ontology", raw) if isinstance(raw, dict) else raw

        with self.ids_lock_path.open(encoding="utf-8") as fh:
            self.ids: dict[str, Any] = json.load(fh)

        _validate(self.model, self.ids)

    # ------------------------------------------------------------------
    # Internal: iterate (relative_path, content_dict) for every part
    # ------------------------------------------------------------------

    def _iter_parts(self) -> list[tuple[str, dict[str, Any]]]:
        """Return all ontology parts as (relative_path, content_dict) pairs.

        The top-level ``definition.json`` manifest is *not* included here —
        it references these parts.  The ``.platform`` file IS included.
        """
        entity_ids: dict[str, str] = self.ids.get("entityTypes", {})
        rel_ids: dict[str, str] = self.ids.get("relationshipTypes", {})
        ontology_name: str = self.model.get("name", "FabricKG")

        parts: list[tuple[str, dict[str, Any]]] = []

        # .platform
        parts.append((".platform", _build_platform(ontology_name)))

        # Entity types → definition.json + DataBindings/{guid}.json
        for et in self.model.get("entityTypes", []):
            name = et["name"]
            type_id = entity_ids[name]

            parts.append(
                (
                    f"EntityTypes/{type_id}/definition.json",
                    _build_entity_definition(et, type_id),
                )
            )

            db = et.get("dataBinding", {})
            table = db.get("table", "")
            guid = _derive_guid(name, table)
            parts.append(
                (
                    f"EntityTypes/{type_id}/DataBindings/{guid}.json",
                    _build_data_binding(et, guid, self.lakehouse_id),
                )
            )

        # Relationship types → definition.json + Contextualizations/{guid}.json
        for rt in self.model.get("relationshipTypes", []):
            name = rt["name"]
            type_id = rel_ids[name]

            parts.append(
                (
                    f"RelationshipTypes/{type_id}/definition.json",
                    _build_relationship_definition(rt, type_id, entity_ids, rel_ids),
                )
            )

            db = rt.get("dataBinding", {})
            table = db.get("table", "")
            guid = _derive_guid(name, table)
            parts.append(
                (
                    f"RelationshipTypes/{type_id}/Contextualizations/{guid}.json",
                    _build_contextualization(rt, guid, self.lakehouse_id),
                )
            )

        return parts

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_rest_parts(self) -> list[dict[str, Any]]:
        """Return all parts in REST InlineBase64 payload format.

        Each item is::

            {
                "path":        "<relative path under the ontology root>",
                "payload":     "<base64-encoded UTF-8 JSON>",
                "payloadType": "InlineBase64",
            }

        This is exactly the format consumed by the Fabric REST API
        ``/workspaces/{id}/items`` create/update endpoint.
        """
        return [
            {
                "path": path,
                "payload": _b64(content),
                "payloadType": "InlineBase64",
            }
            for path, content in self._iter_parts()
        ]

    def compile(self, out_dir: Path | str) -> Path:
        """Write all ontology definition files to *out_dir*.

        Creates the full directory structure::

            out_dir/
              .platform
              definition.json
              EntityTypes/{typeId}/definition.json
              EntityTypes/{typeId}/DataBindings/{guid}.json
              RelationshipTypes/{typeId}/definition.json
              RelationshipTypes/{typeId}/Contextualizations/{guid}.json

        Returns the ``out_dir`` path for chaining.
        """
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)

        parts = self._iter_parts()

        # Write individual part files
        for rel_path, content in parts:
            target = out / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(content, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        # Write top-level definition.json (manifest with Base64 payloads)
        definition: dict[str, Any] = {
            "parts": [
                {
                    "path": rel_path,
                    "payload": _b64(content),
                    "payloadType": "InlineBase64",
                }
                for rel_path, content in parts
            ]
        }
        (out / "definition.json").write_text(
            json.dumps(definition, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        return out
