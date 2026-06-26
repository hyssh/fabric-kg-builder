"""onelake_multitype — materialize per-type entity and per-pair edge Delta tables.

The multi-type ontology (deploy-ontology --multitype) binds each Fabric
EntityType to its *own* Lakehouse table (``entities_component``,
``entities_procedure``, ...) and each typed RelationshipType to its own edge
table (``rel_procedure_step``, ...).  Fabric data bindings have no row filter, so
these per-type tables must be materialized in the Lakehouse before the ontology
definition is pushed.

This module reads the canonical ``entities.parquet`` / ``relationships.parquet``
and writes the per-type/per-pair slices to OneLake as Delta tables using the same
verified ``write_deltalake`` pattern as :mod:`onelake_writer`.

The bound entity columns are kept lean (entity_id / entity_type / display_name /
canonical_key); edge tables keep source_entity_id / target_entity_id.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

STATUS_PLANNED = "planned"
STATUS_OK = "ok"
STATUS_SKIPPED = "skipped"
STATUS_ERROR = "error"

# Columns kept on each per-type entity table (must match the ontology binding).
_ENTITY_BIND_COLS = ["entity_id", "entity_type", "display_name", "canonical_key"]
# Columns kept on each per-pair edge table.
_EDGE_BIND_COLS = ["source_entity_id", "target_entity_id"]


def _default_token_provider() -> str:
    from azure.identity import DefaultAzureCredential  # type: ignore[import]

    cred = DefaultAzureCredential()
    return cred.get_token("https://storage.azure.com/.default").token


def _onelake_path(workspace_id: str, lakehouse_item_id: str, schema: str, table: str) -> str:
    return (
        f"abfss://{workspace_id}@onelake.dfs.fabric.microsoft.com"
        f"/{lakehouse_item_id}/Tables/{schema}/{table}"
    )


def materialize_multitype_tables(
    parquet_dir: Path,
    plan: Any,
    workspace_id: str,
    lakehouse_item_id: str,
    schema: str = "dbo",
    token_provider: Callable[[], str] | None = None,
    mock: bool = False,
) -> dict[str, str]:
    """Write per-type entity tables and per-pair edge tables to OneLake.

    Parameters
    ----------
    parquet_dir:
        Directory holding ``entities.parquet`` and ``relationships.parquet``.
    plan:
        A ``MultitypePlan`` (from :mod:`ontology.multitype_plan`).
    mock:
        When True, plan only — no Arrow read, no network.

    Returns a mapping of ``table_name -> status``.
    """
    parquet_dir = Path(parquet_dir)
    results: dict[str, str] = {}

    if mock:
        for et in plan.entity_types:
            results[et.table_name] = STATUS_PLANNED
        for rp in plan.relationship_pairs:
            results[rp.table_name] = STATUS_PLANNED
        return results

    import pyarrow as pa  # type: ignore[import]
    import pyarrow.compute as pc  # type: ignore[import]
    import pyarrow.parquet as pq  # type: ignore[import]
    from deltalake import write_deltalake  # type: ignore[import]

    token_fn = token_provider or _default_token_provider
    tok = token_fn()
    storage_options = {"bearer_token": tok, "use_fabric_endpoint": "true"}

    # ---- Per-type entity tables -------------------------------------------
    ent = pq.read_table(str(parquet_dir / "entities.parquet"))
    present = [c for c in _ENTITY_BIND_COLS if c in ent.schema.names]
    ent_lean = ent.select(present)
    etype_col = ent.column("entity_type")

    # Map entity_id -> entity_type for edge slicing.
    id_to_type = dict(
        zip(ent.column("entity_id").to_pylist(), ent.column("entity_type").to_pylist())
    )

    for et in plan.entity_types:
        try:
            mask = pc.equal(etype_col, pa.scalar(et.type_name))
            slice_tbl = ent_lean.filter(mask)
            write_deltalake(
                _onelake_path(workspace_id, lakehouse_item_id, schema, et.table_name),
                slice_tbl,
                mode="overwrite",
                schema_mode="overwrite",
                storage_options=storage_options,
            )
            logger.info(
                "[onelake_multitype] OK entity table %s (%d rows)",
                et.table_name,
                slice_tbl.num_rows,
            )
            results[et.table_name] = STATUS_OK
        except Exception as exc:  # noqa: BLE001
            logger.error("[onelake_multitype] ERROR %s: %s", et.table_name, exc)
            results[et.table_name] = f"{STATUS_ERROR}: {exc}"

    # ---- Per-pair edge tables ---------------------------------------------
    rel = pq.read_table(str(parquet_dir / "relationships.parquet"))
    src_ids = rel.column("source_entity_id").to_pylist()
    tgt_ids = rel.column("target_entity_id").to_pylist()
    src_types = [id_to_type.get(s) for s in src_ids]
    tgt_types = [id_to_type.get(t) for t in tgt_ids]

    for rp in plan.relationship_pairs:
        try:
            keep_src: list[str] = []
            keep_tgt: list[str] = []
            for s, t, st, tt in zip(src_ids, tgt_ids, src_types, tgt_types):
                if st == rp.source_type and tt == rp.target_type:
                    keep_src.append(s)
                    keep_tgt.append(t)
            edge_tbl = pa.table({"source_entity_id": keep_src, "target_entity_id": keep_tgt})
            write_deltalake(
                _onelake_path(workspace_id, lakehouse_item_id, schema, rp.table_name),
                edge_tbl,
                mode="overwrite",
                schema_mode="overwrite",
                storage_options=storage_options,
            )
            logger.info(
                "[onelake_multitype] OK edge table %s (%d rows)",
                rp.table_name,
                edge_tbl.num_rows,
            )
            results[rp.table_name] = STATUS_OK
        except Exception as exc:  # noqa: BLE001
            logger.error("[onelake_multitype] ERROR %s: %s", rp.table_name, exc)
            results[rp.table_name] = f"{STATUS_ERROR}: {exc}"

    return results
