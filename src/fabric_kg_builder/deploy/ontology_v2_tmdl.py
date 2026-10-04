"""Convert compiled v1 Ontology JSON parts into the v2 (TMDL) definition format.

Fabric Ontology items exist in two definition formats that share the same item
type: v1 (``.platform`` ``config.version`` "1.0", ``EntityTypes/<id>/...`` JSON
parts) and v2 (``config.version`` "2.0", TMDL parts). Only v2 items open in the
new ontology experience, where inheritance is shown as "entity to inherit
from". This module is a pure post-processing step: it reads the already
compiled, bound v1 parts and emits an equivalent v2 definition.

Live-probed v2 encodings this module relies on:

* ``baseEntityType: <Name>`` on entities; abstract entities have no
  ``backingTable`` and may still declare ``keyProperty``;
* ``redefines: <AncestorEntity>.<property>`` (path form; the JSON form is
  rejected) for the inherited key/label redefinitions;
* property bindings use ``backingConfiguration`` / ``valueColumn: t.c``;
* a description set locally on a derived entity/property must be declared as
  an override with the child object ``overriddenMetadataField description``,
  placed after all scalar properties (otherwise the service rejects it with
  ``EntityInheritanceMetadataValueWithoutOverride``);
* a relationship is backed by a junction table alias that is not any entity's
  ``backingTable`` plus two TOM relationships whose ``fromColumn`` is the
  junction column (many side) and ``toColumn`` is the entity key column.
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

V2_PLATFORM_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/"
    "platformProperties/2.0.0/schema.json"
)
V2_DEFINITION_VERSION = "2.0"
EXPRESSION_NAME = "DirectLakeSource"
_LINEAGE_NAMESPACE = uuid.UUID("6f0f3a52-8f61-5c1e-9d39-0b7c2f0a7e21")
_SIMPLE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# v1 entity property valueType -> v2 entity property dataType.
_PROPERTY_DATA_TYPES = {
    "String": "String",
    "Boolean": "Boolean",
    "BigInt": "Int64",
    "Double": "Double",
    "DateTime": "DateTime",
    "Decimal": "Decimal",
}
# v2 entity property dataType -> table column (AS) dataType.
_COLUMN_DATA_TYPES = {
    "String": "string",
    "Boolean": "boolean",
    "Int64": "int64",
    "Double": "double",
    "DateTime": "dateTime",
    "Decimal": "decimal",
}


class OntologyV2ConversionError(ValueError):
    """Raised when v1 parts cannot be represented as a v2 definition."""


@dataclass
class _Property:
    id: str
    name: str
    data_type: str
    redefines: str | None
    description: str
    column: str | None = None


@dataclass
class _Entity:
    id: str
    name: str
    base_id: str | None
    key_ids: list[str]
    description: str
    properties: list[_Property]
    table: str | None = None


@dataclass
class _Relationship:
    id: str
    name: str
    source_id: str
    target_id: str
    description: str
    table: str
    source_column: str
    target_column: str


@dataclass
class _Tables:
    columns: dict[str, dict[str, str]] = field(default_factory=dict)

    def add(self, table: str, column: str, data_type: str) -> None:
        cols = self.columns.setdefault(table, {})
        previous = cols.get(column)
        if previous is not None and previous != data_type:
            raise OntologyV2ConversionError(
                f"column {table}.{column} bound with conflicting types "
                f"{previous!r} and {data_type!r}"
            )
        cols[column] = data_type


def _lineage(*key: str) -> str:
    return str(uuid.uuid5(_LINEAGE_NAMESPACE, "\u0000".join(key)))


def _ident(name: str) -> str:
    if _SIMPLE_IDENTIFIER.match(name):
        return name
    return "'" + name.replace("'", "''") + "'"


def _doc(text: str, indent: str = "") -> list[str]:
    return [f"{indent}/// {line}".rstrip() for line in text.strip().splitlines()]


def _decode(part: Mapping[str, Any]) -> Any:
    return json.loads(base64.b64decode(part["payload"]))


def _encode(path: str, text: str) -> dict[str, str]:
    return {
        "path": path,
        "payload": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "payloadType": "InlineBase64",
    }


def _description(definition: Mapping[str, Any]) -> str:
    enrichment = definition.get("semanticEnrichment") or {}
    return str(enrichment.get("description") or "")


def _parse(v1_parts: Iterable[Mapping[str, Any]]):
    entities: dict[str, _Entity] = {}
    bindings: dict[str, Mapping[str, Any]] = {}
    rel_defs: dict[str, Mapping[str, Any]] = {}
    contexts: dict[str, list[Mapping[str, Any]]] = {}
    for part in v1_parts:
        path = part["path"]
        if m := re.fullmatch(r"EntityTypes/(\d+)/definition\.json", path):
            d = _decode(part)
            props = []
            for p in d.get("properties") or []:
                vt = p.get("valueType")
                if vt not in _PROPERTY_DATA_TYPES:
                    raise OntologyV2ConversionError(
                        f"unsupported valueType {vt!r} on {d['name']}.{p['name']}"
                    )
                props.append(
                    _Property(
                        id=str(p["id"]),
                        name=p["name"],
                        data_type=_PROPERTY_DATA_TYPES[vt],
                        redefines=(str(p["redefines"]) if p.get("redefines") else None),
                        description=_description(p),
                    )
                )
            if d.get("timeseriesProperties"):
                raise OntologyV2ConversionError(
                    f"time-series properties on {d['name']} are not supported"
                )
            entities[m.group(1)] = _Entity(
                id=m.group(1),
                name=d["name"],
                base_id=(str(d["baseEntityTypeId"]) if d.get("baseEntityTypeId") else None),
                key_ids=[str(x) for x in d.get("entityIdParts") or []],
                description=_description(d),
                properties=props,
            )
        elif m := re.fullmatch(r"EntityTypes/(\d+)/DataBindings/[^/]+\.json", path):
            if m.group(1) in bindings:
                raise OntologyV2ConversionError(
                    f"entity type {m.group(1)} has more than one data binding"
                )
            bindings[m.group(1)] = _decode(part)
        elif m := re.fullmatch(r"RelationshipTypes/(\d+)/definition\.json", path):
            rel_defs[m.group(1)] = _decode(part)
        elif m := re.fullmatch(
            r"RelationshipTypes/(\d+)/Contextualizations/[^/]+\.json", path
        ):
            contexts.setdefault(m.group(1), []).append(_decode(part))
    return entities, bindings, rel_defs, contexts


def _bind_entities(
    entities: dict[str, _Entity], bindings: Mapping[str, Mapping[str, Any]]
) -> None:
    for entity_id, binding in bindings.items():
        entity = entities.get(entity_id)
        if entity is None:
            raise OntologyV2ConversionError(
                f"data binding for unknown entity type {entity_id}"
            )
        config = binding["dataBindingConfiguration"]
        if config.get("dataBindingType", "NonTimeSeries") != "NonTimeSeries":
            raise OntologyV2ConversionError(
                f"unsupported binding type on {entity.name}"
            )
        entity.table = config["sourceTableProperties"]["sourceTableName"]
        by_id = {p.id: p for p in entity.properties}
        for pb in config.get("propertyBindings") or []:
            prop = by_id.get(str(pb["targetPropertyId"]))
            if prop is None:
                raise OntologyV2ConversionError(
                    f"binding targets unknown property {pb['targetPropertyId']} "
                    f"on {entity.name}"
                )
            prop.column = pb["sourceColumnName"]


def _find_property(
    entities: Mapping[str, _Entity], entity: _Entity, property_id: str
) -> tuple[_Entity, _Property]:
    current: _Entity | None = entity
    seen: set[str] = set()
    while current is not None and current.id not in seen:
        seen.add(current.id)
        for prop in current.properties:
            if prop.id == property_id:
                return current, prop
        current = entities.get(current.base_id) if current.base_id else None
    raise OntologyV2ConversionError(
        f"property {property_id} not found on {entity.name} or its ancestors"
    )


def _relationships(
    entities: Mapping[str, _Entity],
    rel_defs: Mapping[str, Mapping[str, Any]],
    contexts: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[_Relationship]:
    out: list[_Relationship] = []
    for rel_id, d in sorted(rel_defs.items(), key=lambda kv: int(kv[0])):
        ctxs = contexts.get(rel_id) or []
        if len(ctxs) != 1:
            raise OntologyV2ConversionError(
                f"relationship {d['name']} needs exactly one contextualization, "
                f"found {len(ctxs)}"
            )
        ctx = ctxs[0]
        source_id = str(d["source"]["entityTypeId"])
        target_id = str(d["target"]["entityTypeId"])
        for end_id in (source_id, target_id):
            if end_id not in entities or entities[end_id].table is None:
                raise OntologyV2ConversionError(
                    f"relationship {d['name']} references unbound entity {end_id}"
                )
        src_bind = ctx.get("sourceKeyRefBindings") or []
        tgt_bind = ctx.get("targetKeyRefBindings") or []
        if len(src_bind) != 1 or len(tgt_bind) != 1:
            raise OntologyV2ConversionError(
                f"relationship {d['name']} must use single-column key bindings"
            )
        out.append(
            _Relationship(
                id=rel_id,
                name=d["name"],
                source_id=source_id,
                target_id=target_id,
                description=_description(d),
                table=ctx["dataBindingTable"]["sourceTableName"],
                source_column=src_bind[0]["sourceColumnName"],
                target_column=tgt_bind[0]["sourceColumnName"],
            )
        )
    return out


def _key_column(entities: Mapping[str, _Entity], entity: _Entity) -> tuple[str, str]:
    if len(entity.key_ids) != 1:
        raise OntologyV2ConversionError(
            f"entity {entity.name} must have exactly one key property"
        )
    _, prop = _find_property(entities, entity, entity.key_ids[0])
    if prop.column is None:
        raise OntologyV2ConversionError(f"key of {entity.name} is not bound")
    return prop.column, prop.data_type


# A locally set description on a derived entity/property must be marked as
# an override of the inherited value (EntityInheritanceMetadataValueWithoutOverride).
_OVERRIDE_DESCRIPTION = "overriddenMetadataField description"


def _entity_tmdl(entities: Mapping[str, _Entity], entity: _Entity) -> str:
    lines = _doc(entity.description)
    lines += [f"entity {_ident(entity.name)}", f"\tlineageTag: {_lineage('entity', entity.name)}"]
    if entity.table:
        lines.append(f"\tbackingTable: {_ident(entity.table)}")
    if entity.base_id:
        lines.append(f"\tbaseEntityType: {_ident(entities[entity.base_id].name)}")
    if entity.key_ids:
        if len(entity.key_ids) != 1:
            raise OntologyV2ConversionError(
                f"entity {entity.name} has a composite key; not supported"
            )
        _, key = _find_property(entities, entity, entity.key_ids[0])
        lines.append(f"\tkeyProperty: {_ident(key.name)}")
    if entity.base_id and entity.description:
        lines += ["", f"\t{_OVERRIDE_DESCRIPTION}"]
    for prop in entity.properties:
        lines.append("")
        lines += _doc(prop.description, "\t")
        lines += [
            f"\tproperty {_ident(prop.name)}",
            f"\t\tdataType: {prop.data_type}",
            f"\t\tlineageTag: {_lineage('property', entity.name, prop.name)}",
        ]
        if prop.redefines:
            if not entity.base_id:
                raise OntologyV2ConversionError(
                    f"{entity.name}.{prop.name} redefines without a base entity"
                )
            owner, ancestor = _find_property(
                entities, entities[entity.base_id], prop.redefines
            )
            if ancestor.data_type != prop.data_type:
                raise OntologyV2ConversionError(
                    f"{entity.name}.{prop.name} changes the type of "
                    f"{owner.name}.{ancestor.name}"
                )
            lines.append(f"\t\tredefines: {_ident(owner.name)}.{_ident(ancestor.name)}")
            if prop.description:
                lines += ["", f"\t\t{_OVERRIDE_DESCRIPTION}"]
        if prop.column is not None:
            lines += [
                "",
                "\t\tbackingConfiguration",
                f"\t\t\tvalueColumn: {_ident(entity.table or '')}.{_ident(prop.column)}",
            ]
    return "\n".join(lines) + "\n"


def _table_tmdl(alias: str, source_table: str, columns: Mapping[str, str]) -> str:
    lines = [f"table {_ident(alias)}", f"\tlineageTag: {_lineage('table', alias)}", ""]
    for column, data_type in columns.items():
        lines += [
            f"\tcolumn {_ident(column)}",
            f"\t\tdataType: {data_type}",
            f"\t\tlineageTag: {_lineage('column', alias, column)}",
            f"\t\tsourceColumn: {column}",
            "",
        ]
    lines += [
        f"\tpartition {_ident(alias)} = entity",
        "\t\tmode: directLake",
        "\t\tsource",
        f"\t\t\tentityName: {source_table}",
        "\t\t\tschemaName: dbo",
        f"\t\t\texpressionSource: {EXPRESSION_NAME}",
    ]
    return "\n".join(lines) + "\n"


def _topological(entities: Mapping[str, _Entity]) -> list[_Entity]:
    ordered: list[_Entity] = []
    state: dict[str, int] = {}

    def visit(entity: _Entity) -> None:
        if state.get(entity.id) == 2:
            return
        if state.get(entity.id) == 1:
            raise OntologyV2ConversionError(f"inheritance cycle at {entity.name}")
        state[entity.id] = 1
        if entity.base_id:
            if entity.base_id not in entities:
                raise OntologyV2ConversionError(
                    f"{entity.name} inherits from unknown entity {entity.base_id}"
                )
            visit(entities[entity.base_id])
        state[entity.id] = 2
        ordered.append(entity)

    for entity_id in sorted(entities, key=int):
        visit(entities[entity_id])
    return ordered


def convert_v1_parts_to_v2_tmdl(
    v1_parts: Iterable[Mapping[str, Any]],
    *,
    workspace_id: str,
    lakehouse_id: str,
    display_name: str,
    column_types: Mapping[str, Mapping[str, str]] | None = None,
) -> list[dict[str, str]]:
    """Return InlineBase64 v2 parts equivalent to the given v1 ontology parts.

    ``column_types`` optionally overrides the inferred AS column dataType per
    ``{source_table: {column: dataType}}`` (for example from Parquet schemas).
    """
    entities, bindings, rel_defs, contexts = _parse(v1_parts)
    if not entities:
        raise OntologyV2ConversionError("no entity types in v1 parts")
    _bind_entities(entities, bindings)
    relationships = _relationships(entities, rel_defs, contexts)
    overrides = column_types or {}
    ordered = _topological(entities)

    names = [e.name.casefold() for e in ordered] + [r.name.casefold() for r in relationships]
    if len(set(names)) != len(names):
        raise OntologyV2ConversionError("entity/relationship names collide case-insensitively")

    tables = _Tables()
    sources: dict[str, str] = {}

    def column_type(source: str, column: str, inferred: str) -> str:
        return overrides.get(source, {}).get(column, inferred)

    for entity in ordered:
        if entity.table is None:
            continue
        sources[entity.table] = entity.table
        for prop in entity.properties:
            if prop.column is not None:
                tables.add(
                    entity.table,
                    prop.column,
                    column_type(entity.table, prop.column, _COLUMN_DATA_TYPES[prop.data_type]),
                )

    entity_tables = set(sources)
    tom_relationships: list[tuple[str, str, str, bool]] = []
    for rel in relationships:
        alias = rel.table
        if alias in entity_tables:
            alias = f"{rel.table}_junction"
        if alias in sources:
            raise OntologyV2ConversionError(f"junction table {rel.table} is reused")
        sources[alias] = rel.table
        ends = []
        for end, column in (("source", rel.source_column), ("target", rel.target_column)):
            entity = entities[rel.source_id if end == "source" else rel.target_id]
            key_column, key_type = _key_column(entities, entity)
            tables.add(
                alias, column, column_type(rel.table, column, _COLUMN_DATA_TYPES[key_type])
            )
            tom_name = f"{rel.name}__{end}"
            ends.append(tom_name)
            # Two relationships between the same junction/entity pair (a
            # self-relationship) must leave the second one inactive.
            active = not (end == "target" and rel.source_id == rel.target_id)
            tom_relationships.append(
                (tom_name, f"{_ident(alias)}.{_ident(column)}",
                 f"{_ident(entity.table or '')}.{_ident(key_column)}", active)
            )
        rel.table = alias

    parts: list[dict[str, str]] = []
    platform = {
        "$schema": V2_PLATFORM_SCHEMA,
        "metadata": {"type": "Ontology", "displayName": display_name},
        "config": {
            "version": V2_DEFINITION_VERSION,
            "logicalId": "00000000-0000-0000-0000-000000000000",
        },
    }
    parts.append(_encode(".platform", json.dumps(platform, indent=2) + "\n"))
    parts.append(_encode("database.tmdl", "database\n\tcompatibilityLevel: 1000000\n"))
    model = ["model Model", ""]
    model += [f"ref table {_ident(t)}" for t in sources]
    model += [f"ref entity {_ident(e.name)}" for e in ordered]
    model += ["", "ref namespace default", ""]
    parts.append(_encode("model.tmdl", "\n".join(model)))
    parts.append(_encode("namespaces/default.tmdl", "namespace default\n\tlineageTag: default\n"))
    onelake = f"https://onelake.dfs.fabric.microsoft.com/{workspace_id}/{lakehouse_id}"
    parts.append(
        _encode(
            "expressions.tmdl",
            f"expression {EXPRESSION_NAME} = let Source = AzureStorage.DataLake("
            f'"{onelake}", [HierarchicalNavigation=true]) in Source\n'
            f"\tlineageTag: {_lineage('expression', EXPRESSION_NAME)}\n",
        )
    )
    for alias, source in sources.items():
        parts.append(
            _encode(f"tables/{alias}.tmdl", _table_tmdl(alias, source, tables.columns.get(alias, {})))
        )
    for entity in ordered:
        parts.append(_encode(f"entities/{entity.name}.tmdl", _entity_tmdl(entities, entity)))
    if relationships:
        rel_lines: list[str] = []
        for name, from_col, to_col, active in tom_relationships:
            rel_lines += [f"relationship {_ident(name)}"]
            if not active:
                rel_lines.append("\tisActive: false")
            rel_lines += [f"\tfromColumn: {from_col}", f"\ttoColumn: {to_col}", ""]
        parts.append(_encode("relationships.tmdl", "\n".join(rel_lines)))
        er_lines: list[str] = []
        for rel in relationships:
            er_lines += _doc(rel.description)
            er_lines += [
                f"entityRelationship {_ident(rel.name)}",
                f"\tlineageTag: {_lineage('entityRelationship', rel.name)}",
                f"\tfromEntity: {_ident(entities[rel.source_id].name)}",
                f"\ttoEntity: {_ident(entities[rel.target_id].name)}",
                "",
                "\tbackingConfiguration",
                "\t\ttype: table",
                f"\t\ttable: {_ident(rel.table)}",
                f"\t\tfromRelationship: {_ident(rel.name + '__source')}",
                f"\t\ttoRelationship: {_ident(rel.name + '__target')}",
                "",
            ]
        parts.append(_encode("entityRelationships.tmdl", "\n".join(er_lines)))
    return parts


_ARROW_TYPES = {
    "string": "string",
    "large_string": "string",
    "bool": "boolean",
    "int8": "int64",
    "int16": "int64",
    "int32": "int64",
    "int64": "int64",
    "float": "double",
    "double": "double",
}


def column_types_from_parquet_dir(tables_dir: Any) -> dict[str, dict[str, str]]:
    """Infer ``{table: {column: dataType}}`` from ``<table>.parquet`` schemas."""
    from pathlib import Path

    import pyarrow.parquet as pq

    result: dict[str, dict[str, str]] = {}
    for path in sorted(Path(tables_dir).glob("*.parquet")):
        mapping: dict[str, str] = {}
        for fld in pq.read_schema(path):
            arrow = str(fld.type)
            if arrow.startswith("timestamp") or arrow.startswith("date"):
                mapping[fld.name] = "dateTime"
            elif arrow.startswith("decimal"):
                mapping[fld.name] = "decimal"
            elif arrow in _ARROW_TYPES:
                mapping[fld.name] = _ARROW_TYPES[arrow]
        result[path.stem] = mapping
    return result
