"""Isolated local L4 audit and asserted-only semantic serving projection."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import ValidationError

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback is exercised on Windows.
    fcntl = None

from fabric_kg_builder.contracts.base import (
    canonical_json,
    canonical_sha256,
    deterministic_contract_id,
)
from fabric_kg_builder.contracts.evidence import EvidenceSpanV1_1
from fabric_kg_builder.contracts.extraction import RequiredMemberManifestV1_1
from fabric_kg_builder.contracts.identity import CanonicalIdentityEnvelope
from fabric_kg_builder.contracts.lifecycle import (
    AssertionState,
    CandidateAccountingDisposition,
    CandidateLifecycleRecord,
)
from fabric_kg_builder.contracts.projection import (
    AuditProjection,
    SemanticServingProjection,
    validate_asserted_serving_subset,
)
from fabric_kg_builder.contracts.publication import (
    ProjectionEquivalence,
    ProjectionEvidence,
    PublicationAuthorityReferences,
)
from fabric_kg_builder.contracts.receipts import (
    ArtifactEntry,
    ArtifactManifest,
    StageReceipt,
)
from fabric_kg_builder.contracts.resources import (
    StageResourceMetrics,
    validate_receipt_resources,
)
from fabric_kg_builder.enrichment.schema2_evidence import (
    L3_EXTRACTION_PURPOSE,
    resolve_most_specific_classification,
)
from fabric_kg_builder.enrichment.schema2_stage import L2_RESPONSE_SCHEMA_HASH
from fabric_kg_builder.enrichment.schema2_validation_stage import (
    L3_ACCEPTED_VERSIONS,
    L3_EVIDENCE_SPAN_VERSION,
    L3_STAGE_NAME,
    CandidateValidationRecord,
    L3LeafResult,
    L3StageResult,
    l3_input_fingerprint,
)
from fabric_kg_builder.model.arrow_schemas import L4_PROJECTION_TABLE_SCHEMAS
from fabric_kg_builder.platform import process_resource_usage
from fabric_kg_builder.semantic.source_tables import (
    L4_ACCEPTED_VERSIONS,
    L4_PROJECTION_CODE_VERSION,
    SealedL4ServingSource,
)

L4_STAGE_NAME = "schema2-audit-serving-projection"
L4_STAGE_CONTRACT_VERSION = "1.0.0"
L4_STATE_DIR = Path(".fkg") / "l4"
L4_RUNS_DIRNAME = "runs"

_TABLE_ORDER = tuple(L4_PROJECTION_TABLE_SCHEMAS)
_PROJECTION_FILES = {
    "l4-audit-projection": Path("audit-projection.json"),
    "l4-semantic-serving-projection": Path("semantic-serving-projection.json"),
    "l4-parquet-projection-equivalence": Path("projection-equivalence.json"),
}
_STAGE_FILES = {
    Path("output-manifest.json"),
    Path("resource-metrics.json"),
    Path("stage-receipt.json"),
}


class L4ProjectionError(ValueError):
    """Fail-closed L4 error with a stable operator-facing code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class L4ProjectionRows:
    audit_candidates: tuple[dict[str, Any], ...]
    semantic_publication_authority: tuple[dict[str, Any], ...]
    semantic_asserted_entities: tuple[dict[str, Any], ...]
    semantic_entity_type_assertions: tuple[dict[str, Any], ...]
    semantic_asserted_relationships: tuple[dict[str, Any], ...]
    semantic_asserted_properties: tuple[dict[str, Any], ...]
    semantic_required_member_manifests: tuple[dict[str, Any], ...]
    semantic_required_members: tuple[dict[str, Any], ...]

    def tables(self) -> dict[str, tuple[dict[str, Any], ...]]:
        return {
            name: getattr(self, name)
            for name in _TABLE_ORDER
        }


@dataclass(frozen=True)
class L4StageResult:
    source: L3StageResult
    rows: L4ProjectionRows
    audit_projection: AuditProjection
    serving_projection: SemanticServingProjection
    projection_equivalences: tuple[ProjectionEquivalence, ...]
    output_manifest: ArtifactManifest
    metrics: StageResourceMetrics
    receipt: StageReceipt
    state_root: Path
    run_root: Path
    reused: bool

    def sealed_source(self) -> SealedL4ServingSource:
        return SealedL4ServingSource(
            root=self.run_root,
            projection=self.serving_projection,
            receipt=self.receipt,
            manifest=self.output_manifest,
            input_manifest=self.source.output_manifest,
        )


def _identity(
    source: L3StageResult,
    *,
    contract_kind: str,
) -> CanonicalIdentityEnvelope:
    values = source.receipt.identity.model_dump(mode="python", round_trip=True)
    values.update({
        "contract_kind": contract_kind,
        "semantic_contract_hash": source.inputs.hierarchy.domain_contract_hash,
        "canonical_schema_version": "2.0",
        "parent_artifact_ids": tuple(sorted({
            *source.receipt.identity.parent_artifact_ids,
            source.output_manifest.artifact_manifest_id,
        })),
    })
    return CanonicalIdentityEnvelope.model_validate(values)


def _schema_descriptor(schema: pa.Schema) -> list[dict[str, Any]]:
    return [
        {
            "name": field.name,
            "type": str(field.type),
            "nullable": field.nullable,
        }
        for field in schema
    ]


def _schema_hash(schema: pa.Schema) -> str:
    return canonical_sha256(_schema_descriptor(schema))


def l4_input_fingerprint(source: L3StageResult) -> str:
    """Bind L4 reuse to L3, all sealed authorities, code, and physical schemas."""

    return canonical_sha256({
        "stage": L4_STAGE_NAME,
        "stage_contract_version": L4_STAGE_CONTRACT_VERSION,
        "projection_code_version": L4_PROJECTION_CODE_VERSION,
        "accepted_contract_versions": L4_ACCEPTED_VERSIONS,
        "l3_receipt_hash": source.receipt.receipt_hash,
        "l3_output_manifest_hash": source.output_manifest.manifest_hash,
        "authorities": source.inputs.authority_hashes,
        "required_member_manifest_hashes": sorted(
            manifest.manifest_hash for manifest in source.required_member_manifests
        ),
        "physical_schema_hashes": {
            name: _schema_hash(schema)
            for name, schema in L4_PROJECTION_TABLE_SCHEMAS.items()
        },
    })


def l4_run_root(state_root: Path, fingerprint: str) -> Path:
    return state_root / L4_RUNS_DIRNAME / fingerprint


def _artifact_by_id(
    manifest: ArtifactManifest,
    artifact_id: str,
    *,
    code: str,
) -> ArtifactEntry:
    matches = [entry for entry in manifest.entries if entry.artifact_id == artifact_id]
    if len(matches) != 1:
        raise L4ProjectionError(
            code,
            f"expected exactly one L3 artifact {artifact_id!r}",
        )
    return matches[0]


def _canonical_json_size(value: Any) -> int:
    return len((canonical_json(value) + "\n").encode("utf-8"))


def _l3_current_state_index(source: L3StageResult) -> dict[str, Any]:
    by_state: defaultdict[str, set[str]] = defaultdict(set)
    for leaf in source.leaves:
        for result in leaf.candidate_results:
            by_state[result.current_state].add(result.candidate_id)
    return {
        "counts_by_state": [
            [state, len(candidate_ids)]
            for state, candidate_ids in sorted(by_state.items())
        ],
        "candidate_ids_by_state": {
            state: sorted(candidate_ids)
            for state, candidate_ids in sorted(by_state.items())
        },
    }


def _l3_reason_code_index(source: L3StageResult) -> dict[str, Any]:
    by_reason: defaultdict[str, set[str]] = defaultdict(set)
    for leaf in source.leaves:
        for result in leaf.candidate_results:
            for reason in result.reason_codes:
                by_reason[reason].add(result.candidate_id)
    collection_reasons: defaultdict[str, set[str]] = defaultdict(set)
    for record in source.required_member_outcomes:
        for reason in record.outcome.reason_codes:
            collection_reasons[reason].add(
                record.outcome.required_member_set_proposal_id
            )
    return {
        "candidate_reason_counts": [
            [reason, len(candidate_ids)]
            for reason, candidate_ids in sorted(by_reason.items())
        ],
        "candidate_ids_by_reason": {
            reason: sorted(candidate_ids)
            for reason, candidate_ids in sorted(by_reason.items())
        },
        "collection_reason_counts": [
            [reason, len(proposal_ids)]
            for reason, proposal_ids in sorted(collection_reasons.items())
        ],
        "proposal_ids_by_reason": {
            reason: sorted(proposal_ids)
            for reason, proposal_ids in sorted(collection_reasons.items())
        },
        "domain_rereview_requested": sorted(
            by_reason.get("DOMAIN_REREVIEW_REQUESTED", set())
        ),
    }


def _l3_identity_index(source: L3StageResult) -> dict[str, Any]:
    entities: defaultdict[str, dict[str, set[str]]] = defaultdict(
        lambda: {
            "classification_version_ids": set(),
            "semantic_type_ids": set(),
            "candidate_ids": set(),
            "witness_kinds": set(),
        }
    )
    relationships: defaultdict[str, dict[str, set[str]]] = defaultdict(
        lambda: {"candidate_ids": set(), "endpoint_pairs": set(), "states": set()}
    )
    recomputed: dict[str, bool] = {}
    for leaf in source.leaves:
        for classification in leaf.classifications:
            bucket = entities[classification.entity_id]
            bucket["classification_version_ids"].add(
                classification.classification_version_id
            )
            if classification.semantic_type_id:
                bucket["semantic_type_ids"].add(classification.semantic_type_id)
            bucket["candidate_ids"].add(classification.candidate_id)
        for result in leaf.candidate_results:
            if result.candidate_kind == "entity":
                bucket = entities[result.semantic_id]
                bucket["witness_kinds"].add(result.identity_witness_kind)
                recomputed[result.semantic_id] = (
                    recomputed.get(result.semantic_id, False)
                    or result.identity_recomputed
                )
            elif result.candidate_kind == "relationship":
                edge = relationships[result.semantic_id]
                edge["candidate_ids"].add(result.candidate_id)
                edge["endpoint_pairs"].add(
                    f"{result.resolved_source_entity_id}->"
                    f"{result.resolved_target_entity_id}"
                )
                edge["states"].add(result.current_state)
    hierarchy = source.inputs.hierarchy
    return {
        "entities": [
            {
                "entity_id": entity_id,
                "classification_version_ids": sorted(
                    bucket["classification_version_ids"]
                ),
                "semantic_type_ids": sorted(bucket["semantic_type_ids"]),
                "most_specific_type_id": resolve_most_specific_classification(
                    bucket["semantic_type_ids"],
                    hierarchy,
                ).most_specific_type_id,
                "candidate_ids": sorted(bucket["candidate_ids"]),
                "identity_recomputed": recomputed.get(entity_id, False),
                "identity_witness_kinds": sorted(bucket["witness_kinds"]),
            }
            for entity_id, bucket in sorted(entities.items())
        ],
        "relationships": [
            {
                "relationship_id": relationship_id,
                "candidate_ids": sorted(bucket["candidate_ids"]),
                "endpoint_pairs": sorted(bucket["endpoint_pairs"]),
                "current_states": sorted(bucket["states"]),
            }
            for relationship_id, bucket in sorted(relationships.items())
        ],
    }


def _validate_l3_indexes(source: L3StageResult) -> None:
    expected = {
        "l3-identity-index": (
            "l3.identity_index",
            _l3_identity_index(source),
        ),
        "l3-current-state-index": (
            "l3.current_state_index",
            _l3_current_state_index(source),
        ),
        "l3-reason-code-index": (
            "l3.reason_code_index",
            _l3_reason_code_index(source),
        ),
    }
    for artifact_id, (contract_kind, payload) in expected.items():
        entry = _artifact_by_id(
            source.output_manifest,
            artifact_id,
            code="L4_INPUT_MANIFEST_INVALID",
        )
        if (
            entry.contract_kind != contract_kind
            or entry.contract_version != "1.0.0"
            or entry.schema_hash
            != canonical_sha256({
                "contract_kind": contract_kind,
                "version": "1.0.0",
            })
            or entry.content_hash != canonical_sha256(payload)
            or entry.byte_count != _canonical_json_size(payload)
            or entry.row_count is not None
            or entry.canonical_id_set_hash is not None
        ):
            raise L4ProjectionError(
                "L4_INPUT_MANIFEST_INVALID",
                f"L3 artifact {artifact_id} differs from consumed candidate results",
            )


def _validate_l3_artifacts(source: L3StageResult) -> None:
    """Reconcile every L3 value consumed by L4 with the succeeded handoff."""

    receipt = source.receipt
    if (
        receipt.stage_id != "L3"
        or receipt.stage_name != L3_STAGE_NAME
        or receipt.stage_contract_version != "1.0.0"
        or receipt.status != "succeeded"
    ):
        raise L4ProjectionError(
            "L4_INPUT_RECEIPT_INVALID",
            "L4 requires one succeeded L3 receipt",
        )
    if dict(receipt.accepted_contract_versions) != dict(L3_ACCEPTED_VERSIONS):
        raise L4ProjectionError(
            "L4_CONTRACT_VERSION_UNSUPPORTED",
            "L3 receipt did not bind the exact accepted contract versions",
        )
    if (
        receipt.output_manifest_id != source.output_manifest.artifact_manifest_id
        or receipt.output_manifest_hash != source.output_manifest.manifest_hash
    ):
        raise L4ProjectionError(
            "L4_INPUT_MANIFEST_INVALID",
            "L3 receipt and output manifest differ",
        )
    if (
        receipt.input_manifest_id != source.input_manifest.artifact_manifest_id
        or receipt.input_manifest_hash != source.input_manifest.manifest_hash
        or receipt.skip_key != l3_input_fingerprint(source.inputs)
    ):
        raise L4ProjectionError(
            "L4_INPUT_MANIFEST_INVALID",
            "L3 receipt does not bind the consumed L3 inputs",
        )
    try:
        validate_receipt_resources(receipt, source.metrics)
    except ValueError as exc:
        raise L4ProjectionError("L4_RESOURCE_BINDING_INVALID", str(exc)) from exc

    domain_hash = source.inputs.hierarchy.domain_contract_hash
    if (
        receipt.identity.domain_contract_hash != domain_hash
        or source.inputs.domain_contract.approval.contract_hash != domain_hash
    ):
        raise L4ProjectionError(
            "L4_DOMAIN_HASH_MISMATCH",
            "L3 receipt and approved DomainContractV2 authority differ",
        )
    authority = source.inputs.authority_hashes
    if authority != {
        "domain_contract_hash": domain_hash,
        "hierarchy_hash": source.inputs.domain_contract.hierarchy_closure.hierarchy_hash,
        "identity_policy_hash": source.inputs.domain_contract.identity_policy_hash,
        "completeness_requirement_hash": (
            source.inputs.domain_contract.completeness_requirement_hash
        ),
        "external_reference_decision_hash": (
            source.inputs.domain_contract.external_reference_decision_hash
        ),
    }:
        raise L4ProjectionError(
            "L4_AUTHORITY_HASH_MISMATCH",
            "L3 compiled authorities differ from the sealed DomainContractV2",
        )

    seen_candidates: set[str] = set()
    for leaf in source.leaves:
        batch_id = leaf.extraction_candidate_batch_id
        _validate_leaf_manifest(source.output_manifest, leaf)
        batch = source.inputs.batch_by_id[batch_id]
        batch_entry = _artifact_by_id(
            source.inputs.l2_output_manifest,
            batch_id,
            code="L4_INPUT_MANIFEST_INVALID",
        )
        recomputed_batch_hash = canonical_sha256(
            batch.model_dump(mode="json", exclude={"batch_hash"})
        )
        recomputed_candidate_id_set_hash = canonical_sha256(
            sorted(candidate.candidate_id for candidate in batch.candidates)
        )
        if (
            batch_entry.contract_kind != "c0.extraction_candidate_batch"
            or batch_entry.contract_version != "1.0.0"
            or batch_entry.schema_hash
            != canonical_sha256(type(batch).model_json_schema())
            or batch.batch_hash != recomputed_batch_hash
            or batch_entry.content_hash != recomputed_batch_hash
            or batch_entry.byte_count != _canonical_json_size(batch)
            or batch.candidate_id_set_hash != recomputed_candidate_id_set_hash
            or batch_entry.canonical_id_set_hash
            != recomputed_candidate_id_set_hash
            or batch_entry.row_count != len(batch.candidates)
            or batch.retained_candidate_count != len(batch.candidates)
            or batch.input_candidate_count
            != len(batch.candidate_dispositions)
        ):
            raise L4ProjectionError(
                "L4_INPUT_MANIFEST_INVALID",
                f"L2 candidate batch {batch_id} differs from its manifest entry",
            )
        proposals = source.inputs.proposed_partitions[batch_id]
        proposal_entry = _artifact_by_id(
            source.inputs.l2_output_manifest,
            f"{batch_id}:proposals",
            code="L4_INPUT_MANIFEST_INVALID",
        )
        proposal_payload = [
            proposal.model_dump(mode="json") for proposal in proposals
        ]
        if (
            proposal_entry.contract_kind != "l2.proposed_candidate_partition"
            or proposal_entry.contract_version != "1.0.0"
            or proposal_entry.schema_hash != L2_RESPONSE_SCHEMA_HASH
            or proposal_entry.content_hash != canonical_sha256(proposal_payload)
            or proposal_entry.byte_count != _canonical_json_size(proposal_payload)
            or proposal_entry.row_count != len(proposals)
            or proposal_entry.canonical_id_set_hash
            != canonical_sha256(
                sorted(proposal.candidate_id for proposal in proposals)
            )
        ):
            raise L4ProjectionError(
                "L4_INPUT_MANIFEST_INVALID",
                f"L2 proposal partition {batch_id} differs from its manifest entry",
            )
        proposal_by_candidate = {
            proposal.candidate_id: proposal for proposal in proposals
        }
        for result in leaf.candidate_results:
            if result.candidate_id in seen_candidates:
                raise L4ProjectionError(
                    "L4_ACCOUNTING_INCOMPLETE",
                    f"candidate {result.candidate_id} appears in multiple L3 leaves",
                )
            seen_candidates.add(result.candidate_id)
            proposal = proposal_by_candidate.get(result.candidate_id)
            if proposal is None or (
                result.candidate_version_id != proposal.candidate_version_id
                or result.candidate_kind != proposal.candidate_kind
                or result.semantic_id != proposal.semantic_id
                or result.approved_semantic_id != proposal.approved_semantic_id
                or result.source_unit_id != proposal.source_unit_id
                or result.ignored_model_evidence_id
                != (
                    proposal.proposed_anchor.model_authored_evidence_id
                    if proposal.proposed_anchor is not None
                    else None
                )
            ):
                raise L4ProjectionError(
                    "L4_INPUT_MANIFEST_INVALID",
                    f"L3 candidate result {result.candidate_id} reinterprets "
                    "its sealed L2 proposal",
                )

        if {
            candidate.candidate_id for candidate in batch.candidates
        } != {
            result.candidate_id for result in leaf.candidate_results
        }:
            raise L4ProjectionError(
                "L4_ACCOUNTING_INCOMPLETE",
                f"L3 leaf {batch_id} does not cover every retained candidate",
            )

    _validate_l3_indexes(source)

    proposal_by_id = {
        proposal.required_member_set_proposal_id: proposal
        for proposal in source.inputs.required_member_proposals
    }
    for proposal_id, proposal in proposal_by_id.items():
        entry = _artifact_by_id(
            source.inputs.l2_output_manifest,
            proposal_id,
            code="L4_INPUT_MANIFEST_INVALID",
        )
        proposal_hash = canonical_sha256(
            proposal.model_dump(mode="json", exclude={"proposal_hash"})
        )
        member_id_set_hash = canonical_sha256(
            sorted(member.member_canonical_id for member in proposal.members)
        )
        if (
            proposal.proposal_hash != proposal_hash
            or entry.contract_kind != "c0.required_member_set_proposal"
            or entry.contract_version != "1.1.0"
            or entry.schema_hash != canonical_sha256(type(proposal).model_json_schema())
            or entry.content_hash != proposal_hash
            or entry.byte_count != _canonical_json_size(proposal)
            or entry.row_count != len(proposal.members)
            or entry.canonical_id_set_hash != member_id_set_hash
        ):
            raise L4ProjectionError(
                "L4_INPUT_MANIFEST_INVALID",
                f"RequiredMemberSetProposal {proposal_id} differs from "
                "its L2 manifest entry",
            )
        batch = source.inputs.batch_by_id.get(
            proposal.extraction_candidate_batch_id
        )
        try:
            if batch is None:
                raise ValueError("proposal batch is missing")
            proposal.validate_against_batch(batch)
        except ValueError as exc:
            raise L4ProjectionError(
                "L4_REQUIRED_MEMBER_EQUIVALENCE_FAILED",
                f"RequiredMemberSetProposal {proposal_id} does not bind "
                f"its candidate batch: {exc}",
            ) from exc
    outcome_by_id = {
        record.outcome.required_member_set_proposal_id: record
        for record in source.required_member_outcomes
    }
    if (
        len(proposal_by_id) != len(source.inputs.required_member_proposals)
        or len(outcome_by_id) != len(source.required_member_outcomes)
        or set(outcome_by_id) != set(proposal_by_id)
    ):
        raise L4ProjectionError(
            "L4_REQUIRED_MEMBER_EQUIVALENCE_FAILED",
            "L3 required-member outcomes do not partition the proposal set",
        )

    manifest_ids: set[str] = set()
    for proposal_id, record in outcome_by_id.items():
        outcome = record.outcome
        outcome_payload = {
            key: (list(value) if isinstance(value, tuple) else value)
            for key, value in outcome.__dict__.items()
        }
        outcome_payload["role_coverage"] = [
            list(item) for item in outcome.role_coverage
        ]
        outcome_payload["required_member_manifest_id"] = (
            record.manifest.required_member_manifest_id
            if record.manifest is not None
            else None
        )
        outcome_entry = _artifact_by_id(
            source.output_manifest,
            f"{proposal_id}:outcome",
            code="L4_INPUT_MANIFEST_INVALID",
        )
        if (
            outcome_entry.contract_kind != "l3.required_member_outcome"
            or outcome_entry.contract_version != "1.0.0"
            or outcome_entry.schema_hash
            != canonical_sha256({
                "contract_kind": "l3.required_member_outcome",
                "version": "1.0.0",
            })
            or outcome_entry.content_hash != canonical_sha256(outcome_payload)
            or outcome_entry.byte_count != _canonical_json_size(outcome_payload)
            or outcome_entry.row_count != len(outcome.verified_member_ids)
            or outcome_entry.canonical_id_set_hash
            != canonical_sha256(sorted(outcome.verified_member_ids))
            or (record.manifest is not None)
            != (outcome.completeness_state == "complete")
        ):
            raise L4ProjectionError(
                "L4_REQUIRED_MEMBER_EQUIVALENCE_FAILED",
                f"L3 required-member outcome differs for {proposal_id}",
            )
        if record.manifest is None:
            continue
        manifest = record.manifest
        recomputed_manifest_hash = canonical_sha256(
            manifest.model_dump(
                mode="json",
                exclude={"manifest_hash", "sealed_at_utc"},
            )
        )
        if manifest.required_member_manifest_id in manifest_ids:
            raise L4ProjectionError(
                "L4_REQUIRED_MEMBER_EQUIVALENCE_FAILED",
                "duplicate RequiredMemberManifest ID",
            )
        manifest_ids.add(manifest.required_member_manifest_id)
        if manifest.manifest_hash != recomputed_manifest_hash:
            raise L4ProjectionError(
                "L4_REQUIRED_MEMBER_EQUIVALENCE_FAILED",
                f"RequiredMemberManifest {manifest.required_member_manifest_id} "
                "hash does not recompute",
            )
        try:
            manifest.validate_against_proposal(proposal_by_id[proposal_id])
        except ValueError as exc:
            raise L4ProjectionError(
                "L4_REQUIRED_MEMBER_EQUIVALENCE_FAILED",
                f"RequiredMemberManifest {manifest.required_member_manifest_id} "
                f"reinterprets its C0 proposal: {exc}",
            ) from exc
        if (
            outcome.scope_canonical_id != manifest.scope_canonical_id
            or outcome.requirement_id != manifest.completeness_requirement_id
            or tuple(outcome.verified_member_ids)
            != tuple(sorted(
                member.member_canonical_id for member in manifest.members
            ))
            or outcome.verified_member_count != len(manifest.members)
            or outcome.specified_expected_count != manifest.expected_cardinality
            or outcome.specified_minimum_count != manifest.minimum_cardinality
            or outcome.specified_maximum_count != manifest.maximum_cardinality
            or outcome.recomputed_collection_hash
            != manifest.authoritative_collection_hash
            or outcome.proposal_collection_hash
            != manifest.authoritative_collection_hash
        ):
            raise L4ProjectionError(
                "L4_REQUIRED_MEMBER_EQUIVALENCE_FAILED",
                f"L3 required-member outcome contradicts "
                f"{manifest.required_member_manifest_id}",
            )
        entry = _artifact_by_id(
            source.output_manifest,
            manifest.required_member_manifest_id,
            code="L4_INPUT_MANIFEST_INVALID",
        )
        if (
            entry.contract_kind != "c0.required_member_manifest"
            or entry.contract_version != "1.1.0"
            or entry.schema_hash
            != canonical_sha256(RequiredMemberManifestV1_1.model_json_schema())
            or entry.content_hash != manifest.manifest_hash
            or entry.byte_count != _canonical_json_size(manifest)
            or entry.canonical_id_set_hash != manifest.member_set_hash
            or entry.row_count != len(manifest.members)
        ):
            raise L4ProjectionError(
                "L4_INPUT_MANIFEST_INVALID",
                f"RequiredMemberManifest {manifest.required_member_manifest_id} "
                "differs from the L3 output manifest",
            )


def _validate_leaf_manifest(
    manifest: ArtifactManifest,
    leaf: L3LeafResult,
) -> None:
    batch_id = leaf.extraction_candidate_batch_id
    payloads = (
        (
            f"{batch_id}:evidence",
            "c0.evidence_span",
            L3_EVIDENCE_SPAN_VERSION,
            canonical_sha256(EvidenceSpanV1_1.model_json_schema()),
            [span.model_dump(mode="json") for span in leaf.evidence_spans],
            len(leaf.evidence_spans),
            canonical_sha256(
                sorted(span.evidence_span_id for span in leaf.evidence_spans)
            ),
        ),
        (
            f"{batch_id}:lifecycle:l3",
            "c0.candidate_lifecycle_record",
            "1.0.0",
            canonical_sha256(CandidateLifecycleRecord.model_json_schema()),
            [record.model_dump(mode="json") for record in leaf.lifecycle_records],
            len(leaf.lifecycle_records),
            canonical_sha256(
                sorted(
                    record.lifecycle_record_id
                    for record in leaf.lifecycle_records
                )
            ),
        ),
        (
            f"{batch_id}:classifications",
            "l3.classification_assertion",
            "1.0.0",
            canonical_sha256({
                "contract_kind": "l3.classification_assertion",
                "version": "1.0.0",
            }),
            [item.__dict__ for item in leaf.classifications],
            len(leaf.classifications),
            canonical_sha256(
                sorted(
                    item.classification_version_id
                    for item in leaf.classifications
                )
            ),
        ),
        (
            f"{batch_id}:property-observations",
            "l3.property_observation",
            "1.0.0",
            canonical_sha256({
                "contract_kind": "l3.property_observation",
                "version": "1.0.0",
            }),
            [item.__dict__ for item in leaf.property_observations],
            len(leaf.property_observations),
            canonical_sha256(
                sorted(
                    item.property_observation_id
                    for item in leaf.property_observations
                )
            ),
        ),
    )
    for (
        artifact_id,
        contract_kind,
        contract_version,
        schema_hash,
        payload,
        count,
        id_set_hash,
    ) in payloads:
        entry = _artifact_by_id(
            manifest,
            artifact_id,
            code="L4_INPUT_MANIFEST_INVALID",
        )
        if (
            entry.contract_kind != contract_kind
            or entry.contract_version != contract_version
            or entry.schema_hash != schema_hash
            or entry.content_hash != canonical_sha256(payload)
            or entry.byte_count != _canonical_json_size(payload)
            or entry.row_count != count
            or entry.canonical_id_set_hash != id_set_hash
        ):
            raise L4ProjectionError(
                "L4_INPUT_MANIFEST_INVALID",
                f"L3 artifact {artifact_id} differs from its manifest entry",
            )


def _seal_row(values: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(values)
    row["row_hash"] = canonical_sha256(row)
    return row


def _candidate_indexes(
    source: L3StageResult,
) -> tuple[
    dict[str, CandidateValidationRecord],
    dict[str, Any],
    dict[str, list[Any]],
    dict[str, list[Any]],
]:
    results: dict[str, CandidateValidationRecord] = {}
    classifications: defaultdict[str, list[Any]] = defaultdict(list)
    observations: defaultdict[str, list[Any]] = defaultdict(list)
    lifecycle: dict[str, Any] = {}
    for leaf in source.leaves:
        for result in leaf.candidate_results:
            if result.candidate_id in results:
                raise L4ProjectionError(
                    "L4_ACCOUNTING_INCOMPLETE",
                    f"duplicate L3 candidate result {result.candidate_id}",
                )
            results[result.candidate_id] = result
        for record in leaf.lifecycle_records:
            lifecycle[record.candidate_id] = record
        for item in leaf.classifications:
            classifications[item.candidate_id].append(item)
        for item in leaf.property_observations:
            observations[item.candidate_id].append(item)
    if set(results) != set(lifecycle):
        raise L4ProjectionError(
            "L4_LIFECYCLE_INCOMPLETE",
            "every retained candidate requires exactly one L3 lifecycle record",
        )
    for candidate_id, result in results.items():
        record = lifecycle[candidate_id]
        if (
            record.candidate_version_id != result.candidate_version_id
            or record.candidate_kind != result.candidate_kind
            or record.to_state.value != result.current_state
            or tuple(record.reason_codes) != tuple(result.reason_codes)
            or tuple(record.evidence_span_ids) != tuple(result.evidence_span_ids)
            or record.resolved_source_entity_id
            != result.resolved_source_entity_id
            or record.resolved_target_entity_id
            != result.resolved_target_entity_id
            or tuple(record.source_inheritance_path)
            != tuple(result.source_inheritance_path)
            or tuple(record.target_inheritance_path)
            != tuple(result.target_inheritance_path)
        ):
            raise L4ProjectionError(
                "L4_LIFECYCLE_INCOMPLETE",
                f"candidate {candidate_id} differs from its sealed L3 lifecycle",
            )
    return results, lifecycle, dict(classifications), dict(observations)


def _audit_rows_and_dispositions(
    source: L3StageResult,
    results: Mapping[str, CandidateValidationRecord],
) -> tuple[
    tuple[dict[str, Any], ...],
    tuple[CandidateAccountingDisposition, ...],
    Counter[AssertionState],
    Counter[str],
]:
    rows: list[dict[str, Any]] = []
    dispositions: list[CandidateAccountingDisposition] = []
    states: Counter[AssertionState] = Counter()
    reasons: Counter[str] = Counter()
    seen_inputs: set[str] = set()
    for batch_id in source.inputs.leaf_batch_ids:
        batch = source.inputs.batch_by_id[batch_id]
        for original in batch.candidate_dispositions:
            if original.input_candidate_id in seen_inputs:
                raise L4ProjectionError(
                    "L4_ACCOUNTING_INCOMPLETE",
                    f"input candidate {original.input_candidate_id} is duplicated",
                )
            seen_inputs.add(original.input_candidate_id)
            retained_id = (
                original.retained_candidate_id
                if original.disposition == "retained"
                else original.deduplicated_into_candidate_id
            )
            result = results.get(str(retained_id))
            if result is None:
                raise L4ProjectionError(
                    "L4_ACCOUNTING_INCOMPLETE",
                    f"accounting target {retained_id!r} has no L3 current state",
                )
            if original.disposition == "retained":
                state = AssertionState(result.current_state)
                reason_codes = tuple(result.reason_codes)
                current_state: AssertionState | None = state
                states[state] += 1
            else:
                reason_codes = tuple(original.reason_codes)
                current_state = None
            reasons.update(reason_codes)
            disposition = CandidateAccountingDisposition.model_validate({
                **original.model_dump(mode="python", round_trip=True),
                "current_state": current_state,
                "reason_codes": reason_codes,
            })
            dispositions.append(disposition)
            rows.append(_seal_row({
                "input_candidate_id": disposition.input_candidate_id,
                "disposition": disposition.disposition,
                "retained_candidate_id": disposition.retained_candidate_id,
                "deduplicated_into_candidate_id": (
                    disposition.deduplicated_into_candidate_id
                ),
                "candidate_id": result.candidate_id,
                "candidate_kind": result.candidate_kind,
                "semantic_assertion_id": result.semantic_id,
                "approved_semantic_id": result.approved_semantic_id,
                "lifecycle_state": (
                    current_state.value if current_state is not None else None
                ),
                "reason_codes": list(reason_codes),
                "evidence_span_ids": list(result.evidence_span_ids),
                "resolved_source_entity_id": result.resolved_source_entity_id,
                "resolved_target_entity_id": result.resolved_target_entity_id,
                "source_inheritance_path": list(result.source_inheritance_path),
                "target_inheritance_path": list(result.target_inheritance_path),
                "source_manifest_hash": source.output_manifest.manifest_hash,
            }))

    input_count = sum(
        source.inputs.batch_by_id[batch_id].input_candidate_count
        for batch_id in source.inputs.leaf_batch_ids
    )
    retained_count = sum(
        source.inputs.batch_by_id[batch_id].retained_candidate_count
        for batch_id in source.inputs.leaf_batch_ids
    )
    deduplicated_count = sum(
        source.inputs.batch_by_id[batch_id].deduplicated_input_count
        for batch_id in source.inputs.leaf_batch_ids
    )
    if (
        len(rows) != input_count
        or len(results) != retained_count
        or input_count != retained_count + deduplicated_count
        or sum(states.values()) != retained_count
    ):
        raise L4ProjectionError(
            "L4_ACCOUNTING_INCOMPLETE",
            "input = retained + deduplicated and retained = lifecycle states "
            "did not reconcile",
        )
    return (
        tuple(sorted(rows, key=lambda item: item["input_candidate_id"])),
        tuple(dispositions),
        states,
        reasons,
    )


def _verified_evidence(source: L3StageResult) -> dict[str, Any]:
    spans: dict[str, Any] = {}
    for span in source.evidence_spans:
        if span.evidence_span_id in spans:
            raise L4ProjectionError(
                "L4_EVIDENCE_INVALID",
                f"duplicate evidence span {span.evidence_span_id}",
            )
        if (
            span.purpose != L3_EXTRACTION_PURPOSE
            or span.identity.domain_contract_hash
            != source.inputs.hierarchy.domain_contract_hash
        ):
            raise L4ProjectionError(
                "L4_EVIDENCE_INVALID",
                f"evidence span {span.evidence_span_id} is not verified extraction evidence",
            )
        spans[span.evidence_span_id] = span
    return spans


def _require_evidence(
    result: CandidateValidationRecord,
    evidence: Mapping[str, Any],
) -> tuple[str, ...]:
    ids = tuple(result.evidence_span_ids)
    if not ids or any(evidence_id not in evidence for evidence_id in ids):
        raise L4ProjectionError(
            "L4_ASSERTED_EVIDENCE_INVALID",
            f"asserted candidate {result.candidate_id} lacks verified evidence",
        )
    return ids


def _serving_rows(
    source: L3StageResult,
    results: Mapping[str, CandidateValidationRecord],
    classifications: Mapping[str, list[Any]],
    observations: Mapping[str, list[Any]],
) -> tuple[
    tuple[dict[str, Any], ...],
    tuple[dict[str, Any], ...],
    tuple[dict[str, Any], ...],
    tuple[dict[str, Any], ...],
]:
    hierarchy = source.inputs.hierarchy
    domain_hash = hierarchy.domain_contract_hash
    evidence = _verified_evidence(source)
    asserted = [
        item for item in results.values()
        if item.current_state == AssertionState.ASSERTED.value
    ]
    by_kind_and_id: defaultdict[tuple[str, str], list[CandidateValidationRecord]] = (
        defaultdict(list)
    )
    for result in asserted:
        by_kind_and_id[(result.candidate_kind, result.semantic_id)].append(result)

    entity_rows: list[dict[str, Any]] = []
    type_rows: list[dict[str, Any]] = []
    for (kind, entity_id), group in sorted(by_kind_and_id.items()):
        if kind != "entity":
            continue
        if any(
            not item.identity_recomputed
            or item.identity_witness_kind not in {
                "persisted_business_key",
                "derived_source_identity",
            }
            for item in group
        ):
            raise L4ProjectionError(
                "L4_ASSERTED_IDENTITY_INVALID",
                f"asserted entity {entity_id} lacks a verified identity-root witness",
            )
        candidate_ids = sorted(item.candidate_id for item in group)
        evidence_ids = sorted({
            evidence_id
            for item in group
            for evidence_id in _require_evidence(item, evidence)
        })
        classification_rows = [
            item
            for candidate_id in candidate_ids
            for item in classifications.get(candidate_id, ())
            if item.classification_state == AssertionState.ASSERTED.value
        ]
        type_ids = {
            item.semantic_type_id
            for item in classification_rows
            if item.semantic_type_id is not None
        }
        resolution = resolve_most_specific_classification(type_ids, hierarchy)
        most_specific = resolution.most_specific_type_id
        if (
            most_specific is None
            or most_specific not in hierarchy.entity_by_id
            or resolution.reason_codes
            or any(
                item.hierarchy_hash != hierarchy.hierarchy_hash
                or item.identity_policy_hash != hierarchy.identity_policy_hash
                for item in classification_rows
            )
        ):
            raise L4ProjectionError(
                "L4_ASSERTED_HIERARCHY_INVALID",
                f"asserted entity {entity_id} lacks exact sealed classification authority",
            )
        definition = hierarchy.entity_by_id[most_specific]
        if (
            not definition.evidence_span_ids
            and not definition.competency_question_ids
            and definition.governance_rationale is None
        ):
            raise L4ProjectionError(
                "L4_GOVERNANCE_INVALID",
                f"semantic type {most_specific} lacks approved governance support",
            )
        asserted_types = (
            most_specific,
            *hierarchy.ancestors_by_type.get(most_specific, ()),
        )
        entity_rows.append(_seal_row({
            "entity_id": entity_id,
            "most_specific_type_id": most_specific,
            "asserted_type_ids": list(asserted_types),
            "candidate_ids": candidate_ids,
            "evidence_span_ids": evidence_ids,
            "hierarchy_hash": hierarchy.hierarchy_hash,
            "identity_policy_hash": hierarchy.identity_policy_hash,
            "domain_contract_hash": domain_hash,
            "semantic_contract_hash": domain_hash,
        }))
        for type_id in asserted_types:
            type_rows.append(_seal_row({
                "entity_id": entity_id,
                "semantic_type_id": type_id,
                "most_specific_type_id": most_specific,
                "is_most_specific": type_id == most_specific,
                "hierarchy_depth": hierarchy.depth_by_type[type_id],
                "hierarchy_hash": hierarchy.hierarchy_hash,
                "identity_policy_hash": hierarchy.identity_policy_hash,
            }))

    entity_type_by_id = {
        row["entity_id"]: row["most_specific_type_id"] for row in entity_rows
    }
    relationship_rows: list[dict[str, Any]] = []
    for (kind, relationship_id), group in sorted(by_kind_and_id.items()):
        if kind != "relationship":
            continue
        approved_ids = {item.approved_semantic_id for item in group}
        endpoints = {
            (item.resolved_source_entity_id, item.resolved_target_entity_id)
            for item in group
        }
        if len(approved_ids) != 1 or None in approved_ids or len(endpoints) != 1:
            raise L4ProjectionError(
                "L4_ASSERTED_RELATIONSHIP_INVALID",
                f"asserted relationship {relationship_id} has conflicting authority",
            )
        semantic_relationship_id = str(next(iter(approved_ids)))
        definition = hierarchy.relationship_by_id.get(semantic_relationship_id)
        source_id, target_id = next(iter(endpoints))
        if (
            definition is None
            or definition.publication_policy != "asserted_only"
            or definition.evidence_policy != "exact_span_required"
            or source_id not in entity_type_by_id
            or target_id not in entity_type_by_id
        ):
            raise L4ProjectionError(
                "L4_ASSERTED_RELATIONSHIP_INVALID",
                f"asserted relationship {relationship_id} has unpublished endpoints "
                "or stale policy",
            )
        source_type = entity_type_by_id[source_id]
        target_type = entity_type_by_id[target_id]
        source_outcome = hierarchy.endpoint_outcome(
            semantic_relationship_id,
            source_type,
            role="source",
        )
        target_outcome = hierarchy.endpoint_outcome(
            semantic_relationship_id,
            target_type,
            role="target",
        )
        expected_source_path = source_outcome.inheritance_path
        expected_target_path = target_outcome.inheritance_path
        if (
            not source_outcome.compatible
            or not target_outcome.compatible
            or any(
                tuple(item.source_inheritance_path) != expected_source_path
                or tuple(item.target_inheritance_path) != expected_target_path
                for item in group
            )
        ):
            raise L4ProjectionError(
                "L4_ASSERTED_ENDPOINT_INVALID",
                f"asserted relationship {relationship_id} has stale endpoint proof",
            )
        evidence_ids = sorted({
            evidence_id
            for item in group
            for evidence_id in _require_evidence(item, evidence)
        })
        relationship_rows.append(_seal_row({
            "relationship_id": relationship_id,
            "semantic_relationship_id": semantic_relationship_id,
            "source_entity_id": source_id,
            "target_entity_id": target_id,
            "candidate_ids": sorted(item.candidate_id for item in group),
            "evidence_span_ids": evidence_ids,
            "source_inheritance_path": list(expected_source_path),
            "target_inheritance_path": list(expected_target_path),
            "hierarchy_hash": hierarchy.hierarchy_hash,
            "domain_contract_hash": domain_hash,
            "semantic_contract_hash": domain_hash,
        }))

    property_rows: list[dict[str, Any]] = []
    for (kind, property_assertion_id), group in sorted(by_kind_and_id.items()):
        if kind != "property":
            continue
        observed = [
            item
            for result in group
            for item in observations.get(result.candidate_id, ())
            if item.observation_state == AssertionState.ASSERTED.value
        ]
        property_ids = {item.effective_property_id for item in observed}
        value_types = {item.value_type for item in observed}
        if (
            len(observed) != len(group)
            or len(property_ids) != 1
            or None in property_ids
            or len(value_types) != 1
            or None in value_types
        ):
            raise L4ProjectionError(
                "L4_ASSERTED_PROPERTY_INVALID",
                f"asserted property {property_assertion_id} lacks exact L3 validation",
            )
        evidence_ids = sorted({
            evidence_id
            for item in group
            for evidence_id in _require_evidence(item, evidence)
        })
        property_rows.append(_seal_row({
            "property_assertion_id": property_assertion_id,
            "semantic_property_id": str(next(iter(property_ids))),
            "candidate_ids": sorted(item.candidate_id for item in group),
            "value_type": str(next(iter(value_types))),
            "evidence_span_ids": evidence_ids,
            "domain_contract_hash": domain_hash,
            "semantic_contract_hash": domain_hash,
        }))

    return (
        tuple(entity_rows),
        tuple(sorted(type_rows, key=lambda row: (
            row["entity_id"],
            not row["is_most_specific"],
            row["semantic_type_id"],
        ))),
        tuple(relationship_rows),
        tuple(property_rows),
    )


def _required_member_manifest_values(
    manifest: RequiredMemberManifestV1_1,
) -> dict[str, Any]:
    ordering = manifest.ordering_policy
    return {
        "required_member_manifest_id": manifest.required_member_manifest_id,
        "required_member_set_proposal_id": manifest.required_member_set_proposal_id,
        "required_member_set_proposal_hash": (
            manifest.required_member_set_proposal_hash
        ),
        "scope_canonical_id": manifest.scope_canonical_id,
        "membership_semantic_relationship_id": (
            manifest.membership_semantic_relationship_id
        ),
        "ordering_mode": ordering.mode,
        "ordinal_property_id": ordering.ordinal_property_id,
        "ordinal_value_type": ordering.ordinal_value_type,
        "ordering_direction": ordering.direction,
        "unique_ordinals": ordering.unique_ordinals,
        "contiguous": ordering.contiguous,
        "member_order_encoding": ordering.member_order_encoding,
        "expected_cardinality": manifest.expected_cardinality,
        "minimum_cardinality": manifest.minimum_cardinality,
        "maximum_cardinality": manifest.maximum_cardinality,
        "required_role_ids": list(manifest.required_role_ids),
        "member_count": len(manifest.members),
        "member_set_hash": manifest.member_set_hash,
        "ordered_member_tuple_hash": manifest.ordered_member_tuple_hash,
        "authoritative_collection_hash": manifest.authoritative_collection_hash,
        "domain_contract_hash": manifest.domain_contract_hash,
        "hierarchy_hash": manifest.hierarchy_hash,
        "identity_policy_hash": manifest.identity_policy_hash,
        "completeness_requirement_id": manifest.completeness_requirement_id,
        "completeness_requirement_hash": manifest.completeness_requirement_hash,
        "source_corpus_manifest_id": manifest.source_corpus_manifest_id,
        "source_corpus_manifest_hash": manifest.source_corpus_manifest_hash,
        "source_unit_manifest_id": manifest.source_unit_manifest_id,
        "source_unit_manifest_hash": manifest.source_unit_manifest_hash,
        "extraction_candidate_batch_id": manifest.extraction_candidate_batch_id,
        "extraction_candidate_batch_hash": manifest.extraction_candidate_batch_hash,
        "validator_name": manifest.validator_name,
        "validator_version": manifest.validator_version,
        "manifest_hash": manifest.manifest_hash,
    }


def project_required_member_manifests(
    manifests: Sequence[RequiredMemberManifestV1_1],
) -> tuple[dict[str, Any], ...]:
    """Project one authority row per manifest, including empty collections."""

    return tuple(
        _seal_row(_required_member_manifest_values(manifest))
        for manifest in sorted(
            manifests,
            key=lambda item: item.required_member_manifest_id,
        )
    )


def project_required_members(
    manifests: Sequence[RequiredMemberManifestV1_1],
) -> tuple[dict[str, Any], ...]:
    """Copy manifest fields into physical rows without deriving membership or hashes."""

    rows: list[dict[str, Any]] = []
    for manifest in sorted(
        manifests,
        key=lambda item: item.required_member_manifest_id,
    ):
        ordering = manifest.ordering_policy
        for index, member in enumerate(manifest.members):
            rows.append(_seal_row({
                "required_member_manifest_id": manifest.required_member_manifest_id,
                "required_member_set_proposal_id": (
                    manifest.required_member_set_proposal_id
                ),
                "required_member_set_proposal_hash": (
                    manifest.required_member_set_proposal_hash
                ),
                "manifest_member_index": index,
                "scope_canonical_id": manifest.scope_canonical_id,
                "membership_semantic_relationship_id": (
                    manifest.membership_semantic_relationship_id
                ),
                "member_canonical_id": member.member_canonical_id,
                "member_semantic_type_id": member.member_semantic_type_id,
                "member_role_id": member.member_role_id,
                "member_order": member.member_order,
                "candidate_id": member.candidate_id,
                "supporting_evidence_span_ids": list(
                    member.supporting_evidence_span_ids
                ),
                "member_hash": member.member_hash,
                "ordering_mode": ordering.mode,
                "ordinal_property_id": ordering.ordinal_property_id,
                "ordinal_value_type": ordering.ordinal_value_type,
                "ordering_direction": ordering.direction,
                "unique_ordinals": ordering.unique_ordinals,
                "contiguous": ordering.contiguous,
                "member_order_encoding": ordering.member_order_encoding,
                "expected_cardinality": manifest.expected_cardinality,
                "minimum_cardinality": manifest.minimum_cardinality,
                "maximum_cardinality": manifest.maximum_cardinality,
                "required_role_ids": list(manifest.required_role_ids),
                "member_set_hash": manifest.member_set_hash,
                "ordered_member_tuple_hash": manifest.ordered_member_tuple_hash,
                "authoritative_collection_hash": (
                    manifest.authoritative_collection_hash
                ),
                "domain_contract_hash": manifest.domain_contract_hash,
                "hierarchy_hash": manifest.hierarchy_hash,
                "identity_policy_hash": manifest.identity_policy_hash,
                "completeness_requirement_id": (
                    manifest.completeness_requirement_id
                ),
                "completeness_requirement_hash": (
                    manifest.completeness_requirement_hash
                ),
                "source_corpus_manifest_id": manifest.source_corpus_manifest_id,
                "source_corpus_manifest_hash": manifest.source_corpus_manifest_hash,
                "source_unit_manifest_id": manifest.source_unit_manifest_id,
                "source_unit_manifest_hash": manifest.source_unit_manifest_hash,
                "extraction_candidate_batch_id": (
                    manifest.extraction_candidate_batch_id
                ),
                "extraction_candidate_batch_hash": (
                    manifest.extraction_candidate_batch_hash
                ),
                "manifest_hash": manifest.manifest_hash,
            }))
    projected = tuple(rows)
    validate_required_member_projection(
        manifests,
        project_required_member_manifests(manifests),
        projected,
    )
    return projected


def validate_required_member_projection(
    manifests: Sequence[RequiredMemberManifestV1_1],
    manifest_rows: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
) -> None:
    """Prove physical equality while treating manifest hashes as copied authority."""

    manifest_by_id = {
        manifest.required_member_manifest_id: manifest for manifest in manifests
    }
    if len(manifest_by_id) != len(manifests):
        raise L4ProjectionError(
            "L4_REQUIRED_MEMBER_EQUIVALENCE_FAILED",
            "RequiredMemberManifest IDs are not unique",
        )
    physical_manifests: dict[str, Mapping[str, Any]] = {}
    for row in manifest_rows:
        manifest_id = str(row.get("required_member_manifest_id") or "")
        if manifest_id not in manifest_by_id or manifest_id in physical_manifests:
            raise L4ProjectionError(
                "L4_REQUIRED_MEMBER_EQUIVALENCE_FAILED",
                f"physical projection contains extra or duplicate manifest {manifest_id!r}",
            )
        physical_manifests[manifest_id] = row
    if set(physical_manifests) != set(manifest_by_id):
        raise L4ProjectionError(
            "L4_REQUIRED_MEMBER_EQUIVALENCE_FAILED",
            "physical projection is missing a RequiredMemberManifest authority row",
        )
    for manifest_id, manifest in manifest_by_id.items():
        expected = _seal_row(_required_member_manifest_values(manifest))
        if dict(physical_manifests[manifest_id]) != expected:
            raise L4ProjectionError(
                "L4_REQUIRED_MEMBER_EQUIVALENCE_FAILED",
                f"physical manifest authority differs for {manifest_id}",
            )

    grouped: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        manifest_id = str(row.get("required_member_manifest_id") or "")
        if manifest_id not in manifest_by_id:
            raise L4ProjectionError(
                "L4_REQUIRED_MEMBER_EQUIVALENCE_FAILED",
                f"physical projection contains extra manifest {manifest_id!r}",
            )
        values = {key: value for key, value in row.items() if key != "row_hash"}
        if row.get("row_hash") != canonical_sha256(values):
            raise L4ProjectionError(
                "L4_REQUIRED_MEMBER_EQUIVALENCE_FAILED",
                f"physical projection row hash differs for {manifest_id}",
            )
        grouped[manifest_id].append(row)
    for manifest_id, manifest in manifest_by_id.items():
        physical = sorted(
            grouped.get(manifest_id, ()),
            key=lambda row: int(row["manifest_member_index"]),
        )
        expected_tuples = [
            (
                index,
                member.member_canonical_id,
                member.member_semantic_type_id,
                member.member_role_id,
                member.member_order,
                member.candidate_id,
                tuple(member.supporting_evidence_span_ids),
                member.member_hash,
            )
            for index, member in enumerate(manifest.members)
        ]
        actual_tuples = [
            (
                int(row["manifest_member_index"]),
                row["member_canonical_id"],
                row["member_semantic_type_id"],
                row["member_role_id"],
                row["member_order"],
                row["candidate_id"],
                tuple(row["supporting_evidence_span_ids"]),
                row["member_hash"],
            )
            for row in physical
        ]
        if actual_tuples != expected_tuples:
            raise L4ProjectionError(
                "L4_REQUIRED_MEMBER_EQUIVALENCE_FAILED",
                f"member IDs/types/roles/order differ for {manifest_id}",
            )
        ordering = manifest.ordering_policy
        repeated = {
            "required_member_set_proposal_id": manifest.required_member_set_proposal_id,
            "required_member_set_proposal_hash": (
                manifest.required_member_set_proposal_hash
            ),
            "scope_canonical_id": manifest.scope_canonical_id,
            "membership_semantic_relationship_id": (
                manifest.membership_semantic_relationship_id
            ),
            "ordering_mode": ordering.mode,
            "ordinal_property_id": ordering.ordinal_property_id,
            "ordinal_value_type": ordering.ordinal_value_type,
            "ordering_direction": ordering.direction,
            "unique_ordinals": ordering.unique_ordinals,
            "contiguous": ordering.contiguous,
            "member_order_encoding": ordering.member_order_encoding,
            "expected_cardinality": manifest.expected_cardinality,
            "minimum_cardinality": manifest.minimum_cardinality,
            "maximum_cardinality": manifest.maximum_cardinality,
            "required_role_ids": list(manifest.required_role_ids),
            "member_set_hash": manifest.member_set_hash,
            "ordered_member_tuple_hash": manifest.ordered_member_tuple_hash,
            "authoritative_collection_hash": manifest.authoritative_collection_hash,
            "domain_contract_hash": manifest.domain_contract_hash,
            "hierarchy_hash": manifest.hierarchy_hash,
            "identity_policy_hash": manifest.identity_policy_hash,
            "completeness_requirement_id": manifest.completeness_requirement_id,
            "completeness_requirement_hash": manifest.completeness_requirement_hash,
            "source_corpus_manifest_id": manifest.source_corpus_manifest_id,
            "source_corpus_manifest_hash": manifest.source_corpus_manifest_hash,
            "source_unit_manifest_id": manifest.source_unit_manifest_id,
            "source_unit_manifest_hash": manifest.source_unit_manifest_hash,
            "extraction_candidate_batch_id": manifest.extraction_candidate_batch_id,
            "extraction_candidate_batch_hash": (
                manifest.extraction_candidate_batch_hash
            ),
            "manifest_hash": manifest.manifest_hash,
        }
        if any(
            any(row[field] != value for field, value in repeated.items())
            for row in physical
        ):
            raise L4ProjectionError(
                "L4_REQUIRED_MEMBER_EQUIVALENCE_FAILED",
                f"policy, bounds, collection, or source hashes differ for {manifest_id}",
            )


def _table_hashes(
    rows: Iterable[Mapping[str, Any]],
    *,
    id_field: str,
) -> tuple[str, str]:
    values = list(rows)
    return (
        canonical_sha256(sorted({str(row[id_field]) for row in values})),
        canonical_sha256(values),
    )


def _build_projections(
    source: L3StageResult,
    rows: L4ProjectionRows,
    dispositions: tuple[CandidateAccountingDisposition, ...],
    states: Counter[AssertionState],
    reasons: Counter[str],
    fingerprint: str,
) -> tuple[AuditProjection, SemanticServingProjection]:
    results = source.candidate_results
    serving_tables = {
        "entity": (
            rows.semantic_asserted_entities,
            "entity_id",
        ),
        "relationship": (
            rows.semantic_asserted_relationships,
            "relationship_id",
        ),
        "property": (
            rows.semantic_asserted_properties,
            "property_assertion_id",
        ),
    }
    serving_hashes: dict[str, str] = {}
    serving_row_hashes: dict[str, str] = {}
    serving_ids: dict[str, tuple[str, ...]] = {}
    for kind, (table_rows, id_field) in serving_tables.items():
        id_hash, row_hash = _table_hashes(table_rows, id_field=id_field)
        serving_hashes[kind] = id_hash
        serving_row_hashes[kind] = row_hash
        serving_ids[kind] = tuple(sorted(row[id_field] for row in table_rows))

    audit_identity = _identity(source, contract_kind="c0.audit_projection")
    serving_identity = _identity(
        source,
        contract_kind="c0.semantic_serving_projection",
    )
    audit_ids = {
        kind: tuple(sorted({
            item.semantic_id for item in results if item.candidate_kind == kind
        }))
        for kind in ("entity", "relationship", "property")
    }
    audit_hashes: dict[str, str] = {}
    audit_row_hashes: dict[str, str] = {}
    for kind in ("entity", "relationship", "property"):
        kind_rows = [
            row for row in rows.audit_candidates
            if row["candidate_kind"] == kind
        ]
        audit_hashes[kind] = canonical_sha256(list(audit_ids[kind]))
        # C0 requires equal audit/serving memberships to share this canonical
        # assertion fingerprint. The physical audit table is independently
        # byte-bound and read-back-verified by its L4 ArtifactEntry.
        audit_row_hashes[kind] = (
            serving_row_hashes[kind]
            if audit_ids[kind] == serving_ids[kind]
            else canonical_sha256(kind_rows)
        )

    input_count = len(dispositions)
    retained_count = sum(
        disposition.disposition == "retained" for disposition in dispositions
    )
    deduplicated_count = input_count - retained_count
    audit_values = {
        "identity": audit_identity,
        "projection_id": deterministic_contract_id(
            "audit-projection",
            {"fingerprint": fingerprint},
        ),
        "projection_version": "1.0",
        "source_manifest_hash": source.output_manifest.manifest_hash,
        "input_candidate_count": input_count,
        "retained_candidate_count": retained_count,
        "deduplicated_input_count": deduplicated_count,
        "candidate_dispositions": dispositions,
        "lifecycle_state_counts": {
            state: states.get(state, 0) for state in AssertionState
        },
        "reason_code_counts": dict(sorted(reasons.items())),
        "entity_assertion_ids": audit_ids["entity"],
        "relationship_assertion_ids": audit_ids["relationship"],
        "property_assertion_ids": audit_ids["property"],
        "canonical_id_set_hashes": audit_hashes,
        "canonical_row_hashes": audit_row_hashes,
        "artifact_manifest_id": source.output_manifest.artifact_manifest_id,
    }
    audit = AuditProjection(
        **audit_values,
        projection_hash=canonical_sha256(audit_values),
    )

    evidence_ids = tuple(sorted({
        evidence_id
        for table_rows, _ in serving_tables.values()
        for row in table_rows
        for evidence_id in row["evidence_span_ids"]
    }))
    serving_values = {
        "identity": serving_identity,
        "projection_id": deterministic_contract_id(
            "semantic-serving-projection",
            {"fingerprint": fingerprint},
        ),
        "projection_version": "1.0",
        "audit_projection_id": audit.projection_id,
        "source_manifest_hash": source.output_manifest.manifest_hash,
        "sealed_domain_contract_hash": source.inputs.hierarchy.domain_contract_hash,
        "sealed_semantic_contract_hash": source.inputs.hierarchy.domain_contract_hash,
        "included_states": (AssertionState.ASSERTED,),
        "entity_assertion_ids": serving_ids["entity"],
        "relationship_assertion_ids": serving_ids["relationship"],
        "property_assertion_ids": serving_ids["property"],
        "evidence_span_ids": evidence_ids,
        "canonical_id_set_hashes": serving_hashes,
        "canonical_row_hashes": serving_row_hashes,
        "artifact_manifest_id": source.output_manifest.artifact_manifest_id,
        "sealed_at_utc": source.receipt.completed_at_utc,
    }
    serving = SemanticServingProjection(
        **serving_values,
        projection_hash=canonical_sha256({
            key: value
            for key, value in serving_values.items()
            if key != "sealed_at_utc"
        }),
    )
    validate_asserted_serving_subset(
        audit,
        serving,
        asserted_entity_ids=set(serving_ids["entity"]),
        asserted_relationship_ids=set(serving_ids["relationship"]),
        asserted_property_ids=set(serving_ids["property"]),
    )
    return audit, serving


def _projection_equivalences(
    source: L3StageResult,
    serving: SemanticServingProjection,
    expected_manifest_rows: Sequence[Mapping[str, Any]],
    expected_member_rows: Sequence[Mapping[str, Any]],
    read_back_manifest_rows: Sequence[Mapping[str, Any]],
    read_back_member_rows: Sequence[Mapping[str, Any]],
) -> tuple[ProjectionEquivalence, ...]:
    validate_required_member_projection(
        source.required_member_manifests,
        read_back_manifest_rows,
        read_back_member_rows,
    )
    expected_manifest_by_id = {
        str(row["required_member_manifest_id"]): row
        for row in expected_manifest_rows
    }
    read_back_manifest_by_id = {
        str(row["required_member_manifest_id"]): row
        for row in read_back_manifest_rows
    }
    expected_by_manifest: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    read_back_by_manifest: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in expected_member_rows:
        expected_by_manifest[str(row["required_member_manifest_id"])].append(row)
    for row in read_back_member_rows:
        read_back_by_manifest[str(row["required_member_manifest_id"])].append(row)
    result: list[ProjectionEquivalence] = []
    schema_hash = canonical_sha256(RequiredMemberManifestV1_1.model_json_schema())
    for manifest in sorted(
        source.required_member_manifests,
        key=lambda item: item.required_member_manifest_id,
    ):
        manifest_rows = sorted(
            expected_by_manifest[manifest.required_member_manifest_id],
            key=lambda row: int(row["manifest_member_index"]),
        )
        persisted_rows = sorted(
            read_back_by_manifest[manifest.required_member_manifest_id],
            key=lambda row: int(row["manifest_member_index"]),
        )
        expected_ids = [
            f"{manifest.required_member_manifest_id}|{row['member_canonical_id']}"
            for row in manifest_rows
        ]
        persisted_ids = [
            f"{manifest.required_member_manifest_id}|{row['member_canonical_id']}"
            for row in persisted_rows
        ]
        expected_evidence = ProjectionEvidence(
            count=len(manifest_rows),
            canonical_id_set_hash=canonical_sha256(sorted(expected_ids)),
            row_fingerprint=canonical_sha256({
                "manifest": expected_manifest_by_id[
                    manifest.required_member_manifest_id
                ],
                "members": manifest_rows,
            }),
        )
        persisted_evidence = ProjectionEvidence(
            count=len(persisted_rows),
            canonical_id_set_hash=canonical_sha256(sorted(persisted_ids)),
            row_fingerprint=canonical_sha256({
                "manifest": read_back_manifest_by_id[
                    manifest.required_member_manifest_id
                ],
                "members": persisted_rows,
            }),
        )
        equivalent = persisted_evidence == expected_evidence
        if not equivalent:
            raise L4ProjectionError(
                "L4_REQUIRED_MEMBER_EQUIVALENCE_FAILED",
                f"persisted required-member rows differ for "
                f"{manifest.required_member_manifest_id}",
            )
        authority = PublicationAuthorityReferences(
            required_member_manifest_id=manifest.required_member_manifest_id,
            required_member_manifest_schema_hash=schema_hash,
            required_member_manifest_hash=manifest.manifest_hash,
            authoritative_collection_hash=manifest.authoritative_collection_hash,
            source_artifact_manifest_id=source.output_manifest.artifact_manifest_id,
            source_artifact_manifest_hash=source.output_manifest.manifest_hash,
        )
        crosswalk_id = deterministic_contract_id(
            "l4-local-parquet-crosswalk",
            {
                "required_member_manifest_id": manifest.required_member_manifest_id,
                "schema_hash": canonical_sha256({
                    name: _schema_hash(L4_PROJECTION_TABLE_SCHEMAS[name])
                    for name in (
                        "semantic_required_member_manifests",
                        "semantic_required_members",
                    )
                }),
            },
        )
        crosswalk_hash = canonical_sha256({
            "publication_crosswalk_id": crosswalk_id,
            "source_tables": [
                "semantic_required_member_manifests",
                "semantic_required_members",
            ],
            "field_mapping": {
                name: [
                    field.name for field in L4_PROJECTION_TABLE_SCHEMAS[name]
                ]
                for name in (
                    "semantic_required_member_manifests",
                    "semantic_required_members",
                )
            },
            "authority": authority,
        })
        values = {
            "identity": _identity(
                source,
                contract_kind="c0.projection_equivalence",
            ),
            "projection_equivalence_id": deterministic_contract_id(
                "projection-equivalence",
                {
                    "projection_kind": "parquet",
                    "required_member_manifest_id": (
                        manifest.required_member_manifest_id
                    ),
                    "source_projection_hash": serving.projection_hash,
                },
            ),
            "authority": authority,
            "publication_crosswalk_id": crosswalk_id,
            "publication_crosswalk_hash": crosswalk_hash,
            "source_projection_id": serving.projection_id,
            "source_projection_hash": serving.projection_hash,
            "projection_kind": "parquet",
            "expected": expected_evidence,
            "compiled": expected_evidence,
            "deployed": persisted_evidence,
            "read_back": persisted_evidence,
            "missing_canonical_ids": (),
            "extra_canonical_ids": (),
            "equivalent": equivalent,
        }
        result.append(ProjectionEquivalence(
            **values,
            equivalence_hash=canonical_sha256(values),
        ))
    return tuple(result)


def build_l4_projection(
    source: L3StageResult,
) -> tuple[
    L4ProjectionRows,
    AuditProjection,
    SemanticServingProjection,
    str,
]:
    """Build all L4 logical rows and C0 headers without filesystem or remote I/O."""

    _validate_l3_artifacts(source)
    results, _lifecycle, classifications, observations = _candidate_indexes(source)
    audit_rows, dispositions, states, reasons = _audit_rows_and_dispositions(
        source,
        results,
    )
    entities, types, relationships, properties = _serving_rows(
        source,
        results,
        classifications,
        observations,
    )
    required_member_manifests = project_required_member_manifests(
        source.required_member_manifests
    )
    required_members = project_required_members(source.required_member_manifests)
    domain_contract = source.inputs.domain_contract
    relationship_payload = [
        item.model_dump(mode="json")
        for item in domain_contract.candidate_model.relationship_types
    ]
    graph_policy_payload = {
        "reasoning_policy": domain_contract.reasoning_policy.model_dump(mode="json"),
        "question_plans": [
            item.model_dump(mode="json")
            for item in domain_contract.question_plans
        ],
    }
    publication_authority = (_seal_row({
        "authority_id": "l4-publication-authority",
        "domain_contract_json": canonical_json(domain_contract),
        "domain_contract_hash": source.inputs.hierarchy.domain_contract_hash,
        "hierarchy_hash": domain_contract.hierarchy_closure.hierarchy_hash,
        "identity_policy_hash": domain_contract.identity_policy_hash,
        "relationship_vocabulary_hash": canonical_sha256(relationship_payload),
        "graph_policy_hash": canonical_sha256(graph_policy_payload),
        "graph_max_hops": domain_contract.reasoning_policy.max_hops,
    }),)
    rows = L4ProjectionRows(
        audit_candidates=audit_rows,
        semantic_publication_authority=publication_authority,
        semantic_asserted_entities=entities,
        semantic_entity_type_assertions=types,
        semantic_asserted_relationships=relationships,
        semantic_asserted_properties=properties,
        semantic_required_member_manifests=required_member_manifests,
        semantic_required_members=required_members,
    )
    fingerprint = l4_input_fingerprint(source)
    audit, serving = _build_projections(
        source,
        rows,
        dispositions,
        states,
        reasons,
        fingerprint,
    )
    return rows, audit, serving, fingerprint


def _write_json(path: Path, value: Any) -> bytes:
    payload = (canonical_json(value) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def _write_parquet(path: Path, rows: Sequence[Mapping[str, Any]], schema: pa.Schema) -> bytes:
    table = pa.Table.from_pylist(list(rows), schema=schema)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        table,
        path,
        compression="zstd",
        version="2.6",
        use_dictionary=False,
        write_statistics=True,
    )
    return path.read_bytes()


def _read_back_parquet_tables(
    root: Path,
    expected: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, tuple[dict[str, Any], ...]]:
    read_back: dict[str, tuple[dict[str, Any], ...]] = {}
    for name in _TABLE_ORDER:
        table = pq.read_table(root / f"{name}.parquet")
        if table.schema != L4_PROJECTION_TABLE_SCHEMAS[name]:
            raise L4ProjectionError(
                "L4_PARQUET_EQUIVALENCE_FAILED",
                f"persisted Arrow schema differs for {name}",
            )
        actual_rows = tuple(table.to_pylist())
        expected_rows = tuple(dict(row) for row in expected[name])
        if (
            len(actual_rows) != len(expected_rows)
            or canonical_sha256(actual_rows) != canonical_sha256(expected_rows)
        ):
            raise L4ProjectionError(
                "L4_PARQUET_EQUIVALENCE_FAILED",
                f"persisted row count or fingerprint differs for {name}",
            )
        read_back[name] = actual_rows
    return read_back


def _entry(
    *,
    artifact_id: str,
    contract_kind: str,
    schema_hash: str,
    payload: bytes,
    row_count: int,
    canonical_id_set_hash: str | None,
    media_type: str,
) -> ArtifactEntry:
    return ArtifactEntry(
        artifact_id=artifact_id,
        contract_kind=contract_kind,
        contract_version="1.0.0",
        schema_hash=schema_hash,
        content_hash=hashlib.sha256(payload).hexdigest(),
        canonical_id_set_hash=canonical_id_set_hash,
        row_count=row_count,
        byte_count=len(payload),
        partition_count=1,
        media_type=media_type,
        immutable_locator=None,
        blob_asset_ref_id=None,
    )


def _output_manifest(
    source: L3StageResult,
    *,
    fingerprint: str,
    entries: Sequence[ArtifactEntry],
) -> ArtifactManifest:
    ordered = tuple(sorted(entries, key=lambda item: item.artifact_id))
    values = {
        "identity": _identity(source, contract_kind="c0.artifact_manifest"),
        "artifact_manifest_id": deterministic_contract_id(
            "artifact-manifest",
            {"stage": "L4", "fingerprint": fingerprint},
        ),
        "entries": ordered,
        "total_row_count": sum(entry.row_count or 0 for entry in ordered),
        "total_byte_count": sum(entry.byte_count for entry in ordered),
    }
    return ArtifactManifest(
        **values,
        manifest_hash=canonical_sha256(values),
    )


def _metrics(
    source: L3StageResult,
    *,
    fingerprint: str,
    started: float,
    storage_write_bytes: int,
) -> StageResourceMetrics:
    usage = process_resource_usage()
    values = {
        "identity": _identity(
            source,
            contract_kind="c0.stage_resource_metrics",
        ),
        "resource_metrics_id": deterministic_contract_id(
            "stage-resource-metrics",
            {"stage": "L4", "fingerprint": fingerprint},
        ),
        "stage_id": "L4",
        "stage_name": L4_STAGE_NAME,
        "wall_ms": max(0, int((time.perf_counter() - started) * 1000)),
        "cpu_ms": max(0, int(time.process_time() * 1000)),
        "peak_rss_bytes": usage.peak_rss_bytes,
        "storage_read_bytes": source.output_manifest.total_byte_count,
        "storage_write_bytes": storage_write_bytes,
        "network_request_bytes": 0,
        "network_response_bytes": 0,
        "source_units_read": 0,
        "source_units_written": 0,
        "source_units_skipped": 0,
        "document_intelligence_calls": 0,
        "document_intelligence_pages": 0,
        "foundry_calls": 0,
        "foundry_input_tokens": 0,
        "foundry_output_tokens": 0,
        "embedding_calls": 0,
        "embedding_items": 0,
        "fabric_calls": 0,
        "fabric_rows_read": 0,
        "fabric_rows_written": 0,
        "search_calls": 0,
        "search_documents_read": 0,
        "search_documents_written": 0,
        "retry_count": 0,
        "retry_wait_ms": 0,
        "cache_hits": 0,
        "cache_misses": 0,
        "max_observed_concurrency": 1,
        "budget_snapshot_hash": canonical_sha256({
            "stage": "L4",
            "numeric_thresholds": None,
        }),
        "exceeded_dimensions": (),
    }
    return StageResourceMetrics(
        **values,
        metrics_hash=canonical_sha256(values),
    )


def _receipt(
    source: L3StageResult,
    *,
    fingerprint: str,
    output_manifest: ArtifactManifest,
    metrics: StageResourceMetrics,
    started_at_utc: datetime,
) -> StageReceipt:
    values = {
        "identity": _identity(source, contract_kind="c0.stage_receipt"),
        "stage_receipt_id": deterministic_contract_id(
            "stage-receipt",
            {"stage": "L4", "fingerprint": fingerprint},
        ),
        "stage_id": "L4",
        "stage_name": L4_STAGE_NAME,
        "stage_contract_version": L4_STAGE_CONTRACT_VERSION,
        "status": "succeeded",
        "input_manifest_id": source.output_manifest.artifact_manifest_id,
        "input_manifest_hash": source.output_manifest.manifest_hash,
        "output_manifest_id": output_manifest.artifact_manifest_id,
        "output_manifest_hash": output_manifest.manifest_hash,
        "skip_key": fingerprint,
        "accepted_contract_versions": L4_ACCEPTED_VERSIONS,
        "resource_metrics_id": metrics.resource_metrics_id,
        "resource_metrics_hash": metrics.metrics_hash,
        "attempt_count": 1,
        "remote_operation_refs": (),
        "error_codes": (),
        "started_at_utc": started_at_utc,
        "completed_at_utc": datetime.now(timezone.utc),
    }
    return StageReceipt(
        **values,
        receipt_hash=canonical_sha256({
            key: value
            for key, value in values.items()
            if key not in {"started_at_utc", "completed_at_utc"}
        }),
    )


def _artifact_relative_path(artifact_id: str) -> Path:
    if artifact_id.startswith("l4-table:"):
        return Path(f"{artifact_id.removeprefix('l4-table:')}.parquet")
    try:
        return _PROJECTION_FILES[artifact_id]
    except KeyError as exc:
        raise L4ProjectionError(
            "L4_OUTPUT_MANIFEST_INVALID",
            f"unknown L4 output artifact {artifact_id!r}",
        ) from exc


def _existing_is_intact(
    run_root: Path,
    *,
    source: L3StageResult,
    expected_manifest: ArtifactManifest,
    fingerprint: str,
) -> tuple[ArtifactManifest, StageResourceMetrics, StageReceipt] | None:
    if not run_root.exists():
        return None
    try:
        manifest = ArtifactManifest.model_validate_json(
            (run_root / "output-manifest.json").read_text(encoding="utf-8")
        )
        metrics = StageResourceMetrics.model_validate_json(
            (run_root / "resource-metrics.json").read_text(encoding="utf-8")
        )
        receipt = StageReceipt.model_validate_json(
            (run_root / "stage-receipt.json").read_text(encoding="utf-8")
        )
    except (OSError, ValidationError, ValueError, json.JSONDecodeError):
        return None
    expected_receipt_identity = _identity(
        source,
        contract_kind="c0.stage_receipt",
    )
    expected_metrics_identity = _identity(
        source,
        contract_kind="c0.stage_resource_metrics",
    )
    expected_receipt_id = deterministic_contract_id(
        "stage-receipt",
        {"stage": "L4", "fingerprint": fingerprint},
    )
    expected_metrics_id = deterministic_contract_id(
        "stage-resource-metrics",
        {"stage": "L4", "fingerprint": fingerprint},
    )
    if (
        manifest != expected_manifest
        or receipt.identity != expected_receipt_identity
        or receipt.stage_receipt_id != expected_receipt_id
        or receipt.stage_id != "L4"
        or receipt.stage_name != L4_STAGE_NAME
        or receipt.stage_contract_version != L4_STAGE_CONTRACT_VERSION
        or receipt.status != "succeeded"
        or receipt.skip_key != fingerprint
        or receipt.input_manifest_id != source.output_manifest.artifact_manifest_id
        or receipt.input_manifest_hash != source.output_manifest.manifest_hash
        or receipt.output_manifest_id != manifest.artifact_manifest_id
        or receipt.output_manifest_hash != manifest.manifest_hash
        or dict(receipt.accepted_contract_versions) != L4_ACCEPTED_VERSIONS
        or receipt.resource_metrics_id != expected_metrics_id
        or receipt.attempt_count != 1
        or receipt.remote_operation_refs
        or receipt.error_codes
        or metrics.identity != expected_metrics_identity
        or metrics.resource_metrics_id != expected_metrics_id
        or metrics.stage_id != "L4"
        or metrics.stage_name != L4_STAGE_NAME
        or metrics.storage_read_bytes != source.output_manifest.total_byte_count
        or metrics.storage_write_bytes
        != (
            expected_manifest.total_byte_count
            + len((canonical_json(expected_manifest) + "\n").encode("utf-8"))
        )
        or metrics.network_request_bytes != 0
        or metrics.network_response_bytes != 0
        or metrics.source_units_read != 0
        or metrics.source_units_written != 0
        or metrics.source_units_skipped != 0
        or metrics.document_intelligence_calls != 0
        or metrics.document_intelligence_pages != 0
        or metrics.foundry_calls != 0
        or metrics.foundry_input_tokens != 0
        or metrics.foundry_output_tokens != 0
        or metrics.embedding_calls != 0
        or metrics.embedding_items != 0
        or metrics.fabric_calls != 0
        or metrics.fabric_rows_read != 0
        or metrics.fabric_rows_written != 0
        or metrics.search_calls != 0
        or metrics.search_documents_read != 0
        or metrics.search_documents_written != 0
        or metrics.retry_count != 0
        or metrics.retry_wait_ms != 0
        or metrics.cache_hits != 0
        or metrics.cache_misses != 0
        or metrics.max_observed_concurrency != 1
        or metrics.budget_snapshot_hash
        != canonical_sha256({"stage": "L4", "numeric_thresholds": None})
        or metrics.exceeded_dimensions
    ):
        return None
    try:
        validate_receipt_resources(receipt, metrics)
    except ValueError:
        return None
    expected_files = {
        _artifact_relative_path(entry.artifact_id) for entry in manifest.entries
    } | _STAGE_FILES
    actual_files = {
        path.relative_to(run_root)
        for path in run_root.rglob("*")
        if path.is_file()
    }
    if actual_files != expected_files:
        return None
    for entry in manifest.entries:
        path = run_root / _artifact_relative_path(entry.artifact_id)
        try:
            payload = path.read_bytes()
        except OSError:
            return None
        if (
            len(payload) != entry.byte_count
            or hashlib.sha256(payload).hexdigest() != entry.content_hash
        ):
            return None
    return manifest, metrics, receipt


def _publish_atomic(temp_root: Path, run_root: Path) -> None:
    run_root.parent.mkdir(parents=True, exist_ok=True)
    backup = run_root.with_name(f"{run_root.name}.replaced")
    if backup.exists():
        shutil.rmtree(backup)
    if run_root.exists():
        run_root.replace(backup)
    try:
        temp_root.replace(run_root)
    except OSError:
        if backup.exists() and not run_root.exists():
            backup.replace(run_root)
        raise
    if backup.exists():
        shutil.rmtree(backup)


@contextmanager
def _publication_lock(state_root: Path, fingerprint: str):
    lock_root = state_root / "locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / f"{fingerprint}.lock"
    with lock_path.open("a+b") as handle:
        if fcntl is None:
            raise L4ProjectionError(
                "L4_PUBLICATION_LOCK_UNAVAILABLE",
                "this platform does not provide the required local file lock",
            )
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_l4(
    source: L3StageResult,
    *,
    state_root: Path = L4_STATE_DIR,
) -> L4StageResult:
    """Persist one complete deterministic local L4 projection or reuse it intact."""

    started = time.perf_counter()
    started_at_utc = datetime.now(timezone.utc)
    rows, audit, serving, fingerprint = build_l4_projection(source)
    run_root = l4_run_root(state_root, fingerprint)
    state_root.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix=f".l4-{fingerprint[:12]}-", dir=state_root))
    try:
        entries: list[ArtifactEntry] = []
        for name, table_rows in rows.tables().items():
            schema = L4_PROJECTION_TABLE_SCHEMAS[name]
            payload = _write_parquet(
                temp_root / f"{name}.parquet",
                table_rows,
                schema,
            )
            row_ids = {
                "audit_candidates": lambda row: row["input_candidate_id"],
                "semantic_publication_authority": lambda row: row["authority_id"],
                "semantic_asserted_entities": lambda row: row["entity_id"],
                "semantic_entity_type_assertions": lambda row: (
                    f"{row['entity_id']}|{row['semantic_type_id']}"
                ),
                "semantic_asserted_relationships": lambda row: (
                    row["relationship_id"]
                ),
                "semantic_asserted_properties": lambda row: (
                    row["property_assertion_id"]
                ),
                "semantic_required_member_manifests": lambda row: (
                    row["required_member_manifest_id"]
                ),
                "semantic_required_members": lambda row: (
                    f"{row['required_member_manifest_id']}|"
                    f"{row['member_canonical_id']}"
                ),
            }[name]
            entries.append(_entry(
                artifact_id=f"l4-table:{name}",
                contract_kind=f"l4.{name}",
                schema_hash=_schema_hash(schema),
                payload=payload,
                row_count=len(table_rows),
                canonical_id_set_hash=canonical_sha256(
                    sorted({str(row_ids(row)) for row in table_rows})
                ),
                media_type="application/vnd.apache.parquet",
            ))

        read_back_tables = _read_back_parquet_tables(
            temp_root,
            rows.tables(),
        )
        equivalences = _projection_equivalences(
            source,
            serving,
            rows.semantic_required_member_manifests,
            rows.semantic_required_members,
            read_back_tables["semantic_required_member_manifests"],
            read_back_tables["semantic_required_members"],
        )
        projection_payloads = (
            (
                "l4-audit-projection",
                "c0.audit_projection",
                audit,
                canonical_sha256(AuditProjection.model_json_schema()),
            ),
            (
                "l4-semantic-serving-projection",
                "c0.semantic_serving_projection",
                serving,
                canonical_sha256(SemanticServingProjection.model_json_schema()),
            ),
            (
                "l4-parquet-projection-equivalence",
                "c0.projection_equivalence",
                list(equivalences),
                canonical_sha256({
                    "type": "array",
                    "items": ProjectionEquivalence.model_json_schema(),
                }),
            ),
        )
        for artifact_id, kind, value, schema_hash in projection_payloads:
            payload = _write_json(
                temp_root / _PROJECTION_FILES[artifact_id],
                value,
            )
            entries.append(_entry(
                artifact_id=artifact_id,
                contract_kind=kind,
                schema_hash=schema_hash,
                payload=payload,
                row_count=len(value) if isinstance(value, list) else 1,
                canonical_id_set_hash=None,
                media_type="application/json",
            ))

        output_manifest = _output_manifest(
            source,
            fingerprint=fingerprint,
            entries=entries,
        )
        output_bytes = _write_json(
            temp_root / "output-manifest.json",
            output_manifest,
        )
        with _publication_lock(state_root, fingerprint):
            existing = _existing_is_intact(
                run_root,
                source=source,
                expected_manifest=output_manifest,
                fingerprint=fingerprint,
            )
            if existing is not None:
                manifest, metrics, receipt = existing
                shutil.rmtree(temp_root)
                return L4StageResult(
                    source=source,
                    rows=rows,
                    audit_projection=audit,
                    serving_projection=serving,
                    projection_equivalences=equivalences,
                    output_manifest=manifest,
                    metrics=metrics,
                    receipt=receipt,
                    state_root=state_root,
                    run_root=run_root,
                    reused=True,
                )

            storage_write_bytes = output_manifest.total_byte_count + len(output_bytes)
            metrics = _metrics(
                source,
                fingerprint=fingerprint,
                started=started,
                storage_write_bytes=storage_write_bytes,
            )
            _write_json(temp_root / "resource-metrics.json", metrics)
            receipt = _receipt(
                source,
                fingerprint=fingerprint,
                output_manifest=output_manifest,
                metrics=metrics,
                started_at_utc=started_at_utc,
            )
            validate_receipt_resources(receipt, metrics)
            _write_json(temp_root / "stage-receipt.json", receipt)
            _publish_atomic(temp_root, run_root)
            return L4StageResult(
                source=source,
                rows=rows,
                audit_projection=audit,
                serving_projection=serving,
                projection_equivalences=equivalences,
                output_manifest=output_manifest,
                metrics=metrics,
                receipt=receipt,
                state_root=state_root,
                run_root=run_root,
                reused=False,
            )
    finally:
        if temp_root.exists():
            shutil.rmtree(temp_root)
