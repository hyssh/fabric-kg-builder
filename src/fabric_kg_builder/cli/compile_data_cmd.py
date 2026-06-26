"""compile-data command — convert enriched JSON to canonical Parquet tables.

Pipeline stage 4 (SPEC-001 §7).

Reads all ``*.json`` batch files produced by ``enrich`` from *input_path*
(default: ``build/enriched``), runs data-integrity gates VAL-001..VAL-007,
then writes the 8 canonical Parquet tables to *output_path*
(default: ``build/parquet``) via the shared Parquet writer.

Exit codes
----------
0  Success
1  I/O or unexpected error
5  Data-integrity gate failed (duplicate IDs or dangling FKs)
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click

from fabric_kg_builder.parquet.writer import write_all_tables
from fabric_kg_builder.validate.data_gates import Violation, run_gates

# ---------------------------------------------------------------------------
# Datetime coercion — enriched JSON stores datetimes as ISO strings
# ---------------------------------------------------------------------------

# Map table name → tuple of field names that are pa.timestamp columns.
_TS_FIELDS: dict[str, tuple[str, ...]] = {
    "entities": ("created_at", "updated_at"),
    "relationships": ("created_at",),
    "chunks": ("created_at",),
    "evidence": ("created_at",),
    "source_files": ("ingested_at",),
    "document_elements": ("extracted_at",),
    "visual_assets": ("created_at",),
    "visual_regions": ("created_at",),
}


def _parse_dt(value: Any) -> datetime | None:
    """Coerce *value* to a UTC-aware datetime, or return None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None
    return None


def _coerce_row(table_name: str, row: dict) -> dict:
    """Return a shallow copy of *row* with timestamp fields coerced to datetime."""
    ts_fields = _TS_FIELDS.get(table_name, ())
    if not ts_fields:
        return row
    row = dict(row)
    for field in ts_fields:
        if field in row:
            row[field] = _parse_dt(row[field])
    return row


# ---------------------------------------------------------------------------
# JSON loader
# ---------------------------------------------------------------------------

# Files to skip in build/enriched/
_SKIP_NAMES = {".checkpoint.json", "domain.json"}


def _load_enriched_json(input_dir: Path) -> dict[str, list[dict]]:
    """Load all batch JSON files from *input_dir*.

    Each file must be a dict with any subset of the keys:
    ``entities``, ``relationships``, ``chunks``, ``evidence``.

    Returns
    -------
    dict
        Mapping table_name → merged list of row dicts (datetime-coerced).
    """
    table_rows: dict[str, list[dict]] = {
        "entities": [],
        "relationships": [],
        "chunks": [],
        "evidence": [],
        "source_files": [],
        "document_elements": [],
        "visual_assets": [],
        "visual_regions": [],
    }

    json_files = sorted(input_dir.glob("*.json"))
    if not json_files:
        click.echo(f"  [warn] No JSON files found in {input_dir}", err=True)
        return table_rows

    for path in json_files:
        if path.name in _SKIP_NAMES:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise click.ClickException(f"Failed to read {path}: {exc}") from exc

        if not isinstance(data, dict):
            raise click.ClickException(
                f"{path}: expected a JSON object, got {type(data).__name__}"
            )

        for table in (
            "entities", "relationships", "chunks", "evidence",
            "source_files", "document_elements", "visual_assets", "visual_regions",
        ):
            rows = data.get(table, [])
            if not isinstance(rows, list):
                raise click.ClickException(
                    f"{path}: '{table}' must be a list, got {type(rows).__name__}"
                )
            table_rows[table].extend(
                _coerce_row(table, row) for row in rows
            )

    return table_rows


# Primary key column per canonical table — used to dedup identical rows.
# Deterministic IDs mean identical content yields identical IDs; collisions are
# exact duplicates (e.g. the same evidence span linked from two passes) and are
# safe to collapse to a single row.
_PRIMARY_KEYS: dict[str, str] = {
    "entities": "entity_id",
    "relationships": "relationship_id",
    "chunks": "chunk_id",
    "evidence": "evidence_id",
    "source_files": "source_file_id",
    "document_elements": "document_element_id",
    "visual_assets": "image_id",
    "visual_regions": "visual_region_id",
}


def _resolve_duplicates(table_rows: dict[str, list[dict]]) -> dict[str, int]:
    """Resolve rows that share a primary key (deterministic-ID collisions).

    Same ID means the same logical thing (IDs are content/identity hashes):

    * **entities** are MERGED — the same entity extracted across multiple
      sections is combined: aliases are unioned, the highest ``confidence`` is
      kept, and the first non-empty ``description`` wins.  This is canonical
      entity resolution, not an error.
    * **all other tables** keep the first occurrence (identical IDs are
      duplicate rows — e.g. the same evidence span or chunk produced twice).

    Returns the per-table count of rows collapsed.
    """
    dropped: dict[str, int] = {}

    # --- Entities: merge by entity_id ---------------------------------------
    entities = table_rows.get("entities", [])
    if entities:
        merged: dict[Any, dict] = {}
        order: list[Any] = []
        collapsed = 0
        for row in entities:
            eid = row.get("entity_id")
            if eid is None or eid not in merged:
                if eid is not None:
                    merged[eid] = dict(row)
                    order.append(eid)
                else:
                    order.append(id(row))
                    merged[order[-1]] = dict(row)
                continue
            collapsed += 1
            existing = merged[eid]
            existing_aliases = existing.get("aliases") or []
            new_aliases = row.get("aliases") or []
            existing["aliases"] = list(dict.fromkeys([*existing_aliases, *new_aliases]))
            if (row.get("confidence") or 0.0) > (existing.get("confidence") or 0.0):
                existing["confidence"] = row.get("confidence")
            if not existing.get("description") and row.get("description"):
                existing["description"] = row.get("description")
        table_rows["entities"] = [merged[k] for k in order]
        if collapsed:
            dropped["entities"] = collapsed

    # --- Other tables: keep first by primary key ----------------------------
    for table, rows in table_rows.items():
        if table == "entities":
            continue
        pk = _PRIMARY_KEYS.get(table)
        if not pk or not rows:
            continue
        seen: set = set()
        unique: list[dict] = []
        collapsed = 0
        for row in rows:
            key = row.get(pk)
            if key is not None and key in seen:
                collapsed += 1
                continue
            if key is not None:
                seen.add(key)
            unique.append(row)
        table_rows[table] = unique
        if collapsed:
            dropped[table] = collapsed

    return dropped


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


_COMPILE_DATA_EPILOG = """\b
Example:
  fabric-kg compile-data
  fabric-kg compile-data --input build/enriched --out build/parquet

Questions? hyssh@microsoft.com
"""


@click.command("compile-data", epilog=_COMPILE_DATA_EPILOG,
               context_settings={"max_content_width": 120})
@click.option(
    "--input", "input_path",
    default="build/enriched",
    show_default=True,
    type=click.Path(),
    help="Directory containing enriched canonical JSON files (output of 'enrich').",
)
@click.option(
    "--out", "output_path",
    default="build/parquet",
    show_default=True,
    type=click.Path(),
    help="Output directory for the 8 canonical Parquet tables.",
)
@click.option(
    "--validate", "run_validate",
    is_flag=True,
    default=False,
    help="Run additional schema validation checks after writing Parquet tables.",
)
def compile_data_cmd(input_path: str, output_path: str, run_validate: bool) -> None:
    """Convert enriched JSON to the 8 canonical Parquet tables.

    Reads per-file canonical JSON batch files from --input (build/enriched by
    default), merges entity duplicates (union aliases, max confidence), runs
    data-integrity gates VAL-001..VAL-012 to catch duplicate IDs and dangling
    foreign keys, then writes all 8 tables to --out (build/parquet by default):

      entities · relationships · chunks · evidence
      source_files · document_elements · visual_assets · visual_regions

    Exit codes: 0 success · 1 I/O or unexpected error · 5 data-integrity failure.
    """
    input_dir = Path(input_path)
    output_dir = Path(output_path)

    # --- Validate input directory -------------------------------------------
    if not input_dir.exists():
        raise click.ClickException(
            f"Input directory does not exist: {input_dir}"
        )
    if not input_dir.is_dir():
        raise click.ClickException(
            f"--input must be a directory, not a file: {input_dir}"
        )

    click.echo(f"[compile-data] Loading enriched JSON from {input_dir} ...")

    # --- Load enriched JSON --------------------------------------------------
    try:
        table_rows = _load_enriched_json(input_dir)
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f"Unexpected error loading input: {exc}") from exc

    total_loaded = sum(len(v) for v in table_rows.values())
    click.echo(
        f"  Loaded: entities={len(table_rows['entities'])}, "
        f"relationships={len(table_rows['relationships'])}, "
        f"chunks={len(table_rows['chunks'])}, "
        f"evidence={len(table_rows['evidence'])}, "
        f"source_files={len(table_rows['source_files'])}, "
        f"document_elements={len(table_rows['document_elements'])}, "
        f"visual_assets={len(table_rows['visual_assets'])}, "
        f"visual_regions={len(table_rows['visual_regions'])}"
    )

    # --- Capture source identity sets for the additivity (superset) guard ----
    # Every real entity_id and relationship_id present in the enriched input MUST
    # survive into the compiled output.  Densify and compile-data are strictly
    # additive by contract; this guard fails the build if anything is dropped.
    source_entity_ids = {
        r.get("entity_id") for r in table_rows["entities"] if r.get("entity_id")
    }
    source_relationship_ids = {
        r.get("relationship_id")
        for r in table_rows["relationships"]
        if r.get("relationship_id")
    }

    # --- Resolve duplicate primary keys (merge entities, dedup the rest) -----
    dropped = _resolve_duplicates(table_rows)
    if dropped:
        click.echo(
            "  Resolved duplicates: "
            + ", ".join(f"{n} {t}" for t, n in sorted(dropped.items()))
        )

    # --- Additivity guard: no real source entity/relationship may be dropped --
    out_entity_ids = {
        r.get("entity_id") for r in table_rows["entities"] if r.get("entity_id")
    }
    out_relationship_ids = {
        r.get("relationship_id")
        for r in table_rows["relationships"]
        if r.get("relationship_id")
    }
    missing_entities = source_entity_ids - out_entity_ids
    missing_relationships = source_relationship_ids - out_relationship_ids
    if missing_entities or missing_relationships:
        click.echo(
            f"  [FAIL] additivity guard: {len(missing_entities)} entity id(s) and "
            f"{len(missing_relationships)} relationship id(s) from the input were "
            f"dropped during compile. The pipeline must preserve all existing edges.",
            err=True,
        )
        for eid in list(missing_entities)[:5]:
            click.echo(f"    dropped entity_id: {eid}", err=True)
        for rid in list(missing_relationships)[:5]:
            click.echo(f"    dropped relationship_id: {rid}", err=True)
        sys.exit(5)
    click.echo(
        f"  Additivity guard OK — {len(source_entity_ids)} entities + "
        f"{len(source_relationship_ids)} relationships preserved."
    )

    # --- Data-integrity gates (VAL-001..VAL-012) -----------------------------
    click.echo("[compile-data] Running data-integrity gates (VAL-001..VAL-012) ...")
    violations: list[Violation] = run_gates(table_rows)

    if violations:
        click.echo(
            f"  [FAIL] {len(violations)} data-integrity violation(s) found:",
            err=True,
        )
        for v in violations:
            click.echo(f"    {v}", err=True)
        sys.exit(5)

    click.echo("  All gates passed.")

    # --- Write Parquet tables ------------------------------------------------
    click.echo(f"[compile-data] Writing Parquet tables to {output_dir} ...")
    try:
        written = write_all_tables(table_rows, output_dir)
    except (ValueError, KeyError, OSError) as exc:
        raise click.ClickException(f"Failed to write Parquet: {exc}") from exc

    # --- Summary ------------------------------------------------------------
    click.echo("\n[compile-data] Summary - rows written per table:")
    for table_name, path in sorted(written.items()):
        row_count = len(table_rows[table_name])
        click.echo(f"  {table_name:<25} {row_count:>6} rows  ->  {path}")

    click.echo(
        f"\n[compile-data] Done. "
        f"{len(written)} table(s) written to {output_dir}."
    )
