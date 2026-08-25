"""Isolated local-only L3 evidence-validation stage; never activated by the CLI.

L3 consumes an intact succeeded or skipped L2 handoff and validates every
proposed candidate locally. It makes no LLM, Foundry, Document Intelligence,
embedding, Search, Fabric, or other remote call, and it emits neither
``AuditProjection`` nor ``SemanticServingProjection`` — those belong to L4.
"""

from __future__ import annotations

import json
import os
import resource
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fabric_kg_builder.contracts.base import (
    canonical_json,
    canonical_sha256,
    deterministic_contract_id,
)
from fabric_kg_builder.contracts.evidence import EvidenceSpanV1_1, SourceUnit
from fabric_kg_builder.contracts.extraction import (
    ExtractionCandidateBatch,
    RequiredMemberManifestIdentityV1_1,
    RequiredMemberManifestV1_1,
    RequiredMemberSetProposalV1_1,
)
from fabric_kg_builder.contracts.identity import CanonicalIdentityEnvelope
from fabric_kg_builder.contracts.lifecycle import (
    AssertionState,
    CandidateLifecycleRecord,
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
from fabric_kg_builder.domain.models import CompletenessRequirementV2, DomainContractV2
from fabric_kg_builder.domain.service import compute_contract_hash
from fabric_kg_builder.sources.corpus import DesignSampleManifest, SourceCorpusManifest

from .schema2_evidence import (
    L3_EVIDENCE_SPAN_VERSION,
    L3_EXTRACTION_PURPOSE,
    L3_EXTRACTION_PURPOSE_VERSION,
    L3_EXTRACTION_VERIFIER_NAME,
    L3_EXTRACTION_VERIFIER_VERSION,
    L3_STAGE_CONTRACT_VERSION,
    L3_STAGE_NAME,
    L3_SUPPORTED_EVIDENCE_UNIT_KINDS,
    L3_VALIDATOR_NAME,
    L3_VALIDATOR_VERSION,
    UNKNOWN_TERM_REASON,
    ClassificationResolution,
    CompiledHierarchy,
    CompletenessOutcome,
    EndpointGroundingRequest,
    L3StageError,
    ProposedOccurrenceAnchor,
    VerifiedMember,
    append_current_transition,
    classify_state,
    compile_hierarchy,
    ground_endpoints,
    evaluate_inherited_constraints,
    is_minted_contract_id,
    property_attribution_reasons,
    relationship_direction_reasons,
    require_extraction_evidence,
    resolve_identity_witness,
    resolve_most_specific_classification,
    sorted_reasons,
    validate_property_observation,
    validate_required_member_proposal,
    verify_and_mint_extraction_span,
    SourceUnitIndex,
)
from .schema2_sources import L2_ACCEPTED_VERSIONS, L2_STAGE_NAME, L2StageError
from .schema2_sources import load_l2_inputs

L3_STATE_DIR = Path(".fkg") / "l3"
#: Mutable per-run stage artifacts are scoped by the exact input fingerprint so
#: a changed validator, source, or candidate set reruns instead of colliding.
L3_RUNS_DIRNAME = "runs"
#: Leaf checkpoints are content-addressed by leaf fingerprint, so a stale leaf is
#: never reachable for reuse while intact leaves survive an unrelated change.
L3_LEAF_CACHE_DIRNAME = "leaves"
L3_ACCEPTED_VERSIONS = {
    "c0.artifact_manifest": "1.0.0",
    "c0.candidate_accounting_disposition": "1.0.0",
    "c0.candidate_lifecycle_record": "1.0.0",
    "c0.evidence_span": "1.1.0",
    "c0.extraction_candidate_batch": "1.0.0",
    "c0.required_member_manifest": "1.1.0",
    "c0.required_member_set_proposal": "1.1.0",
    "c0.source_unit": "1.0.0",
    "c0.stage_receipt": "1.0.0",
    "c0.stage_resource_metrics": "1.0.0",
    "domain.contract": "2.0.0",
    "l1.design_sample_manifest": "1.0.0",
    "l1.source_corpus_manifest": "1.0.0",
    "l2.proposed_candidate_partition": "1.0.0",
    "l2.required_member_set_view": "1.1.0",
}
_L2_STATUSES = frozenset({"succeeded", "skipped"})
#: Contract kinds L2 must never have emitted; L3 alone mints these.
_L3_OWNED_KINDS = frozenset({"c0.evidence_span", "c0.required_member_manifest"})
_L2_ERROR_CODE_MAP = {
    "L2_INPUT_RECEIPT_INVALID": "L3_INPUT_RECEIPT_INVALID",
    "L2_SOURCE_MANIFEST_INVALID": "L3_INPUT_MANIFEST_INVALID",
    "L2_DOMAIN_CONTRACT_INVALID": "L3_INPUT_MANIFEST_INVALID",
    "L2_DOMAIN_HASH_MISMATCH": "L3_DOMAIN_HASH_MISMATCH",
    "L2_HIERARCHY_HASH_MISMATCH": "L3_HIERARCHY_HASH_MISMATCH",
    "L2_HIERARCHY_INVALID": "L3_HIERARCHY_INVALID",
    "L2_IDENTITY_POLICY_HASH_MISMATCH": "L3_IDENTITY_POLICY_HASH_MISMATCH",
    "L2_COMPLETENESS_HASH_MISMATCH": "L3_COMPLETENESS_HASH_MISMATCH",
    "L2_EXTERNAL_REFERENCE_DECISION_HASH_MISMATCH": (
        "L3_EXTERNAL_REFERENCE_DECISION_HASH_MISMATCH"
    ),
    "L2_APPROVED_CONCEPT_MISSING": "L3_APPROVED_CONCEPT_MISSING",
    "L2_CONTRACT_VERSION_UNSUPPORTED": "L3_CONTRACT_VERSION_UNSUPPORTED",
}


# ---------------------------------------------------------------------------
# Persisted L2 proposal views
# ---------------------------------------------------------------------------


class _StrictView(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProposedAnchorView(_StrictView):
    """Persisted untrusted L2 anchor; never an evidence identity."""

    span_start: int = Field(ge=0)
    span_end: int = Field(gt=0)
    quote: str = Field(min_length=1)
    model_authored_evidence_id: str | None = None

    def to_anchor(self) -> ProposedOccurrenceAnchor:
        return ProposedOccurrenceAnchor(
            span_start=self.span_start,
            span_end=self.span_end,
            quote=self.quote,
            model_authored_evidence_id=self.model_authored_evidence_id,
        )


class ProposedCandidateView(_StrictView):
    """Exact strict view of one persisted L2 proposed-candidate record."""

    input_candidate_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    candidate_version_id: str = Field(min_length=1)
    candidate_kind: Literal["entity", "relationship", "property"]
    semantic_id: str = Field(min_length=1)
    approved_semantic_id: str | None
    observed_term: str = Field(min_length=1)
    source_unit_id: str = Field(min_length=1)
    work_unit_id: str = Field(min_length=1)
    local_reference: str | None
    classification_version_id: str | None
    proposed_anchor: ProposedAnchorView | None
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposed_source_entity_id: str | None = None
    proposed_target_entity_id: str | None = None
    proposed_source_semantic_type_id: str | None = None
    proposed_target_semantic_type_id: str | None = None
    proposed_member_role_id: str | None = None
    proposed_member_order: int | None = None


# ---------------------------------------------------------------------------
# L3 result records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateValidationRecord:
    candidate_id: str
    candidate_version_id: str
    candidate_kind: str
    semantic_id: str
    approved_semantic_id: str | None
    source_unit_id: str
    current_state: str
    reason_codes: tuple[str, ...]
    evidence_span_ids: tuple[str, ...]
    resolved_source_entity_id: str | None
    resolved_target_entity_id: str | None
    source_inheritance_path: tuple[str, ...]
    target_inheritance_path: tuple[str, ...]
    identity_recomputed: bool
    identity_witness_kind: str
    ignored_model_evidence_id: str | None


@dataclass(frozen=True)
class ClassificationAssertionRecord:
    entity_id: str
    classification_version_id: str
    candidate_id: str
    semantic_type_id: str | None
    classification_state: str
    ancestor_path: tuple[str, ...]
    hierarchy_depth: int
    evidence_span_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]
    hierarchy_hash: str
    identity_policy_hash: str


@dataclass(frozen=True)
class PropertyObservationRecord:
    property_observation_id: str
    candidate_id: str
    effective_property_id: str | None
    observed_term: str
    value_type: str | None
    observation_state: str
    constraint_outcome: tuple[str, ...]
    evidence_span_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class L3LeafResult:
    extraction_candidate_batch_id: str
    leaf_fingerprint: str
    evidence_spans: tuple[EvidenceSpanV1_1, ...]
    lifecycle_records: tuple[CandidateLifecycleRecord, ...]
    candidate_results: tuple[CandidateValidationRecord, ...]
    classifications: tuple[ClassificationAssertionRecord, ...]
    property_observations: tuple[PropertyObservationRecord, ...]
    reason_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class RequiredMemberOutcomeRecord:
    outcome: CompletenessOutcome
    manifest: RequiredMemberManifestV1_1 | None


@dataclass(frozen=True)
class L3Inputs:
    l2_receipt: StageReceipt
    l2_output_manifest: ArtifactManifest
    l2_input_manifest: ArtifactManifest
    l2_metrics: StageResourceMetrics
    source_unit_manifest: ArtifactManifest
    source_units: SourceUnitIndex
    candidate_batches: tuple[ExtractionCandidateBatch, ...]
    leaf_batch_ids: tuple[str, ...]
    proposed_partitions: Mapping[str, tuple[ProposedCandidateView, ...]]
    lifecycle_partitions: Mapping[str, tuple[CandidateLifecycleRecord, ...]]
    required_member_proposals: tuple[RequiredMemberSetProposalV1_1, ...]
    required_member_views: tuple[Mapping[str, Any], ...]
    corpus_manifest: SourceCorpusManifest
    design_sample_manifest: DesignSampleManifest
    domain_contract: DomainContractV2
    hierarchy: CompiledHierarchy

    @property
    def authority_hashes(self) -> dict[str, str]:
        return {
            "domain_contract_hash": self.hierarchy.domain_contract_hash,
            "hierarchy_hash": self.hierarchy.hierarchy_hash,
            "identity_policy_hash": self.hierarchy.identity_policy_hash,
            "completeness_requirement_hash": (
                self.hierarchy.completeness_requirement_hash
            ),
            "external_reference_decision_hash": (
                self.hierarchy.external_reference_decision_hash
            ),
        }

    @property
    def batch_by_id(self) -> dict[str, ExtractionCandidateBatch]:
        return {
            batch.extraction_candidate_batch_id: batch
            for batch in self.candidate_batches
        }


@dataclass(frozen=True)
class L3StageResult:
    inputs: L3Inputs
    leaves: tuple[L3LeafResult, ...]
    required_member_outcomes: tuple[RequiredMemberOutcomeRecord, ...]
    input_manifest: ArtifactManifest
    output_manifest: ArtifactManifest
    metrics: StageResourceMetrics
    receipt: StageReceipt
    reused_leaf_count: int
    recomputed_leaf_count: int
    state_root: Path
    run_root: Path

    @property
    def evidence_spans(self) -> tuple[EvidenceSpanV1_1, ...]:
        return tuple(
            span for leaf in self.leaves for span in leaf.evidence_spans
        )

    @property
    def candidate_results(self) -> tuple[CandidateValidationRecord, ...]:
        return tuple(
            record for leaf in self.leaves for record in leaf.candidate_results
        )

    @property
    def required_member_manifests(self) -> tuple[RequiredMemberManifestV1_1, ...]:
        return tuple(
            item.manifest
            for item in self.required_member_outcomes
            if item.manifest is not None
        )


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def _safe_id(value: str) -> str:
    return value.replace(":", "-", 1)


def l3_run_root(state_root: Path, fingerprint: str) -> Path:
    """Return the fingerprint-scoped directory that owns one run's artifacts."""

    return state_root / L3_RUNS_DIRNAME / fingerprint


def l3_leaf_checkpoint_path(
    state_root: Path,
    batch_id: str,
    leaf_fingerprint: str,
) -> Path:
    """Return the content-addressed checkpoint path for one validation leaf."""

    return (
        state_root
        / L3_LEAF_CACHE_DIRNAME
        / _safe_id(batch_id)
        / f"{leaf_fingerprint}.json"
    )


def _read_json_model(path: Path, model_type: Any, code: str) -> Any:
    try:
        return model_type.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise L3StageError(code, f"invalid artifact {path.name}: {exc}") from exc


def _read_json(path: Path, code: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise L3StageError(code, f"invalid artifact {path.name}: {exc}") from exc


def _persist_json(path: Path, payload: Any) -> bytes:
    encoded = (canonical_json(payload) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        current = path.read_bytes()
        if current != encoded:
            raise L3StageError(
                "L3_OUTPUT_MANIFEST_INVALID",
                f"immutable L3 artifact collision at {path.name}",
            )
        return encoded
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_bytes(encoded)
    os.replace(temp, path)
    return encoded


def _write_cache(path: Path, payload: Any) -> None:
    """Rewrite one internal leaf checkpoint; caches are derived, not published."""

    encoded = (canonical_json(payload) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_bytes(encoded)
    os.replace(temp, path)


def _manifest_entries_by_kind(
    manifest: ArtifactManifest,
    contract_kind: str,
) -> tuple[ArtifactEntry, ...]:
    return tuple(
        entry for entry in manifest.entries if entry.contract_kind == contract_kind
    )


def _require_version(entry: ArtifactEntry, version: str) -> ArtifactEntry:
    if entry.contract_version != version:
        raise L3StageError(
            "L3_CONTRACT_VERSION_UNSUPPORTED",
            f"{entry.contract_kind}@{entry.contract_version} is unsupported",
        )
    return entry


def _artifact_entry(
    *,
    artifact_id: str,
    contract_kind: str,
    contract_version: str,
    schema_hash: str,
    content_hash: str,
    byte_count: int,
    row_count: int | None,
    canonical_id_set_hash: str | None = None,
    partition_count: int = 1,
) -> ArtifactEntry:
    return ArtifactEntry(
        artifact_id=artifact_id,
        contract_kind=contract_kind,
        contract_version=contract_version,
        schema_hash=schema_hash,
        content_hash=content_hash,
        canonical_id_set_hash=canonical_id_set_hash,
        row_count=row_count,
        byte_count=byte_count,
        partition_count=partition_count,
        media_type="application/json",
        immutable_locator=None,
        blob_asset_ref_id=None,
    )


def _manifest(
    *,
    identity: CanonicalIdentityEnvelope,
    label: str,
    entries: Sequence[ArtifactEntry],
) -> ArtifactManifest:
    ordered = tuple(sorted(entries, key=lambda item: item.artifact_id))
    manifest_id = deterministic_contract_id(
        "artifact-manifest",
        {
            "stage_id": "L3",
            "label": label,
            "entries": [entry.model_dump(mode="json") for entry in ordered],
        },
    )
    values = {
        "identity": identity.model_copy(
            update={"contract_kind": "c0.artifact_manifest"}
        ),
        "artifact_manifest_id": manifest_id,
        "entries": ordered,
        "total_row_count": sum(
            entry.row_count for entry in ordered if entry.row_count is not None
        ),
        "total_byte_count": sum(entry.byte_count for entry in ordered),
    }
    return ArtifactManifest(**values, manifest_hash=canonical_sha256(values))


def _validation_identity(
    base: CanonicalIdentityEnvelope,
    *,
    contract_kind: str,
) -> CanonicalIdentityEnvelope:
    """Strip source/model binding; L3 identity is validator-bound, not model-bound."""

    values = base.model_dump(mode="python")
    values.update(
        {
            "contract_kind": contract_kind,
            "asset_id": None,
            "asset_version_id": None,
            "source_file_id": None,
            "source_unit_id": None,
            "content_hash": None,
            "immutable_locator": None,
            "prompt_version": None,
            "prompt_hash": None,
            "model_version": None,
            "model_hash": None,
            "extractor_name": L3_VALIDATOR_NAME,
            "extractor_version": L3_VALIDATOR_VERSION,
        }
    )
    return CanonicalIdentityEnvelope.model_validate(values)


# ---------------------------------------------------------------------------
# Entry gate
# ---------------------------------------------------------------------------


def assert_l2_did_not_mint_l3_artifacts(manifest: ArtifactManifest) -> None:
    """Reject any handoff where L2 already minted an L3-owned contract."""

    for entry in manifest.entries:
        if entry.contract_kind in _L3_OWNED_KINDS:
            raise L3StageError(
                "L3_INPUT_MANIFEST_INVALID",
                f"L2 must not emit {entry.contract_kind}",
            )


def load_l3_inputs(
    *,
    l2_state_root: Path = Path(".fkg") / "l2",
    l1_state_root: Path = Path(".fkg") / "l1",
    domain_path: Path = Path("domain.yaml"),
) -> L3Inputs:
    """Load an intact L2 handoff and prove complete accounting before validation."""

    receipt = _read_json_model(
        l2_state_root / "stage-receipt.json",
        StageReceipt,
        "L3_INPUT_RECEIPT_INVALID",
    )
    if (
        receipt.stage_id != "L2"
        or receipt.stage_name != L2_STAGE_NAME
        or receipt.status not in _L2_STATUSES
        or receipt.stage_contract_version != "1.0.0"
    ):
        raise L3StageError(
            "L3_INPUT_RECEIPT_INVALID",
            "L3 requires one succeeded or skipped L2 extraction receipt",
        )
    if receipt.error_codes:
        raise L3StageError(
            "L3_INPUT_RECEIPT_INVALID",
            "L3 cannot consume an L2 receipt carrying error codes",
        )
    if dict(receipt.accepted_contract_versions) != dict(L2_ACCEPTED_VERSIONS):
        raise L3StageError(
            "L3_CONTRACT_VERSION_UNSUPPORTED",
            "L2 did not bind the exact accepted contract versions",
        )

    output_manifest = _read_json_model(
        l2_state_root / "output-manifest.json",
        ArtifactManifest,
        "L3_INPUT_MANIFEST_INVALID",
    )
    input_manifest = _read_json_model(
        l2_state_root / "input-manifest.json",
        ArtifactManifest,
        "L3_INPUT_MANIFEST_INVALID",
    )
    if (
        receipt.output_manifest_id != output_manifest.artifact_manifest_id
        or receipt.output_manifest_hash != output_manifest.manifest_hash
    ):
        raise L3StageError(
            "L3_INPUT_MANIFEST_INVALID",
            "L2 output manifest ID/hash differs from its receipt",
        )
    if (
        receipt.input_manifest_id != input_manifest.artifact_manifest_id
        or receipt.input_manifest_hash != input_manifest.manifest_hash
    ):
        raise L3StageError(
            "L3_INPUT_MANIFEST_INVALID",
            "L2 input manifest ID/hash differs from its receipt",
        )
    metrics = _read_json_model(
        l2_state_root / "resource-metrics.json",
        StageResourceMetrics,
        "L3_RESOURCE_BINDING_INVALID",
    )
    try:
        validate_receipt_resources(receipt, metrics)
    except ValueError as exc:
        raise L3StageError("L3_RESOURCE_BINDING_INVALID", str(exc)) from exc
    assert_l2_did_not_mint_l3_artifacts(output_manifest)

    try:
        l1_inputs = load_l2_inputs(
            l1_state_root=l1_state_root,
            domain_path=domain_path,
        )
    except L2StageError as exc:
        raise L3StageError(
            _L2_ERROR_CODE_MAP.get(exc.code, "L3_INPUT_MANIFEST_INVALID"),
            f"L1/L2 authority chain is not intact: {exc}",
        ) from exc

    contract = l1_inputs.domain_contract
    domain_hash = compute_contract_hash(contract)
    if receipt.identity.domain_contract_hash != domain_hash:
        raise L3StageError(
            "L3_DOMAIN_HASH_MISMATCH",
            "L2 receipt domain authority differs from the approved domain",
        )
    hierarchy = compile_hierarchy(contract)

    source_unit_manifest, source_units = _load_source_units(
        l2_state_root,
        output_manifest,
    )
    (
        batches,
        leaf_batch_ids,
        proposed_partitions,
        lifecycle_partitions,
    ) = _load_candidate_partitions(l2_state_root, output_manifest)
    proposals, views = _load_required_member_sets(l2_state_root, output_manifest)

    inputs = L3Inputs(
        l2_receipt=receipt,
        l2_output_manifest=output_manifest,
        l2_input_manifest=input_manifest,
        l2_metrics=metrics,
        source_unit_manifest=source_unit_manifest,
        source_units=source_units,
        candidate_batches=batches,
        leaf_batch_ids=leaf_batch_ids,
        proposed_partitions=proposed_partitions,
        lifecycle_partitions=lifecycle_partitions,
        required_member_proposals=proposals,
        required_member_views=views,
        corpus_manifest=l1_inputs.corpus_manifest,
        design_sample_manifest=l1_inputs.design_sample_manifest,
        domain_contract=contract,
        hierarchy=hierarchy,
    )
    _validate_accounting(inputs)
    return inputs


def _load_source_units(
    l2_state_root: Path,
    output_manifest: ArtifactManifest,
) -> tuple[ArtifactManifest, SourceUnitIndex]:
    manifest = _read_json_model(
        l2_state_root / "source-unit-manifest.json",
        ArtifactManifest,
        "L3_INPUT_MANIFEST_INVALID",
    )
    matches = [
        entry
        for entry in _manifest_entries_by_kind(output_manifest, "c0.artifact_manifest")
        if entry.artifact_id == manifest.artifact_manifest_id
    ]
    if len(matches) != 1:
        raise L3StageError(
            "L3_INPUT_MANIFEST_INVALID",
            "L2 output manifest must reference exactly one SourceUnit manifest",
        )
    entry = _require_version(matches[0], "1.0.0")
    if entry.content_hash != manifest.manifest_hash:
        raise L3StageError(
            "L3_INPUT_MANIFEST_INVALID",
            "SourceUnit manifest hash differs from the L2 output manifest",
        )
    units: list[SourceUnit] = []
    for unit_entry in manifest.entries:
        _require_version(unit_entry, "1.0.0")
        if unit_entry.contract_kind != "c0.source_unit":
            raise L3StageError(
                "L3_INPUT_MANIFEST_INVALID",
                "SourceUnit manifest contains a foreign contract kind",
            )
        path = (
            l2_state_root
            / "source-units"
            / f"{_safe_id(unit_entry.artifact_id)}.json"
        )
        unit = _read_json_model(path, SourceUnit, "L3_SOURCE_UNIT_MISSING")
        if unit.source_unit_id != unit_entry.artifact_id:
            raise L3StageError(
                "L3_SOURCE_UNIT_MISSING",
                f"SourceUnit partition {path.name} has a foreign source_unit_id",
            )
        if canonical_sha256(unit) != unit_entry.content_hash:
            raise L3StageError(
                "L3_SOURCE_UNIT_MISSING",
                f"SourceUnit {unit.source_unit_id} content hash differs",
            )
        units.append(unit)
    return manifest, SourceUnitIndex(units)


def _load_candidate_partitions(
    l2_state_root: Path,
    output_manifest: ArtifactManifest,
) -> tuple[
    tuple[ExtractionCandidateBatch, ...],
    tuple[str, ...],
    dict[str, tuple[ProposedCandidateView, ...]],
    dict[str, tuple[CandidateLifecycleRecord, ...]],
]:
    batches: dict[str, ExtractionCandidateBatch] = {}
    for entry in _manifest_entries_by_kind(
        output_manifest,
        "c0.extraction_candidate_batch",
    ):
        _require_version(entry, "1.0.0")
        batch: ExtractionCandidateBatch | None = None
        for folder in ("candidate-batches", "required-member-candidate-batches"):
            path = l2_state_root / folder / f"{_safe_id(entry.artifact_id)}.json"
            if path.exists():
                batch = _read_json_model(
                    path,
                    ExtractionCandidateBatch,
                    "L3_INPUT_MANIFEST_INVALID",
                )
                break
        if batch is None:
            raise L3StageError(
                "L3_INPUT_MANIFEST_INVALID",
                f"candidate batch partition is missing for {entry.artifact_id}",
            )
        if (
            batch.extraction_candidate_batch_id != entry.artifact_id
            or batch.batch_hash != entry.content_hash
            or batch.candidate_id_set_hash != entry.canonical_id_set_hash
        ):
            raise L3StageError(
                "L3_INPUT_MANIFEST_INVALID",
                f"candidate batch {entry.artifact_id} differs from its manifest entry",
            )
        batches[batch.extraction_candidate_batch_id] = batch

    proposed: dict[str, tuple[ProposedCandidateView, ...]] = {}
    for entry in _manifest_entries_by_kind(
        output_manifest,
        "l2.proposed_candidate_partition",
    ):
        _require_version(entry, "1.0.0")
        batch_id, _, suffix = entry.artifact_id.rpartition(":")
        if suffix != "proposals" or batch_id not in batches:
            raise L3StageError(
                "L3_INPUT_MANIFEST_INVALID",
                f"proposal partition {entry.artifact_id} has no candidate batch",
            )
        raw = _read_json(
            l2_state_root / "proposed-candidates" / f"{_safe_id(batch_id)}.json",
            "L3_INPUT_MANIFEST_INVALID",
        )
        if canonical_sha256(raw) != entry.content_hash:
            raise L3StageError(
                "L3_INPUT_MANIFEST_INVALID",
                f"proposal partition {entry.artifact_id} content hash differs",
            )
        try:
            records = tuple(
                ProposedCandidateView.model_validate(item) for item in raw
            )
        except (TypeError, ValidationError) as exc:
            raise L3StageError(
                "L3_INPUT_MANIFEST_INVALID",
                f"proposal partition {entry.artifact_id} is not strictly valid: {exc}",
            ) from exc
        proposed[batch_id] = tuple(
            sorted(records, key=lambda item: item.candidate_id)
        )

    lifecycle: dict[str, tuple[CandidateLifecycleRecord, ...]] = {}
    for entry in _manifest_entries_by_kind(
        output_manifest,
        "c0.candidate_lifecycle_record",
    ):
        _require_version(entry, "1.0.0")
        batch_id, _, suffix = entry.artifact_id.rpartition(":")
        if suffix != "lifecycle" or batch_id not in batches:
            raise L3StageError(
                "L3_INPUT_MANIFEST_INVALID",
                f"lifecycle partition {entry.artifact_id} has no candidate batch",
            )
        raw = _read_json(
            l2_state_root / "lifecycle" / f"{_safe_id(batch_id)}.json",
            "L3_INPUT_MANIFEST_INVALID",
        )
        if canonical_sha256(raw) != entry.content_hash:
            raise L3StageError(
                "L3_INPUT_MANIFEST_INVALID",
                f"lifecycle partition {entry.artifact_id} content hash differs",
            )
        try:
            records = tuple(
                CandidateLifecycleRecord.model_validate_json(json.dumps(item))
                for item in raw
            )
        except (TypeError, ValidationError) as exc:
            raise L3StageError(
                "L3_LIFECYCLE_CHAIN_INVALID",
                f"lifecycle partition {entry.artifact_id} is invalid: {exc}",
            ) from exc
        lifecycle[batch_id] = records

    leaf_ids = tuple(sorted(proposed))
    if set(leaf_ids) != set(lifecycle):
        raise L3StageError(
            "L3_ACCOUNTING_INCOMPLETE",
            "every L2 leaf requires one proposal and one lifecycle partition",
        )
    return (
        tuple(batches[key] for key in sorted(batches)),
        leaf_ids,
        proposed,
        lifecycle,
    )


def _load_required_member_sets(
    l2_state_root: Path,
    output_manifest: ArtifactManifest,
) -> tuple[
    tuple[RequiredMemberSetProposalV1_1, ...],
    tuple[Mapping[str, Any], ...],
]:
    proposals: list[RequiredMemberSetProposalV1_1] = []
    for entry in _manifest_entries_by_kind(
        output_manifest,
        "c0.required_member_set_proposal",
    ):
        _require_version(entry, "1.1.0")
        proposal = _read_json_model(
            l2_state_root
            / "required-member-proposals"
            / f"{_safe_id(entry.artifact_id)}.json",
            RequiredMemberSetProposalV1_1,
            "L3_INPUT_MANIFEST_INVALID",
        )
        if (
            proposal.required_member_set_proposal_id != entry.artifact_id
            or proposal.proposal_hash != entry.content_hash
        ):
            raise L3StageError(
                "L3_INPUT_MANIFEST_INVALID",
                f"required-member proposal {entry.artifact_id} differs from manifest",
            )
        proposals.append(proposal)

    views: list[Mapping[str, Any]] = []
    for entry in _manifest_entries_by_kind(
        output_manifest,
        "l2.required_member_set_view",
    ):
        _require_version(entry, "1.1.0")
        proposal_id, _, suffix = entry.artifact_id.rpartition(":")
        if suffix != "view":
            raise L3StageError(
                "L3_INPUT_MANIFEST_INVALID",
                f"required-member view {entry.artifact_id} is malformed",
            )
        raw = _read_json(
            l2_state_root
            / "required-member-views"
            / f"{_safe_id(proposal_id)}.json",
            "L3_INPUT_MANIFEST_INVALID",
        )
        if canonical_sha256(raw) != entry.content_hash:
            raise L3StageError(
                "L3_INPUT_MANIFEST_INVALID",
                f"required-member view {entry.artifact_id} content hash differs",
            )
        views.append(raw)
    proposal_ids = {item.required_member_set_proposal_id for item in proposals}
    view_ids = {str(item.get("proposal_hash", "")) for item in views}
    if len(view_ids) != len(views):
        raise L3StageError(
            "L3_ACCOUNTING_INCOMPLETE",
            "required-member views must be unique per proposal",
        )
    if len(proposal_ids) != len(proposals):
        raise L3StageError(
            "L3_ACCOUNTING_INCOMPLETE",
            "required-member proposals must be unique",
        )
    return (
        tuple(
            sorted(proposals, key=lambda item: item.required_member_set_proposal_id)
        ),
        tuple(views),
    )


def _validate_required_member_policy_binding(
    *,
    proposal: RequiredMemberSetProposalV1_1,
    requirement: CompletenessRequirementV2,
) -> None:
    """Re-bind every collection policy field to its sealed Domain authority.

    A proposal is internally self-consistent by construction — its own hashes
    recompute over whatever policy it carries — so self-consistency proves
    nothing about the approved authority. Each policy field is therefore
    compared exactly against the sealed ``CompletenessRequirementV2`` structured
    fact set, cardinality expectation, and required-role authority, and any
    divergence fails closed before a single candidate is validated.
    """

    fact_set = requirement.structured_fact_set
    if fact_set is None:
        raise L3StageError(
            "L3_COMPLETENESS_HASH_MISMATCH",
            f"{requirement.requirement_id} does not govern a structured fact set",
        )
    if (
        proposal.membership_semantic_relationship_id
        != fact_set.membership_relationship_type_id
    ):
        raise L3StageError(
            "L3_COMPLETENESS_HASH_MISMATCH",
            "required-member proposal membership relationship differs from the "
            "approved structured fact set",
        )
    approved_ordering = {
        "mode": fact_set.ordering_policy.mode,
        "ordinal_property_id": fact_set.ordering_policy.ordinal_property_id,
        "ordinal_value_type": fact_set.ordering_policy.ordinal_value_type,
        "direction": fact_set.ordering_policy.direction,
        "unique_ordinals": fact_set.ordering_policy.unique_ordinals,
        "contiguous": fact_set.ordering_policy.contiguous,
        "member_order_encoding": (
            "zero_based_contiguous"
            if fact_set.ordering_policy.mode == "ordered"
            else None
        ),
    }
    proposed_ordering = {
        key: getattr(proposal.ordering_policy, key) for key in approved_ordering
    }
    if proposed_ordering != approved_ordering:
        raise L3StageError(
            "L3_COMPLETENESS_HASH_MISMATCH",
            "required-member proposal ordering policy differs from the approved "
            "structured fact set",
        )
    cardinality = fact_set.cardinality
    approved_bounds = (
        (
            cardinality.expected_count,
            cardinality.minimum_count,
            cardinality.maximum_count,
        )
        if cardinality is not None
        else (None, None, None)
    )
    proposed_bounds = (
        proposal.expected_cardinality,
        proposal.minimum_cardinality,
        proposal.maximum_cardinality,
    )
    if proposed_bounds != approved_bounds:
        raise L3StageError(
            "L3_COMPLETENESS_HASH_MISMATCH",
            "required-member proposal cardinality bounds differ from the approved "
            "cardinality expectation",
        )
    if tuple(sorted(proposal.required_role_ids)) != tuple(
        sorted(fact_set.member_role_ids)
    ):
        raise L3StageError(
            "L3_COMPLETENESS_HASH_MISMATCH",
            "required-member proposal required roles differ from the approved "
            "member-role authority",
        )


def _validate_accounting(inputs: L3Inputs) -> None:
    """Prove complete L2 accounting before any candidate validation begins."""

    batch_by_id = inputs.batch_by_id
    all_candidate_ids: set[str] = set()
    for batch_id in inputs.leaf_batch_ids:
        batch = batch_by_id[batch_id]
        records = inputs.proposed_partitions[batch_id]
        lifecycle_records = inputs.lifecycle_partitions[batch_id]
        try:
            batch.validate_core_references(
                lifecycle_records=lifecycle_records,
                evidence_spans=(),
            )
        except ValueError as exc:
            raise L3StageError(
                "L3_ACCOUNTING_INCOMPLETE",
                f"batch {batch_id} does not reconcile with C0 records: {exc}",
            ) from exc
        batch_candidate_ids = {item.candidate_id for item in batch.candidates}
        record_ids = {item.candidate_id for item in records}
        if record_ids != batch_candidate_ids:
            raise L3StageError(
                "L3_ACCOUNTING_INCOMPLETE",
                f"batch {batch_id} retained candidates differ from its proposals",
            )
        if len(record_ids) != len(records):
            raise L3StageError(
                "L3_ACCOUNTING_INCOMPLETE",
                f"batch {batch_id} contains duplicate proposal records",
            )
        lifecycle_by_candidate: dict[str, CandidateLifecycleRecord] = {}
        for record in lifecycle_records:
            if (
                record.sequence != 0
                or record.from_state is not None
                or record.to_state is not AssertionState.PROPOSED
            ):
                raise L3StageError(
                    "L3_LIFECYCLE_CHAIN_INVALID",
                    "L2 must hand off sequence-zero proposed events only",
                )
            if record.candidate_id in lifecycle_by_candidate:
                raise L3StageError(
                    "L3_LIFECYCLE_CHAIN_INVALID",
                    f"duplicate initial lifecycle event for {record.candidate_id}",
                )
            lifecycle_by_candidate[record.candidate_id] = record
        if set(lifecycle_by_candidate) != batch_candidate_ids:
            raise L3StageError(
                "L3_ACCOUNTING_INCOMPLETE",
                f"batch {batch_id} lacks one initial lifecycle event per candidate",
            )
        retained = {
            item.retained_candidate_id
            for item in batch.candidate_dispositions
            if item.disposition == "retained"
        }
        dedup_targets = {
            item.deduplicated_into_candidate_id
            for item in batch.candidate_dispositions
            if item.disposition == "deduplicated"
        }
        if retained != batch_candidate_ids or not dedup_targets <= retained:
            raise L3StageError(
                "L3_ACCOUNTING_INCOMPLETE",
                f"batch {batch_id} accounting does not reconcile",
            )
        for record in records:
            inputs.source_units.require(record.source_unit_id)
        all_candidate_ids |= batch_candidate_ids

    for batch_id, batch in sorted(batch_by_id.items()):
        if batch_id in inputs.leaf_batch_ids:
            continue
        missing = {
            item.candidate_id for item in batch.candidates
        } - all_candidate_ids
        if missing:
            raise L3StageError(
                "L3_ACCOUNTING_INCOMPLETE",
                f"merged batch {batch_id} references unaccounted candidates",
            )

    for proposal in inputs.required_member_proposals:
        batch = batch_by_id.get(proposal.extraction_candidate_batch_id)
        if batch is None or batch.batch_hash != proposal.extraction_candidate_batch_hash:
            raise L3StageError(
                "L3_ACCOUNTING_INCOMPLETE",
                "required-member proposal batch binding does not resolve",
            )
        try:
            proposal.validate_against_batch(batch)
        except ValueError as exc:
            raise L3StageError(
                "L3_ACCOUNTING_INCOMPLETE",
                f"required-member proposal does not match its batch: {exc}",
            ) from exc
        requirement = inputs.hierarchy.requirement_by_id.get(
            proposal.completeness_requirement_id
        )
        if requirement is None or requirement.structured_fact_set is None:
            raise L3StageError(
                "L3_COMPLETENESS_HASH_MISMATCH",
                "required-member proposal cites an unapproved completeness requirement",
            )
        if proposal.completeness_requirement_hash != canonical_sha256(
            requirement.model_dump(mode="json")
        ):
            raise L3StageError(
                "L3_COMPLETENESS_HASH_MISMATCH",
                "required-member proposal completeness hash does not recompute",
            )
        _validate_required_member_policy_binding(
            proposal=proposal,
            requirement=requirement,
        )
        for reference in (
            ("domain_contract_hash", inputs.hierarchy.domain_contract_hash),
            ("hierarchy_hash", inputs.hierarchy.hierarchy_hash),
            ("identity_policy_hash", inputs.hierarchy.identity_policy_hash),
        ):
            if getattr(proposal, reference[0]) != reference[1]:
                raise L3StageError(
                    "L3_DOMAIN_HASH_MISMATCH",
                    f"required-member proposal {reference[0]} drifted",
                )
        unresolved = {
            member.candidate_id for member in proposal.members
        } - all_candidate_ids
        if unresolved:
            raise L3StageError(
                "L3_ACCOUNTING_INCOMPLETE",
                "required-member proposal cites candidates outside every leaf",
            )


# ---------------------------------------------------------------------------
# Cross-leaf shared context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SharedContext:
    classification_by_entity: Mapping[str, ClassificationResolution]
    entity_type_by_id: Mapping[str, str | None]
    entity_anchor_by_key: Mapping[tuple[str, str], ProposedOccurrenceAnchor]
    local_reference_index: Mapping[tuple[str, str], tuple[str, ...]]
    local_keys_by_entity: Mapping[str, tuple[tuple[str, str], ...]]
    identity_conflict_entity_ids: frozenset[str]
    relationship_identity_conflicts: frozenset[str]
    entity_ids: frozenset[str]

    def context_hash(
        self,
        entity_ids: Iterable[str],
        relationship_ids: Iterable[str],
        source_unit_ids: Iterable[str],
    ) -> str:
        scoped_entities = sorted(set(entity_ids))
        scoped_relationships = sorted(set(relationship_ids))
        scoped_source_units = set(source_unit_ids)
        return canonical_sha256(
            {
                "classifications": [
                    [
                        entity_id,
                        list(
                            self.classification_by_entity[
                                entity_id
                            ].candidate_type_ids
                        )
                        if entity_id in self.classification_by_entity
                        else [],
                        self.entity_type_by_id.get(entity_id),
                        entity_id in self.identity_conflict_entity_ids,
                        entity_id in self.entity_ids,
                    ]
                    for entity_id in scoped_entities
                ],
                "relationship_conflicts": [
                    relationship_id
                    for relationship_id in scoped_relationships
                    if relationship_id in self.relationship_identity_conflicts
                ],
                "entity_anchors": [
                    [
                        entity_id,
                        source_unit_id,
                        anchor.span_start,
                        anchor.span_end,
                        anchor.quote,
                        anchor.model_authored_evidence_id,
                    ]
                    for (
                        entity_id,
                        source_unit_id,
                    ), anchor in sorted(self.entity_anchor_by_key.items())
                    if entity_id in scoped_entities
                    and source_unit_id in scoped_source_units
                ],
                "local_reference_index": [
                    [source_unit_id, local_reference, list(entity_ids)]
                    for (
                        source_unit_id,
                        local_reference,
                    ), entity_ids in sorted(self.local_reference_index.items())
                    if source_unit_id in scoped_source_units
                ],
            }
        )


def _build_shared_context(
    inputs: L3Inputs,
) -> _SharedContext:
    hierarchy = inputs.hierarchy
    types_by_entity: defaultdict[str, set[str]] = defaultdict(set)
    roots_by_entity: defaultdict[str, set[str]] = defaultdict(set)
    anchors: dict[tuple[str, str], ProposedOccurrenceAnchor] = {}
    local_index: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    triples_by_relationship: defaultdict[str, set[tuple[str, str, str]]] = defaultdict(
        set
    )
    entity_ids: set[str] = set()

    for batch_id in inputs.leaf_batch_ids:
        for record in inputs.proposed_partitions[batch_id]:
            if record.candidate_kind == "entity":
                entity_ids.add(record.semantic_id)
                if record.approved_semantic_id is not None:
                    types_by_entity[record.semantic_id].add(record.approved_semantic_id)
                    definition = hierarchy.entity_by_id.get(
                        record.approved_semantic_id
                    )
                    if definition is not None:
                        roots_by_entity[record.semantic_id].add(
                            definition.identity_root_type_id
                        )
                if record.proposed_anchor is not None:
                    key = (record.semantic_id, record.source_unit_id)
                    anchors.setdefault(key, record.proposed_anchor.to_anchor())
                if record.local_reference:
                    local_index[
                        (record.source_unit_id, record.local_reference.casefold())
                    ].add(record.semantic_id)
            elif record.candidate_kind == "relationship":
                relationship = (
                    hierarchy.relationship_by_id.get(record.approved_semantic_id)
                    if record.approved_semantic_id is not None
                    else None
                )
                predicate = (
                    relationship.predicate_id
                    if relationship is not None
                    else f"observed:{record.observed_term.casefold()}"
                )
                triples_by_relationship[record.semantic_id].add(
                    (
                        predicate,
                        record.proposed_source_entity_id or "",
                        record.proposed_target_entity_id or "",
                    )
                )

    classification_by_entity = {
        entity_id: resolve_most_specific_classification(type_ids, hierarchy)
        for entity_id, type_ids in sorted(types_by_entity.items())
    }
    local_keys_by_entity: defaultdict[str, set[tuple[str, str]]] = defaultdict(set)
    for key, values in local_index.items():
        for entity_id in values:
            local_keys_by_entity[entity_id].add(key)
    return _SharedContext(
        classification_by_entity=classification_by_entity,
        entity_type_by_id={
            entity_id: resolution.most_specific_type_id
            for entity_id, resolution in classification_by_entity.items()
        },
        entity_anchor_by_key=anchors,
        local_reference_index={
            key: tuple(sorted(values)) for key, values in local_index.items()
        },
        local_keys_by_entity={
            entity_id: tuple(sorted(keys))
            for entity_id, keys in local_keys_by_entity.items()
        },
        identity_conflict_entity_ids=frozenset(
            entity_id for entity_id, roots in roots_by_entity.items() if len(roots) > 1
        ),
        relationship_identity_conflicts=frozenset(
            relationship_id
            for relationship_id, triples in triples_by_relationship.items()
            if len(triples) > 1
        ),
        entity_ids=frozenset(entity_ids),
    )


# ---------------------------------------------------------------------------
# Leaf validation
# ---------------------------------------------------------------------------


def _identity_witness(
    record: ProposedCandidateView,
    *,
    hierarchy: CompiledHierarchy,
    project_id: str,
) -> tuple[bool, str, tuple[str, ...]]:
    """Recompute a stable entity ID, or fail closed when it is not provable."""

    outcome = resolve_identity_witness(
        semantic_id=record.semantic_id,
        approved_semantic_id=record.approved_semantic_id,
        source_unit_id=record.source_unit_id,
        local_reference=record.local_reference,
        hierarchy=hierarchy,
        project_id=project_id,
    )
    return outcome.recomputed, outcome.witness_kind, outcome.reason_codes


def _resolve_endpoint(
    *,
    entity_id: str | None,
    source_unit_id: str,
    shared: _SharedContext,
) -> tuple[str | None, str | None, tuple[str, ...]]:
    """Resolve one local endpoint reference to exactly one retained entity."""

    if entity_id is None or entity_id not in shared.entity_ids:
        return None, None, ("ENDPOINT_UNRESOLVED",)
    for key in shared.local_keys_by_entity.get(entity_id, ()):
        # Case-insensitive local references resolve only when unambiguous.
        if key[0] == source_unit_id and len(shared.local_reference_index[key]) != 1:
            return None, None, ("ENDPOINT_UNRESOLVED",)
    classification = shared.entity_type_by_id.get(entity_id)
    if classification is None:
        return entity_id, None, ("ENDPOINT_UNRESOLVED",)
    return entity_id, classification, ()


def _validate_leaf(
    *,
    batch: ExtractionCandidateBatch,
    records: Sequence[ProposedCandidateView],
    lifecycle_by_candidate: Mapping[str, CandidateLifecycleRecord],
    inputs: L3Inputs,
    shared: _SharedContext,
    lifecycle_identity: CanonicalIdentityEnvelope,
    occurred_at_utc: datetime,
    leaf_fingerprint: str,
) -> L3LeafResult:
    hierarchy = inputs.hierarchy
    index = inputs.source_units
    project_id = batch.identity.project_id
    spans: dict[str, EvidenceSpanV1_1] = {}
    lifecycle_records: list[CandidateLifecycleRecord] = []
    results: list[CandidateValidationRecord] = []
    classifications: list[ClassificationAssertionRecord] = []
    observations: list[PropertyObservationRecord] = []
    reason_counter: Counter[str] = Counter()

    for record in sorted(records, key=lambda item: item.candidate_id):
        reasons: set[str] = set()
        source_unit = index.require(record.source_unit_id)
        if source_unit.unit_kind not in L3_SUPPORTED_EVIDENCE_UNIT_KINDS:
            reasons.add("EVIDENCE_MODALITY_UNSUPPORTED")
        anchor = (
            record.proposed_anchor.to_anchor()
            if record.proposed_anchor is not None
            else None
        )
        outcome = _mint_evidence(
            source_unit=source_unit,
            anchor=anchor,
            occurred_at_utc=occurred_at_utc,
        )
        reasons.update(outcome.reason_codes)
        evidence_ids: tuple[str, ...] = ()
        if outcome.span is not None:
            existing = spans.get(outcome.span.evidence_span_id)
            if existing is not None and existing != outcome.span:
                raise L3StageError(
                    "L3_EVIDENCE_ID_COLLISION",
                    f"evidence ID collision {outcome.span.evidence_span_id}",
                )
            spans[outcome.span.evidence_span_id] = outcome.span
            evidence_ids = (outcome.span.evidence_span_id,)

        if record.approved_semantic_id is None:
            reasons.add(UNKNOWN_TERM_REASON[record.candidate_kind])
            reasons.add("DOMAIN_REREVIEW_REQUESTED")

        resolved_source: str | None = None
        resolved_target: str | None = None
        source_path: tuple[str, ...] = ()
        target_path: tuple[str, ...] = ()
        identity_recomputed = False
        witness_kind = "not_applicable"
        property_reasons: tuple[str, ...] = ()

        if record.candidate_kind == "entity":
            identity_recomputed, witness_kind, identity_reasons = _identity_witness(
                record,
                hierarchy=hierarchy,
                project_id=project_id,
            )
            reasons.update(identity_reasons)
            if record.semantic_id in shared.identity_conflict_entity_ids:
                reasons.add("IDENTITY_POLICY_VIOLATION")
            resolution = shared.classification_by_entity.get(record.semantic_id)
            reasons.update(_entity_reasons(record, hierarchy, resolution))
        elif record.candidate_kind == "relationship":
            (
                relationship_reasons,
                resolved_source,
                resolved_target,
                source_path,
                target_path,
            ) = _relationship_reasons(
                record=record,
                hierarchy=hierarchy,
                shared=shared,
                source_unit=source_unit,
                anchor=anchor,
                evidence_verified=outcome.span is not None,
            )
            reasons.update(relationship_reasons)
            if not is_minted_contract_id(record.semantic_id, "relationship"):
                reasons.add("SEMANTIC_ID_MISMATCH")
            if record.semantic_id in shared.relationship_identity_conflicts:
                reasons.add("SEMANTIC_ID_MISMATCH")
            # The frozen L2 carrier never persists the model-proposed direction
            # token, so a relationship is never asserted as direction-proven.
            reasons.update(
                relationship_direction_reasons(
                    proposed_direction=None,
                    direction_persisted=False,
                    blocking_reason_codes=reasons,
                )
            )
        else:
            property_reasons = validate_property_observation(
                hierarchy=hierarchy,
                owner_type_id=None,
                property_id=record.approved_semantic_id,
                value_available=False,
            )
            reasons.update(property_reasons)
            # Owner attribution and the observed value are likewise not
            # persisted, so inheritance and value conformance stay unproven.
            reasons.update(
                property_attribution_reasons(
                    owner_attribution_persisted=False,
                    value_persisted=False,
                    blocking_reason_codes=reasons,
                )
            )

        reason_codes = sorted_reasons(reasons)
        state = classify_state(reason_codes)
        if state is AssertionState.ASSERTED and not evidence_ids:
            raise L3StageError(
                "L3_VALIDATION_RESULT_INCOMPLETE",
                f"asserted candidate {record.candidate_id} has no verified evidence",
            )
        prior = lifecycle_by_candidate.get(record.candidate_id)
        if prior is None:
            raise L3StageError(
                "L3_LIFECYCLE_CHAIN_INVALID",
                f"candidate {record.candidate_id} has no initial lifecycle event",
            )
        lifecycle_records.append(
            append_current_transition(
                prior,
                identity=lifecycle_identity,
                to_state=state,
                reason_codes=reason_codes,
                evidence_span_ids=evidence_ids,
                resolved_source_entity_id=resolved_source,
                resolved_target_entity_id=resolved_target,
                source_inheritance_path=source_path,
                target_inheritance_path=target_path,
                occurred_at_utc=occurred_at_utc,
                validator_name=L3_VALIDATOR_NAME,
                validator_version=L3_VALIDATOR_VERSION,
            )
        )
        for reason in reason_codes:
            reason_counter[reason] += 1
        results.append(
            CandidateValidationRecord(
                candidate_id=record.candidate_id,
                candidate_version_id=record.candidate_version_id,
                candidate_kind=record.candidate_kind,
                semantic_id=record.semantic_id,
                approved_semantic_id=record.approved_semantic_id,
                source_unit_id=record.source_unit_id,
                current_state=state.value,
                reason_codes=reason_codes,
                evidence_span_ids=evidence_ids,
                resolved_source_entity_id=resolved_source,
                resolved_target_entity_id=resolved_target,
                source_inheritance_path=source_path,
                target_inheritance_path=target_path,
                identity_recomputed=identity_recomputed,
                identity_witness_kind=witness_kind,
                ignored_model_evidence_id=outcome.ignored_model_evidence_id,
            )
        )
        if record.candidate_kind == "entity":
            classifications.append(
                _classification_record(
                    record=record,
                    hierarchy=hierarchy,
                    state=state,
                    reason_codes=reason_codes,
                    evidence_ids=evidence_ids,
                    shared=shared,
                )
            )
        elif record.candidate_kind == "property":
            declaration = (
                hierarchy.property_by_id.get(record.approved_semantic_id)
                if record.approved_semantic_id is not None
                else None
            )
            observations.append(
                PropertyObservationRecord(
                    property_observation_id=record.semantic_id,
                    candidate_id=record.candidate_id,
                    effective_property_id=record.approved_semantic_id,
                    observed_term=record.observed_term,
                    value_type=declaration.value_type if declaration else None,
                    observation_state=state.value,
                    constraint_outcome=property_reasons,
                    evidence_span_ids=evidence_ids,
                    reason_codes=reason_codes,
                )
            )

    return L3LeafResult(
        extraction_candidate_batch_id=batch.extraction_candidate_batch_id,
        leaf_fingerprint=leaf_fingerprint,
        evidence_spans=tuple(spans[key] for key in sorted(spans)),
        lifecycle_records=tuple(
            sorted(lifecycle_records, key=lambda item: item.lifecycle_record_id)
        ),
        candidate_results=tuple(results),
        classifications=tuple(classifications),
        property_observations=tuple(observations),
        reason_counts=tuple(sorted(reason_counter.items())),
    )


def _mint_evidence(
    *,
    source_unit: SourceUnit,
    anchor: ProposedOccurrenceAnchor | None,
    occurred_at_utc: datetime,
):
    return verify_and_mint_extraction_span(
        source_unit=source_unit,
        anchor=anchor,
        verified_at_utc=occurred_at_utc,
        expected_source_text_hash=source_unit.text_content_hash,
    )


def _entity_reasons(
    record: ProposedCandidateView,
    hierarchy: CompiledHierarchy,
    resolution: ClassificationResolution | None,
) -> tuple[str, ...]:
    reasons: set[str] = set()
    if record.approved_semantic_id is None:
        return ()
    if record.approved_semantic_id not in hierarchy.entity_by_id:
        return ("HIERARCHY_CONCEPT_MISSING",)
    reasons.update(
        evaluate_inherited_constraints(
            record.approved_semantic_id,
            hierarchy,
            observed_property_ids=None,
        )
    )
    if resolution is not None:
        reasons.update(resolution.reason_codes)
        if (
            not resolution.ambiguous
            and resolution.most_specific_type_id is not None
            and resolution.most_specific_type_id != record.approved_semantic_id
        ):
            # Superseded classification versions stay addressable but unresolved.
            reasons.add("AMBIGUOUS_SIBLING_CLASSIFICATION")
    return sorted_reasons(reasons)


def _relationship_reasons(
    *,
    record: ProposedCandidateView,
    hierarchy: CompiledHierarchy,
    shared: _SharedContext,
    source_unit: SourceUnit,
    anchor: ProposedOccurrenceAnchor | None,
    evidence_verified: bool,
) -> tuple[tuple[str, ...], str | None, str | None, tuple[str, ...], tuple[str, ...]]:
    reasons: set[str] = set()
    if record.approved_semantic_id is None:
        return sorted_reasons(reasons), None, None, (), ()
    relationship = hierarchy.relationship_by_id.get(record.approved_semantic_id)
    if relationship is None:
        return ("HIERARCHY_CONCEPT_MISSING",), None, None, (), ()

    source_id, source_type, source_reasons = _resolve_endpoint(
        entity_id=record.proposed_source_entity_id,
        source_unit_id=record.source_unit_id,
        shared=shared,
    )
    target_id, target_type, target_reasons = _resolve_endpoint(
        entity_id=record.proposed_target_entity_id,
        source_unit_id=record.source_unit_id,
        shared=shared,
    )
    reasons.update(source_reasons)
    reasons.update(target_reasons)
    if source_id is not None and source_id == target_id:
        reasons.add("ENDPOINT_UNRESOLVED")
    source_path: tuple[str, ...] = ()
    target_path: tuple[str, ...] = ()
    if source_type is not None and target_type is not None:
        source_outcome = hierarchy.endpoint_outcome(
            relationship.relationship_type_id,
            source_type,
            role="source",
        )
        target_outcome = hierarchy.endpoint_outcome(
            relationship.relationship_type_id,
            target_type,
            role="target",
        )
        if not source_outcome.compatible or not target_outcome.compatible:
            swapped_source = hierarchy.endpoint_outcome(
                relationship.relationship_type_id,
                target_type,
                role="source",
            )
            swapped_target = hierarchy.endpoint_outcome(
                relationship.relationship_type_id,
                source_type,
                role="target",
            )
            if swapped_source.compatible and swapped_target.compatible:
                # A reversed proposal is rejected, never silently swapped.
                reasons.add("DIRECTION_MISMATCH")
            else:
                reasons.update(source_outcome.reason_codes)
                reasons.update(target_outcome.reason_codes)
        else:
            source_path = source_outcome.inheritance_path
            target_path = target_outcome.inheritance_path

    if evidence_verified and anchor is not None and source_id and target_id:
        grounding = ground_endpoints(
            source_text=source_unit.text,
            span_start=anchor.span_start,
            span_end=anchor.span_end,
            requests=(
                EndpointGroundingRequest(
                    endpoint_id=source_id,
                    role="source",
                    terms=_endpoint_terms(shared, source_id, record.source_unit_id),
                    anchor=_endpoint_anchor(
                        shared,
                        source_id,
                        record.source_unit_id,
                        anchor,
                    ),
                ),
                EndpointGroundingRequest(
                    endpoint_id=target_id,
                    role="target",
                    terms=_endpoint_terms(shared, target_id, record.source_unit_id),
                    anchor=_endpoint_anchor(
                        shared,
                        target_id,
                        record.source_unit_id,
                        anchor,
                    ),
                ),
            ),
        )
        reasons.update(grounding.reason_codes)
    return sorted_reasons(reasons), source_id, target_id, source_path, target_path


def _endpoint_terms(
    shared: _SharedContext,
    entity_id: str,
    source_unit_id: str,
) -> tuple[str, ...]:
    anchor = shared.entity_anchor_by_key.get((entity_id, source_unit_id))
    return (anchor.quote,) if anchor is not None else ()


def _endpoint_anchor(
    shared: _SharedContext,
    entity_id: str,
    source_unit_id: str,
    relationship_anchor: ProposedOccurrenceAnchor,
) -> ProposedOccurrenceAnchor | None:
    anchor = shared.entity_anchor_by_key.get((entity_id, source_unit_id))
    if anchor is None:
        return None
    inside = (
        relationship_anchor.span_start
        <= anchor.span_start
        < anchor.span_end
        <= relationship_anchor.span_end
    )
    return anchor if inside else None


def _classification_record(
    *,
    record: ProposedCandidateView,
    hierarchy: CompiledHierarchy,
    state: AssertionState,
    reason_codes: tuple[str, ...],
    evidence_ids: tuple[str, ...],
    shared: _SharedContext,
) -> ClassificationAssertionRecord:
    type_id = record.approved_semantic_id
    ancestors = (
        (type_id,) + hierarchy.ancestors_by_type.get(type_id, ())
        if type_id in hierarchy.entity_by_id
        else ()
    )
    resolution = shared.classification_by_entity.get(record.semantic_id)
    classification_state = state.value
    if resolution is not None and resolution.ambiguous:
        classification_state = AssertionState.UNRESOLVED.value
    return ClassificationAssertionRecord(
        entity_id=record.semantic_id,
        classification_version_id=(
            record.classification_version_id or record.candidate_version_id
        ),
        candidate_id=record.candidate_id,
        semantic_type_id=type_id,
        classification_state=classification_state,
        ancestor_path=ancestors,
        hierarchy_depth=hierarchy.depth_by_type.get(type_id or "", 0),
        evidence_span_ids=evidence_ids,
        reason_codes=reason_codes,
        hierarchy_hash=hierarchy.hierarchy_hash,
        identity_policy_hash=hierarchy.identity_policy_hash,
    )


# ---------------------------------------------------------------------------
# Leaf checkpoint serialization
# ---------------------------------------------------------------------------


def _leaf_to_dict(leaf: L3LeafResult) -> dict[str, Any]:
    payload = {
        "extraction_candidate_batch_id": leaf.extraction_candidate_batch_id,
        "leaf_fingerprint": leaf.leaf_fingerprint,
        "evidence_spans": [span.model_dump(mode="json") for span in leaf.evidence_spans],
        "lifecycle_records": [
            record.model_dump(mode="json") for record in leaf.lifecycle_records
        ],
        "candidate_results": [item.__dict__ for item in leaf.candidate_results],
        "classifications": [item.__dict__ for item in leaf.classifications],
        "property_observations": [
            item.__dict__ for item in leaf.property_observations
        ],
        "reason_counts": [list(item) for item in leaf.reason_counts],
    }
    # The checkpoint is derived state, so its integrity is proved by a canonical
    # payload hash that is recomputed before any leaf is reused.
    return {**payload, "leaf_payload_hash": canonical_sha256(payload)}


def _leaf_payload_hash(raw: Mapping[str, Any]) -> str:
    return canonical_sha256(
        {key: value for key, value in raw.items() if key != "leaf_payload_hash"}
    )


def _leaf_from_dict(raw: Mapping[str, Any]) -> L3LeafResult:
    if _leaf_payload_hash(raw) != str(raw["leaf_payload_hash"]):
        raise ValueError("leaf checkpoint payload hash does not recompute")
    return L3LeafResult(
        extraction_candidate_batch_id=str(raw["extraction_candidate_batch_id"]),
        leaf_fingerprint=str(raw["leaf_fingerprint"]),
        evidence_spans=tuple(
            EvidenceSpanV1_1.model_validate_json(json.dumps(item))
            for item in raw["evidence_spans"]
        ),
        lifecycle_records=tuple(
            CandidateLifecycleRecord.model_validate_json(json.dumps(item))
            for item in raw["lifecycle_records"]
        ),
        candidate_results=tuple(
            CandidateValidationRecord(
                **{
                    key: (
                        tuple(value)
                        if key
                        in {
                            "reason_codes",
                            "evidence_span_ids",
                            "source_inheritance_path",
                            "target_inheritance_path",
                        }
                        else value
                    )
                    for key, value in item.items()
                }
            )
            for item in raw["candidate_results"]
        ),
        classifications=tuple(
            ClassificationAssertionRecord(
                **{
                    key: (
                        tuple(value)
                        if key
                        in {"ancestor_path", "evidence_span_ids", "reason_codes"}
                        else value
                    )
                    for key, value in item.items()
                }
            )
            for item in raw["classifications"]
        ),
        property_observations=tuple(
            PropertyObservationRecord(
                **{
                    key: (
                        tuple(value)
                        if key
                        in {
                            "constraint_outcome",
                            "evidence_span_ids",
                            "reason_codes",
                        }
                        else value
                    )
                    for key, value in item.items()
                }
            )
            for item in raw["property_observations"]
        ),
        reason_counts=tuple((str(item[0]), int(item[1])) for item in raw["reason_counts"]),
    )


# ---------------------------------------------------------------------------
# Deterministic fingerprints
# ---------------------------------------------------------------------------


def _verifier_binding() -> list[str]:
    return [
        L3_EXTRACTION_VERIFIER_NAME,
        L3_EXTRACTION_VERIFIER_VERSION,
        L3_EXTRACTION_PURPOSE,
        L3_EXTRACTION_PURPOSE_VERSION,
        L3_EVIDENCE_SPAN_VERSION,
    ]


def l3_input_fingerprint(inputs: L3Inputs) -> str:
    """Bind every semantic input; hierarchy depth is reported, never tied to K."""

    return canonical_sha256(
        {
            "l2_receipt_hash": inputs.l2_receipt.receipt_hash,
            "l2_output_manifest_id": (
                inputs.l2_output_manifest.artifact_manifest_id
            ),
            "l2_output_manifest_hash": inputs.l2_output_manifest.manifest_hash,
            "l2_input_manifest_hash": inputs.l2_input_manifest.manifest_hash,
            "source_corpus_manifest_id": (
                inputs.corpus_manifest.source_corpus_manifest_id
            ),
            "source_corpus_manifest_hash": inputs.corpus_manifest.corpus_hash,
            "source_unit_manifest_id": (
                inputs.source_unit_manifest.artifact_manifest_id
            ),
            "source_unit_manifest_hash": inputs.source_unit_manifest.manifest_hash,
            "source_unit_id_set_hash": inputs.source_units.source_unit_id_set_hash,
            "source_unit_content_hash": (
                inputs.source_units.source_unit_content_hash
            ),
            "candidate_batch_hashes": sorted(
                batch.batch_hash for batch in inputs.candidate_batches
            ),
            "required_member_proposal_hashes": sorted(
                proposal.proposal_hash
                for proposal in inputs.required_member_proposals
            ),
            **inputs.authority_hashes,
            "hierarchy_depth": inputs.hierarchy.hierarchy_depth,
            "validator": [L3_VALIDATOR_NAME, L3_VALIDATOR_VERSION],
            "verifier": _verifier_binding(),
            "stage_contract_version": L3_STAGE_CONTRACT_VERSION,
            "accepted_contract_versions": L3_ACCEPTED_VERSIONS,
        }
    )


def _leaf_fingerprint(
    *,
    inputs: L3Inputs,
    shared: _SharedContext,
    batch_id: str,
) -> str:
    batch = inputs.batch_by_id[batch_id]
    records = inputs.proposed_partitions[batch_id]
    entity_ids: set[str] = set()
    relationship_ids: set[str] = set()
    source_units: set[tuple[str, str]] = set()
    for record in records:
        source_units.add(
            (
                record.source_unit_id,
                # The complete verified artifact, so a same-text SourceUnit whose
                # unit kind, offset unit, ordinal, or locator changed can never
                # address an earlier leaf.
                inputs.source_units.semantic_hash(record.source_unit_id),
            )
        )
        if record.candidate_kind == "entity":
            entity_ids.add(record.semantic_id)
        elif record.candidate_kind == "relationship":
            relationship_ids.add(record.semantic_id)
            for endpoint in (
                record.proposed_source_entity_id,
                record.proposed_target_entity_id,
            ):
                if endpoint:
                    entity_ids.add(endpoint)
    return canonical_sha256(
        {
            "batch_hash": batch.batch_hash,
            "candidate_version_ids": sorted(
                item.candidate_version_id for item in batch.candidates
            ),
            "source_units": sorted(list(item) for item in source_units),
            "initial_lifecycle_hashes": sorted(
                record.transition_hash
                for record in inputs.lifecycle_partitions[batch_id]
            ),
            **inputs.authority_hashes,
            "validator": [L3_VALIDATOR_NAME, L3_VALIDATOR_VERSION],
            "verifier": _verifier_binding(),
            "context_hash": shared.context_hash(
                entity_ids,
                relationship_ids,
                (source_unit_id for source_unit_id, _ in source_units),
            ),
        }
    )


# ---------------------------------------------------------------------------
# Structured fact-set completeness
# ---------------------------------------------------------------------------


_STATE_PRECEDENCE = {
    AssertionState.ASSERTED: 0,
    AssertionState.UNRESOLVED: 1,
    AssertionState.DISCOVERY: 2,
    AssertionState.UNSUPPORTED: 3,
    AssertionState.REJECTED: 4,
}


def _best_result(
    results: Sequence[CandidateValidationRecord],
) -> CandidateValidationRecord | None:
    if not results:
        return None
    return sorted(
        results,
        key=lambda item: (
            _STATE_PRECEDENCE[AssertionState(item.current_state)],
            item.candidate_id,
        ),
    )[0]


def _validate_required_member_sets(
    *,
    inputs: L3Inputs,
    shared: _SharedContext,
    leaves: Sequence[L3LeafResult],
    identity: CanonicalIdentityEnvelope,
    sealed_at_utc: datetime,
) -> tuple[RequiredMemberOutcomeRecord, ...]:
    results_by_candidate = {
        result.candidate_id: result
        for leaf in leaves
        for result in leaf.candidate_results
    }
    entity_results: defaultdict[str, list[CandidateValidationRecord]] = defaultdict(list)
    membership_index: defaultdict[
        tuple[str, str, str], list[tuple[ProposedCandidateView, CandidateValidationRecord]]
    ] = defaultdict(list)
    for batch_id in inputs.leaf_batch_ids:
        for record in inputs.proposed_partitions[batch_id]:
            result = results_by_candidate.get(record.candidate_id)
            if result is None:
                raise L3StageError(
                    "L3_VALIDATION_RESULT_INCOMPLETE",
                    f"candidate {record.candidate_id} has no validation result",
                )
            if record.candidate_kind == "entity":
                entity_results[record.semantic_id].append(result)
            elif (
                record.candidate_kind == "relationship"
                and record.approved_semantic_id is not None
            ):
                source_id = (
                    result.resolved_source_entity_id
                    or record.proposed_source_entity_id
                    or ""
                )
                target_id = (
                    result.resolved_target_entity_id
                    or record.proposed_target_entity_id
                    or ""
                )
                membership_index[
                    (record.approved_semantic_id, source_id, target_id)
                ].append((record, result))

    outcomes: list[RequiredMemberOutcomeRecord] = []
    # Only locally minted extraction evidence proves an observed count. Bounded
    # L1 design-sample evidence is design context and is explicitly prohibited.
    minted_evidence_ids = {
        span.evidence_span_id for leaf in leaves for span in leaf.evidence_spans
    }
    design_evidence_ids = {
        evidence_id
        for entry in inputs.design_sample_manifest.entries
        for evidence_id in entry.evidence_span_ids
    }
    for proposal in inputs.required_member_proposals:
        requirement = inputs.hierarchy.requirement_by_id[
            proposal.completeness_requirement_id
        ]
        fact_set = requirement.structured_fact_set
        assert fact_set is not None
        verified_members: list[VerifiedMember] = []
        for member in proposal.members:
            member_result = _best_result(
                entity_results.get(member.member_canonical_id, [])
            )
            membership_pairs = membership_index.get(
                (
                    fact_set.membership_relationship_type_id,
                    proposal.scope_canonical_id,
                    member.member_canonical_id,
                ),
                [],
            ) + membership_index.get(
                (
                    fact_set.membership_relationship_type_id,
                    member.member_canonical_id,
                    proposal.scope_canonical_id,
                ),
                [],
            )
            membership = (
                sorted(
                    membership_pairs,
                    key=lambda item: (
                        _STATE_PRECEDENCE[AssertionState(item[1].current_state)],
                        item[1].candidate_id,
                    ),
                )[0]
                if membership_pairs
                else None
            )
            member_type = (
                shared.entity_type_by_id.get(member.member_canonical_id)
                or (member_result.approved_semantic_id if member_result else None)
                or member.member_semantic_type_id
            )
            verified_members.append(
                VerifiedMember(
                    member_canonical_id=member.member_canonical_id,
                    member_semantic_type_id=member_type,
                    member_role_id=(
                        membership[0].proposed_member_role_id if membership else None
                    ),
                    member_order=(
                        membership[0].proposed_member_order if membership else None
                    ),
                    candidate_id=member.candidate_id,
                    member_state=(
                        AssertionState(member_result.current_state)
                        if member_result is not None
                        else AssertionState.UNRESOLVED
                    ),
                    membership_state=(
                        AssertionState(membership[1].current_state)
                        if membership is not None
                        else AssertionState.UNRESOLVED
                    ),
                    membership_evidence_span_ids=(
                        membership[1].evidence_span_ids if membership else ()
                    ),
                    member_evidence_span_ids=(
                        member_result.evidence_span_ids if member_result else ()
                    ),
                )
            )
        outcome = validate_required_member_proposal(
            proposal_id=proposal.required_member_set_proposal_id,
            requirement=requirement,
            scope_canonical_id=proposal.scope_canonical_id,
            ordering_policy=proposal.ordering_policy,
            required_role_ids=proposal.required_role_ids,
            expected_cardinality=proposal.expected_cardinality,
            minimum_cardinality=proposal.minimum_cardinality,
            maximum_cardinality=proposal.maximum_cardinality,
            proposal_members=proposal.members,
            verified_members=verified_members,
            hierarchy=inputs.hierarchy,
            authority=proposal.authority,
            membership_semantic_relationship_id=(
                proposal.membership_semantic_relationship_id
            ),
            proposal_collection_hash=proposal.authoritative_collection_hash,
            approved_cardinality_evidence_ids=minted_evidence_ids,
            prohibited_cardinality_evidence_ids=design_evidence_ids,
            adjacency_policy=None,
        )
        manifest: RequiredMemberManifestV1_1 | None = None
        if outcome.completeness_state == "complete":
            manifest = _seal_manifest(
                proposal=proposal,
                identity=identity,
                sealed_at_utc=sealed_at_utc,
            )
        outcomes.append(RequiredMemberOutcomeRecord(outcome=outcome, manifest=manifest))
    return tuple(outcomes)


def _seal_manifest(
    *,
    proposal: RequiredMemberSetProposalV1_1,
    identity: CanonicalIdentityEnvelope,
    sealed_at_utc: datetime,
) -> RequiredMemberManifestV1_1:
    values = proposal.identity.model_dump(mode="python")
    values.update(
        {
            "contract_kind": "c0.required_member_manifest",
            "contract_version": "1.1.0",
            "extractor_name": identity.extractor_name,
            "extractor_version": identity.extractor_version,
            "prompt_version": None,
            "prompt_hash": None,
            "model_version": None,
            "model_hash": None,
        }
    )
    manifest_identity = RequiredMemberManifestIdentityV1_1.model_validate(values)
    manifest_id = deterministic_contract_id(
        "required-member-manifest",
        {
            "required_member_set_proposal_id": (
                proposal.required_member_set_proposal_id
            ),
            "proposal_hash": proposal.proposal_hash,
            "validator": [L3_VALIDATOR_NAME, L3_VALIDATOR_VERSION],
        },
    )
    try:
        manifest = RequiredMemberManifestV1_1.seal_from_proposal(
            proposal,
            identity=manifest_identity,
            required_member_manifest_id=manifest_id,
            validator_name=L3_VALIDATOR_NAME,
            validator_version=L3_VALIDATOR_VERSION,
            sealed_at_utc=sealed_at_utc,
        )
    except ValueError as exc:
        raise L3StageError(
            "L3_VALIDATION_RESULT_INCOMPLETE",
            f"complete collection cannot be sealed by the C0 factory: {exc}",
        ) from exc
    manifest.validate_against_proposal(proposal)
    return manifest


# ---------------------------------------------------------------------------
# Audit-ready indexes and manifest reconciliation
# ---------------------------------------------------------------------------


def _identity_index(
    leaves: Sequence[L3LeafResult],
    shared: _SharedContext,
) -> dict[str, Any]:
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
    for leaf in leaves:
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
    return {
        "entities": [
            {
                "entity_id": entity_id,
                "classification_version_ids": sorted(
                    bucket["classification_version_ids"]
                ),
                "semantic_type_ids": sorted(bucket["semantic_type_ids"]),
                "most_specific_type_id": shared.entity_type_by_id.get(entity_id),
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


def _current_state_index(leaves: Sequence[L3LeafResult]) -> dict[str, Any]:
    by_state: defaultdict[str, set[str]] = defaultdict(set)
    for leaf in leaves:
        for result in leaf.candidate_results:
            by_state[result.current_state].add(result.candidate_id)
    return {
        "counts_by_state": [
            [state, len(values)] for state, values in sorted(by_state.items())
        ],
        "candidate_ids_by_state": {
            state: sorted(values) for state, values in sorted(by_state.items())
        },
    }


def _reason_code_index(
    leaves: Sequence[L3LeafResult],
    outcomes: Sequence[RequiredMemberOutcomeRecord],
) -> dict[str, Any]:
    by_reason: defaultdict[str, set[str]] = defaultdict(set)
    for leaf in leaves:
        for result in leaf.candidate_results:
            for reason in result.reason_codes:
                by_reason[reason].add(result.candidate_id)
    collection_reasons: defaultdict[str, set[str]] = defaultdict(set)
    for record in outcomes:
        for reason in record.outcome.reason_codes:
            collection_reasons[reason].add(
                record.outcome.required_member_set_proposal_id
            )
    return {
        "candidate_reason_counts": [
            [reason, len(values)] for reason, values in sorted(by_reason.items())
        ],
        "candidate_ids_by_reason": {
            reason: sorted(values) for reason, values in sorted(by_reason.items())
        },
        "collection_reason_counts": [
            [reason, len(values)]
            for reason, values in sorted(collection_reasons.items())
        ],
        "proposal_ids_by_reason": {
            reason: sorted(values)
            for reason, values in sorted(collection_reasons.items())
        },
        "domain_rereview_requested": sorted(
            by_reason.get("DOMAIN_REREVIEW_REQUESTED", set())
        ),
    }


def _reconcile_derived_leaves(
    *,
    inputs: L3Inputs,
    shared: _SharedContext,
    leaves: Sequence[L3LeafResult],
    lifecycle_identity: CanonicalIdentityEnvelope,
    occurred_at_utc: datetime,
) -> None:
    """Re-derive every published leaf from sealed inputs and require exact equality.

    A reused checkpoint is derived state with no sealed C0 carrier of its own for
    reason sets, states, classifications, or property observations, and the one
    self-hashing carrier it does contain — ``CandidateLifecycleRecord`` — is
    sealed with an unkeyed hash that any writer can recompute. Reconciliation
    therefore re-derives the complete outcome of every candidate kind from the
    sealed L2 proposal partition, the verified SourceUnit artifacts, and the
    compiled Domain authority, and refuses to publish anything that is not
    byte-identical to that derivation.
    """

    for leaf in leaves:
        batch_id = leaf.extraction_candidate_batch_id
        if batch_id not in inputs.proposed_partitions:
            raise L3StageError(
                "L3_VALIDATION_RESULT_INCOMPLETE",
                f"leaf {batch_id} has no persisted L2 proposal partition",
            )
        expected = _validate_leaf(
            batch=inputs.batch_by_id[batch_id],
            records=inputs.proposed_partitions[batch_id],
            lifecycle_by_candidate={
                record.candidate_id: record
                for record in inputs.lifecycle_partitions[batch_id]
            },
            inputs=inputs,
            shared=shared,
            lifecycle_identity=lifecycle_identity,
            occurred_at_utc=occurred_at_utc,
            leaf_fingerprint=_leaf_fingerprint(
                inputs=inputs,
                shared=shared,
                batch_id=batch_id,
            ),
        )
        expected_payload = _leaf_to_dict(expected)
        reported_payload = _leaf_to_dict(leaf)
        if expected_payload["leaf_payload_hash"] == (
            reported_payload["leaf_payload_hash"]
        ):
            continue
        divergent = sorted(
            {
                str(item["candidate_id"])
                for key in ("candidate_results", "lifecycle_records")
                for item, other in zip(
                    reported_payload[key],
                    expected_payload[key],
                )
                if item != other
            }
        )
        raise L3StageError(
            "L3_VALIDATION_RESULT_INCOMPLETE",
            f"leaf {batch_id} does not re-derive from its sealed inputs"
            + (f": {divergent}" if divergent else ""),
        )


def _reconcile_candidate_bindings(
    *,
    inputs: L3Inputs,
    results: Sequence[CandidateValidationRecord],
) -> None:
    """Re-derive every derived candidate field instead of trusting a cached leaf.

    ``identity_recomputed``, ``identity_witness_kind``, and the copied L2
    bindings have no sealed C0 carrier of their own, so a reused leaf could
    otherwise claim an identity it never reproduced or a candidate binding L2
    never proposed. Each entity result therefore re-runs the sealed identity rule
    and every result is re-bound to its exact persisted L2 proposal before
    publication.
    """

    proposals: dict[str, tuple[ProposedCandidateView, str]] = {}
    for batch_id in inputs.leaf_batch_ids:
        project_id = inputs.batch_by_id[batch_id].identity.project_id
        for record in inputs.proposed_partitions[batch_id]:
            proposals[record.candidate_id] = (record, project_id)
    for result in results:
        binding = proposals.get(result.candidate_id)
        if binding is None:
            raise L3StageError(
                "L3_VALIDATION_RESULT_INCOMPLETE",
                f"candidate {result.candidate_id} has no persisted L2 proposal",
            )
        record, project_id = binding
        if (
            result.candidate_kind != record.candidate_kind
            or result.candidate_version_id != record.candidate_version_id
            or result.semantic_id != record.semantic_id
            or result.approved_semantic_id != record.approved_semantic_id
            or result.source_unit_id != record.source_unit_id
        ):
            raise L3StageError(
                "L3_VALIDATION_RESULT_INCOMPLETE",
                f"candidate {result.candidate_id} diverges from its L2 proposal",
            )
        if result.candidate_kind != "entity":
            if result.identity_recomputed or result.identity_witness_kind != (
                "not_applicable"
            ):
                raise L3StageError(
                    "L3_VALIDATION_RESULT_INCOMPLETE",
                    f"candidate {result.candidate_id} claims a foreign identity witness",
                )
            continue
        outcome = resolve_identity_witness(
            semantic_id=record.semantic_id,
            approved_semantic_id=record.approved_semantic_id,
            source_unit_id=record.source_unit_id,
            local_reference=record.local_reference,
            hierarchy=inputs.hierarchy,
            project_id=project_id,
        )
        if (
            outcome.recomputed != result.identity_recomputed
            or outcome.witness_kind != result.identity_witness_kind
            or not set(outcome.reason_codes) <= set(result.reason_codes)
        ):
            raise L3StageError(
                "L3_VALIDATION_RESULT_INCOMPLETE",
                f"entity {result.candidate_id} identity witness does not re-derive",
            )


def _reconcile_lifecycle_chain(
    *,
    results: Sequence[CandidateValidationRecord],
    lifecycle: Sequence[CandidateLifecycleRecord],
) -> None:
    """Prove every reported current state equals its sealed appended transition.

    Only ``CandidateLifecycleRecord`` is self-hashing, so a reused derived leaf
    could otherwise report a current state, reason set, or evidence set that its
    own appended transition never sealed. Each reported result is therefore
    re-derived from the sealed record and re-classified before publication.
    """

    sealed_by_candidate: dict[str, CandidateLifecycleRecord] = {}
    for record in lifecycle:
        if record.candidate_id in sealed_by_candidate:
            raise L3StageError(
                "L3_LIFECYCLE_CHAIN_INVALID",
                f"duplicate appended transition for {record.candidate_id}",
            )
        sealed_by_candidate[record.candidate_id] = record
    for result in results:
        sealed = sealed_by_candidate.get(result.candidate_id)
        if sealed is None:
            raise L3StageError(
                "L3_LIFECYCLE_CHAIN_INVALID",
                f"candidate {result.candidate_id} has no appended transition",
            )
        try:
            reported_state = AssertionState(result.current_state)
        except ValueError as exc:
            raise L3StageError(
                "L3_LIFECYCLE_CHAIN_INVALID",
                f"candidate {result.candidate_id} reports an unknown state",
            ) from exc
        if classify_state(result.reason_codes) is not reported_state:
            raise L3StageError(
                "L3_LIFECYCLE_CHAIN_INVALID",
                f"candidate {result.candidate_id} state contradicts its reasons",
            )
        divergent = (
            sealed.to_state is not reported_state
            or sealed.candidate_kind != result.candidate_kind
            or sealed.candidate_version_id != result.candidate_version_id
            or tuple(sealed.reason_codes) != sorted_reasons(result.reason_codes)
            or tuple(sealed.evidence_span_ids)
            != tuple(sorted(set(result.evidence_span_ids)))
            or sealed.resolved_source_entity_id != result.resolved_source_entity_id
            or sealed.resolved_target_entity_id != result.resolved_target_entity_id
            or tuple(sealed.source_inheritance_path) != result.source_inheritance_path
            or tuple(sealed.target_inheritance_path) != result.target_inheritance_path
        )
        if divergent:
            raise L3StageError(
                "L3_LIFECYCLE_CHAIN_INVALID",
                f"candidate {result.candidate_id} diverges from its sealed transition",
            )


def _reconcile(
    *,
    inputs: L3Inputs,
    shared: _SharedContext,
    leaves: Sequence[L3LeafResult],
    outcomes: Sequence[RequiredMemberOutcomeRecord],
    lifecycle_identity: CanonicalIdentityEnvelope,
    occurred_at_utc: datetime,
) -> None:
    """Fail closed unless every audit obligation reconciles exactly."""

    retained = {
        candidate.candidate_id
        for batch_id in inputs.leaf_batch_ids
        for candidate in inputs.batch_by_id[batch_id].candidates
    }
    results = [result for leaf in leaves for result in leaf.candidate_results]
    result_ids = [result.candidate_id for result in results]
    if len(result_ids) != len(set(result_ids)):
        raise L3StageError(
            "L3_VALIDATION_RESULT_INCOMPLETE",
            "each retained candidate requires exactly one current state",
        )
    if set(result_ids) != retained:
        raise L3StageError(
            "L3_VALIDATION_RESULT_INCOMPLETE",
            "every input candidate must remain audit-addressable",
        )
    lifecycle = [
        record for leaf in leaves for record in leaf.lifecycle_records
    ]
    if len({record.lifecycle_record_id for record in lifecycle}) != len(lifecycle):
        raise L3StageError(
            "L3_LIFECYCLE_CHAIN_INVALID",
            "appended lifecycle record IDs must be unique",
        )
    if {record.candidate_id for record in lifecycle} != retained:
        raise L3StageError(
            "L3_LIFECYCLE_CHAIN_INVALID",
            "every retained candidate requires exactly one appended transition",
        )
    _reconcile_lifecycle_chain(results=results, lifecycle=lifecycle)
    _reconcile_candidate_bindings(inputs=inputs, results=results)
    _reconcile_derived_leaves(
        inputs=inputs,
        shared=shared,
        leaves=leaves,
        lifecycle_identity=lifecycle_identity,
        occurred_at_utc=occurred_at_utc,
    )
    spans = {
        span.evidence_span_id: span for leaf in leaves for span in leaf.evidence_spans
    }
    for span in spans.values():
        require_extraction_evidence(span)
        try:
            span.verify_against(inputs.source_units.require(span.source_unit_id))
        except ValueError as exc:
            raise L3StageError(
                "L3_VALIDATION_RESULT_INCOMPLETE",
                f"minted evidence {span.evidence_span_id} no longer verifies: {exc}",
            ) from exc
    model_authored = {
        result.ignored_model_evidence_id
        for result in results
        if result.ignored_model_evidence_id
    }
    if model_authored & set(spans):
        raise L3StageError(
            "L3_EVIDENCE_ID_COLLISION",
            "a model-authored ID must never enter the verified evidence set",
        )
    for result in results:
        unresolved = set(result.evidence_span_ids) - set(spans)
        if unresolved:
            raise L3StageError(
                "L3_VALIDATION_RESULT_INCOMPLETE",
                f"candidate {result.candidate_id} names unresolved evidence",
            )
        if result.current_state == AssertionState.ASSERTED.value:
            if not result.evidence_span_ids:
                raise L3StageError(
                    "L3_VALIDATION_RESULT_INCOMPLETE",
                    f"asserted candidate {result.candidate_id} lacks evidence",
                )
            if result.candidate_kind == "relationship" and (
                result.resolved_source_entity_id is None
                or result.resolved_target_entity_id is None
            ):
                raise L3StageError(
                    "L3_VALIDATION_RESULT_INCOMPLETE",
                    f"asserted relationship {result.candidate_id} lacks endpoints",
                )
            if result.approved_semantic_id is None:
                raise L3StageError(
                    "L3_VALIDATION_RESULT_INCOMPLETE",
                    f"asserted candidate {result.candidate_id} has no approved ID",
                )
            if result.candidate_kind == "entity" and not result.identity_recomputed:
                raise L3StageError(
                    "L3_VALIDATION_RESULT_INCOMPLETE",
                    f"asserted entity {result.candidate_id} has an unproven identity",
                )
    for leaf in leaves:
        for classification in leaf.classifications:
            if (
                classification.hierarchy_hash != inputs.hierarchy.hierarchy_hash
                or classification.identity_policy_hash
                != inputs.hierarchy.identity_policy_hash
            ):
                raise L3StageError(
                    "L3_OUTPUT_MANIFEST_INVALID",
                    "classification assertions must bind the exact sealed hashes",
                )
            if classification.classification_state == AssertionState.ASSERTED.value:
                if (
                    classification.semantic_type_id
                    not in inputs.hierarchy.entity_by_id
                    or not classification.evidence_span_ids
                ):
                    raise L3StageError(
                        "L3_VALIDATION_RESULT_INCOMPLETE",
                        "asserted classifications require approved types and evidence",
                    )
        for observation in leaf.property_observations:
            if observation.observation_state == AssertionState.ASSERTED.value and (
                observation.effective_property_id
                not in inputs.hierarchy.property_by_id
                or not observation.evidence_span_ids
            ):
                raise L3StageError(
                    "L3_VALIDATION_RESULT_INCOMPLETE",
                    "asserted property observations require approved IDs and evidence",
                )
    outcome_ids = [
        record.outcome.required_member_set_proposal_id for record in outcomes
    ]
    proposal_ids = [
        proposal.required_member_set_proposal_id
        for proposal in inputs.required_member_proposals
    ]
    if sorted(outcome_ids) != sorted(proposal_ids) or len(outcome_ids) != len(
        set(outcome_ids)
    ):
        raise L3StageError(
            "L3_VALIDATION_RESULT_INCOMPLETE",
            "every required-member proposal requires exactly one L3 outcome",
        )
    manifest_ids = [
        record.manifest.required_member_manifest_id
        for record in outcomes
        if record.manifest is not None
    ]
    if len(manifest_ids) != len(set(manifest_ids)):
        raise L3StageError(
            "L3_OUTPUT_MANIFEST_INVALID",
            "required-member manifest IDs must be unique",
        )
    for record in outcomes:
        if (record.manifest is not None) != (
            record.outcome.completeness_state == "complete"
        ):
            raise L3StageError(
                "L3_VALIDATION_RESULT_INCOMPLETE",
                "only complete collections may be sealed as manifests",
            )
        if record.manifest is None and not record.outcome.reason_codes:
            raise L3StageError(
                "L3_VALIDATION_RESULT_INCOMPLETE",
                "an unresolved collection must name its missing obligations",
            )
    _reconcile_collection_partition(
        proposals=inputs.required_member_proposals,
        outcomes=outcomes,
    )


def _reconcile_collection_partition(
    *,
    proposals: Sequence[RequiredMemberSetProposalV1_1],
    outcomes: Sequence[RequiredMemberOutcomeRecord],
) -> None:
    """Prove the sealed/unresolved split partitions every proposal exactly once.

    ``c0.required_member_manifest@1.1.0`` can only carry a complete collection,
    so an incomplete collection is intentionally not representable as a manifest.
    That is an explicit frozen-contract constraint, not an equivalence: the
    unresolved ``l3.required_member_outcome`` is the only audit-addressable
    carrier for it. This proof keeps the two carriers a strict partition of the
    proposal set — no proposal is dropped, duplicated, or silently upgraded.
    """

    proposal_ids = [
        proposal.required_member_set_proposal_id for proposal in proposals
    ]
    if len(proposal_ids) != len(set(proposal_ids)):
        raise L3StageError(
            "L3_ACCOUNTING_INCOMPLETE",
            "required-member proposals must be unique",
        )
    sealed: list[str] = []
    unresolved: list[str] = []
    for record in outcomes:
        proposal_id = record.outcome.required_member_set_proposal_id
        if record.manifest is None:
            unresolved.append(proposal_id)
            continue
        if record.manifest.required_member_set_proposal_id != proposal_id:
            raise L3StageError(
                "L3_OUTPUT_MANIFEST_INVALID",
                f"sealed manifest for {proposal_id} names a foreign proposal",
            )
        sealed.append(proposal_id)
    if len(sealed) != len(set(sealed)) or len(unresolved) != len(set(unresolved)):
        raise L3StageError(
            "L3_VALIDATION_RESULT_INCOMPLETE",
            "a proposal may appear at most once per completeness carrier",
        )
    if set(sealed) & set(unresolved):
        raise L3StageError(
            "L3_VALIDATION_RESULT_INCOMPLETE",
            "a proposal cannot be both sealed complete and unresolved",
        )
    if sorted(sealed + unresolved) != sorted(proposal_ids):
        raise L3StageError(
            "L3_VALIDATION_RESULT_INCOMPLETE",
            "sealed manifests and unresolved outcomes must cover every proposal",
        )


# ---------------------------------------------------------------------------
# Manifests, metrics, and receipts
# ---------------------------------------------------------------------------


def _input_manifest(
    *,
    inputs: L3Inputs,
    identity: CanonicalIdentityEnvelope,
    fingerprint: str,
) -> ArtifactManifest:
    authority_payload = {
        **inputs.authority_hashes,
        "l2_receipt_hash": inputs.l2_receipt.receipt_hash,
        "l2_output_manifest_hash": inputs.l2_output_manifest.manifest_hash,
        "source_corpus_manifest_hash": inputs.corpus_manifest.corpus_hash,
        "source_unit_manifest_hash": inputs.source_unit_manifest.manifest_hash,
        "design_sample_manifest_hash": inputs.design_sample_manifest.sample_hash,
        "hierarchy_depth": inputs.hierarchy.hierarchy_depth,
        "l3_input_fingerprint": fingerprint,
    }
    authority_bytes = (
        json.dumps(
            authority_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    entries = [
        _artifact_entry(
            artifact_id=inputs.l2_receipt.stage_receipt_id,
            contract_kind="c0.stage_receipt",
            contract_version="1.0.0",
            schema_hash=canonical_sha256(StageReceipt.model_json_schema()),
            content_hash=inputs.l2_receipt.receipt_hash,
            byte_count=len(
                (canonical_json(inputs.l2_receipt) + "\n").encode("utf-8")
            ),
            row_count=1,
        ),
        _artifact_entry(
            artifact_id=inputs.source_unit_manifest.artifact_manifest_id,
            contract_kind="c0.artifact_manifest",
            contract_version="1.0.0",
            schema_hash=canonical_sha256(ArtifactManifest.model_json_schema()),
            content_hash=inputs.source_unit_manifest.manifest_hash,
            byte_count=len(
                (canonical_json(inputs.source_unit_manifest) + "\n").encode("utf-8")
            ),
            row_count=inputs.source_unit_manifest.total_row_count,
            canonical_id_set_hash=inputs.source_units.source_unit_id_set_hash,
        ),
        _artifact_entry(
            artifact_id="l3-authority-binding",
            contract_kind="l3.authority_binding",
            contract_version="1.0.0",
            schema_hash=canonical_sha256(
                {"contract_kind": "l3.authority_binding", "version": "1.0.0"}
            ),
            content_hash=canonical_sha256(authority_payload),
            byte_count=len(authority_bytes),
            row_count=1,
        ),
    ]
    return _manifest(identity=identity, label="input", entries=entries)


def _l2_reference_entries(inputs: L3Inputs) -> tuple[ArtifactEntry, ...]:
    """Reference unchanged C0-owner L2 artifacts without rewriting them."""

    referenced_kinds = {
        "c0.extraction_candidate_batch",
        "c0.candidate_lifecycle_record",
        "c0.required_member_set_proposal",
        "l2.proposed_candidate_partition",
    }
    return tuple(
        entry
        for entry in inputs.l2_output_manifest.entries
        if entry.contract_kind in referenced_kinds
    )


def _output_artifacts(
    *,
    state_root: Path,
    inputs: L3Inputs,
    shared: _SharedContext,
    leaves: Sequence[L3LeafResult],
    outcomes: Sequence[RequiredMemberOutcomeRecord],
) -> tuple[ArtifactEntry, ...]:
    entries: list[ArtifactEntry] = list(_l2_reference_entries(inputs))
    for leaf in sorted(leaves, key=lambda item: item.extraction_candidate_batch_id):
        batch_id = leaf.extraction_candidate_batch_id
        safe = _safe_id(batch_id)
        span_payload = [span.model_dump(mode="json") for span in leaf.evidence_spans]
        payload = _persist_json(
            state_root / "evidence-spans" / f"{safe}.json",
            span_payload,
        )
        entries.append(
            _artifact_entry(
                artifact_id=f"{batch_id}:evidence",
                contract_kind="c0.evidence_span",
                contract_version=L3_EVIDENCE_SPAN_VERSION,
                schema_hash=canonical_sha256(EvidenceSpanV1_1.model_json_schema()),
                content_hash=canonical_sha256(span_payload),
                byte_count=len(payload),
                row_count=len(leaf.evidence_spans),
                canonical_id_set_hash=canonical_sha256(
                    sorted(span.evidence_span_id for span in leaf.evidence_spans)
                ),
            )
        )
        lifecycle_payload = [
            record.model_dump(mode="json") for record in leaf.lifecycle_records
        ]
        payload = _persist_json(
            state_root / "lifecycle" / f"{safe}.json",
            lifecycle_payload,
        )
        entries.append(
            _artifact_entry(
                artifact_id=f"{batch_id}:lifecycle:l3",
                contract_kind="c0.candidate_lifecycle_record",
                contract_version="1.0.0",
                schema_hash=canonical_sha256(
                    CandidateLifecycleRecord.model_json_schema()
                ),
                content_hash=canonical_sha256(lifecycle_payload),
                byte_count=len(payload),
                row_count=len(leaf.lifecycle_records),
                canonical_id_set_hash=canonical_sha256(
                    sorted(
                        record.lifecycle_record_id
                        for record in leaf.lifecycle_records
                    )
                ),
            )
        )
        classification_payload = [item.__dict__ for item in leaf.classifications]
        payload = _persist_json(
            state_root / "classifications" / f"{safe}.json",
            classification_payload,
        )
        entries.append(
            _artifact_entry(
                artifact_id=f"{batch_id}:classifications",
                contract_kind="l3.classification_assertion",
                contract_version="1.0.0",
                schema_hash=canonical_sha256(
                    {
                        "contract_kind": "l3.classification_assertion",
                        "version": "1.0.0",
                    }
                ),
                content_hash=canonical_sha256(classification_payload),
                byte_count=len(payload),
                row_count=len(leaf.classifications),
                canonical_id_set_hash=canonical_sha256(
                    sorted(
                        item.classification_version_id
                        for item in leaf.classifications
                    )
                ),
            )
        )
        observation_payload = [item.__dict__ for item in leaf.property_observations]
        payload = _persist_json(
            state_root / "property-observations" / f"{safe}.json",
            observation_payload,
        )
        entries.append(
            _artifact_entry(
                artifact_id=f"{batch_id}:property-observations",
                contract_kind="l3.property_observation",
                contract_version="1.0.0",
                schema_hash=canonical_sha256(
                    {"contract_kind": "l3.property_observation", "version": "1.0.0"}
                ),
                content_hash=canonical_sha256(observation_payload),
                byte_count=len(payload),
                row_count=len(leaf.property_observations),
                canonical_id_set_hash=canonical_sha256(
                    sorted(
                        item.property_observation_id
                        for item in leaf.property_observations
                    )
                ),
            )
        )

    for label, payload_value, kind in (
        ("identity-index", _identity_index(leaves, shared), "l3.identity_index"),
        (
            "current-state-index",
            _current_state_index(leaves),
            "l3.current_state_index",
        ),
        (
            "reason-code-index",
            _reason_code_index(leaves, outcomes),
            "l3.reason_code_index",
        ),
    ):
        payload = _persist_json(state_root / f"{label}.json", payload_value)
        entries.append(
            _artifact_entry(
                artifact_id=f"l3-{label}",
                contract_kind=kind,
                contract_version="1.0.0",
                schema_hash=canonical_sha256(
                    {"contract_kind": kind, "version": "1.0.0"}
                ),
                content_hash=canonical_sha256(payload_value),
                byte_count=len(payload),
                row_count=None,
            )
        )

    for record in sorted(
        outcomes,
        key=lambda item: item.outcome.required_member_set_proposal_id,
    ):
        proposal_id = record.outcome.required_member_set_proposal_id
        outcome_payload = {
            key: (list(value) if isinstance(value, tuple) else value)
            for key, value in record.outcome.__dict__.items()
        }
        outcome_payload["role_coverage"] = [
            list(item) for item in record.outcome.role_coverage
        ]
        outcome_payload["required_member_manifest_id"] = (
            record.manifest.required_member_manifest_id
            if record.manifest is not None
            else None
        )
        payload = _persist_json(
            state_root
            / "required-member-outcomes"
            / f"{_safe_id(proposal_id)}.json",
            outcome_payload,
        )
        entries.append(
            _artifact_entry(
                artifact_id=f"{proposal_id}:outcome",
                contract_kind="l3.required_member_outcome",
                contract_version="1.0.0",
                schema_hash=canonical_sha256(
                    {
                        "contract_kind": "l3.required_member_outcome",
                        "version": "1.0.0",
                    }
                ),
                content_hash=canonical_sha256(outcome_payload),
                byte_count=len(payload),
                row_count=len(record.outcome.verified_member_ids),
                canonical_id_set_hash=canonical_sha256(
                    sorted(record.outcome.verified_member_ids)
                ),
            )
        )
        if record.manifest is None:
            continue
        payload = _persist_json(
            state_root
            / "required-member-manifests"
            / f"{_safe_id(record.manifest.required_member_manifest_id)}.json",
            record.manifest,
        )
        entries.append(
            _artifact_entry(
                artifact_id=record.manifest.required_member_manifest_id,
                contract_kind="c0.required_member_manifest",
                contract_version="1.1.0",
                schema_hash=canonical_sha256(
                    RequiredMemberManifestV1_1.model_json_schema()
                ),
                content_hash=record.manifest.manifest_hash,
                byte_count=len(payload),
                row_count=len(record.manifest.members),
                canonical_id_set_hash=record.manifest.member_set_hash,
            )
        )
    return tuple(entries)


def _resource_metrics(
    *,
    identity: CanonicalIdentityEnvelope,
    inputs: L3Inputs,
    reused_leaves: int,
    recomputed_leaves: int,
    storage_read_bytes: int,
    storage_write_bytes: int,
    started: float,
) -> StageResourceMetrics:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    peak_rss = int(usage.ru_maxrss)
    if os.uname().sysname != "Darwin":
        peak_rss *= 1024
    metrics_id = deterministic_contract_id(
        "stage-resource-metrics",
        {
            "stage_id": "L3",
            "run_id": identity.run_id,
            "source_unit_manifest_hash": inputs.source_unit_manifest.manifest_hash,
            "l2_output_manifest_hash": inputs.l2_output_manifest.manifest_hash,
        },
    )
    values = {
        "identity": identity.model_copy(
            update={"contract_kind": "c0.stage_resource_metrics"}
        ),
        "resource_metrics_id": metrics_id,
        "stage_id": "L3",
        "stage_name": L3_STAGE_NAME,
        "wall_ms": max(0, int((time.perf_counter() - started) * 1000)),
        "cpu_ms": max(0, int((usage.ru_utime + usage.ru_stime) * 1000)),
        "peak_rss_bytes": peak_rss,
        "storage_read_bytes": storage_read_bytes,
        "storage_write_bytes": storage_write_bytes,
        # L3 is local-only: every remote dimension stays exactly zero.
        "network_request_bytes": 0,
        "network_response_bytes": 0,
        "source_units_read": len(inputs.source_units),
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
        "cache_hits": reused_leaves,
        "cache_misses": recomputed_leaves,
        "max_observed_concurrency": 1,
        "budget_snapshot_hash": canonical_sha256(
            {
                "hierarchy_depth": inputs.hierarchy.hierarchy_depth,
                "numeric_performance_thresholds": None,
                "remote_calls_allowed": 0,
            }
        ),
        "exceeded_dimensions": (),
    }
    return StageResourceMetrics(**values, metrics_hash=canonical_sha256(values))


REMOTE_METRIC_DIMENSIONS = (
    "document_intelligence_calls",
    "document_intelligence_pages",
    "embedding_calls",
    "embedding_items",
    "fabric_calls",
    "fabric_rows_read",
    "fabric_rows_written",
    "foundry_calls",
    "foundry_input_tokens",
    "foundry_output_tokens",
    "network_request_bytes",
    "network_response_bytes",
    "search_calls",
    "search_documents_read",
    "search_documents_written",
)


def assert_local_only(metrics: StageResourceMetrics) -> None:
    """Prove no remote resource dimension was consumed by L3."""

    nonzero = [
        dimension
        for dimension in REMOTE_METRIC_DIMENSIONS
        if getattr(metrics, dimension) != 0
    ]
    if nonzero:
        raise L3StageError(
            "L3_RESOURCE_BINDING_INVALID",
            f"L3 must not consume remote resources: {nonzero}",
        )


def run_l3(
    *,
    state_root: Path = L3_STATE_DIR,
    l2_state_root: Path = Path(".fkg") / "l2",
    l1_state_root: Path = Path(".fkg") / "l1",
    domain_path: Path = Path("domain.yaml"),
) -> L3StageResult:
    """Validate L2 proposals locally; no remote call, projection, or publication."""

    started = time.perf_counter()
    started_at_utc = datetime.now(timezone.utc)
    inputs = load_l3_inputs(
        l2_state_root=l2_state_root,
        l1_state_root=l1_state_root,
        domain_path=domain_path,
    )
    shared = _build_shared_context(inputs)
    identity = _validation_identity(
        inputs.l2_receipt.identity,
        contract_kind="l3.stage",
    )
    lifecycle_identity = _validation_identity(
        identity,
        contract_kind="c0.candidate_lifecycle_record",
    )
    fingerprint = l3_input_fingerprint(inputs)
    run_root = l3_run_root(state_root, fingerprint)
    input_manifest = _input_manifest(
        inputs=inputs,
        identity=identity,
        fingerprint=fingerprint,
    )
    _persist_json(run_root / "input-manifest.json", input_manifest)
    occurred_at_utc = inputs.l2_receipt.completed_at_utc

    leaves: list[L3LeafResult] = []
    reused = 0
    recomputed = 0
    for batch_id in inputs.leaf_batch_ids:
        leaf_fingerprint = _leaf_fingerprint(
            inputs=inputs,
            shared=shared,
            batch_id=batch_id,
        )
        checkpoint_path = l3_leaf_checkpoint_path(
            state_root,
            batch_id,
            leaf_fingerprint,
        )
        leaf = _reuse_leaf(checkpoint_path, batch_id, leaf_fingerprint)
        if leaf is None:
            leaf = _validate_leaf(
                batch=inputs.batch_by_id[batch_id],
                records=inputs.proposed_partitions[batch_id],
                lifecycle_by_candidate={
                    record.candidate_id: record
                    for record in inputs.lifecycle_partitions[batch_id]
                },
                inputs=inputs,
                shared=shared,
                lifecycle_identity=lifecycle_identity,
                occurred_at_utc=occurred_at_utc,
                leaf_fingerprint=leaf_fingerprint,
            )
            _write_cache(checkpoint_path, _leaf_to_dict(leaf))
            recomputed += 1
        else:
            reused += 1
        leaves.append(leaf)

    outcomes = _validate_required_member_sets(
        inputs=inputs,
        shared=shared,
        leaves=leaves,
        identity=identity,
        sealed_at_utc=occurred_at_utc,
    )
    _reconcile(
        inputs=inputs,
        shared=shared,
        leaves=leaves,
        outcomes=outcomes,
        lifecycle_identity=lifecycle_identity,
        occurred_at_utc=occurred_at_utc,
    )

    output_entries = _output_artifacts(
        state_root=run_root,
        inputs=inputs,
        shared=shared,
        leaves=leaves,
        outcomes=outcomes,
    )
    output_manifest = _manifest(
        identity=identity,
        label="output",
        entries=output_entries,
    )
    output_payload = _persist_json(
        run_root / "output-manifest.json",
        output_manifest,
    )

    prior_receipt_path = run_root / "stage-receipt.json"
    prior_metrics_path = run_root / "resource-metrics.json"
    if prior_receipt_path.exists() and prior_metrics_path.exists():
        prior_receipt = _read_json_model(
            prior_receipt_path,
            StageReceipt,
            "L3_INPUT_RECEIPT_INVALID",
        )
        prior_metrics = _read_json_model(
            prior_metrics_path,
            StageResourceMetrics,
            "L3_RESOURCE_BINDING_INVALID",
        )
        if (
            prior_receipt.status == "succeeded"
            and prior_receipt.skip_key == fingerprint
            and prior_receipt.input_manifest_id
            == input_manifest.artifact_manifest_id
            and prior_receipt.input_manifest_hash == input_manifest.manifest_hash
            and prior_receipt.output_manifest_id
            == output_manifest.artifact_manifest_id
            and prior_receipt.output_manifest_hash == output_manifest.manifest_hash
        ):
            try:
                validate_receipt_resources(prior_receipt, prior_metrics)
            except ValueError as exc:
                raise L3StageError("L3_RESOURCE_BINDING_INVALID", str(exc)) from exc
            assert_local_only(prior_metrics)
            return L3StageResult(
                inputs=inputs,
                leaves=tuple(leaves),
                required_member_outcomes=outcomes,
                input_manifest=input_manifest,
                output_manifest=output_manifest,
                metrics=prior_metrics,
                receipt=prior_receipt,
                reused_leaf_count=reused,
                recomputed_leaf_count=recomputed,
                state_root=state_root,
                run_root=run_root,
            )
        raise L3StageError(
            "L3_OUTPUT_MANIFEST_INVALID",
            "persisted L3 receipt does not satisfy exact skip bindings",
        )
    if prior_receipt_path.exists() and not prior_metrics_path.exists():
        raise L3StageError(
            "L3_RESOURCE_BINDING_INVALID",
            "persisted L3 receipt is missing its bound resource metrics",
        )

    if prior_metrics_path.exists():
        metrics = _read_json_model(
            prior_metrics_path,
            StageResourceMetrics,
            "L3_RESOURCE_BINDING_INVALID",
        )
        expected_identity = identity.model_copy(
            update={"contract_kind": "c0.stage_resource_metrics"}
        )
        if (
            metrics.identity != expected_identity
            or metrics.stage_id != "L3"
            or metrics.stage_name != L3_STAGE_NAME
            or metrics.source_units_read != len(inputs.source_units)
        ):
            raise L3StageError(
                "L3_RESOURCE_BINDING_INVALID",
                "persisted L3 resource metrics do not match exact resume bindings",
            )
    else:
        metrics = _resource_metrics(
            identity=identity,
            inputs=inputs,
            reused_leaves=reused,
            recomputed_leaves=recomputed,
            storage_read_bytes=inputs.source_unit_manifest.total_byte_count,
            storage_write_bytes=output_manifest.total_byte_count
            + len(output_payload),
            started=started,
        )
        _persist_json(run_root / "resource-metrics.json", metrics)
    assert_local_only(metrics)

    receipt_values = {
        "identity": identity.model_copy(
            update={"contract_kind": "c0.stage_receipt"}
        ),
        "stage_receipt_id": deterministic_contract_id(
            "stage-receipt",
            {"stage_id": "L3", "run_id": identity.run_id, "skip_key": fingerprint},
        ),
        "stage_id": "L3",
        "stage_name": L3_STAGE_NAME,
        "stage_contract_version": L3_STAGE_CONTRACT_VERSION,
        "status": "succeeded",
        "input_manifest_id": input_manifest.artifact_manifest_id,
        "input_manifest_hash": input_manifest.manifest_hash,
        "output_manifest_id": output_manifest.artifact_manifest_id,
        "output_manifest_hash": output_manifest.manifest_hash,
        "skip_key": fingerprint,
        "accepted_contract_versions": L3_ACCEPTED_VERSIONS,
        "resource_metrics_id": metrics.resource_metrics_id,
        "resource_metrics_hash": metrics.metrics_hash,
        "attempt_count": 1,
        "remote_operation_refs": (),
        "error_codes": (),
        "started_at_utc": started_at_utc,
        "completed_at_utc": datetime.now(timezone.utc),
    }
    receipt = StageReceipt(
        **receipt_values,
        receipt_hash=canonical_sha256(
            {
                key: value
                for key, value in receipt_values.items()
                if key not in {"started_at_utc", "completed_at_utc"}
            }
        ),
    )
    try:
        validate_receipt_resources(receipt, metrics)
    except ValueError as exc:
        raise L3StageError("L3_RESOURCE_BINDING_INVALID", str(exc)) from exc
    _persist_json(run_root / "stage-receipt.json", receipt)
    return L3StageResult(
        inputs=inputs,
        leaves=tuple(leaves),
        required_member_outcomes=outcomes,
        input_manifest=input_manifest,
        output_manifest=output_manifest,
        metrics=metrics,
        receipt=receipt,
        reused_leaf_count=reused,
        recomputed_leaf_count=recomputed,
        state_root=state_root,
        run_root=run_root,
    )


def _reuse_leaf(
    checkpoint_path: Path,
    batch_id: str,
    leaf_fingerprint: str,
) -> L3LeafResult | None:
    """Reuse only an intact leaf; a corrupt or stale leaf reruns on its own.

    Reuse requires the checkpoint's own canonical payload hash to recompute, the
    batch binding to match, and the leaf fingerprint to equal the fingerprint
    recomputed from the current inputs, validator, verifier, and sealed
    authorities. Any other checkpoint is discarded rather than trusted.
    """

    if not checkpoint_path.exists():
        return None
    try:
        raw = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        leaf = _leaf_from_dict(raw)
    except (
        OSError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValidationError,
        ValueError,
    ):
        return None
    if (
        leaf.extraction_candidate_batch_id != batch_id
        or leaf.leaf_fingerprint != leaf_fingerprint
    ):
        return None
    return leaf
