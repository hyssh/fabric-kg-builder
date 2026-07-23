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
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import yaml

from fabric_kg_builder.ontology.identity_validation import validate_identity

# ---------------------------------------------------------------------------
# Schema URL constants
# ---------------------------------------------------------------------------

_PLATFORM_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/"
    "platformProperties/2.0.0/schema.json"
)
_ENTITY_TYPE_DEF_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/ontology/"
    "entityType/1.0.0/schema.json"
)
_DATA_BINDING_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/ontology/"
    "dataBinding/1.0.0/schema.json"
)
_RELATIONSHIP_TYPE_DEF_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/ontology/"
    "relationshipType/1.0.0/schema.json"
)
_CONTEXTUALIZATION_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/ontology/"
    "contextualization/1.0.0/schema.json"
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
    "int": "BigInt",
    "double": "Double",
    "boolean": "Boolean",
    "timestamp": "DateTime",
    "blob_url": "String",
}

_MAX_POSITIVE_BIGINT = 2**63 - 1

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


def _derive_bigint(seed: str) -> str:
    """Return a deterministic positive signed 64-bit integer string."""
    raw = int.from_bytes(
        hashlib.sha256(seed.encode("utf-8")).digest()[:8],
        "big",
    )
    return str((raw % (_MAX_POSITIVE_BIGINT - 1)) + 1)


def _entity_id_property_names(et: dict[str, Any]) -> list[str]:
    explicit = et.get("entityIdProperties")
    if explicit:
        return [str(name) for name in explicit]
    property_names = {
        str(prop.get("name"))
        for prop in et.get("properties", [])
        if prop.get("name")
    }
    source_column = str(
        et.get("dataBinding", {}).get("entityIdColumn") or "entity_id"
    )
    if source_column in property_names:
        return [source_column]
    if "entity_id" in property_names:
        return ["entity_id"]
    return [source_column]


def _display_name_property_name(et: dict[str, Any]) -> str:
    explicit = et.get("displayNameProperty")
    if explicit:
        return str(explicit)
    property_names = {
        str(prop.get("name"))
        for prop in et.get("properties", [])
        if prop.get("name")
    }
    if "display_name" in property_names:
        return "display_name"
    source_column = str(
        et.get("dataBinding", {}).get("displayNameColumn")
        or _entity_id_property_names(et)[0]
    )
    return source_column


def _normalized_entity_properties(
    et: dict[str, Any],
) -> list[dict[str, Any]]:
    properties = [dict(prop) for prop in et.get("properties", [])]
    property_names = {
        str(prop.get("name"))
        for prop in properties
        if prop.get("name")
    }
    required_names = {
        *_entity_id_property_names(et),
        _display_name_property_name(et),
    }
    for name in sorted(required_names - property_names):
        properties.append(
            {
                "name": name,
                "type": "string",
                "required": True,
                "description": "Fabric Ontology binding key.",
            }
        )
    return properties


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


def _physical_type_ids(
    ids: dict[str, Any],
) -> tuple[dict[str, str], dict[str, str]]:
    """Read legacy numeric maps or canonical semantic lock bindings."""
    if "entityTypes" in ids or "relationshipTypes" in ids:
        return (
            dict(ids.get("entityTypes", {})),
            dict(ids.get("relationshipTypes", {})),
        )

    def _extract(section: str) -> dict[str, str]:
        values = ids.get(section, {})
        if not isinstance(values, dict):
            return {}
        result: dict[str, str] = {}
        for name, binding in values.items():
            if not isinstance(binding, dict):
                continue
            fabric_id = binding.get("fabric_id")
            if fabric_id:
                result[str(name)] = str(fabric_id)
        return result

    return _extract("entity_types"), _extract("relationship_types")


def resolve_entity_identity_columns(
    model: dict[str, Any],
) -> dict[str, tuple[str, str]]:
    """Return the physical identity column for every entity binding.

    Returns a mapping of ``{entity_name: (binding_table, entityIdColumn)}``.
    Falls back to ``"entity_id"`` when no explicit ``entityIdColumn`` is
    declared — consistent with the rest of the compiler.
    """
    result: dict[str, tuple[str, str]] = {}
    for et in model.get("entityTypes", []):
        name = str(et.get("name", ""))
        if not name:
            continue
        db = et.get("dataBinding", {}) or {}
        table = str(db.get("table", ""))
        id_col = str(db.get("entityIdColumn") or "entity_id")
        result[name] = (table, id_col)
    return result


def validate_relationship_keys(
    model: dict[str, Any],
) -> list[str]:
    """Validate relationship FK endpoint columns against entity identity columns.

    Rules:
    - ``sourceEntityIdColumn`` must be non-empty when ``sourceType`` is declared.
    - ``targetEntityIdColumn`` must be non-empty when ``targetType`` is declared.
    - When the relationship binding table equals the entity binding table, the
      FK column must equal the entity's declared ``entityIdColumn``.

    Returns a list of ``ONTOLOGY_RELATIONSHIP_KEY_MISMATCH`` error message strings.
    A non-empty list should be treated as a compilation error by callers.
    """
    entity_identity = resolve_entity_identity_columns(model)
    errors: list[str] = []

    for rt in model.get("relationshipTypes", []):
        name = str(rt.get("name", ""))
        src_type = rt.get("sourceType") or ""
        tgt_type = rt.get("targetType") or ""
        db = rt.get("dataBinding", {}) or {}
        rel_table = str(db.get("table", ""))
        src_col = str(db.get("sourceEntityIdColumn") or "")
        tgt_col = str(db.get("targetEntityIdColumn") or "")

        if not src_type and not tgt_type:
            continue

        if src_type and not src_col:
            _, src_id = entity_identity.get(str(src_type), ("", "entity_id"))
            errors.append(
                f"ONTOLOGY_RELATIONSHIP_KEY_MISMATCH: relationship '{name}', "
                f"source endpoint column is empty/missing, "
                f"expected entity identity column '{src_id}' "
                f"(entity type: {src_type})"
            )

        if tgt_type and not tgt_col:
            _, tgt_id = entity_identity.get(str(tgt_type), ("", "entity_id"))
            errors.append(
                f"ONTOLOGY_RELATIONSHIP_KEY_MISMATCH: relationship '{name}', "
                f"target endpoint column is empty/missing, "
                f"expected entity identity column '{tgt_id}' "
                f"(entity type: {tgt_type})"
            )

        # Same-table binding: FK column name must equal entity identity column.
        # Cross-table bindings (the common case) are FK references and do NOT
        # require name equality — only that the column is declared.
        if src_type and src_col and str(src_type) in entity_identity:
            src_table, src_id = entity_identity[str(src_type)]
            if (
                rel_table
                and src_table
                and rel_table == src_table
                and src_col != src_id
            ):
                errors.append(
                    f"ONTOLOGY_RELATIONSHIP_KEY_MISMATCH: relationship '{name}', "
                    f"source endpoint column '{src_col}' does not match "
                    f"entity identity column '{src_id}' "
                    f"(entity type: {src_type}, shared table: {rel_table})"
                )

        if tgt_type and tgt_col and str(tgt_type) in entity_identity:
            tgt_table, tgt_id = entity_identity[str(tgt_type)]
            if (
                rel_table
                and tgt_table
                and rel_table == tgt_table
                and tgt_col != tgt_id
            ):
                errors.append(
                    f"ONTOLOGY_RELATIONSHIP_KEY_MISMATCH: relationship '{name}', "
                    f"target endpoint column '{tgt_col}' does not match "
                    f"entity identity column '{tgt_id}' "
                    f"(entity type: {tgt_type}, shared table: {rel_table})"
                )

    return errors


def validate_date_types(
    model: dict[str, Any],
) -> list[str]:
    """Validate that timestamp-typed properties are not declared as partial dates.

    A property typed ``timestamp`` maps to Fabric ``DateTime``, which requires
    full ISO-8601 precision.  If a property is annotated with
    ``datePrecision: year``, ``datePrecision: year-month``, or
    ``datePrecision: partial``, it is incompatible with the strict DateTime
    mapping and must use ``string`` instead.

    Returns a list of ``PARTIAL_DATE_INCOMPATIBLE`` error message strings.
    """
    errors: list[str] = []
    for et in model.get("entityTypes", []):
        et_name = str(et.get("name", ""))
        for prop in et.get("properties", []):
            prop_type = str(prop.get("type", ""))
            if prop_type != "timestamp":
                continue
            prop_name = str(prop.get("name", ""))
            precision = prop.get("datePrecision") or prop.get("date_precision") or ""
            if str(precision).lower() in ("year", "year-month", "partial"):
                errors.append(
                    f"PARTIAL_DATE_INCOMPATIBLE: entity type '{et_name}', "
                    f"property '{prop_name}' declared as 'timestamp' (Fabric DateTime) "
                    f"but datePrecision='{precision}' indicates partial dates. "
                    "Use type 'string' to preserve year-only or year-month values "
                    "without inventing missing precision."
                )
    return errors


def _validate(model: dict[str, Any], ids: dict[str, Any]) -> None:
    """Raise :class:`OntologyCompilerError` if the model or ID lock is invalid.

    Checks (per SPEC-003 §6.9 steps 3–5):
    1. Every entity type in model.yaml has an ID in ids.lock.json.
    2. Every relationship type in model.yaml has an ID in ids.lock.json.
    3. No duplicate IDs across entity and relationship type maps.
    4. Every relationship type references known entity type names.
    5. Relationship FK endpoint columns are compatible with entity identity columns.
    6. Timestamp-typed properties are not annotated as partial-precision dates.
    """
    entity_ids, rel_ids = _physical_type_ids(ids)

    known_entity_names: set[str] = {et["name"] for et in model.get("entityTypes", [])}

    # 1. Entity type IDs
    for et in model.get("entityTypes", []):
        name = et["name"]
        if name not in entity_ids:
            raise OntologyCompilerError(
                f"Entity type '{name}' in model.yaml has no matching ID in ids.lock.json"
            )
        binding_id = et.get("dataBinding", {}).get("bindingId")
        if binding_id:
            try:
                uuid.UUID(str(binding_id))
            except ValueError as exc:
                raise OntologyCompilerError(
                    f"Entity type '{name}' has invalid bindingId "
                    f"'{binding_id}'."
                ) from exc
        property_ids = [
            str(prop.get("id") or _derive_bigint(f"{entity_ids[name]}:{prop['name']}"))
            for prop in _normalized_entity_properties(et)
        ]
        for property_id in property_ids:
            try:
                numeric_id = int(property_id)
            except ValueError as exc:
                raise OntologyCompilerError(
                    f"Entity type '{name}' has non-numeric property ID "
                    f"'{property_id}'."
                ) from exc
            if numeric_id <= 0 or numeric_id > _MAX_POSITIVE_BIGINT:
                raise OntologyCompilerError(
                    f"Entity type '{name}' has property ID '{property_id}' "
                    "outside Fabric's positive signed 64-bit range."
                )
        if len(property_ids) != len(set(property_ids)):
            raise OntologyCompilerError(
                f"Entity type '{name}' has duplicate property IDs."
            )

    # 2. Relationship type IDs
    for rt in model.get("relationshipTypes", []):
        name = rt["name"]
        if name not in rel_ids:
            raise OntologyCompilerError(
                f"Relationship type '{name}' in model.yaml has no matching ID in ids.lock.json"
            )
        contextualization_id = rt.get("dataBinding", {}).get(
            "contextualizationId"
        )
        if contextualization_id:
            try:
                uuid.UUID(str(contextualization_id))
            except ValueError as exc:
                raise OntologyCompilerError(
                    f"Relationship type '{name}' has invalid "
                    f"contextualizationId '{contextualization_id}'."
                ) from exc

    # 3. No duplicate IDs
    all_ids = list(entity_ids.values()) + list(rel_ids.values())
    for et in model.get("entityTypes", []):
        type_id = entity_ids[et["name"]]
        all_ids.extend(
            str(prop.get("id") or _derive_bigint(f"{type_id}:{prop['name']}"))
            for prop in _normalized_entity_properties(et)
        )
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

    # 5. Relationship FK endpoint columns must be compatible with entity identity columns
    key_errors = validate_relationship_keys(model)
    if key_errors:
        raise OntologyCompilerError(
            "Relationship endpoint key validation failed:\n"
            + "\n".join(f"  {e}" for e in key_errors)
        )

    # 6. Timestamp properties must not declare partial-date precision
    date_errors = validate_date_types(model)
    if date_errors:
        raise OntologyCompilerError(
            "Date type validation failed:\n"
            + "\n".join(f"  {e}" for e in date_errors)
        )

    # 7. OKV-001 / OKV-002 identity domain and partial-date gates.
    #    validate_identity() detects cross-table domain mismatches (OKV-001) and
    #    timestamp-typed properties whose name suggests partial dates (OKV-002)
    #    WITHOUT requiring a manual datePrecision annotation.
    okv_violations = validate_identity(model)
    okv_errors = [v for v in okv_violations if v.severity == "error"]
    if okv_errors:
        raise OntologyCompilerError(
            "Identity validation failed:\n"
            + "\n".join(f"  [{v.gate_id}] {v.message}" for v in okv_errors)
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


def _property_ids(
    et: dict[str, Any],
    type_id: str,
) -> dict[str, str]:
    return {
        prop["name"]: str(
            prop.get("id") or _derive_bigint(f"{type_id}:{prop['name']}")
        )
        for prop in _normalized_entity_properties(et)
    }


def _build_property(
    prop: dict[str, Any],
    property_id: str,
) -> dict[str, Any]:
    ptype = prop.get("type", "string")
    fabric_type = _PROP_TYPE_MAP.get(ptype, "String")
    return {
        "id": property_id,
        "name": prop["name"],
        "redefines": None,
        "baseTypeNamespaceType": None,
        "valueType": fabric_type,
    }


def _build_entity_definition(et: dict[str, Any], type_id: str) -> dict[str, Any]:
    property_ids = _property_ids(et, type_id)
    entity_id_properties = _entity_id_property_names(et)
    display_name_property = _display_name_property_name(et)
    if (
        not entity_id_properties
        or any(name not in property_ids for name in entity_id_properties)
    ):
        raise OntologyCompilerError(
            f"Entity type '{et['name']}' does not map every entity ID part "
            "to a declared property."
        )
    if display_name_property not in property_ids:
        raise OntologyCompilerError(
            f"Entity type '{et['name']}' display-name column does not map "
            "to a declared property."
        )
    return {
        "$schema": _ENTITY_TYPE_DEF_SCHEMA,
        "id": type_id,
        "namespace": "usertypes",
        "baseEntityTypeId": None,
        "name": et["name"],
        "entityIdParts": [
            property_ids[name] for name in entity_id_properties
        ],
        "displayNamePropertyId": property_ids[display_name_property],
        "namespaceType": "Custom",
        "visibility": "Visible",
        "properties": [
            _build_property(prop, property_ids[prop["name"]])
            for prop in _normalized_entity_properties(et)
        ],
        "timeseriesProperties": [],
        "untypedProperties": [],
    }


def _build_data_binding(
    et: dict[str, Any],
    binding_guid: str,
    workspace_id: str,
    lakehouse_id: str,
    schema: str,
    type_id: str,
) -> dict[str, Any]:
    db = et.get("dataBinding", {})
    property_ids = _property_ids(et, type_id)
    source_columns = {
        str(item["property"]): str(item["column"])
        for item in db.get("additionalColumns", [])
    }
    entity_id_properties = _entity_id_property_names(et)
    display_name_property = _display_name_property_name(et)
    if len(entity_id_properties) == 1:
        source_columns.setdefault(
            entity_id_properties[0],
            str(db.get("entityIdColumn", "")),
        )
    source_columns.setdefault(
        str(display_name_property),
        str(db.get("displayNameColumn", "")),
    )
    property_bindings = [
        {
            "sourceColumnName": source_columns[property_name],
            "targetPropertyId": property_ids[property_name],
        }
        for property_name in property_ids
        if source_columns.get(property_name)
    ]
    return {
        "$schema": _DATA_BINDING_SCHEMA,
        "id": binding_guid,
        "dataBindingConfiguration": {
            "dataBindingType": "NonTimeSeries",
            "propertyBindings": property_bindings,
            "sourceTableProperties": {
                "sourceType": "LakehouseTable",
                "workspaceId": workspace_id,
                "itemId": lakehouse_id,
                "sourceTableName": db.get("table", ""),
                "sourceSchema": schema,
            },
        },
    }


def _build_relationship_definition(
    rt: dict[str, Any],
    type_id: str,
    entity_ids: dict[str, str],
    rel_ids: dict[str, str],
) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "$schema": _RELATIONSHIP_TYPE_DEF_SCHEMA,
        "id": type_id,
        "namespace": "usertypes",
        "name": rt["name"],
        "namespaceType": "Custom",
        "source": {
            "entityTypeId": entity_ids.get(rt.get("sourceType", ""), "")
        },
        "target": {
            "entityTypeId": entity_ids.get(rt.get("targetType", ""), "")
        },
    }
    return obj


def _build_contextualization(
    rt: dict[str, Any],
    ctx_guid: str,
    workspace_id: str,
    lakehouse_id: str,
    schema: str,
    source_property_id: str,
    target_property_id: str,
) -> dict[str, Any]:
    db = rt.get("dataBinding", {})
    return {
        "$schema": _CONTEXTUALIZATION_SCHEMA,
        "id": ctx_guid,
        "dataBindingTable": {
            "workspaceId": workspace_id,
            "itemId": lakehouse_id,
            "sourceTableName": db.get("table", ""),
            "sourceSchema": schema,
            "sourceType": "LakehouseTable",
        },
        "sourceKeyRefBindings": [
            {
                "sourceColumnName": db.get("sourceEntityIdColumn", ""),
                "targetPropertyId": source_property_id,
            }
        ],
        "targetKeyRefBindings": [
            {
                "sourceColumnName": db.get("targetEntityIdColumn", ""),
                "targetPropertyId": target_property_id,
            }
        ],
    }


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
        workspace_id: str = "",
        lakehouse_id: str = "",
        schema: str = "dbo",
    ) -> None:
        self.model_path = Path(model_path)
        self.ids_lock_path = Path(ids_lock_path)
        self.workspace_id = workspace_id
        self.lakehouse_id = lakehouse_id
        self.schema = schema

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
        entity_ids, rel_ids = _physical_type_ids(self.ids)
        ontology_name: str = self.model.get("name", "FabricKG")
        entity_identifier_ids: dict[str, str] = {}

        parts: list[tuple[str, dict[str, Any]]] = []

        # .platform
        parts.append((".platform", _build_platform(ontology_name)))

        # Entity types → definition.json + DataBindings/{guid}.json
        for et in self.model.get("entityTypes", []):
            name = et["name"]
            type_id = entity_ids[name]
            property_ids = _property_ids(et, type_id)
            entity_id_properties = _entity_id_property_names(et)
            if len(entity_id_properties) != 1:
                raise OntologyCompilerError(
                    f"Entity type '{name}' must expose exactly one entity ID "
                    "property for relationship contextualization."
                )
            entity_identifier_ids[name] = property_ids[
                entity_id_properties[0]
            ]

            parts.append(
                (
                    f"EntityTypes/{type_id}/definition.json",
                    _build_entity_definition(et, type_id),
                )
            )

            db = et.get("dataBinding", {})
            table = db.get("table", "")
            guid = db.get("bindingId") or _derive_guid(name, table)
            parts.append(
                (
                    f"EntityTypes/{type_id}/DataBindings/{guid}.json",
                    _build_data_binding(
                    et,
                    guid,
                    self.workspace_id,
                    self.lakehouse_id,
                    self.schema,
                    type_id,
                    ),
                )
            )

        # Relationship types → definition.json + Contextualizations/{guid}.json
        for rt in self.model.get("relationshipTypes", []):
            name = rt["name"]
            type_id = rel_ids[name]

            parts.append(
                (
                    f"RelationshipTypes/{type_id}/definition.json",
                    _build_relationship_definition(
                    rt,
                    type_id,
                    entity_ids,
                    rel_ids,
                    ),
                )
            )

            db = rt.get("dataBinding", {})
            table = db.get("table", "")
            guid = db.get("contextualizationId") or _derive_guid(name, table)
            parts.append(
                (
                    f"RelationshipTypes/{type_id}/Contextualizations/{guid}.json",
                    _build_contextualization(
                    rt,
                    guid,
                    self.workspace_id,
                    self.lakehouse_id,
                    self.schema,
                    entity_identifier_ids[rt["sourceType"]],
                    entity_identifier_ids[rt["targetType"]],
                    ),
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

    def get_identity_mappings(self) -> dict[str, Any]:
        """Return entity and relationship identity mappings for dry-run output.

        Returns a dict with two keys:

        ``entities``
            ``{entity_name: {"table": str, "identity_column": str}}``
        ``relationships``
            ``{rel_name: {"table": str, "source_type": str,
                          "source_fk_column": str,
                          "target_type": str, "target_fk_column": str}}``

        These mappings expose the physical column resolution so that dry-run
        output can show how every endpoint is bound, and so that
        ONTOLOGY_RELATIONSHIP_KEY_MISMATCH errors can be traced to the
        correct column.
        """
        entity_identity = resolve_entity_identity_columns(self.model)
        entities_out: dict[str, Any] = {
            name: {"table": table, "identity_column": col}
            for name, (table, col) in entity_identity.items()
        }

        relationships_out: dict[str, Any] = {}
        for rt in self.model.get("relationshipTypes", []):
            name = str(rt.get("name", ""))
            db = rt.get("dataBinding", {}) or {}
            relationships_out[name] = {
                "table": str(db.get("table", "")),
                "source_type": str(rt.get("sourceType") or ""),
                "source_fk_column": str(db.get("sourceEntityIdColumn") or ""),
                "target_type": str(rt.get("targetType") or ""),
                "target_fk_column": str(db.get("targetEntityIdColumn") or ""),
            }

        return {"entities": entities_out, "relationships": relationships_out}
