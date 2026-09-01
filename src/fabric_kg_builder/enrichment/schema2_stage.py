"""Schema-constrained L2 stage wiring activated by ``fabric-kg enrich``."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fabric_kg_builder.contracts.base import (
    canonical_json,
    canonical_sha256,
    deterministic_contract_id,
)
from fabric_kg_builder.contracts.extraction import ExtractionAuthorityReferences
from fabric_kg_builder.contracts.extraction import ExtractionCandidateBatch
from fabric_kg_builder.contracts.identity import CanonicalIdentityEnvelope
from fabric_kg_builder.contracts.receipts import (
    ArtifactEntry,
    ArtifactManifest,
    StageReceipt,
)
from fabric_kg_builder.contracts.resources import (
    StageResourceMetrics,
    validate_receipt_resources,
)
from fabric_kg_builder.domain.models import CompletenessRequirementV2
from fabric_kg_builder.domain.service import compute_contract_hash
from fabric_kg_builder.platform import process_resource_usage

from .schema2_extraction import (
    L2_EXTRACTOR_VERSION,
    L2_PROMPT_VERSION,
    ExtractionLeafResult,
    ProposedRequiredMemberSetView,
    build_candidate_batch,
    build_required_member_set_proposals,
    compile_closed_vocabulary,
    derive_collection_member_fragments,
    extraction_leaf_from_dict,
    extraction_leaf_to_dict,
    merge_candidate_batches,
    render_extraction_prompt,
)
from .schema2_sources import (
    L2_ACCEPTED_VERSIONS,
    L2_STAGE_NAME,
    L2Inputs,
    MaterializedCorpus,
    SourceCorpusReader,
    l2_input_fingerprint,
    load_l2_inputs,
    materialize_source_corpus,
)
from .schema2_work_units import (
    CandidateModelService,
    WorkUnitCheckpoint,
    execute_work_manifest,
    plan_work_units,
)

L2_RESPONSE_SCHEMA_HASH = canonical_sha256(
    {
        "candidate_kinds": ["entity", "property", "relationship"],
        "proposal_only": True,
        "verified_evidence_fields": False,
        "assertion_state_fields": False,
        "required_member_observation_fields": ["member_role_id", "member_order"],
    }
)


@dataclass(frozen=True)
class L2DryRunPlan:
    status: str
    corpus_entry_count: int
    complete_corpus_hash: str
    design_sample_entry_count: int
    domain_contract_hash: str
    hierarchy_hash: str
    identity_policy_hash: str
    completeness_requirement_hash: str
    max_relations_per_work_unit: int
    remote_calls: int
    writes: int


@dataclass(frozen=True)
class L2StageResult:
    inputs: L2Inputs
    materialized: MaterializedCorpus
    leaves: tuple[ExtractionLeafResult, ...]
    required_member_sets: tuple[ProposedRequiredMemberSetView, ...]
    input_manifest: ArtifactManifest
    output_manifest: ArtifactManifest
    metrics: StageResourceMetrics
    receipt: StageReceipt


def dry_run_l2(
    *,
    l1_state_root: Path = Path(".fkg") / "l1",
    domain_path: Path = Path("domain.yaml"),
) -> L2DryRunPlan:
    """Validate the L1 gate and plan L2 without adapters, writes, or remote calls."""

    inputs = load_l2_inputs(
        l1_state_root=l1_state_root,
        domain_path=domain_path,
    )
    contract = inputs.domain_contract
    compile_closed_vocabulary(contract)
    return L2DryRunPlan(
        status="planned",
        corpus_entry_count=inputs.corpus_manifest.total_entry_count,
        complete_corpus_hash=inputs.corpus_manifest.corpus_hash,
        design_sample_entry_count=len(inputs.design_sample_manifest.entries),
        domain_contract_hash=compute_contract_hash(contract),
        hierarchy_hash=contract.hierarchy_closure.hierarchy_hash,
        identity_policy_hash=contract.identity_policy_hash,
        completeness_requirement_hash=contract.completeness_requirement_hash,
        max_relations_per_work_unit=(
            contract.reasoning_policy.max_relations_per_work_unit
        ),
        remote_calls=0,
        writes=0,
    )


def _clean_identity(
    base: CanonicalIdentityEnvelope,
    *,
    contract_kind: str,
    prompt_version: str,
    prompt_hash: str,
    model_version: str,
    model_hash: str,
    extractor_name: str,
    extractor_version: str,
) -> CanonicalIdentityEnvelope:
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
            "prompt_version": prompt_version,
            "prompt_hash": prompt_hash,
            "model_version": model_version,
            "model_hash": model_hash,
            "extractor_name": extractor_name,
            "extractor_version": extractor_version,
        }
    )
    return CanonicalIdentityEnvelope.model_validate(values)


def _authority(
    inputs: L2Inputs,
    materialized: MaterializedCorpus,
    requirement: CompletenessRequirementV2 | None = None,
) -> ExtractionAuthorityReferences:
    requirement_id = (
        requirement.requirement_id
        if requirement is not None
        else deterministic_contract_id(
            "completeness-requirement-set",
            [
                item.requirement_id
                for item in inputs.domain_contract.completeness_requirements
            ],
        )
    )
    requirement_hash = (
        canonical_sha256(requirement.model_dump(mode="json"))
        if requirement is not None
        else inputs.domain_contract.completeness_requirement_hash
    )
    return ExtractionAuthorityReferences(
        source_corpus_manifest_id=inputs.corpus_manifest.source_corpus_manifest_id,
        source_corpus_manifest_hash=inputs.corpus_manifest.corpus_hash,
        source_unit_manifest_id=(
            materialized.source_unit_manifest.artifact_manifest_id
        ),
        source_unit_manifest_hash=materialized.source_unit_manifest.manifest_hash,
        domain_contract_hash=compute_contract_hash(inputs.domain_contract),
        completeness_requirement_id=requirement_id,
        completeness_requirement_hash=requirement_hash,
        hierarchy_hash=inputs.domain_contract.hierarchy_closure.hierarchy_hash,
        identity_policy_hash=inputs.domain_contract.identity_policy_hash,
    )


def _artifact_entry(
    *,
    artifact_id: str,
    contract_kind: str,
    contract_version: str,
    schema_hash: str,
    content_hash: str,
    payload: bytes,
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
        byte_count=len(payload),
        partition_count=partition_count,
        media_type="application/json",
        immutable_locator=None,
        blob_asset_ref_id=None,
    )


def _manifest(
    *,
    identity: CanonicalIdentityEnvelope,
    label: str,
    entries: tuple[ArtifactEntry, ...],
) -> ArtifactManifest:
    ordered = tuple(sorted(entries, key=lambda item: item.artifact_id))
    manifest_id = deterministic_contract_id(
        "artifact-manifest",
        {
            "stage_id": "L2",
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


def _persist_json(path: Path, payload: Any) -> bytes:
    encoded = (canonical_json(payload) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        current = path.read_bytes()
        if current != encoded:
            raise ValueError(f"immutable L2 artifact collision at {path}")
    else:
        temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temp.write_bytes(encoded)
        os.replace(temp, path)
    return encoded


def _input_manifest(
    *,
    inputs: L2Inputs,
    materialized: MaterializedCorpus,
    identity: CanonicalIdentityEnvelope,
    fingerprint: str,
) -> ArtifactManifest:
    authority_payload = {
        **inputs.authority_hashes,
        "l1_receipt_hash": inputs.l1_receipt.receipt_hash,
        "l1_output_manifest_hash": inputs.l1_output_manifest.manifest_hash,
        "source_corpus_manifest_hash": inputs.corpus_manifest.corpus_hash,
        "source_unit_manifest_hash": materialized.source_unit_manifest.manifest_hash,
        "l2_input_fingerprint": fingerprint,
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
    entries = (
        _artifact_entry(
            artifact_id=inputs.l1_receipt.stage_receipt_id,
            contract_kind="c0.stage_receipt",
            contract_version="1.0.0",
            schema_hash=canonical_sha256(StageReceipt.model_json_schema()),
            content_hash=inputs.l1_receipt.receipt_hash,
            payload=(canonical_json(inputs.l1_receipt) + "\n").encode("utf-8"),
            row_count=1,
        ),
        _artifact_entry(
            artifact_id=materialized.source_unit_manifest.artifact_manifest_id,
            contract_kind="c0.artifact_manifest",
            contract_version="1.0.0",
            schema_hash=canonical_sha256(ArtifactManifest.model_json_schema()),
            content_hash=materialized.source_unit_manifest.manifest_hash,
            payload=(
                canonical_json(materialized.source_unit_manifest) + "\n"
            ).encode("utf-8"),
            row_count=materialized.source_unit_manifest.total_row_count,
            canonical_id_set_hash=materialized.report.source_unit_id_set_hash,
        ),
        _artifact_entry(
            artifact_id="l2-authority-binding",
            contract_kind="l2.authority_binding",
            contract_version="1.0.0",
            schema_hash=canonical_sha256(
                {"contract_kind": "l2.authority_binding", "version": "1.0.0"}
            ),
            content_hash=canonical_sha256(authority_payload),
            payload=authority_bytes,
            row_count=1,
        ),
    )
    return _manifest(identity=identity, label="input", entries=entries)


def _output_artifacts(
    *,
    state_root: Path,
    materialized: MaterializedCorpus,
    leaves: tuple[ExtractionLeafResult, ...],
    required_member_sets: tuple[ProposedRequiredMemberSetView, ...],
    required_member_batches: tuple[ExtractionCandidateBatch, ...],
) -> tuple[ArtifactEntry, ...]:
    entries: list[ArtifactEntry] = []
    manifest_payload = _persist_json(
        state_root / "source-unit-manifest.json",
        materialized.source_unit_manifest,
    )
    entries.append(
        _artifact_entry(
            artifact_id=materialized.source_unit_manifest.artifact_manifest_id,
            contract_kind="c0.artifact_manifest",
            contract_version="1.0.0",
            schema_hash=canonical_sha256(ArtifactManifest.model_json_schema()),
            content_hash=materialized.source_unit_manifest.manifest_hash,
            payload=manifest_payload,
            row_count=materialized.source_unit_manifest.total_row_count,
            canonical_id_set_hash=materialized.report.source_unit_id_set_hash,
        )
    )
    report_payload = _persist_json(
        state_root / "corpus-materialization-report.json",
        {
            **materialized.report.__dict__,
            "dispositions": [
                item.__dict__ for item in materialized.report.dispositions
            ],
        },
    )
    entries.append(
        _artifact_entry(
            artifact_id="l2-corpus-materialization-report",
            contract_kind="l2.corpus_materialization_report",
            contract_version="1.0.0",
            schema_hash=canonical_sha256(
                {
                    "contract_kind": "l2.corpus_materialization_report",
                    "version": "1.0.0",
                }
            ),
            content_hash=materialized.report.report_hash,
            payload=report_payload,
            row_count=len(materialized.report.dispositions),
        )
    )
    for leaf in sorted(leaves, key=lambda item: item.batch.extraction_candidate_batch_id):
        batch_payload = _persist_json(
            state_root
            / "candidate-batches"
            / f"{leaf.batch.extraction_candidate_batch_id.replace(':', '-', 1)}.json",
            leaf.batch,
        )
        entries.append(
            _artifact_entry(
                artifact_id=leaf.batch.extraction_candidate_batch_id,
                contract_kind="c0.extraction_candidate_batch",
                contract_version="1.0.0",
                schema_hash=canonical_sha256(
                    type(leaf.batch).model_json_schema()
                ),
                content_hash=leaf.batch.batch_hash,
                payload=batch_payload,
                row_count=leaf.batch.retained_candidate_count,
                canonical_id_set_hash=leaf.batch.candidate_id_set_hash,
            )
        )
        leaf_payload = extraction_leaf_to_dict(leaf)
        proposal_hash = canonical_sha256(
            leaf_payload["proposed_candidates"]
        )
        proposal_payload = _persist_json(
            state_root
            / "proposed-candidates"
            / f"{leaf.batch.extraction_candidate_batch_id.replace(':', '-', 1)}.json",
            leaf_payload["proposed_candidates"],
        )
        entries.append(
            _artifact_entry(
                artifact_id=f"{leaf.batch.extraction_candidate_batch_id}:proposals",
                contract_kind="l2.proposed_candidate_partition",
                contract_version="1.0.0",
                schema_hash=L2_RESPONSE_SCHEMA_HASH,
                content_hash=proposal_hash,
                payload=proposal_payload,
                row_count=len(leaf.proposed_candidates),
                canonical_id_set_hash=canonical_sha256(
                    sorted(item.candidate_id for item in leaf.proposed_candidates)
                ),
            )
        )
        lifecycle_payload = _persist_json(
            state_root
            / "lifecycle"
            / f"{leaf.batch.extraction_candidate_batch_id.replace(':', '-', 1)}.json",
            [record.model_dump(mode="json") for record in leaf.lifecycle_records],
        )
        entries.append(
            _artifact_entry(
                artifact_id=f"{leaf.batch.extraction_candidate_batch_id}:lifecycle",
                contract_kind="c0.candidate_lifecycle_record",
                contract_version="1.0.0",
                schema_hash=canonical_sha256(
                    leaf.lifecycle_records[0].model_json_schema()
                    if leaf.lifecycle_records
                    else {"empty": True}
                ),
                content_hash=canonical_sha256(
                    [
                        record.model_dump(mode="json")
                        for record in leaf.lifecycle_records
                    ]
                ),
                payload=lifecycle_payload,
                row_count=len(leaf.lifecycle_records),
                canonical_id_set_hash=canonical_sha256(
                    sorted(
                        record.lifecycle_record_id
                        for record in leaf.lifecycle_records
                    )
                ),
            )
        )
    batches_by_id = {
        batch.extraction_candidate_batch_id: batch
        for batch in required_member_batches
    }
    for view in sorted(
        required_member_sets,
        key=lambda item: item.proposal.required_member_set_proposal_id,
    ):
        proposal = view.proposal
        batch = batches_by_id[proposal.extraction_candidate_batch_id]
        batch_payload = _persist_json(
            state_root
            / "required-member-candidate-batches"
            / f"{batch.extraction_candidate_batch_id.replace(':', '-', 1)}.json",
            batch,
        )
        entries.append(
            _artifact_entry(
                artifact_id=batch.extraction_candidate_batch_id,
                contract_kind="c0.extraction_candidate_batch",
                contract_version="1.0.0",
                schema_hash=canonical_sha256(
                    ExtractionCandidateBatch.model_json_schema()
                ),
                content_hash=batch.batch_hash,
                payload=batch_payload,
                row_count=batch.retained_candidate_count,
                canonical_id_set_hash=batch.candidate_id_set_hash,
            )
        )
        proposal_payload = _persist_json(
            state_root
            / "required-member-proposals"
            / f"{proposal.required_member_set_proposal_id.replace(':', '-', 1)}.json",
            proposal,
        )
        entries.append(
            _artifact_entry(
                artifact_id=proposal.required_member_set_proposal_id,
                contract_kind="c0.required_member_set_proposal",
                contract_version="1.1.0",
                schema_hash=canonical_sha256(type(proposal).model_json_schema()),
                content_hash=proposal.proposal_hash,
                payload=proposal_payload,
                row_count=len(proposal.members),
                canonical_id_set_hash=canonical_sha256(
                    sorted(member.member_canonical_id for member in proposal.members)
                ),
            )
        )
        view_payload = {
            key: value
            for key, value in view.__dict__.items()
            if key not in {"proposal", "view_hash"}
        }
        view_payload["proposal_hash"] = proposal.proposal_hash
        persisted_view = _persist_json(
            state_root
            / "required-member-views"
            / f"{proposal.required_member_set_proposal_id.replace(':', '-', 1)}.json",
            view_payload,
        )
        entries.append(
            _artifact_entry(
                artifact_id=f"{proposal.required_member_set_proposal_id}:view",
                contract_kind="l2.required_member_set_view",
                contract_version="1.1.0",
                schema_hash=canonical_sha256(
                    {
                        "contract_kind": "l2.required_member_set_view",
                        "version": "1.1.0",
                    }
                ),
                content_hash=view.view_hash,
                payload=persisted_view,
                row_count=len(view.member_entity_ids),
                canonical_id_set_hash=canonical_sha256(
                    sorted(view.member_entity_ids)
                ),
            )
        )
    return tuple(entries)


def _resource_metrics(
    *,
    identity: CanonicalIdentityEnvelope,
    inputs: L2Inputs,
    materialized: MaterializedCorpus,
    model_calls: int,
    cache_hits: int,
    storage_write_bytes: int,
    started: float,
) -> StageResourceMetrics:
    usage = process_resource_usage()
    metrics_id = deterministic_contract_id(
        "stage-resource-metrics",
        {
            "stage_id": "L2",
            "run_id": identity.run_id,
            "source_unit_manifest_hash": (
                materialized.source_unit_manifest.manifest_hash
            ),
        },
    )
    values = {
        "identity": identity.model_copy(
            update={"contract_kind": "c0.stage_resource_metrics"}
        ),
        "resource_metrics_id": metrics_id,
        "stage_id": "L2",
        "stage_name": L2_STAGE_NAME,
        "wall_ms": max(0, int((time.perf_counter() - started) * 1000)),
        "cpu_ms": max(0, int(usage.cpu_seconds * 1000)),
        "peak_rss_bytes": usage.peak_rss_bytes,
        "storage_read_bytes": inputs.corpus_manifest.total_byte_count,
        "storage_write_bytes": storage_write_bytes,
        "network_request_bytes": 0,
        "network_response_bytes": 0,
        "source_units_read": len(materialized.source_units),
        "source_units_written": len(materialized.source_units),
        "source_units_skipped": 0,
        "document_intelligence_calls": 0,
        "document_intelligence_pages": 0,
        "foundry_calls": model_calls,
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
        "cache_hits": cache_hits,
        "cache_misses": model_calls,
        "max_observed_concurrency": 1,
        "budget_snapshot_hash": canonical_sha256(
            {
                "max_relations_per_work_unit": (
                    inputs.domain_contract.reasoning_policy.max_relations_per_work_unit
                ),
                "numeric_performance_thresholds": None,
            }
        ),
        "exceeded_dimensions": (),
    }
    return StageResourceMetrics(**values, metrics_hash=canonical_sha256(values))


def run_l2(
    *,
    reader: SourceCorpusReader,
    service: CandidateModelService,
    state_root: Path = Path(".fkg") / "l2",
    l1_state_root: Path = Path(".fkg") / "l1",
    domain_path: Path = Path("domain.yaml"),
    prompt_version: str = L2_PROMPT_VERSION,
    prompt_hash: str,
    model_version: str,
    model_hash: str,
    extractor_name: str = "schema2-extractor",
    extractor_version: str = L2_EXTRACTOR_VERSION,
    classifier_version: str = "closed-vocabulary/1.0.0",
    max_concurrent: int = 1,
    service_batch_size: int = 1,
) -> L2StageResult:
    """Run L2 only; no L3 validation, evidence minting, or publication occurs."""

    started = time.perf_counter()
    started_at_utc = datetime.now(timezone.utc)
    inputs = load_l2_inputs(
        l1_state_root=l1_state_root,
        domain_path=domain_path,
    )
    vocabulary = compile_closed_vocabulary(inputs.domain_contract)
    materialized = materialize_source_corpus(inputs, reader)
    identity = _clean_identity(
        inputs.l1_receipt.identity,
        contract_kind="l2.stage",
        prompt_version=prompt_version,
        prompt_hash=prompt_hash,
        model_version=model_version,
        model_hash=model_hash,
        extractor_name=extractor_name,
        extractor_version=extractor_version,
    )
    fingerprint = l2_input_fingerprint(
        inputs,
        materialized.source_unit_manifest,
        prompt_version=prompt_version,
        prompt_hash=prompt_hash,
        model_version=model_version,
        model_hash=model_hash,
        extractor_name=extractor_name,
        extractor_version=extractor_version,
        response_schema_hash=L2_RESPONSE_SCHEMA_HASH,
        split_policy_version="paragraph-sentence-token/1.0.0",
    )
    input_manifest = _input_manifest(
        inputs=inputs,
        materialized=materialized,
        identity=identity,
        fingerprint=fingerprint,
    )
    _persist_json(state_root / "input-manifest.json", input_manifest)
    for source_unit in materialized.source_units:
        _persist_json(
            state_root
            / "source-units"
            / f"{source_unit.source_unit_id.replace(':', '-', 1)}.json",
            source_unit,
        )
    roots = plan_work_units(
        materialized.source_units,
        pass_name="schema-constrained-extraction",
        authority_fingerprint=fingerprint,
    )
    authority = _authority(inputs, materialized)
    checkpoint = WorkUnitCheckpoint(
        state_root / "checkpoint.json",
        state_root / "checkpoint-leaves",
    )

    def prompt_builder(work_unit):
        return render_extraction_prompt(
            vocabulary,
            source_unit_id=work_unit.source_unit_id,
            source_text_hash=work_unit.source_text_hash,
            source_text=work_unit.anchored_text,
            slice_start=work_unit.slice_start,
            slice_end=work_unit.slice_end,
        )

    def processor(work_unit, response):
        candidates = (
            response.get("candidates")
            if isinstance(response, dict)
            else response
        )
        leaf = build_candidate_batch(
            candidates,
            vocabulary=vocabulary,
            contract=inputs.domain_contract,
            authority=authority,
            base_identity=identity,
            source_unit_id=work_unit.source_unit_id,
            work_unit_id=work_unit.work_unit_id,
            classifier_version=classifier_version,
            prompt_hash=prompt_hash,
            model_hash=model_hash,
            extractor_name=extractor_name,
            extractor_version=extractor_version,
            occurred_at_utc=inputs.l1_receipt.completed_at_utc,
        )
        return extraction_leaf_to_dict(leaf)

    executions = execute_work_manifest(
        roots,
        service=service,
        prompt_builder=prompt_builder,
        processor=processor,
        checkpoint=checkpoint,
        max_relations_per_work_unit=(
            inputs.domain_contract.reasoning_policy.max_relations_per_work_unit
        ),
        max_concurrent=max_concurrent,
        service_batch_size=service_batch_size,
    )
    leaves = tuple(
        extraction_leaf_from_dict(result)
        for execution in executions
        for result in execution.leaf_results
    )
    fragments = derive_collection_member_fragments(
        leaves,
        contract=inputs.domain_contract,
    )
    required_member_sets = build_required_member_set_proposals(
        fragments,
        leaves=leaves,
        contract=inputs.domain_contract,
        authority_factory=lambda requirement: _authority(
            inputs,
            materialized,
            requirement,
        ),
        base_identity=identity,
    )
    requirements_by_id = {
        requirement.requirement_id: requirement
        for requirement in inputs.domain_contract.completeness_requirements
    }
    required_member_batches = tuple(
        merge_candidate_batches(
            leaves,
            authority=_authority(
                inputs,
                materialized,
                requirements_by_id[view.requirement_id],
            ),
            base_identity=identity,
            merge_key=f"{view.requirement_id}:{view.aggregate_entity_id}",
        )
        for view in required_member_sets
    )
    if any(
        view.proposal.extraction_candidate_batch_id
        != batch.extraction_candidate_batch_id
        or view.proposal.extraction_candidate_batch_hash != batch.batch_hash
        for view, batch in zip(required_member_sets, required_member_batches)
    ):
        raise ValueError("required-member proposal batch binding is not reproducible")
    output_entries = _output_artifacts(
        state_root=state_root,
        materialized=materialized,
        leaves=leaves,
        required_member_sets=required_member_sets,
        required_member_batches=required_member_batches,
    )
    output_manifest = _manifest(
        identity=identity,
        label="output",
        entries=output_entries,
    )
    output_payload = _persist_json(
        state_root / "output-manifest.json",
        output_manifest,
    )
    prior_receipt_path = state_root / "stage-receipt.json"
    prior_metrics_path = state_root / "resource-metrics.json"
    if prior_receipt_path.exists() and prior_metrics_path.exists():
        try:
            prior_receipt = StageReceipt.model_validate_json(
                prior_receipt_path.read_text(encoding="utf-8")
            )
            prior_metrics = StageResourceMetrics.model_validate_json(
                prior_metrics_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise ValueError("corrupt persisted L2 skip artifacts") from exc
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
            validate_receipt_resources(prior_receipt, prior_metrics)
            return L2StageResult(
                inputs=inputs,
                materialized=materialized,
                leaves=leaves,
                required_member_sets=required_member_sets,
                input_manifest=input_manifest,
                output_manifest=output_manifest,
                metrics=prior_metrics,
                receipt=prior_receipt,
            )
        raise ValueError("persisted L2 receipt does not satisfy exact skip bindings")
    if prior_receipt_path.exists() and not prior_metrics_path.exists():
        raise ValueError("persisted L2 receipt is missing its bound resource metrics")

    storage_write_bytes = (
        output_manifest.total_byte_count + len(output_payload)
    )
    if prior_metrics_path.exists():
        try:
            metrics = StageResourceMetrics.model_validate_json(
                prior_metrics_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise ValueError("corrupt persisted L2 resource metrics") from exc
        expected_metrics_identity = identity.model_copy(
            update={"contract_kind": "c0.stage_resource_metrics"}
        )
        if (
            metrics.identity != expected_metrics_identity
            or metrics.stage_id != "L2"
            or metrics.stage_name != L2_STAGE_NAME
            or metrics.source_units_read != len(materialized.source_units)
            or metrics.source_units_written != len(materialized.source_units)
        ):
            raise ValueError(
                "persisted L2 resource metrics do not match exact resume bindings"
            )
    else:
        metrics = _resource_metrics(
            identity=identity,
            inputs=inputs,
            materialized=materialized,
            model_calls=sum(item.model_call_count for item in executions),
            cache_hits=sum(item.reused_leaf_count for item in executions),
            storage_write_bytes=storage_write_bytes,
            started=started,
        )
        _persist_json(state_root / "resource-metrics.json", metrics)
    receipt_values = {
        "identity": identity.model_copy(
            update={"contract_kind": "c0.stage_receipt"}
        ),
        "stage_receipt_id": deterministic_contract_id(
            "stage-receipt",
            {"stage_id": "L2", "run_id": identity.run_id, "skip_key": fingerprint},
        ),
        "stage_id": "L2",
        "stage_name": L2_STAGE_NAME,
        "stage_contract_version": "1.0.0",
        "status": "succeeded",
        "input_manifest_id": input_manifest.artifact_manifest_id,
        "input_manifest_hash": input_manifest.manifest_hash,
        "output_manifest_id": output_manifest.artifact_manifest_id,
        "output_manifest_hash": output_manifest.manifest_hash,
        "skip_key": fingerprint,
        "accepted_contract_versions": L2_ACCEPTED_VERSIONS,
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
    validate_receipt_resources(receipt, metrics)
    _persist_json(state_root / "stage-receipt.json", receipt)
    return L2StageResult(
        inputs=inputs,
        materialized=materialized,
        leaves=leaves,
        required_member_sets=required_member_sets,
        input_manifest=input_manifest,
        output_manifest=output_manifest,
        metrics=metrics,
        receipt=receipt,
    )
