"""Compile a Fabric DirectLake SemanticModel definition from materialized L5a tables.

The Fabric SemanticModel item is definition-bearing and its parts are TMDL
documents, not the ``model.bim`` JSON form.  The REST create path rejects the
JSON form's ``partition.mode`` property, so this module emits TMDL exclusively.

DirectLake partitions can only project scalar Delta columns.  Complex columns
(arrays, maps, structs) are therefore *excluded* rather than coerced, and every
exclusion is returned on the compilation result so the caller can report the
narrowing instead of silently shipping a model that omits data.
"""

from __future__ import annotations

import base64
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from .lakehouse_schema import resolve_lakehouse_schema

_SEMANTIC_MODEL_NAMESPACE = uuid.UUID("6f1c6d9e-6d0f-5f2b-9a1d-2f7c4b8e51aa")

# TMDL bare identifiers admit a leading underscore, which matters because every
# L5a projected column is ``__``-prefixed.  Quoting them instead is accepted but
# Fabric normalizes the quotes away on ingest, so emitting the bare form keeps a
# recompile byte-comparable against a live ``getDefinition`` readback.
_SIMPLE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Arrow type name -> TMDL dataType.  Anything absent is a complex type that
# DirectLake cannot project and that we refuse to guess at.
_SCALAR_DATA_TYPES = {
    "string": "string",
    "large_string": "string",
    "bool": "boolean",
    "int8": "int64",
    "int16": "int64",
    "int32": "int64",
    "int64": "int64",
    "float": "double",
    "double": "double",
    "date32[day]": "dateTime",
    "date64[ms]": "dateTime",
}


@dataclass(frozen=True)
class ExcludedColumn:
    """A Delta column DirectLake cannot project, recorded for honest reporting."""

    table_name: str
    column_name: str
    arrow_type: str


@dataclass(frozen=True)
class FabricSemanticModelCompilation:
    """The compiled TMDL parts plus every narrowing the compiler had to make.

    ``parts`` are API-ready: each carries a base64 ``payload`` and
    ``payloadType``, matching
    :mod:`fabric_kg_builder.deploy.fabric_ontology_definition` so both
    definition compilers can be POSTed without per-caller encoding.
    """

    parts: list[dict[str, str]]
    table_names: list[str]
    excluded_columns: list[ExcludedColumn]


def _part(path: str, payload: str) -> dict[str, str]:
    return {
        "path": path,
        "payload": base64.b64encode(payload.encode("utf-8")).decode("ascii"),
        "payloadType": "InlineBase64",
    }


def _stable_guid(*key: str) -> str:
    return str(uuid.uuid5(_SEMANTIC_MODEL_NAMESPACE, "\u0000".join(key)))


def _tmdl_identifier(name: str) -> str:
    """Quote a TMDL identifier unless it is already a bare simple name."""
    if _SIMPLE_IDENTIFIER.match(name):
        return name
    return "'" + name.replace("'", "''") + "'"


def _table_tmdl(
    table_name: str,
    columns: Sequence[tuple[str, str]],
    expression_name: str,
    lakehouse_schema: str | None = None,
) -> str:
    quoted_table = _tmdl_identifier(table_name)
    # The lineage tag names the source object as the SQL endpoint exposes it,
    # which is unqualified when the Lakehouse has no schemas.
    source_lineage = (
        f"[{lakehouse_schema}].[{table_name}]"
        if lakehouse_schema
        else f"[{table_name}]"
    )
    lines = [
        f"table {quoted_table}",
        f"\tlineageTag: {_stable_guid('table', table_name)}",
        f"\tsourceLineageTag: {source_lineage}",
        "",
    ]
    for column_name, data_type in columns:
        lines += [
            f"\tcolumn {_tmdl_identifier(column_name)}",
            f"\t\tdataType: {data_type}",
            f"\t\tlineageTag: {_stable_guid('column', table_name, column_name)}",
            f"\t\tsourceLineageTag: {column_name}",
            "\t\tsummarizeBy: none",
            f"\t\tsourceColumn: {column_name}",
            "",
            "\t\tannotation SummarizationSetBy = Automatic",
            "",
        ]
    lines += [
        f"\tpartition {quoted_table} = entity",
        "\t\tmode: directLake",
        "\t\tsource",
        f"\t\t\tentityName: {table_name}",
    ]
    # Only a schema-enabled Lakehouse stores tables under Tables/<schema>/.
    # Naming a schema that does not exist yields a partition that validates but
    # resolves to nothing.
    if lakehouse_schema:
        lines.append(f"\t\t\tschemaName: {lakehouse_schema}")
    lines += [
        f"\t\t\texpressionSource: {_tmdl_identifier(expression_name)}",
        "",
    ]
    return "\n".join(lines)


def _expressions_tmdl(expression_name: str, workspace_id: str, lakehouse_id: str) -> str:
    onelake = (
        f"https://onelake.dfs.fabric.microsoft.com/{workspace_id}/{lakehouse_id}"
    )
    return "\n".join(
        [
            f"expression {_tmdl_identifier(expression_name)} =",
            "\t\tlet",
            f'\t\t    Source = AzureStorage.DataLake("{onelake}", [HierarchicalNavigation=true])',
            "\t\tin",
            "\t\t    Source",
            f"\tlineageTag: {_stable_guid('expression', expression_name)}",
            "",
            "\tannotation PBI_IncludeFutureArtifacts = False",
            "",
        ]
    )


def _model_tmdl(expression_name: str, table_names: Sequence[str]) -> str:
    lines = [
        "model Model",
        "\tculture: en-US",
        "\tdefaultPowerBIDataSourceVersion: powerBI_V3",
        "\tsourceQueryCulture: en-US",
        "\tdataAccessOptions",
        "\t\tlegacyRedirects",
        "\t\treturnErrorValuesAsNull",
        "",
        f'annotation PBI_QueryOrder = ["{expression_name}"]',
        "",
    ]
    lines += [f"ref table {_tmdl_identifier(name)}" for name in table_names]
    lines.append("")
    return "\n".join(lines)


def compile_fabric_semantic_model_definition(
    *,
    tables_root: Path,
    workspace_id: str,
    lakehouse_id: str,
    lakehouse: Any,
    expression_name: str = "DirectLake - fabric_kg_024",
) -> FabricSemanticModelCompilation:
    """Compile the DirectLake TMDL parts for every materialized L5a table.

    ``tables_root`` holds one ``<table_id>.parquet`` per publication table, as
    written by ``fabric-kg app publish-structured --materialize``.  The parquet
    schema is the authority for the Delta table schema, so the model can never
    claim a column the registered Delta table does not carry.
    """

    lakehouse_schema = resolve_lakehouse_schema(lakehouse)
    parquet_files = sorted(tables_root.glob("*.parquet"))
    if not parquet_files:
        raise ValueError(f"no materialized tables found under {tables_root}")

    parts: list[dict[str, str]] = []
    table_names: list[str] = []
    excluded: list[ExcludedColumn] = []

    for parquet_file in parquet_files:
        table_name = parquet_file.stem
        schema = pq.read_schema(parquet_file)
        columns: list[tuple[str, str]] = []
        for column_name, arrow_type in zip(schema.names, schema.types, strict=True):
            data_type = _SCALAR_DATA_TYPES.get(str(arrow_type))
            if data_type is None:
                excluded.append(
                    ExcludedColumn(
                        table_name=table_name,
                        column_name=column_name,
                        arrow_type=str(arrow_type),
                    )
                )
                continue
            columns.append((column_name, data_type))
        if not columns:
            excluded.append(
                ExcludedColumn(
                    table_name=table_name,
                    column_name="*",
                    arrow_type="no projectable column",
                )
            )
            continue
        table_names.append(table_name)
        parts.append(
            _part(
                f"definition/tables/{table_name}.tmdl",
                _table_tmdl(
                    table_name, columns, expression_name, lakehouse_schema
                ),
            )
        )

    parts.append(
        _part(
            "definition/expressions.tmdl",
            _expressions_tmdl(expression_name, workspace_id, lakehouse_id),
        )
    )
    parts.append(
        _part("definition/model.tmdl", _model_tmdl(expression_name, table_names))
    )
    parts.append(
        _part("definition/database.tmdl", "database\n\tcompatibilityLevel: 1604\n")
    )
    parts.append(
        _part(
            "definition.pbism",
            (
                "{\n"
                '  "$schema": "https://developer.microsoft.com/json-schemas/fabric/'
                'item/semanticModel/definitionProperties/1.0.0/schema.json",\n'
                '  "version": "4.2",\n'
                '  "settings": {}\n'
                "}\n"
            ),
        )
    )

    return FabricSemanticModelCompilation(
        parts=parts,
        table_names=table_names,
        excluded_columns=excluded,
    )
