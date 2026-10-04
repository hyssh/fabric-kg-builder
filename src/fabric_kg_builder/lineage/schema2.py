"""Read-only data lineage, enabled only by an intact frozen L1 contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from fabric_kg_builder.contracts.receipts import ArtifactManifest, StageReceipt
from fabric_kg_builder.enrichment.schema2_sources import L2StageError, load_l2_inputs
from fabric_kg_builder.enrichment.schema2_validation_stage import (
    l3_input_fingerprint,
    l3_run_root,
    load_l3_inputs,
    proposed_candidate_payload,
)
from fabric_kg_builder.semantic.source_tables import SealedL4ServingSource


def frozen_lineage_authority(*, l1_state: Path, domain: Path) -> dict[str, Any]:
    """Approval status alone is insufficient: verify the entire L1 handoff."""
    try:
        inputs = load_l2_inputs(l1_state_root=l1_state, domain_path=domain)
    except L2StageError as exc:
        raise ValueError(
            f"LINEAGE_SCHEMA_NOT_FROZEN: {exc}. Approve an intact final schema "
            "before tracing instance data; use window-run-history for discovery."
        ) from exc
    return {
        "schema_state": "frozen",
        "collection_starts_at": "L2_after_L1_approval",
        "domain_contract_hash": inputs.domain_contract.approval.contract_hash,
        "approval_context_hash": inputs.approval_context.approval_context_hash,
        "l1_receipt_hash": inputs.l1_receipt.receipt_hash,
        "corpus_hash": inputs.corpus_manifest.corpus_hash,
    }


def trace_schema2_record(
    record_id: str,
    *,
    l1_state: Path,
    domain: Path,
    l2_state: Path,
    l4_run: Path | None = None,
    l3_root: Path | None = None,
) -> dict[str, Any]:
    """Join frozen-schema proposals to their immutable source and optional L4."""
    authority = frozen_lineage_authority(l1_state=l1_state, domain=domain)
    if (l4_run is None) != (l3_root is None):
        raise ValueError("LINEAGE_INPUT_REQUIRED: supply --l4-run and --l3-root together")
    inputs = load_l3_inputs(
        l1_state_root=l1_state, domain_path=domain, l2_state_root=l2_state,
    )
    candidates = [
        (batch_id, candidate)
        for batch_id in inputs.leaf_batch_ids
        for candidate in inputs.proposed_partitions[batch_id]
    ]
    audit_rows: list[dict[str, Any]] = []
    serving_rows: list[dict[str, Any]] = []
    if l4_run is not None and l3_root is not None:
        source = SealedL4ServingSource.from_run(
            l4_run, input_manifest_search_roots=(l3_root,),
        )
        if source.receipt.identity.domain_contract_hash != authority["domain_contract_hash"]:
            raise ValueError("LINEAGE_SCHEMA_DRIFT: L4 belongs to a different frozen schema")
        # A matching domain/run ID does not imply the same extraction responses.
        fingerprint = l3_input_fingerprint(inputs)
        root = l3_root if l3_root.name == fingerprint else l3_run_root(l3_root, fingerprint)
        if not root.is_dir():
            raise ValueError("LINEAGE_RUN_DRIFT: no L3 run matches this exact L2 handoff")
        receipt = StageReceipt.model_validate_json((root / "stage-receipt.json").read_text("utf-8"))
        manifest = ArtifactManifest.model_validate_json((root / "output-manifest.json").read_text("utf-8"))
        input_manifest = ArtifactManifest.model_validate_json((root / "input-manifest.json").read_text("utf-8"))
        if (
            receipt.status != "succeeded" or receipt.skip_key != fingerprint
            or manifest != source.input_manifest
            or receipt.output_manifest_hash != manifest.manifest_hash
            or receipt.input_manifest_hash != input_manifest.manifest_hash
            or not any(
                entry.artifact_id == inputs.l2_receipt.stage_receipt_id
                and entry.content_hash == inputs.l2_receipt.receipt_hash
                for entry in input_manifest.entries
            )
        ):
            raise ValueError("LINEAGE_RUN_DRIFT: L4 is not backed by this exact L2/L3 chain")
        audit_rows = list(source.audit_rows())
        authority["l4_receipt_hash"] = source.receipt.receipt_hash
        authority["l3_output_manifest_hash"] = source.input_manifest.manifest_hash
    matched_audit = [
        row for row in audit_rows
        if record_id in (row["candidate_id"], row["semantic_assertion_id"], row["input_candidate_id"])
    ]
    audited_ids = {row["candidate_id"] for row in matched_audit}
    matches = [
        (batch_id, candidate) for batch_id, candidate in candidates
        if candidate.candidate_id in audited_ids
        or record_id in (
            candidate.candidate_id, candidate.candidate_version_id,
            candidate.input_candidate_id, candidate.semantic_id,
        )
    ]
    if not matches:
        raise ValueError(f"LINEAGE_RECORD_NOT_FOUND: {record_id}")
    match_ids = {candidate.candidate_id for _, candidate in matches}
    if audited_ids - match_ids:
        raise ValueError("LINEAGE_RUN_DRIFT: L4 candidates are absent from the supplied L2")
    if l4_run is not None and l3_root is not None:
        assertion_ids = {
            row["semantic_assertion_id"] for row in audit_rows
            if row["candidate_id"] in match_ids
        }
        for table, key in (
            ("semantic_asserted_entities", "entity_id"),
            ("semantic_asserted_relationships", "relationship_id"),
            ("semantic_asserted_properties", "property_assertion_id"),
        ):
            serving_rows.extend(
                {"table": table, "record": row}
                for row in pq.read_table(source.resolve(table)).to_pylist()
                if row[key] in assertion_ids
            )
    entries = {entry.source_file_id: entry for entry in inputs.corpus_manifest.entries}
    observations = []
    for batch_id, candidate in matches:
        unit = inputs.source_units.require(candidate.source_unit_id)
        entry = entries[unit.source_file_id]
        observations.append({
            "candidate": proposed_candidate_payload(candidate),
            "batch_id": batch_id,
            "batch_hash": inputs.batch_by_id[batch_id].batch_hash,
            "source": {
                "source_unit_id": unit.source_unit_id,
                "source_file_id": unit.source_file_id,
                "source_text_hash": unit.text_content_hash,
                "asset_id": entry.asset_id,
                "asset_version_id": entry.asset_version_id,
                "original_byte_hash": entry.original_byte_hash,
                "relative_source_ref": entry.relative_source_ref,
                "locator": unit.locator.model_dump(mode="json"),
            },
            "proposal_anchor_is_verified_evidence": False,
        })
    return {
        "trace_version": "schema2/1.0.0",
        "record_id": record_id,
        "authority": {
            **authority,
            "l2_receipt_hash": inputs.l2_receipt.receipt_hash,
            "l2_output_manifest_hash": inputs.l2_output_manifest.manifest_hash,
            "extraction_identity": inputs.l2_receipt.identity.model_dump(mode="json"),
        },
        "observations": observations,
        "validation": [
            row for row in audit_rows if row["candidate_id"] in match_ids
        ],
        "serving_records": serving_rows,
        "validation_state": "L4_audit" if l4_run is not None else "L2_proposals_only",
        "semantic_accuracy": "not_assessed",
    }
