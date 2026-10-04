"""Lineage v2 governance — retention, deletion planning, and redaction checks.

Implements the data-lifecycle controls required by LIN-011 and LIN-012:

LIN-011 — Retention, deletion, and orphan checks
    ``build_deletion_plan``   — identify all dependent records before deletion
    ``find_orphaned_records`` — locate records whose FK targets no longer exist
    ``check_orphaned_citations`` — find deployments/citations referencing
                                    non-existent canonical records

LIN-012 — Manifest redaction
    ``check_manifest_redaction`` — verify sensitive canaries do not appear
                                    in manifest serialisation (VAL-049)
    ``redact_for_manifest``      — strip sensitive fields from a row before
                                    persisting to a manifest or log

Contract: explicit errors, no broad except clauses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from fabric_kg_builder.lineage.common import TABLE_ID_FIELDS


# ---------------------------------------------------------------------------
# LIN-011: Retention / deletion planning
# ---------------------------------------------------------------------------

# Tables whose rows carry common lineage fields (asset_id, run_id)
_LINEAGE_ENVELOPE_TABLES = frozenset({
    "source_files",
    "document_elements",
    "chunks",
    "entities",
    "relationships",
    "evidence",
    "visual_assets",
    "visual_regions",
    "claims",
    "clusters",
    "cluster_memberships",
    "drawing_elements",
    "drawing_relationships",
})

def _processing_run_closure(
    processing_runs: list[dict[str, Any]],
    seed_run_ids: set[str],
) -> set[str]:
    """Return seed runs plus every transitive child run."""
    closure = set(seed_run_ids)
    changed = True
    while changed:
        changed = False
        for row in processing_runs:
            run_id = str(row.get("run_id", "") or "")
            parent_run_id = str(row.get("parent_run_id", "") or "")
            if run_id and parent_run_id in closure and run_id not in closure:
                closure.add(run_id)
                changed = True
    return closure


def _append_dependent(
    dependent_records: dict[str, list[str]],
    table_name: str,
    record_id: str,
) -> None:
    """Append a dependent record once while preserving discovery order."""
    records = dependent_records.setdefault(table_name, [])
    if record_id not in records:
        records.append(record_id)


@dataclass
class DeletionPlan:
    """Result of a deletion dependency analysis.

    ``safe_to_delete`` is False whenever any blocker is present.  Callers
    must inspect ``blockers`` and ``orphaned_citations`` before proceeding.
    """

    target_type: str  # "asset" | "run"
    target_id: str
    dependent_records: dict[str, list[str]] = field(default_factory=dict)
    orphaned_citations: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    safe_to_delete: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_type": self.target_type,
            "target_id": self.target_id,
            "dependent_records": self.dependent_records,
            "orphaned_citations": self.orphaned_citations,
            "blockers": self.blockers,
            "safe_to_delete": self.safe_to_delete,
        }

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True)


def build_deletion_plan(
    tables: dict[str, list[dict[str, Any]]],
    *,
    asset_id: str | None = None,
    run_id: str | None = None,
) -> DeletionPlan:
    """Identify all records dependent on *asset_id* or *run_id* before deletion.

    Exactly one of *asset_id* or *run_id* must be specified.

    Finds:
    - All derived records in envelope tables that reference the target
    - Active deployments/citations that still point to dependent records
    - Whether the delete is safe to perform (no orphaned citations)

    Parameters
    ----------
    tables:
        Dict of table_name → list[row_dict].  All relevant tables should be
        included; missing tables are silently treated as empty.
    asset_id:
        Delete by asset identity.
    run_id:
        Delete by processing run identity.

    Returns
    -------
    DeletionPlan
        ``safe_to_delete`` is False whenever orphaned citations or active
        deployments reference records that would be deleted.

    Raises
    ------
    ValueError
        If neither or both of *asset_id* / *run_id* are provided.
    """
    if bool(asset_id) == bool(run_id):
        raise ValueError(
            "Provide exactly one of asset_id or run_id to build_deletion_plan."
        )

    target_type = "asset" if asset_id else "run"
    target_id = asset_id or run_id  # type: ignore[assignment]

    plan = DeletionPlan(target_type=target_type, target_id=target_id)

    affected_asset_version_ids: set[str] = set()
    if asset_id:
        for row in tables.get("asset_versions", []):
            if str(row.get("asset_id", "") or "") == str(asset_id):
                version_id = str(row.get("asset_version_id", "") or "")
                if version_id:
                    affected_asset_version_ids.add(version_id)

    seed_run_ids: set[str] = set()
    if asset_id:
        for table_name in _LINEAGE_ENVELOPE_TABLES:
            for row in tables.get(table_name, []):
                row_asset_id = str(row.get("asset_id", "") or "")
                row_version_id = str(row.get("asset_version_id", "") or "")
                if (
                    row_asset_id == str(asset_id)
                    or row_version_id in affected_asset_version_ids
                ):
                    row_run_id = str(row.get("run_id", "") or "")
                    if row_run_id:
                        seed_run_ids.add(row_run_id)
    else:
        seed_run_ids.add(str(run_id))

    dependent_run_ids = _processing_run_closure(
        tables.get("processing_runs", []),
        seed_run_ids,
    )

    # Collect all affected records. For asset deletion, downstream child-run
    # outputs are included even when they do not repeat the original asset ID.
    if asset_id:
        for row in tables.get("asset_versions", []):
            if str(row.get("asset_id", "") or "") == str(asset_id):
                version_id = row.get("asset_version_id")
                if version_id is not None:
                    _append_dependent(
                        plan.dependent_records,
                        "asset_versions",
                        str(version_id),
                    )

    for table_name in _LINEAGE_ENVELOPE_TABLES:
        pk_field = TABLE_ID_FIELDS.get(table_name)
        if pk_field is None:
            continue
        for row in tables.get(table_name, []):
            row_run_id = str(row.get("run_id", "") or "")
            matches = row_run_id in dependent_run_ids
            if asset_id:
                matches = matches or (
                    str(row.get("asset_id", "") or "") == str(asset_id)
                    or str(row.get("asset_version_id", "") or "")
                    in affected_asset_version_ids
                )
            if not matches:
                continue
            pk_value = row.get(pk_field)
            if pk_value is not None:
                _append_dependent(
                    plan.dependent_records,
                    table_name,
                    str(pk_value),
                )

    for dependent_run_id in sorted(dependent_run_ids):
        _append_dependent(
            plan.dependent_records,
            "processing_runs",
            dependent_run_id,
        )

    # Find active deployments that reference these runs
    for row in tables.get("deployments", []):
        row_run_id = str(row.get("run_id", "") or "")
        if row_run_id in dependent_run_ids:
            dep_id = row.get("deployment_id")
            status = row.get("status", "")
            if dep_id:
                _append_dependent(
                    plan.dependent_records,
                    "deployments",
                    str(dep_id),
                )
            if dep_id and status not in ("superseded", "deleted", "failed"):
                citation = (
                    f"deployment[{dep_id}] status={status!r} "
                    f"target={row.get('target_name')!r}"
                )
                plan.orphaned_citations.append(citation)

    # Check claim_evidence links into affected evidence records
    affected_evidence_ids = set(plan.dependent_records.get("evidence", []))
    affected_claim_ids = set(plan.dependent_records.get("claims", []))
    if affected_evidence_ids:
        for row in tables.get("claim_evidence", []):
            ev_id = str(row.get("evidence_id", "") or "")
            claim_id = str(row.get("claim_id", "") or "")
            if ev_id in affected_evidence_ids and claim_id not in affected_claim_ids:
                plan.orphaned_citations.append(
                    f"claim_evidence[claim={claim_id!r},"
                    f"evidence={ev_id!r}] would be orphaned"
                )

    # Cross-run claims and relationships can point at records produced by the
    # target run/asset. If the referencing row is not also being deleted, it is
    # an external dependency and must block deletion.
    affected_entity_ids = set(plan.dependent_records.get("entities", []))
    affected_relationship_ids = set(plan.dependent_records.get("relationships", []))
    if affected_entity_ids:
        for row in tables.get("relationships", []):
            relationship_id = str(row.get("relationship_id", "") or "")
            if relationship_id in affected_relationship_ids:
                continue
            source_id = str(row.get("source_entity_id", "") or "")
            target_id_value = str(row.get("target_entity_id", "") or "")
            if source_id in affected_entity_ids or target_id_value in affected_entity_ids:
                plan.orphaned_citations.append(
                    f"relationship[{relationship_id!r}] references an affected entity"
                )
        for row in tables.get("claims", []):
            claim_id = str(row.get("claim_id", "") or "")
            if claim_id in affected_claim_ids:
                continue
            subject_id = str(row.get("subject_entity_id", "") or "")
            object_id = str(row.get("object_entity_id", "") or "")
            if subject_id in affected_entity_ids or object_id in affected_entity_ids:
                plan.orphaned_citations.append(
                    f"claim[{claim_id!r}] references an affected entity"
                )

    # Membership rows have composite identities. Record them as dependent links
    # rather than silently omitting them from the deletion impact.
    affected_cluster_ids = set(plan.dependent_records.get("clusters", []))
    for index, row in enumerate(tables.get("cluster_memberships", [])):
        if (
            str(row.get("cluster_id", "") or "") in affected_cluster_ids
            or str(row.get("entity_id", "") or "") in affected_entity_ids
            or str(row.get("relationship_id", "") or "") in affected_relationship_ids
            or str(row.get("claim_id", "") or "") in affected_claim_ids
        ):
            _append_dependent(
                plan.dependent_records,
                "cluster_memberships",
                f"row:{index}",
            )

    # Populate blockers
    if plan.orphaned_citations:
        plan.blockers.append(
            f"{len(plan.orphaned_citations)} orphaned citation(s) must be resolved first"
        )
        plan.safe_to_delete = False

    total_dependent = sum(len(v) for v in plan.dependent_records.values())
    if total_dependent > 0 and plan.safe_to_delete:
        plan.blockers.append(
            f"{total_dependent} dependent record(s) across "
            f"{len(plan.dependent_records)} table(s) will be deleted"
        )

    return plan


@dataclass
class OrphanReport:
    """Records found to have broken FK references."""

    table: str
    fk_field: str
    expected_table: str
    orphan_record_ids: list[str] = field(default_factory=list)
    missing_target_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "fk_field": self.fk_field,
            "expected_table": self.expected_table,
            "orphan_count": len(self.orphan_record_ids),
            "orphan_record_ids": self.orphan_record_ids,
            "missing_target_ids": self.missing_target_ids,
        }


def find_orphaned_records(
    tables: dict[str, list[dict[str, Any]]],
    *,
    check_lineage_envelope: bool = True,
) -> list[OrphanReport]:
    """Scan all tables for FK values pointing to non-existent target records.

    Useful before deletion to surface hidden dependencies and before
    publication to verify referential integrity.

    Parameters
    ----------
    tables:
        Dict of table_name → list[row_dict].
    check_lineage_envelope:
        When True (default), also check the common lineage fields
        (asset_id, asset_version_id, run_id) on every envelope table.

    Returns
    -------
    list[OrphanReport]
        One entry per broken FK relationship found.  Empty list = clean.
    """
    from fabric_kg_builder.lineage.trace import _BACKWARD_EDGES  # local import avoids circular

    # Build PK sets for fast membership test
    pk_sets: dict[str, set[str]] = {}
    for table_name, rows in tables.items():
        pk_field = TABLE_ID_FIELDS.get(table_name)
        if pk_field is None:
            continue
        pk_sets[table_name] = {str(r[pk_field]) for r in rows if r.get(pk_field)}

    # Filter edges to include/exclude envelope edges
    edges_to_check = _BACKWARD_EDGES
    if not check_lineage_envelope:
        edges_to_check = [
            e for e in edges_to_check
            if e[1] not in ("asset_id", "asset_version_id", "run_id")
        ]

    reports: list[OrphanReport] = []
    for from_table, fk_field, to_table, to_pk_field in edges_to_check:
        rows = tables.get(from_table, [])
        if not rows:
            continue
        from_pk = TABLE_ID_FIELDS.get(from_table)
        target_set = pk_sets.get(to_table, set())

        orphan_ids: list[str] = []
        missing_ids: list[str] = []
        for row in rows:
            fk_value = row.get(fk_field)
            if fk_value is None:
                continue  # null FK is allowed
            fk_str = str(fk_value)
            if fk_str not in target_set:
                if from_pk:
                    row_pk = row.get(from_pk)
                    orphan_ids.append(str(row_pk) if row_pk is not None else "?")
                missing_ids.append(fk_str)

        if orphan_ids:
            reports.append(
                OrphanReport(
                    table=from_table,
                    fk_field=fk_field,
                    expected_table=to_table,
                    orphan_record_ids=orphan_ids,
                    missing_target_ids=list(dict.fromkeys(missing_ids)),
                )
            )

    return reports


# ---------------------------------------------------------------------------
# LIN-012: Manifest redaction (VAL-049)
# ---------------------------------------------------------------------------

# Fields that must never appear in manifests, logs, or publication records.
# These are structural names; values are supplied by callers as canaries.
_FORBIDDEN_MANIFEST_FIELDS: frozenset[str] = frozenset({
    "api_key",
    "client_secret",
    "password",
    "token",
    "access_token",
    "refresh_token",
    "connection_string",
    "sas_token",
    "bearer",
    "private_key",
    "credential",
    "secret",
})

# Fields that hold raw source content and should not appear in manifests.
_CONTENT_FIELDS: frozenset[str] = frozenset({
    "content",
    "content_html",
    "text",
    "embedding_text",
    "description",
})


def check_manifest_redaction(
    manifest: dict[str, Any],
    *,
    canaries: list[str],
    raw_content_canaries: list[str] | None = None,
) -> list[str]:
    """Return the list of canary values found in the manifest serialisation.

    VAL-049: The manifest (or any log/JSON output written to disk) must not
    contain credential canaries or raw source-content canaries.

    A canary found in the manifest indicates a redaction failure.

    Parameters
    ----------
    manifest:
        The manifest dict (e.g. the run manifest JSON) to inspect.
    canaries:
        Secret/credential canary values.  These must not appear anywhere in
        the serialised manifest.
    raw_content_canaries:
        Optional source-content canary values (e.g. original document text
        excerpts).  These must not appear in manifests.

    Returns
    -------
    list[str]
        Each entry is a canary value found in the manifest.  Empty = clean.
    """
    serialised = json.dumps(manifest, default=str)
    violations: list[str] = []

    for canary in canaries:
        if canary and canary in serialised:
            violations.append(canary)

    for canary in raw_content_canaries or []:
        if canary and canary in serialised:
            violations.append(canary)

    return violations


def redact_for_manifest(
    row: dict[str, Any],
    *,
    additional_fields: list[str] | None = None,
    replace_with: str = "[REDACTED]",
) -> dict[str, Any]:
    """Return a copy of *row* with sensitive and raw-content fields redacted.

    Used to sanitise canonical rows before writing to run manifests or logs.
    Does not modify the original row.

    Parameters
    ----------
    row:
        A canonical row dict (e.g. from any table).
    additional_fields:
        Caller-provided field names to redact in addition to the built-in set.
    replace_with:
        Replacement marker.  Defaults to ``"[REDACTED]"``.

    Returns
    -------
    dict
        Shallow copy with sensitive field values replaced.
    """
    fields_to_redact = _FORBIDDEN_MANIFEST_FIELDS | _CONTENT_FIELDS
    if additional_fields:
        fields_to_redact = fields_to_redact | frozenset(additional_fields)

    result = dict(row)
    for key in list(result):
        # Check exact field name match
        if key.lower() in fields_to_redact:
            result[key] = replace_with
            continue
        # Check if any forbidden token is a substring of the key (e.g. api_key_value)
        key_lower = key.lower()
        for forbidden in _FORBIDDEN_MANIFEST_FIELDS:
            if forbidden in key_lower:
                result[key] = replace_with
                break

    return result


def check_deployment_record_safety(deployment_row: dict[str, Any]) -> list[str]:
    """Verify a deployment row does not contain secrets or raw source content.

    Returns list of field names that violate the deployment record safety
    contract (should be empty for a correctly constructed DeploymentRow).

    Per LIN-009: Search/Lakehouse/Ontology records carry asset/version/run
    locators but not credentials or source bytes.
    """
    violations: list[str] = []
    for key in deployment_row:
        key_lower = key.lower()
        for forbidden in _FORBIDDEN_MANIFEST_FIELDS:
            if forbidden in key_lower:
                value = deployment_row[key]
                if value and str(value).strip():
                    violations.append(
                        f"deployment field {key!r} appears to contain sensitive data"
                    )
                break
        if key_lower in _CONTENT_FIELDS:
            value = deployment_row[key]
            if value and str(value).strip():
                violations.append(
                    f"deployment field {key!r} contains raw source content"
                )
    return violations
