"""compile-data command — convert enriched JSON to canonical Parquet tables.

Pipeline stage 4 (SPEC-001 §7).

Reads all ``*.json`` batch files produced by ``enrich`` from *input_path*
(default: ``build/enriched``), runs data-integrity gates VAL-001..VAL-007,
then writes the canonical Parquet tables to *output_path*
(default: ``build/parquet``) via the shared Parquet writer.

Exit codes
----------
0  Success
1  I/O or unexpected error
5  Data-integrity gate failed (duplicate IDs or dangling FKs)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click

from fabric_kg_builder.domain.models import DomainContract, DomainContractV2
from fabric_kg_builder.domain.service import (
    DomainContractError,
    compute_contract_hash,
    load_domain_contract,
)
from fabric_kg_builder.lineage.common import ensure_lineage_defaults
from fabric_kg_builder.model.arrow_schemas import DRAWING_TABLE_SCHEMAS
from fabric_kg_builder.parquet.writer import write_all_tables, write_table
from fabric_kg_builder.serving.semantic_projection import (
    SemanticProjectionResult,
    build_semantic_projection,
)
from fabric_kg_builder.validate.data_gates import (
    Violation,
    run_gates,
    run_semantic_projection_gates,
)

# ---------------------------------------------------------------------------
# Datetime coercion — enriched JSON stores datetimes as ISO strings
# ---------------------------------------------------------------------------

# Map table name → tuple of field names that are pa.timestamp columns.
_TS_FIELDS: dict[str, tuple[str, ...]] = {
    "entities": ("created_at", "updated_at"),
    "relationships": ("created_at",),
    "property_observations": ("observed_at", "created_at"),
    "property_conflicts": ("created_at",),
    "chunks": ("created_at",),
    "evidence": ("created_at",),
    "claims": ("valid_from", "valid_to", "observed_at"),
    "source_files": ("ingested_at",),
    "document_elements": ("extracted_at",),
    "visual_assets": ("created_at",),
    "visual_regions": ("created_at",),
    "drawing_elements": ("created_at",),
    "drawing_relationships": ("created_at",),
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
_SKIP_NAMES = {
    ".checkpoint.json",
    "domain.json",
    "domain.review.json",
    "domain.run-manifest.json",
    "semantic-quality-report.json",
    "enrichment-metrics.json",
    ".enrichment-metrics.json",
}


def _project_root_for_input(input_dir: Path) -> Path:
    """Return the trusted project boundary for an enriched artifact directory."""
    resolved = input_dir.resolve()
    if resolved.name == "enriched" and resolved.parent.name == "build":
        return resolved.parent.parent
    return resolved.parent


def _load_schema2_projection_authority(
    input_dir: Path,
) -> tuple[DomainContractV2 | None, dict[str, Any] | None]:
    """Load the approved contract bound by the enrichment run manifest.

    Schema-1 and manifest-free compatibility inputs return ``(None, None)``.
    Schema-2 manifests fail closed when their path, approval, or hash binding is
    missing or stale.
    """
    manifest_path = input_dir / "domain.run-manifest.json"
    if not manifest_path.exists():
        return None, None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(
            f"Invalid domain run manifest {manifest_path}: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise click.ClickException(
            f"Invalid domain run manifest {manifest_path}: expected an object"
        )
    top_version = manifest.get("schema_version")
    raw_contract_meta = manifest.get("domain_contract")
    if raw_contract_meta is not None and not isinstance(raw_contract_meta, dict):
        raise click.ClickException(
            "Domain run manifest domain_contract must be an object."
        )
    contract_meta = raw_contract_meta or {}
    nested_version = contract_meta.get("schema_version")
    v2_binding_marker = any(
        contract_meta.get(key)
        for key in (
            "proposal_hash",
            "source_profile_hash",
            "prompt_hash",
            "model_hash",
        )
    )
    raw_path = contract_meta.get("path")
    if raw_path is not None and (
        not isinstance(raw_path, str) or not raw_path.strip()
    ):
        raise click.ClickException(
            "Domain run manifest contract path is invalid."
        )

    contract: DomainContract | DomainContractV2 | None = None
    contract_path: Path | None = None
    if isinstance(raw_path, str) and raw_path.strip():
        trusted_root = _project_root_for_input(input_dir)
        supplied = Path(raw_path)
        candidates = (
            [supplied.resolve()]
            if supplied.is_absolute()
            else [
                (trusted_root / supplied).resolve(),
                (manifest_path.parent / supplied).resolve(),
            ]
        )
        resolved_candidates = {
            candidate
            for candidate in candidates
            if candidate.exists()
            and candidate.is_file()
            and candidate.is_relative_to(trusted_root)
        }
        if len(resolved_candidates) != 1:
            raise click.ClickException(
                "Manifest contract path is missing, ambiguous, or outside the "
                f"trusted project root {trusted_root}: {raw_path!r}"
            )
        contract_path = next(iter(resolved_candidates))
        try:
            contract = load_domain_contract(contract_path)
        except DomainContractError as exc:
            raise click.ClickException(
                f"Manifest-bound domain contract is invalid: {exc}"
            ) from exc

    if isinstance(contract, DomainContract):
        if (
            top_version not in (None, "1.0")
            or nested_version not in (None, "1.0")
            or v2_binding_marker
        ):
            raise click.ClickException(
                "Manifest markers conflict with the referenced schema-1 contract."
            )
        expected_hash = contract_meta.get("contract_hash")
        if expected_hash and expected_hash != compute_contract_hash(contract):
            raise click.ClickException(
                "Schema-1 domain run manifest contract hash mismatch."
            )
        return None, manifest

    if contract is None:
        if (
            str(top_version) == "2.0"
            or str(nested_version) == "2.0"
            or v2_binding_marker
        ):
            raise click.ClickException(
                "Schema-2 domain run manifest is missing a referenced contract."
            )
        if (
            top_version not in (None, "1.0")
            or nested_version not in (None, "1.0")
        ):
            raise click.ClickException(
                "Domain run manifest has unsupported schema markers."
            )
        return None, manifest

    assert isinstance(contract, DomainContractV2)
    assert contract_path is not None
    if str(top_version) != "2.0" or str(nested_version) != "2.0":
        raise click.ClickException(
            "Manifest markers conflict with the referenced schema-2 contract."
        )
    actual_hash = compute_contract_hash(contract)
    expected_hash = contract_meta.get("contract_hash")
    if expected_hash != actual_hash:
        raise click.ClickException(
            "Schema-2 domain run manifest contract hash mismatch: "
            f"expected {expected_hash!r}, found {actual_hash!r}."
        )
    if contract.approval.status != "approved":
        raise click.ClickException(
            f"Schema-2 contract is not approved: {contract_path}"
        )
    if contract.approval.contract_hash != actual_hash:
        raise click.ClickException(
            "Schema-2 approval is not bound to the active contract hash."
        )
    required_manifest_bindings = {
        "approval_status": contract.approval.status,
        "approved_by": contract.approval.approved_by,
        "approved_at_utc": contract.approval.approved_at_utc,
        "schema_version": contract.schema_version,
        "prompt_version": contract.approval.prompt_version,
        "prompt_hash": contract.approval.prompt_hash,
        "model_version": contract.approval.model_version,
        "model_hash": contract.approval.model_hash,
        "proposal_hash": contract.approval.proposal_hash,
        "source_profile_hash": contract.approval.source_profile_hash,
    }
    inconsistent = [
        key
        for key, expected in required_manifest_bindings.items()
        if not expected or contract_meta.get(key) != expected
    ]
    if inconsistent:
        raise click.ClickException(
            "Schema-2 domain run manifest approval bindings are missing or "
            f"inconsistent: {', '.join(sorted(inconsistent))}."
        )
    return contract, manifest


def _write_projection_receipt(output_dir: Path, receipt: dict[str, Any]) -> Path:
    """Persist one deterministic projection receipt."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "semantic-projection-receipt.json"
    staging_path = output_dir / ".semantic-projection-receipt.json.tmp"
    staging_path.write_text(
        json.dumps(
            receipt,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(staging_path, path)
    return path


def _clear_semantic_outputs(output_dir: Path) -> None:
    """Remove only stale serving files after a schema-2 projection failure."""
    for name in (
        "semantic_entities.parquet",
        "semantic_relationships.parquet",
        "claims.parquet",
        "claim_evidence.parquet",
    ):
        path = output_dir / name
        if path.is_file():
            path.unlink()


def _load_enriched_json(input_dir: Path) -> dict[str, list[dict]]:
    """Load all batch JSON files from *input_dir*.

    Each file must be a dict with any subset of the keys:
    ``entities``, ``relationships``, ``chunks``, ``evidence``,
    ``drawing_elements``, and ``drawing_relationships``.

    Returns
    -------
    dict
        Mapping table_name → merged list of row dicts (datetime-coerced).
    """
    table_rows: dict[str, list[dict]] = {
        "entities": [],
        "relationships": [],
        "property_observations": [],
        "property_conflicts": [],
        "chunks": [],
        "evidence": [],
        "claims": [],
        "claim_evidence": [],
        "source_files": [],
        "document_elements": [],
        "visual_assets": [],
        "visual_regions": [],
        "drawing_elements": [],
        "drawing_relationships": [],
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
            "entities", "relationships", "property_observations",
            "property_conflicts", "chunks", "evidence", "claims", "claim_evidence",
            "source_files", "document_elements", "visual_assets", "visual_regions",
            "drawing_elements", "drawing_relationships",
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


def _load_semantic_quality_report(input_dir: Path):
    """Load and validate optional SPEC-008A enrichment quality evidence."""
    from fabric_kg_builder.semantic.quality import EnrichmentQualityReport

    path = input_dir / "semantic-quality-report.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return EnrichmentQualityReport.model_validate(payload)
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        raise click.ClickException(
            f"Invalid semantic quality report {path}: {exc}"
        ) from exc


# Primary key column per canonical table — used to dedup identical rows.
# Deterministic IDs mean identical content yields identical IDs; collisions are
# exact duplicates (e.g. the same evidence span linked from two passes) and are
# safe to collapse to a single row.
_PRIMARY_KEYS: dict[str, str] = {
    "entities": "entity_id",
    "relationships": "relationship_id",
    "property_observations": "observation_id",
    "property_conflicts": "conflict_id",
    "chunks": "chunk_id",
    "evidence": "evidence_id",
    "claims": "claim_id",
    "claim_evidence": "claim_id",
    "source_files": "source_file_id",
    "document_elements": "document_element_id",
    "visual_assets": "image_id",
    "visual_regions": "visual_region_id",
    "drawing_elements": "element_id",
    "drawing_relationships": "drawing_relationship_id",
}


def _merge_json_objects(
    left: str | None,
    right: str | None,
) -> str | None:
    if not left:
        return right
    if not right:
        return left
    try:
        left_payload = json.loads(left)
        right_payload = json.loads(right)
    except (json.JSONDecodeError, TypeError):
        return left
    if not isinstance(left_payload, dict) or not isinstance(right_payload, dict):
        return left
    merged = dict(left_payload)
    for key, value in right_payload.items():
        if isinstance(value, list) and isinstance(merged.get(key), list):
            merged[key] = list(dict.fromkeys([*merged[key], *value]))
        elif key not in merged or merged[key] in (None, "", [], {}):
            merged[key] = value
    return json.dumps(merged, sort_keys=True)


def _resolve_duplicates(table_rows: dict[str, list[dict]]) -> dict[str, int]:
    """Resolve rows that share a primary key (deterministic-ID collisions).

    Same ID means the same logical thing (IDs are content/identity hashes):

    * **entities** are MERGED — the same entity extracted across multiple
      sections is combined: aliases are unioned, the highest ``confidence`` is
      kept, and the first non-empty ``description`` wins.  This is canonical
      entity resolution, not an error.
    * **relationships and property observations** merge evidence arrays so
      overlap reduction cannot erase provenance.
    * **property conflicts** union their observation IDs.
    * **all other tables** keep the first occurrence.

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
            existing["evidence_ids"] = sorted(
                set(existing.get("evidence_ids") or [])
                | set(row.get("evidence_ids") or [])
            ) or None
            existing["cannot_link_keys"] = sorted(
                set(existing.get("cannot_link_keys") or [])
                | set(row.get("cannot_link_keys") or [])
            ) or None
            existing["properties_json"] = _merge_json_objects(
                existing.get("properties_json"),
                row.get("properties_json"),
            )
        table_rows["entities"] = [merged[k] for k in order]
        if collapsed:
            dropped["entities"] = collapsed

    for table, evidence_field in (
        ("relationships", "evidence_ids"),
        ("property_observations", "evidence_ids"),
    ):
        rows = table_rows.get(table, [])
        pk = _PRIMARY_KEYS[table]
        merged_rows: dict[Any, dict] = {}
        order: list[Any] = []
        collapsed = 0
        for row in rows:
            key = row.get(pk)
            if key is None or key not in merged_rows:
                effective_key = key if key is not None else id(row)
                merged_rows[effective_key] = dict(row)
                order.append(effective_key)
                continue
            collapsed += 1
            existing = merged_rows[key]
            evidence_ids = sorted(
                set(existing.get(evidence_field) or [])
                | set(row.get(evidence_field) or [])
                | (
                    {existing["evidence_id"]}
                    if table == "relationships" and existing.get("evidence_id")
                    else set()
                )
                | (
                    {row["evidence_id"]}
                    if table == "relationships" and row.get("evidence_id")
                    else set()
                )
            )
            existing[evidence_field] = evidence_ids
            if table == "relationships":
                existing["evidence_id"] = (
                    evidence_ids[0] if evidence_ids else None
                )
                existing["source_span_ids"] = sorted(
                    set(existing.get("source_span_ids") or [])
                    | set(row.get("source_span_ids") or [])
                ) or None
                existing["properties_json"] = _merge_json_objects(
                    existing.get("properties_json"),
                    row.get("properties_json"),
                )
                if not existing.get("description") and row.get("description"):
                    existing["description"] = row["description"]
            else:
                existing["source_span_ids"] = sorted(
                    set(existing.get("source_span_ids") or [])
                    | set(row.get("source_span_ids") or [])
                )
                existing["conflict_id"] = (
                    existing.get("conflict_id") or row.get("conflict_id")
                )
            if (row.get("confidence") or 0.0) > (
                existing.get("confidence") or 0.0
            ):
                existing["confidence"] = row.get("confidence")
        table_rows[table] = [merged_rows[key] for key in order]
        if collapsed:
            dropped[table] = collapsed

    conflicts = table_rows.get("property_conflicts", [])
    if conflicts:
        merged_conflicts: dict[Any, dict] = {}
        order = []
        collapsed = 0
        for row in conflicts:
            conflict_id = row.get("conflict_id")
            if conflict_id is None or conflict_id not in merged_conflicts:
                effective_key = (
                    conflict_id if conflict_id is not None else id(row)
                )
                merged_conflicts[effective_key] = dict(row)
                order.append(effective_key)
                continue
            collapsed += 1
            existing = merged_conflicts[conflict_id]
            existing["observation_ids"] = sorted(
                set(existing.get("observation_ids") or [])
                | set(row.get("observation_ids") or [])
            )
        table_rows["property_conflicts"] = [
            merged_conflicts[key] for key in order
        ]
        if collapsed:
            dropped["property_conflicts"] = collapsed

    # --- Other tables: keep first by primary key ----------------------------
    for table, rows in table_rows.items():
        if table in {
            "entities",
            "relationships",
            "property_observations",
            "property_conflicts",
        }:
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


def _backfill_legacy_lineage(
    table_rows: dict[str, list[dict]],
) -> int:
    """Apply the explicit v1-to-v2 lineage boundary for compile-data inputs."""
    backfilled = 0
    required_fields = (
        "project_id",
        "asset_id",
        "asset_version_id",
        "run_id",
        "schema_version",
    )
    for table_name, rows in table_rows.items():
        migrated_rows: list[dict] = []
        for row in rows:
            if any(field not in row for field in required_fields):
                backfilled += 1
            migrated_rows.append(ensure_lineage_defaults(row))
        table_rows[table_name] = migrated_rows
    return backfilled


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


_COMPILE_DATA_EPILOG = """\b
Example:
  fabric-kg compile-data
  fabric-kg compile-data --input build/enriched --out build/parquet

Questions? https://github.com/hyssh/fabric-kg-builder/issues
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
    help="Output directory for canonical Parquet tables.",
)
@click.option(
    "--validate", "run_validate",
    is_flag=True,
    default=False,
    help="Run additional schema validation checks after writing Parquet tables.",
)
def compile_data_cmd(input_path: str, output_path: str, run_validate: bool) -> None:
    """Convert enriched JSON to canonical Parquet tables.

    Reads per-file canonical JSON batch files from --input (build/enriched by
    default), merges entity duplicates (union aliases, max confidence), runs
    data-integrity gates VAL-001..VAL-017 to catch duplicate IDs and dangling
    foreign keys, then writes all content tables to --out (build/parquet by default):

      entities · relationships · chunks · evidence
      semantic_entities · semantic_relationships · claims · claim_evidence
      source_files · document_elements · visual_assets · visual_regions
      drawing_elements · drawing_relationships

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
        quality_report = _load_semantic_quality_report(input_dir)
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f"Unexpected error loading input: {exc}") from exc

    try:
        schema2_contract, domain_run_manifest = (
            _load_schema2_projection_authority(input_dir)
        )
    except click.ClickException as exc:
        _clear_semantic_outputs(output_dir)
        _write_projection_receipt(
            output_dir,
            {
                "receipt_schema_version": "1.0",
                "schema_mode": "schema2",
                "status": "failed",
                "active_contract_hash": None,
                "input_candidate_count": len(table_rows["relationships"]),
                "terminal_counts": {},
                "reason_counts": {"AUTHORITY_INVALID": 1},
                "serving_counts": {
                    "semantic_entities": 0,
                    "semantic_relationships": 0,
                },
                "invariants": [
                    {
                        "gate": "SEM-100",
                        "passed": False,
                        "details": [str(exc)],
                    }
                ],
            },
        )
        raise

    if quality_report is not None:
        click.echo(
            "  Semantic quality: "
            f"status={quality_report.status}, "
            f"property_evidence={quality_report.property_evidence_coverage:.3f}, "
            "relationship_evidence="
            f"{quality_report.relationship_evidence_coverage:.3f}, "
            "endpoint_resolution="
            f"{quality_report.relationship_endpoint_resolution:.3f}"
        )
        if quality_report.status != "passed":
            click.echo(
                "  [FAIL] semantic extraction/enrichment quality gate failed; "
                "review discovery, conflict, description, and evidence findings "
                "in semantic-quality-report.json.",
                err=True,
            )
            sys.exit(5)

    total_loaded = sum(len(v) for v in table_rows.values())
    click.echo(
        f"  Loaded: entities={len(table_rows['entities'])}, "
        f"relationships={len(table_rows['relationships'])}, "
        f"chunks={len(table_rows['chunks'])}, "
        f"evidence={len(table_rows['evidence'])}, "
        f"source_files={len(table_rows['source_files'])}, "
        f"document_elements={len(table_rows['document_elements'])}, "
        f"visual_assets={len(table_rows['visual_assets'])}, "
        f"visual_regions={len(table_rows['visual_regions'])}, "
        f"drawing_elements={len(table_rows['drawing_elements'])}, "
        f"drawing_relationships={len(table_rows['drawing_relationships'])}"
    )

    backfilled = _backfill_legacy_lineage(table_rows)
    if backfilled:
        click.echo(
            f"  Migrated {backfilled} legacy row(s) to the lineage v2 envelope."
        )

    raw_entity_occurrences = [
        dict(row) for row in table_rows["entities"]
    ]
    raw_relationship_occurrences = [
        dict(row) for row in table_rows["relationships"]
    ]

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

    # Deterministic semantic serving layer is generated from the deduplicated
    # canonical tables so graph edges, claims, and citations have one-to-one
    # stable IDs with their serving source rows.
    projection = build_semantic_projection(
        (
            raw_entity_occurrences
            if schema2_contract is not None
            else table_rows["entities"]
        ),
        (
            raw_relationship_occurrences
            if schema2_contract is not None
            else table_rows["relationships"]
        ),
        table_rows["evidence"],
        schema2_contract=schema2_contract,
    )
    projection_receipt: dict[str, Any]
    if isinstance(projection, SemanticProjectionResult):
        table_rows["relationships"] = projection.audit_relationships
        projection_receipt = projection.receipt
    else:
        projection_receipt = {
            "receipt_schema_version": "1.0",
            "schema_mode": "schema1_compatibility",
            "status": "succeeded",
            "active_contract_hash": None,
            "input_candidate_count": len(raw_relationship_occurrences),
            "terminal_counts": {},
            "reason_counts": {},
            "serving_counts": {
                "semantic_entities": len(projection["semantic_entities"]),
                "semantic_relationships": len(
                    projection["semantic_relationships"]
                ),
            },
            "invariants": [],
        }
    receipt_path = output_dir / "semantic-projection-receipt.json"
    # Preserve any upstream claim workflow output; deterministic relationship
    # claims supplement it rather than replacing it.
    existing_claim_ids = {
        row.get("claim_id") for row in table_rows["claims"] if row.get("claim_id")
    }
    table_rows["claims"].extend(
        row for row in projection["claims"]
        if row["claim_id"] not in existing_claim_ids
    )
    existing_claim_evidence = {
        (row.get("claim_id"), row.get("evidence_id"))
        for row in table_rows["claim_evidence"]
    }
    table_rows["claim_evidence"].extend(
        row for row in projection["claim_evidence"]
        if (row["claim_id"], row["evidence_id"]) not in existing_claim_evidence
    )
    table_rows["semantic_entities"] = projection["semantic_entities"]
    table_rows["semantic_relationships"] = projection["semantic_relationships"]
    click.echo(
        "  Semantic projection: "
        f"entities={len(projection['semantic_entities'])}, "
        f"relationships={len(projection['semantic_relationships'])}, "
        f"claims={len(projection['claims'])}, "
        f"claim_evidence={len(projection['claim_evidence'])}"
    )

    # --- Data-integrity gates (VAL-001..VAL-017) -----------------------------
    click.echo("[compile-data] Running data-integrity gates (VAL-001..VAL-017) ...")
    violations: list[Violation] = [
        *run_gates(table_rows),
        *run_semantic_projection_gates(projection_receipt),
    ]

    if violations:
        if schema2_contract is not None:
            _clear_semantic_outputs(output_dir)
        projection_receipt = {
            **projection_receipt,
            "status": "failed",
            "compile_gate_violations": [
                {
                    "gate": violation.gate,
                    "table": violation.table,
                    "message": violation.message,
                }
                for violation in violations
            ],
        }
        receipt_path = _write_projection_receipt(
            output_dir,
            projection_receipt,
        )
        click.echo(
            f"  [FAIL] {len(violations)} data-integrity violation(s) found:",
            err=True,
        )
        for v in violations:
            click.echo(f"    {v}", err=True)
        click.echo(f"    receipt: {receipt_path}", err=True)
        sys.exit(5)

    click.echo("  All gates passed.")

    # --- Write Parquet tables ------------------------------------------------
    click.echo(f"[compile-data] Writing Parquet tables to {output_dir} ...")
    try:
        core_rows = {
            name: rows
            for name, rows in table_rows.items()
            if name not in DRAWING_TABLE_SCHEMAS
            and (
                name not in {"property_observations", "property_conflicts"}
                or rows
            )
        }
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".compile-data-",
            dir=output_dir.parent,
        ) as staging_name:
            staging_dir = Path(staging_name)
            staged = write_all_tables(core_rows, staging_dir)
            for name, schema in DRAWING_TABLE_SCHEMAS.items():
                staged[name] = write_table(
                    name,
                    table_rows[name],
                    staging_dir,
                    schema=schema,
                )
            output_dir.mkdir(parents=True, exist_ok=True)
            written: dict[str, Path] = {}
            for table_name, staged_path in sorted(staged.items()):
                final_path = output_dir / staged_path.name
                os.replace(staged_path, final_path)
                written[table_name] = final_path
        receipt_path = _write_projection_receipt(
            output_dir,
            projection_receipt,
        )
    except (ValueError, KeyError, OSError) as exc:
        if schema2_contract is not None:
            _clear_semantic_outputs(output_dir)
        failed_receipt = {
            **projection_receipt,
            "status": "failed",
            "output_failure": {
                "code": "OUTPUT_WRITE_FAILED",
                "error_type": type(exc).__name__,
            },
        }
        try:
            receipt_path = _write_projection_receipt(
                output_dir,
                failed_receipt,
            )
        except OSError:
            pass
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
