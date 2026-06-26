"""deploy.onelake_writer — write Parquet tables to OneLake as Delta format.

Uses deltalake (delta-rs) to write Delta tables directly to Fabric OneLake via
the abfss:// endpoint.  Authentication uses DefaultAzureCredential (az login in
dev, SPN in CI via FABRIC_CLIENT_ID/FABRIC_CLIENT_SECRET/FABRIC_TENANT_ID in
.env).

Pattern (verified live by coordinator 2026-06-24):
    write_deltalake(
        "abfss://{workspace_id}@onelake.dfs.fabric.microsoft.com/{lakehouse_id}/Tables/{schema}/{table}",
        arrow_table,
        mode="overwrite",
        storage_options={"bearer_token": tok, "use_fabric_endpoint": "true"},
    )

Usage (mock / test — no network)::

    results = deploy_parquet_to_onelake(
        parquet_dir=Path("dist/fabric-kg-package/parquet"),
        workspace_id="9802a28a-...",
        lakehouse_item_id="5064e66b-...",
        schema="dbo",
        tables=["entities", "relationships"],
        mock=True,
    )
    # returns {"entities": "planned", "relationships": "planned"}

Usage (live with lean projection)::

    from fabric_kg_builder.deploy.onelake_writer import LAKEHOUSE_TABLE_PROJECTION
    results = deploy_parquet_to_onelake(
        parquet_dir=Path("dist/fabric-kg-package/parquet"),
        workspace_id="9802a28a-...",
        lakehouse_item_id="5064e66b-...",
        schema="dbo",
        tables=list(LAKEHOUSE_TABLE_PROJECTION.keys()),
        projection=LAKEHOUSE_TABLE_PROJECTION,
    )
    # chunks never appears — excluded from projection (text → AI Search)
    # document_elements written lean (no content/content_html/row_index/col_index)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# Status strings returned per table
STATUS_PLANNED = "planned"
STATUS_OK = "ok"
STATUS_SKIPPED = "skipped"  # parquet file not found on disk
STATUS_ERROR = "error"


# ---------------------------------------------------------------------------
# Lakehouse table projection — graph / ontology scope
# ---------------------------------------------------------------------------
# Maps each Lakehouse-eligible table → columns to keep (None = all columns).
#
# Design rule (SPEC-001 §2.1):
#   Fabric Lakehouse  = graph model + ontology + provenance (structured/queryable)
#   Azure AI Search   = full text + embeddings (chunk content, doc-element text)
#
# "chunks" is intentionally ABSENT — it is pure retrieval text that belongs in
#   AI Search (kg-chunks index), not the Lakehouse.
#
# "document_elements" is written LEAN:
#   Keep  → structural + graph linkage columns (IDs, page, path, sort, blob refs)
#   Drop  → content, content_html  (heavy text — lives in kg-document-elements)
#   Drop  → row_index, col_index   (sparse table-HTML concerns — AI Search only)
LAKEHOUSE_TABLE_PROJECTION: dict[str, list[str] | None] = {
    "source_files": None,   # all columns — file provenance / graph root
    "document_elements": [  # lean — structural + graph linkage only
        "document_element_id",
        "source_file_id",
        "element_type",
        "parent_element_id",
        "page_number",
        "section_path",
        "sort_order",
        "table_id",      # may be absent in current schema — defensive select handles it
        "figure_id",     # may be absent in current schema — defensive select handles it
        "image_id",      # may be absent in current schema — defensive select handles it
        "blob_url",
        "content_hash",
    ],
    "entities": None,        # all columns — graph nodes + ontology bindings
    "relationships": None,   # all columns — graph edges
    "evidence": None,        # all columns — provenance links
    "visual_assets": None,   # all columns — visual ontology assets
    "visual_regions": None,  # all columns — visual ontology regions
    # "chunks" intentionally omitted — text retrieval only → AI Search (kg-chunks)
}

# Ordered list of tables included in the default Lakehouse projection (no chunks).
LAKEHOUSE_TABLES: list[str] = list(LAKEHOUSE_TABLE_PROJECTION.keys())


def _default_token_provider() -> str:
    """Obtain a Bearer token for OneLake using DefaultAzureCredential."""
    from azure.identity import DefaultAzureCredential  # type: ignore[import]

    cred = DefaultAzureCredential()
    return cred.get_token("https://storage.azure.com/.default").token


def deploy_parquet_to_onelake(
    parquet_dir: Path,
    workspace_id: str,
    lakehouse_item_id: str,
    schema: str,
    tables: list[str],
    token_provider: Callable[[], str] | None = None,
    mock: bool = False,
    projection: dict[str, list[str] | None] | None = None,
) -> dict[str, str]:
    """Deploy Parquet files from *parquet_dir* to OneLake as Delta tables.

    For each table name in *tables*, looks for ``{parquet_dir}/{table}.parquet``
    and writes it to ``Tables/{schema}/{table}`` in the target Lakehouse via the
    abfss:// ADLS Gen2 endpoint.

    Parameters
    ----------
    parquet_dir:
        Local directory containing ``{table}.parquet`` files.
    workspace_id:
        Fabric workspace GUID.
    lakehouse_item_id:
        Lakehouse item GUID within the workspace.
    schema:
        Schema name in the schema-enabled Lakehouse (typically ``"dbo"``).
    tables:
        List of table base names to deploy (e.g. ``["entities", "relationships"]``).
    token_provider:
        Optional callable that returns a Bearer token string.  Defaults to
        ``DefaultAzureCredential`` scoped to ``https://storage.azure.com/.default``.
    mock:
        When ``True``, log planned actions and return without any network call.
        Safe for offline use and unit tests.
    projection:
        Optional column projection map (table → list of column names to keep, or
        ``None`` to keep all columns).  Tables absent from the projection dict are
        skipped with status ``"skipped: not in Lakehouse projection"``.  Column
        selection is *defensive*: columns listed in the projection but absent from
        the actual Parquet file are silently ignored (no crash).  Pass
        ``LAKEHOUSE_TABLE_PROJECTION`` to enforce the lean graph/ontology scope.

    Returns
    -------
    dict[str, str]
        Mapping of table name → status string:
        ``"planned"`` (mock), ``"ok"``, ``"skipped"`` (file absent or not in
        projection), or ``"error: <message>"``.
    """
    results: dict[str, str] = {}

    for table in tables:
        # Apply projection: tables absent from the projection dict are excluded.
        if projection is not None and table not in projection:
            logger.info(
                "[onelake_writer] SKIP %s: not in Lakehouse projection (text → AI Search)",
                table,
            )
            results[table] = f"{STATUS_SKIPPED}: not in Lakehouse projection"
            continue

        keep_cols: list[str] | None = projection.get(table) if projection is not None else None

        parquet_path = Path(parquet_dir) / f"{table}.parquet"
        onelake_path = (
            f"abfss://{workspace_id}@onelake.dfs.fabric.microsoft.com"
            f"/{lakehouse_item_id}/Tables/{schema}/{table}"
        )

        if mock:
            if keep_cols is not None:
                logger.info(
                    "[onelake_writer] MOCK: would write %s -> %s (lean projection: %d cols)",
                    parquet_path,
                    onelake_path,
                    len(keep_cols),
                )
            else:
                logger.info(
                    "[onelake_writer] MOCK: would write %s -> %s", parquet_path, onelake_path
                )
            results[table] = STATUS_PLANNED
            continue

        if not parquet_path.exists():
            logger.warning("[onelake_writer] SKIP: %s not found", parquet_path)
            results[table] = STATUS_SKIPPED
            continue

        try:
            import pyarrow.parquet as pq  # type: ignore[import]
            from deltalake import write_deltalake  # type: ignore[import]

            _token_fn = token_provider or _default_token_provider
            tok = _token_fn()

            arrow_table = pq.read_table(str(parquet_path))

            # Apply column projection — defensive: only select columns present in file.
            if keep_cols is not None:
                present = [c for c in keep_cols if c in arrow_table.schema.names]
                dropped = [c for c in arrow_table.schema.names if c not in keep_cols]
                if dropped:
                    logger.info(
                        "[onelake_writer] %s: lean projection applied — "
                        "keeping %d/%d cols, dropping: %s",
                        table,
                        len(present),
                        arrow_table.num_columns,
                        dropped,
                    )
                if present:
                    arrow_table = arrow_table.select(present)

            write_deltalake(
                onelake_path,
                arrow_table,
                mode="overwrite",
                schema_mode="overwrite",
                storage_options={
                    "bearer_token": tok,
                    "use_fabric_endpoint": "true",
                },
            )
            logger.info(
                "[onelake_writer] OK: %s -> %s (%d rows, %d cols)",
                table,
                onelake_path,
                arrow_table.num_rows,
                arrow_table.num_columns,
            )
            results[table] = STATUS_OK

        except Exception as exc:  # noqa: BLE001
            logger.error("[onelake_writer] ERROR writing %s: %s", table, exc)
            results[table] = f"{STATUS_ERROR}: {exc}"

    return results
