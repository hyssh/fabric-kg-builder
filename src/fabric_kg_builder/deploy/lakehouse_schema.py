# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Resolution of the OneLake table schema segment for a target Lakehouse.

A Fabric Lakehouse is either *schema-enabled* or it is not, and the choice is
fixed when the item is created.  The distinction is not cosmetic: it decides
where Delta tables physically live in OneLake.

======================  =====================  ==========================
Lakehouse               ``defaultSchema``      OneLake table path
======================  =====================  ==========================
schema-enabled          ``"dbo"``              ``Tables/dbo/<table>``
not schema-enabled      ``None``               ``Tables/<table>``
======================  =====================  ==========================

Every definition-bearing item that reads those tables has to agree with the
lakehouse on this point, and each expresses it differently — an Ontology data
binding writes ``sourceSchema``, a DirectLake TMDL partition writes
``schemaName``.  Historically each call site hardcoded ``"dbo"``.  Against a
lakehouse created without schemas that produced definitions which *validate*,
*import*, and *read back* correctly while pointing at a OneLake path that does
not exist, so the failure only surfaced later as an empty graph and a
``GraphNotRefreshable`` refresh job.

Resolving the segment in one place keeps the three call sites from drifting
apart again, and makes "which lakehouse is this?" an explicit input rather than
an assumption buried in a payload literal.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "resolve_lakehouse_schema",
    "onelake_tables_path",
    "apply_source_schema",
]


def resolve_lakehouse_schema(lakehouse: Any) -> str | None:
    """Return the schema segment for ``lakehouse``, or ``None`` when unschemad.

    ``lakehouse`` may be the schema name itself (``"dbo"``), ``None``, or the
    Fabric ``GET /lakehouses/{id}`` payload, from which ``properties.
    defaultSchema`` is read.  Accepting the raw payload means callers do not
    have to remember where Fabric hides the flag.
    """

    if lakehouse is None or isinstance(lakehouse, str):
        schema = lakehouse
    elif isinstance(lakehouse, dict):
        properties = lakehouse.get("properties")
        if not isinstance(properties, dict):
            properties = lakehouse
        schema = properties.get("defaultSchema")
    else:  # pragma: no cover - defensive
        raise TypeError(f"unsupported lakehouse descriptor: {type(lakehouse)!r}")

    if schema is None:
        return None
    schema = str(schema).strip()
    return schema or None


def onelake_tables_path(schema: str | None, table_name: str) -> str:
    """Return the OneLake-relative path of ``table_name`` under ``Tables``."""

    if schema:
        return f"Tables/{schema}/{table_name}"
    return f"Tables/{table_name}"


def apply_source_schema(
    source_table_properties: dict[str, Any],
    schema: str | None,
) -> dict[str, Any]:
    """Set or omit ``sourceSchema`` on an Ontology lakehouse-table reference.

    ``sourceSchema`` is optional in the Fabric ontology data-binding schema, and
    omitting it is the correct encoding for a lakehouse without schemas.  The
    key is removed rather than set to ``None`` so the emitted payload matches
    what Fabric itself produces.

    Insertion order matters here.  ``sourceTableProperties`` is deserialized
    polymorphically on the Fabric side and its ``sourceType`` discriminator has
    to stay the first key, so this only ever appends or deletes a trailing key
    and never rebuilds the mapping.
    """

    if schema:
        source_table_properties["sourceSchema"] = schema
    else:
        source_table_properties.pop("sourceSchema", None)
    return source_table_properties
