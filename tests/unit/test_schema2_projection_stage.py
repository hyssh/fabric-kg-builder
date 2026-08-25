"""Isolated L4 audit and asserted-only serving projection tests."""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pyarrow.parquet as pq
import pyarrow as pa
import pytest

from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
from fabric_kg_builder.contracts.extraction import (
    RequiredMemberManifestV1_1,
    RequiredMemberReferenceV1_1,
    RequiredMemberSetProposalV1_1,
    _member_hashes_v1_1,
    authoritative_collection_hash_v1_1,
)
from fabric_kg_builder.contracts.lifecycle import AssertionState
from fabric_kg_builder.contracts.projection import AuditProjection
from fabric_kg_builder.contracts.publication import (
    ProjectionEquivalence,
    ProjectionEvidence,
)
from fabric_kg_builder.contracts.receipts import ArtifactManifest, StageReceipt
from fabric_kg_builder.contracts.resources import StageResourceMetrics
from fabric_kg_builder.enrichment import schema2_validation_stage
from fabric_kg_builder.model.arrow_schemas import L4_PROJECTION_TABLE_SCHEMAS
from fabric_kg_builder.semantic.source_tables import (
    L4_ACCEPTED_VERSIONS,
    SealedL4ServingSource,
    require_l5_publication_receipt,
    resolve_semantic_source_parquet,
)
from fabric_kg_builder.serving import lifecycle_projection
from fabric_kg_builder.serving.lifecycle_projection import (
    L4ProjectionError,
    project_required_member_manifests,
    project_required_members,
    run_l4,
    validate_required_member_projection,
)
from tests.unit.test_schema2_validation_stage import (
    _Service,
    _approved_l1,
    _fact_set,
    _l3,
    _pipeline,
    _run_l2,
    _subtypes,
)


def _all_lifecycle_mutation(candidates, work_unit):
    duplicate = dict(candidates[1])
    unknown = {
        "candidate_kind": "entity",
        "local_id": "unknown-1",
        "observed_type": "InventedThing",
        "label": "Invented",
        "aliases": [],
        "identity_key": {},
        "stable_source_identity": None,
        "anchors": [{
            "span_start": work_unit.slice_start,
            "span_end": work_unit.slice_start + 1,
            "quote": work_unit.text[:1],
            "model_authored_evidence_id": None,
        }],
    }
    no_evidence = {
        **duplicate,
        "local_id": "no-evidence-1",
        "label": "No Evidence",
        "anchors": [],
    }
    bad_quote = {
        **duplicate,
        "local_id": "bad-quote-1",
        "label": "Bad Quote",
        "anchors": [{
            "span_start": work_unit.slice_start,
            "span_end": work_unit.slice_start + 5,
            "quote": "zzzzz",
            "model_authored_evidence_id": None,
        }],
    }
    return candidates + [duplicate, unknown, no_evidence, bad_quote]


def _replace_manifest_entry(
    manifest: ArtifactManifest,
    artifact_id: str,
    **updates,
) -> ArtifactManifest:
    entries = tuple(
        entry.model_copy(update=updates)
        if entry.artifact_id == artifact_id
        else entry
        for entry in manifest.entries
    )
    values = {
        "identity": manifest.identity,
        "artifact_manifest_id": manifest.artifact_manifest_id,
        "entries": entries,
        "total_row_count": sum(entry.row_count or 0 for entry in entries),
        "total_byte_count": sum(entry.byte_count for entry in entries),
    }
    return ArtifactManifest(
        **values,
        manifest_hash=canonical_sha256(values),
    )


def _manifest_with_entries(
    manifest: ArtifactManifest,
    entries: tuple,
) -> ArtifactManifest:
    ordered = tuple(sorted(entries, key=lambda entry: entry.artifact_id))
    values = {
        "identity": manifest.identity,
        "artifact_manifest_id": manifest.artifact_manifest_id,
        "entries": ordered,
        "total_row_count": sum(entry.row_count or 0 for entry in ordered),
        "total_byte_count": sum(entry.byte_count for entry in ordered),
    }
    return ArtifactManifest(
        **values,
        manifest_hash=canonical_sha256(values),
    )


def _rewrite_l4_artifact(
    result,
    *,
    artifact_id: str,
    file_name: str,
    payload: bytes,
) -> tuple[ArtifactManifest, StageResourceMetrics, StageReceipt]:
    manifest = _replace_manifest_entry(
        result.output_manifest,
        artifact_id,
        content_hash=hashlib.sha256(payload).hexdigest(),
        byte_count=len(payload),
    )
    manifest_payload = (canonical_json(manifest) + "\n").encode("utf-8")
    metrics_values = result.metrics.model_dump(
        mode="python",
        exclude={"metrics_hash"},
    )
    metrics_values["storage_write_bytes"] = (
        manifest.total_byte_count + len(manifest_payload)
    )
    metrics = StageResourceMetrics(
        **metrics_values,
        metrics_hash=canonical_sha256(metrics_values),
    )
    receipt_values = result.receipt.model_dump(
        mode="python",
        exclude={"receipt_hash"},
    )
    receipt_values.update({
        "output_manifest_hash": manifest.manifest_hash,
        "resource_metrics_hash": metrics.metrics_hash,
    })
    receipt = StageReceipt(
        **receipt_values,
        receipt_hash=canonical_sha256({
            key: value
            for key, value in receipt_values.items()
            if key not in {"started_at_utc", "completed_at_utc"}
        }),
    )
    (result.run_root / file_name).write_bytes(payload)
    (result.run_root / "output-manifest.json").write_bytes(manifest_payload)
    (result.run_root / "resource-metrics.json").write_text(
        canonical_json(metrics) + "\n",
        encoding="utf-8",
    )
    (result.run_root / "stage-receipt.json").write_text(
        canonical_json(receipt) + "\n",
        encoding="utf-8",
    )
    return manifest, metrics, receipt


def _rewrite_l4_artifacts(
    result,
    artifacts: tuple[tuple[str, str, bytes, dict[str, object]], ...],
) -> tuple[ArtifactManifest, StageResourceMetrics, StageReceipt]:
    manifest = result.output_manifest
    for artifact_id, file_name, payload, entry_updates in artifacts:
        manifest = _replace_manifest_entry(
            manifest,
            artifact_id,
            content_hash=hashlib.sha256(payload).hexdigest(),
            byte_count=len(payload),
            **entry_updates,
        )
        (result.run_root / file_name).write_bytes(payload)
    manifest_payload = (canonical_json(manifest) + "\n").encode("utf-8")
    metrics_values = result.metrics.model_dump(
        mode="python",
        exclude={"metrics_hash"},
    )
    metrics_values["storage_write_bytes"] = (
        manifest.total_byte_count + len(manifest_payload)
    )
    metrics = StageResourceMetrics(
        **metrics_values,
        metrics_hash=canonical_sha256(metrics_values),
    )
    receipt_values = result.receipt.model_dump(
        mode="python",
        exclude={"receipt_hash"},
    )
    receipt_values.update({
        "output_manifest_hash": manifest.manifest_hash,
        "resource_metrics_hash": metrics.metrics_hash,
    })
    receipt = StageReceipt(
        **receipt_values,
        receipt_hash=canonical_sha256({
            key: value
            for key, value in receipt_values.items()
            if key not in {"started_at_utc", "completed_at_utc"}
        }),
    )
    (result.run_root / "output-manifest.json").write_bytes(manifest_payload)
    (result.run_root / "resource-metrics.json").write_text(
        canonical_json(metrics) + "\n",
        encoding="utf-8",
    )
    (result.run_root / "stage-receipt.json").write_text(
        canonical_json(receipt) + "\n",
        encoding="utf-8",
    )
    return manifest, metrics, receipt


def _rewrite_required_member_artifacts(
    result,
    manifest_rows: list[dict[str, object]],
    member_rows: list[dict[str, object]],
) -> tuple[ArtifactManifest, StageResourceMetrics, StageReceipt]:
    manifest_path = (
        result.run_root / "semantic_required_member_manifests.parquet"
    )
    member_path = result.run_root / "semantic_required_members.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            manifest_rows,
            schema=L4_PROJECTION_TABLE_SCHEMAS[
                "semantic_required_member_manifests"
            ],
        ),
        manifest_path,
        compression="zstd",
        version="2.6",
        use_dictionary=False,
        write_statistics=True,
    )
    pq.write_table(
        pa.Table.from_pylist(
            member_rows,
            schema=L4_PROJECTION_TABLE_SCHEMAS["semantic_required_members"],
        ),
        member_path,
        compression="zstd",
        version="2.6",
        use_dictionary=False,
        write_statistics=True,
    )
    manifest_row = manifest_rows[0]
    ordered_members = sorted(
        member_rows,
        key=lambda row: int(row["manifest_member_index"]),
    )
    composite_ids = sorted(
        f"{row['required_member_manifest_id']}|{row['member_canonical_id']}"
        for row in ordered_members
    )
    evidence = ProjectionEvidence(
        count=len(ordered_members),
        canonical_id_set_hash=canonical_sha256(composite_ids),
        row_fingerprint=canonical_sha256({
            "manifest": manifest_row,
            "members": ordered_members,
        }),
    )
    original = result.projection_equivalences[0]
    authority = original.authority.model_copy(update={
        "required_member_manifest_hash": manifest_row["manifest_hash"],
        "authoritative_collection_hash": manifest_row[
            "authoritative_collection_hash"
        ],
    })
    proof_values = original.model_dump(
        mode="python",
        exclude={"equivalence_hash"},
    )
    proof_values.update({
        "authority": authority,
        "publication_crosswalk_hash": canonical_sha256({
            "publication_crosswalk_id": original.publication_crosswalk_id,
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
        }),
        "expected": evidence,
        "compiled": evidence,
        "deployed": evidence,
        "read_back": evidence,
    })
    proof = ProjectionEquivalence(
        **proof_values,
        equivalence_hash=canonical_sha256(proof_values),
    )
    proof_payload = (canonical_json((proof,)) + "\n").encode("utf-8")
    return _rewrite_l4_artifacts(
        result,
        (
            (
                "l4-table:semantic_required_member_manifests",
                "semantic_required_member_manifests.parquet",
                manifest_path.read_bytes(),
                {
                    "canonical_id_set_hash": canonical_sha256(sorted({
                        str(row["required_member_manifest_id"])
                        for row in manifest_rows
                    })),
                    "row_count": len(manifest_rows),
                },
            ),
            (
                "l4-table:semantic_required_members",
                "semantic_required_members.parquet",
                member_path.read_bytes(),
                {
                    "canonical_id_set_hash": canonical_sha256(composite_ids),
                    "row_count": len(member_rows),
                },
            ),
            (
                "l4-parquet-projection-equivalence",
                "projection-equivalence.json",
                proof_payload,
                {"row_count": 1},
            ),
        ),
    )


@pytest.mark.unit
def test_l4_emits_complete_audit_and_asserted_only_serving(tmp_path: Path) -> None:
    l1_root, domain_path, _ = _pipeline(
        tmp_path,
        "records",
        mutate=_all_lifecycle_mutation,
    )
    l3 = _l3(tmp_path, l1_root, domain_path)

    result = run_l4(l3, state_root=tmp_path / ".fkg" / "l4")

    audit = result.audit_projection
    assert audit.input_candidate_count == (
        audit.retained_candidate_count + audit.deduplicated_input_count
    )
    assert audit.deduplicated_input_count == 1
    assert sum(audit.lifecycle_state_counts.values()) == audit.retained_candidate_count
    assert {
        state for state, count in audit.lifecycle_state_counts.items() if count
    } == {
        AssertionState.ASSERTED,
        AssertionState.DISCOVERY,
        AssertionState.UNRESOLVED,
        AssertionState.REJECTED,
        AssertionState.UNSUPPORTED,
    }
    assert len(result.rows.audit_candidates) == audit.input_candidate_count
    assert result.serving_projection.included_states == (AssertionState.ASSERTED,)
    asserted_ids = {
        item.semantic_id
        for item in l3.candidate_results
        if item.candidate_kind == "entity"
        and item.current_state == AssertionState.ASSERTED.value
    }
    assert set(result.serving_projection.entity_assertion_ids) == asserted_ids
    assert not result.serving_projection.relationship_assertion_ids
    assert not result.serving_projection.property_assertion_ids
    assert all(
        row["entity_id"] in asserted_ids
        for row in result.rows.semantic_asserted_entities
    )
    assert all(
        row["most_specific_type_id"] in row["asserted_type_ids"]
        for row in result.rows.semantic_asserted_entities
    )
    assert result.receipt.status == "succeeded"
    assert result.receipt.stage_id == "L4"
    assert not result.receipt.remote_operation_refs
    assert result.metrics.network_request_bytes == 0
    assert result.metrics.fabric_calls == 0
    assert result.metrics.search_calls == 0


@pytest.mark.unit
def test_l4_parquet_is_typed_deterministic_resumable_and_corruption_safe(
    tmp_path: Path,
) -> None:
    l1_root, domain_path, _ = _pipeline(
        tmp_path,
        "records",
        mutate=_all_lifecycle_mutation,
    )
    l3 = _l3(tmp_path, l1_root, domain_path)
    state_root = tmp_path / ".fkg" / "l4"

    first = run_l4(l3, state_root=state_root)
    first_hashes = {
        entry.artifact_id: entry.content_hash
        for entry in first.output_manifest.entries
    }
    for table_name, schema in L4_PROJECTION_TABLE_SCHEMAS.items():
        assert pq.read_schema(first.run_root / f"{table_name}.parquet") == schema
    type_entry = next(
        entry
        for entry in first.output_manifest.entries
        if entry.artifact_id == "l4-table:semantic_entity_type_assertions"
    )
    assert type_entry.canonical_id_set_hash == canonical_sha256(sorted({
        f"{row['entity_id']}|{row['semantic_type_id']}"
        for row in first.rows.semantic_entity_type_assertions
    }))

    reused = run_l4(l3, state_root=state_root)
    assert reused.reused
    assert reused.receipt.receipt_hash == first.receipt.receipt_hash
    assert reused.output_manifest.manifest_hash == first.output_manifest.manifest_hash

    receipt_values = first.receipt.model_dump(
        mode="python",
        exclude={"receipt_hash"},
    )
    receipt_values["accepted_contract_versions"] = {
        "stale.contract": "1.0.0"
    }
    stale_receipt = StageReceipt(
        **receipt_values,
        receipt_hash=canonical_sha256({
            key: value
            for key, value in receipt_values.items()
            if key not in {"started_at_utc", "completed_at_utc"}
        }),
    )
    (first.run_root / "stage-receipt.json").write_text(
        canonical_json(stale_receipt) + "\n",
        encoding="utf-8",
    )
    assert not run_l4(l3, state_root=state_root).reused

    corrupt_path = first.run_root / "semantic_asserted_entities.parquet"
    corrupt_path.write_bytes(corrupt_path.read_bytes() + b"corrupt")
    repaired = run_l4(l3, state_root=state_root)
    assert not repaired.reused
    assert {
        entry.artifact_id: entry.content_hash
        for entry in repaired.output_manifest.entries
    } == first_hashes
    assert run_l4(l3, state_root=state_root).reused


@pytest.mark.unit
def test_l4_rejects_reinterpreted_candidate_results(tmp_path: Path) -> None:
    l1_root, domain_path, _ = _pipeline(tmp_path, "records")
    l3 = _l3(tmp_path, l1_root, domain_path)
    leaf = l3.leaves[0]
    entity_index = next(
        index
        for index, result in enumerate(leaf.candidate_results)
        if result.candidate_kind == "entity"
    )
    original = leaf.candidate_results[entity_index]

    for update in (
        {"semantic_id": "entity:forged"},
        {"approved_semantic_id": "semantic-type:forged"},
        {"identity_witness_kind": "forged-witness"},
        {"ignored_model_evidence_id": "model-evidence:forged"},
        {"resolved_source_entity_id": "entity:forged-source"},
    ):
        candidate_results = list(leaf.candidate_results)
        candidate_results[entity_index] = dataclasses.replace(original, **update)
        forged = dataclasses.replace(
            l3,
            leaves=(
                dataclasses.replace(
                    leaf,
                    candidate_results=tuple(candidate_results),
                ),
                *l3.leaves[1:],
            ),
        )
        with pytest.raises(
            L4ProjectionError,
            match="L4_INPUT_MANIFEST_INVALID|L4_LIFECYCLE_INCOMPLETE",
        ):
            run_l4(forged, state_root=tmp_path / ".fkg" / "l4-forged")


@pytest.mark.unit
def test_l4_rejects_reinterpreted_candidate_accounting(tmp_path: Path) -> None:
    l1_root, domain_path, _ = _pipeline(
        tmp_path,
        "records",
        mutate=_all_lifecycle_mutation,
    )
    l3 = _l3(tmp_path, l1_root, domain_path)
    batch = l3.inputs.candidate_batches[0]
    disposition_index = next(
        index
        for index, disposition in enumerate(batch.candidate_dispositions)
        if disposition.disposition == "deduplicated"
    )
    dispositions = list(batch.candidate_dispositions)
    dispositions[disposition_index] = dispositions[disposition_index].model_copy(
        update={"reason_codes": ("FORGED_ACCOUNTING_REASON",)}
    )
    forged_batch = type(batch).model_construct(
        **{
            **batch.__dict__,
            "candidate_dispositions": tuple(dispositions),
        }
    )
    forged_inputs = dataclasses.replace(
        l3.inputs,
        candidate_batches=(forged_batch, *l3.inputs.candidate_batches[1:]),
    )

    with pytest.raises(
        L4ProjectionError,
        match="L4_INPUT_MANIFEST_INVALID",
    ):
        run_l4(
            dataclasses.replace(l3, inputs=forged_inputs),
            state_root=tmp_path / ".fkg" / "l4-forged-accounting",
        )


@pytest.mark.unit
def test_l4_rejects_upstream_artifact_metadata_mutations(tmp_path: Path) -> None:
    l1_root, domain_path, _ = _pipeline(tmp_path, "records")
    l3 = _l3(tmp_path, l1_root, domain_path)
    batch_id = l3.inputs.leaf_batch_ids[0]
    forged_l2_manifest = _replace_manifest_entry(
        l3.inputs.l2_output_manifest,
        f"{batch_id}:proposals",
        schema_hash="0" * 64,
    )
    with pytest.raises(L4ProjectionError, match="L4_INPUT_MANIFEST_INVALID"):
        run_l4(
            dataclasses.replace(
                l3,
                inputs=dataclasses.replace(
                    l3.inputs,
                    l2_output_manifest=forged_l2_manifest,
                ),
            ),
            state_root=tmp_path / ".fkg" / "l4-forged-l2-metadata",
        )

    forged_l3_manifest = _replace_manifest_entry(
        l3.output_manifest,
        f"{batch_id}:classifications",
        contract_version="9.9.9",
    )
    receipt_values = l3.receipt.model_dump(
        mode="python",
        exclude={"receipt_hash"},
    )
    receipt_values["output_manifest_hash"] = forged_l3_manifest.manifest_hash
    forged_receipt = StageReceipt(
        **receipt_values,
        receipt_hash=canonical_sha256({
            key: value
            for key, value in receipt_values.items()
            if key not in {"started_at_utc", "completed_at_utc"}
        }),
    )
    with pytest.raises(L4ProjectionError, match="L4_INPUT_MANIFEST_INVALID"):
        run_l4(
            dataclasses.replace(
                l3,
                output_manifest=forged_l3_manifest,
                receipt=forged_receipt,
            ),
            state_root=tmp_path / ".fkg" / "l4-forged-l3-metadata",
        )


@pytest.mark.unit
def test_l4_resume_rejects_foreign_receipt_and_metrics_lineage(
    tmp_path: Path,
) -> None:
    l1_root, domain_path, _ = _pipeline(tmp_path, "records")
    l3 = _l3(tmp_path, l1_root, domain_path)
    state_root = tmp_path / ".fkg" / "l4"
    first = run_l4(l3, state_root=state_root)
    metrics_values = first.metrics.model_dump(
        mode="python",
        exclude={"metrics_hash"},
    )
    metrics_values["storage_write_bytes"] += 1
    wrong_write_metrics = StageResourceMetrics(
        **metrics_values,
        metrics_hash=canonical_sha256(metrics_values),
    )
    receipt_values = first.receipt.model_dump(
        mode="python",
        exclude={"receipt_hash"},
    )
    receipt_values["resource_metrics_hash"] = wrong_write_metrics.metrics_hash
    wrong_write_receipt = StageReceipt(
        **receipt_values,
        receipt_hash=canonical_sha256({
            key: value
            for key, value in receipt_values.items()
            if key not in {"started_at_utc", "completed_at_utc"}
        }),
    )
    (first.run_root / "resource-metrics.json").write_text(
        canonical_json(wrong_write_metrics) + "\n",
        encoding="utf-8",
    )
    (first.run_root / "stage-receipt.json").write_text(
        canonical_json(wrong_write_receipt) + "\n",
        encoding="utf-8",
    )
    baseline = run_l4(l3, state_root=state_root)
    assert not baseline.reused

    foreign_identity = baseline.receipt.identity.model_copy(update={
        "parent_artifact_ids": ("artifact-manifest:foreign",),
    })
    metrics_values = baseline.metrics.model_dump(
        mode="python",
        exclude={"metrics_hash"},
    )
    metrics_values.update({
        "identity": foreign_identity.model_copy(update={
            "contract_kind": "c0.stage_resource_metrics",
        }),
        "resource_metrics_id": "stage-resource-metrics:foreign",
    })
    foreign_metrics = StageResourceMetrics(
        **metrics_values,
        metrics_hash=canonical_sha256(metrics_values),
    )
    receipt_values = baseline.receipt.model_dump(
        mode="python",
        exclude={"receipt_hash"},
    )
    receipt_values.update({
        "identity": foreign_identity,
        "stage_receipt_id": "stage-receipt:foreign",
        "resource_metrics_id": foreign_metrics.resource_metrics_id,
        "resource_metrics_hash": foreign_metrics.metrics_hash,
        "attempt_count": 2,
    })
    foreign_receipt = StageReceipt(
        **receipt_values,
        receipt_hash=canonical_sha256({
            key: value
            for key, value in receipt_values.items()
            if key not in {"started_at_utc", "completed_at_utc"}
        }),
    )
    (baseline.run_root / "resource-metrics.json").write_text(
        canonical_json(foreign_metrics) + "\n",
        encoding="utf-8",
    )
    (baseline.run_root / "stage-receipt.json").write_text(
        canonical_json(foreign_receipt) + "\n",
        encoding="utf-8",
    )

    repaired = run_l4(l3, state_root=state_root)

    assert not repaired.reused
    assert repaired.receipt.stage_receipt_id == baseline.receipt.stage_receipt_id
    assert (
        repaired.metrics.resource_metrics_id
        == baseline.metrics.resource_metrics_id
    )
    assert repaired.receipt.attempt_count == 1


@pytest.mark.unit
def test_l4_serializes_concurrent_same_fingerprint_publication(
    tmp_path: Path,
) -> None:
    l1_root, domain_path, _ = _pipeline(tmp_path, "records")
    l3 = _l3(tmp_path, l1_root, domain_path)
    state_root = tmp_path / ".fkg" / "l4"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(
            lambda _index: run_l4(l3, state_root=state_root),
            range(2),
        ))

    assert sorted(result.reused for result in results) == [False, True]
    assert len({result.receipt.receipt_hash for result in results}) == 1
    assert results[0].sealed_source().resolve(
        "semantic_asserted_entities"
    ).is_file()


@pytest.mark.unit
def test_schema2_source_requires_sealed_l4_and_never_falls_back_to_raw(
    tmp_path: Path,
) -> None:
    l1_root, domain_path, _ = _pipeline(tmp_path, "records")
    result = run_l4(
        _l3(tmp_path, l1_root, domain_path),
        state_root=tmp_path / ".fkg" / "l4",
    )
    source = result.sealed_source()

    assert source.resolve("semantic_asserted_entities").name == (
        "semantic_asserted_entities.parquet"
    )
    for raw_name in ("entities", "relationships", "semantic_entities"):
        with pytest.raises(ValueError):
            source.resolve(raw_name)
    with pytest.raises(ValueError, match="L5 persisted publication"):
        require_l5_publication_receipt(source)
    with pytest.raises(ValueError):
        dataclasses.replace(
            source,
            receipt=result.receipt.model_copy(update={"stage_name": "wrong-stage"}),
        )
    corrupted = result.run_root / "semantic_asserted_relationships.parquet"
    proof_path = result.run_root / "projection-equivalence.json"
    proof_payload = proof_path.read_bytes()
    proof_path.write_bytes(proof_payload + b"corrupt")
    with pytest.raises(ValueError, match="projection artifact"):
        result.sealed_source()
    proof_path.write_bytes(proof_payload)
    corrupted.write_bytes(corrupted.read_bytes() + b"corrupt")
    with pytest.raises(ValueError, match="sealed L4 table"):
        source.resolve("semantic_asserted_relationships")

    legacy_root = tmp_path / "schema1"
    legacy_root.mkdir()
    (legacy_root / "entities.parquet").touch()
    assert resolve_semantic_source_parquet(legacy_root, "semantic_entities").name == (
        "entities.parquet"
    )


@pytest.mark.unit
@pytest.mark.parametrize("mutation", ["missing", "extra", "stale"])
def test_schema2_source_requires_exact_l4_accepted_versions(
    tmp_path: Path,
    mutation: str,
) -> None:
    l1_root, domain_path, _ = _pipeline(tmp_path, "records")
    result = run_l4(
        _l3(tmp_path, l1_root, domain_path),
        state_root=tmp_path / ".fkg" / "l4",
    )
    accepted = dict(L4_ACCEPTED_VERSIONS)
    if mutation == "missing":
        accepted.pop("c0.audit_projection")
    elif mutation == "extra":
        accepted["future.contract"] = "1.0.0"
    else:
        accepted["c0.audit_projection"] = "9.9.9"
    receipt_values = result.receipt.model_dump(
        mode="python",
        exclude={"receipt_hash"},
    )
    receipt_values["accepted_contract_versions"] = accepted
    receipt = StageReceipt(
        **receipt_values,
        receipt_hash=canonical_sha256({
            key: value
            for key, value in receipt_values.items()
            if key not in {"started_at_utc", "completed_at_utc"}
        }),
    )

    with pytest.raises(ValueError, match="successful L4 receipt"):
        SealedL4ServingSource(
            root=result.run_root,
            projection=result.serving_projection,
            receipt=receipt,
            manifest=result.output_manifest,
            input_manifest=result.source.output_manifest,
        )


@pytest.mark.unit
def test_schema2_source_rejects_cross_projection_inconsistency(
    tmp_path: Path,
) -> None:
    l1_root, domain_path, _ = _pipeline(tmp_path, "records")
    result = run_l4(
        _l3(tmp_path, l1_root, domain_path),
        state_root=tmp_path / ".fkg" / "l4",
    )
    values = result.audit_projection.model_dump(
        mode="python",
        exclude={"projection_hash"},
    )
    values["entity_assertion_ids"] = ()
    forged = AuditProjection(
        **values,
        projection_hash=canonical_sha256(values),
    )
    payload = (canonical_json(forged) + "\n").encode("utf-8")
    manifest, _metrics, receipt = _rewrite_l4_artifact(
        result,
        artifact_id="l4-audit-projection",
        file_name="audit-projection.json",
        payload=payload,
    )

    with pytest.raises(ValueError, match="audit projection"):
        SealedL4ServingSource(
            root=result.run_root,
            projection=result.serving_projection,
            receipt=receipt,
            manifest=manifest,
            input_manifest=result.source.output_manifest,
        )


@pytest.mark.unit
def test_schema2_source_requires_exact_audit_disposition_coverage(
    tmp_path: Path,
) -> None:
    l1_root, domain_path, _ = _pipeline(
        tmp_path,
        "records",
        mutate=_all_lifecycle_mutation,
    )
    result = run_l4(
        _l3(tmp_path, l1_root, domain_path),
        state_root=tmp_path / ".fkg" / "l4",
    )
    source = result.sealed_source()
    tables = {
        name: tuple(
            pq.read_table(result.run_root / f"{name}.parquet").to_pylist()
        )
        for name in L4_PROJECTION_TABLE_SCHEMAS
    }
    audit_rows = list(tables["audit_candidates"])
    audit_rows[1] = dict(audit_rows[0])
    tables["audit_candidates"] = tuple(audit_rows)

    with pytest.raises(ValueError, match="exactly cover"):
        source._validate_cross_artifact_invariants(
            result.audit_projection,
            result.serving_projection,
            result.projection_equivalences,
            tables,
        )


@pytest.mark.unit
def test_schema2_source_derives_asserted_membership_from_audit_rows(
    tmp_path: Path,
) -> None:
    l1_root, domain_path, _ = _pipeline(
        tmp_path,
        "records",
        mutate=_all_lifecycle_mutation,
    )
    result = run_l4(
        _l3(tmp_path, l1_root, domain_path),
        state_root=tmp_path / ".fkg" / "l4",
    )
    assert not result.projection_equivalences
    serving_entity_ids = {
        row["entity_id"] for row in result.rows.semantic_asserted_entities
    }
    nonasserted_id = next(
        row["semantic_assertion_id"]
        for row in result.rows.audit_candidates
        if row["candidate_kind"] == "entity"
        and row["lifecycle_state"] != AssertionState.ASSERTED.value
        and row["semantic_assertion_id"] not in serving_entity_ids
    )
    template = result.rows.semantic_asserted_entities[0]
    added_entity = {
        **template,
        "entity_id": nonasserted_id,
    }
    added_entity["row_hash"] = canonical_sha256({
        key: value for key, value in added_entity.items() if key != "row_hash"
    })
    entity_rows = tuple(sorted(
        (*result.rows.semantic_asserted_entities, added_entity),
        key=lambda row: row["entity_id"],
    ))
    template_type_rows = [
        row
        for row in result.rows.semantic_entity_type_assertions
        if row["entity_id"] == template["entity_id"]
    ]
    added_type_rows = []
    for row in template_type_rows:
        added = {**row, "entity_id": nonasserted_id}
        added["row_hash"] = canonical_sha256({
            key: value for key, value in added.items() if key != "row_hash"
        })
        added_type_rows.append(added)
    type_rows = tuple(sorted(
        (*result.rows.semantic_entity_type_assertions, *added_type_rows),
        key=lambda row: (
            row["entity_id"],
            not row["is_most_specific"],
            row["semantic_type_id"],
        ),
    ))
    entity_path = result.run_root / "semantic_asserted_entities.parquet"
    type_path = result.run_root / "semantic_entity_type_assertions.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            list(entity_rows),
            schema=L4_PROJECTION_TABLE_SCHEMAS["semantic_asserted_entities"],
        ),
        entity_path,
        compression="zstd",
        version="2.6",
        use_dictionary=False,
        write_statistics=True,
    )
    pq.write_table(
        pa.Table.from_pylist(
            list(type_rows),
            schema=L4_PROJECTION_TABLE_SCHEMAS[
                "semantic_entity_type_assertions"
            ],
        ),
        type_path,
        compression="zstd",
        version="2.6",
        use_dictionary=False,
        write_statistics=True,
    )
    serving_values = result.serving_projection.model_dump(
        mode="python",
        exclude={"projection_hash"},
    )
    serving_hashes = dict(result.serving_projection.canonical_id_set_hashes)
    serving_row_hashes = dict(result.serving_projection.canonical_row_hashes)
    entity_ids = tuple(sorted(row["entity_id"] for row in entity_rows))
    serving_values["entity_assertion_ids"] = entity_ids
    serving_hashes["entity"] = canonical_sha256(entity_ids)
    serving_row_hashes["entity"] = canonical_sha256(entity_rows)
    serving_values["canonical_id_set_hashes"] = serving_hashes
    serving_values["canonical_row_hashes"] = serving_row_hashes
    serving = type(result.serving_projection)(
        **serving_values,
        projection_hash=canonical_sha256({
            key: value
            for key, value in serving_values.items()
            if key != "sealed_at_utc"
        }),
    )
    audit_values = result.audit_projection.model_dump(
        mode="python",
        exclude={"projection_hash"},
    )
    audit_row_hashes = dict(result.audit_projection.canonical_row_hashes)
    if tuple(result.audit_projection.entity_assertion_ids) == entity_ids:
        audit_row_hashes["entity"] = serving_row_hashes["entity"]
    audit_values["canonical_row_hashes"] = audit_row_hashes
    audit = AuditProjection(
        **audit_values,
        projection_hash=canonical_sha256(audit_values),
    )
    manifest, _metrics, receipt = _rewrite_l4_artifacts(
        result,
        (
            (
                "l4-table:semantic_asserted_entities",
                "semantic_asserted_entities.parquet",
                entity_path.read_bytes(),
                {
                    "canonical_id_set_hash": canonical_sha256(entity_ids),
                    "row_count": len(entity_rows),
                },
            ),
            (
                "l4-table:semantic_entity_type_assertions",
                "semantic_entity_type_assertions.parquet",
                type_path.read_bytes(),
                {
                    "canonical_id_set_hash": canonical_sha256(sorted({
                        f"{row['entity_id']}|{row['semantic_type_id']}"
                        for row in type_rows
                    })),
                    "row_count": len(type_rows),
                },
            ),
            (
                "l4-semantic-serving-projection",
                "semantic-serving-projection.json",
                (canonical_json(serving) + "\n").encode("utf-8"),
                {},
            ),
            (
                "l4-audit-projection",
                "audit-projection.json",
                (canonical_json(audit) + "\n").encode("utf-8"),
                {},
            ),
        ),
    )

    with pytest.raises(ValueError, match="audit and serving"):
        SealedL4ServingSource(
            root=result.run_root,
            projection=serving,
            receipt=receipt,
            manifest=manifest,
            input_manifest=result.source.output_manifest,
        )


@pytest.mark.unit
def test_schema2_source_rejects_resealed_serving_lineage_mismatch(
    tmp_path: Path,
) -> None:
    l1_root, domain_path, _ = _pipeline(tmp_path, "records")
    result = run_l4(
        _l3(tmp_path, l1_root, domain_path),
        state_root=tmp_path / ".fkg" / "l4",
    )
    assert not result.projection_equivalences
    rows = [dict(row) for row in result.rows.semantic_asserted_entities]
    rows[0]["candidate_ids"] = ["candidate:forged"]
    rows[0]["row_hash"] = canonical_sha256({
        key: value for key, value in rows[0].items() if key != "row_hash"
    })
    rows_tuple = tuple(rows)
    path = result.run_root / "semantic_asserted_entities.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            rows,
            schema=L4_PROJECTION_TABLE_SCHEMAS["semantic_asserted_entities"],
        ),
        path,
        compression="zstd",
        version="2.6",
        use_dictionary=False,
        write_statistics=True,
    )
    serving_values = result.serving_projection.model_dump(
        mode="python",
        exclude={"projection_hash"},
    )
    serving_row_hashes = dict(result.serving_projection.canonical_row_hashes)
    serving_row_hashes["entity"] = canonical_sha256(rows_tuple)
    serving_values["canonical_row_hashes"] = serving_row_hashes
    serving = type(result.serving_projection)(
        **serving_values,
        projection_hash=canonical_sha256({
            key: value
            for key, value in serving_values.items()
            if key != "sealed_at_utc"
        }),
    )
    audit_values = result.audit_projection.model_dump(
        mode="python",
        exclude={"projection_hash"},
    )
    audit_row_hashes = dict(result.audit_projection.canonical_row_hashes)
    if (
        tuple(result.audit_projection.entity_assertion_ids)
        == tuple(result.serving_projection.entity_assertion_ids)
    ):
        audit_row_hashes["entity"] = serving_row_hashes["entity"]
    audit_values["canonical_row_hashes"] = audit_row_hashes
    audit = AuditProjection(
        **audit_values,
        projection_hash=canonical_sha256(audit_values),
    )
    manifest, _metrics, receipt = _rewrite_l4_artifacts(
        result,
        (
            (
                "l4-table:semantic_asserted_entities",
                "semantic_asserted_entities.parquet",
                path.read_bytes(),
                {},
            ),
            (
                "l4-semantic-serving-projection",
                "semantic-serving-projection.json",
                (canonical_json(serving) + "\n").encode("utf-8"),
                {},
            ),
            (
                "l4-audit-projection",
                "audit-projection.json",
                (canonical_json(audit) + "\n").encode("utf-8"),
                {},
            ),
        ),
    )

    with pytest.raises(ValueError, match="serving entity lineage"):
        SealedL4ServingSource(
            root=result.run_root,
            projection=serving,
            receipt=receipt,
            manifest=manifest,
            input_manifest=result.source.output_manifest,
        )


@pytest.mark.unit
def test_schema2_source_rejects_equivalence_authority_mismatch(
    tmp_path: Path,
) -> None:
    result = run_l4(
        _l3_with_sealed_manifest(tmp_path),
        state_root=tmp_path / ".fkg" / "l4",
    )
    proof = result.projection_equivalences[0]
    authority = proof.authority.model_copy(update={
        "source_artifact_manifest_hash": "f" * 64,
    })
    values = proof.model_dump(mode="python", exclude={"equivalence_hash"})
    values["authority"] = authority
    forged = ProjectionEquivalence(
        **values,
        equivalence_hash=canonical_sha256(values),
    )
    payload = (canonical_json((forged,)) + "\n").encode("utf-8")
    manifest, _metrics, receipt = _rewrite_l4_artifact(
        result,
        artifact_id="l4-parquet-projection-equivalence",
        file_name="projection-equivalence.json",
        payload=payload,
    )

    with pytest.raises(ValueError, match="projection equivalence"):
        SealedL4ServingSource(
            root=result.run_root,
            projection=result.serving_projection,
            receipt=receipt,
            manifest=manifest,
            input_manifest=result.source.output_manifest,
        )


@pytest.mark.unit
def test_schema2_source_requires_deterministic_output_manifest_lineage(
    tmp_path: Path,
) -> None:
    l1_root, domain_path, _ = _pipeline(tmp_path, "records")
    result = run_l4(
        _l3(tmp_path, l1_root, domain_path),
        state_root=tmp_path / ".fkg" / "l4",
    )
    for updates in (
        {"artifact_manifest_id": "artifact-manifest:foreign"},
        {
            "identity": result.output_manifest.identity.model_copy(update={
                "parent_artifact_ids": ("artifact-manifest:foreign-parent",),
            })
        },
    ):
        values = result.output_manifest.model_dump(
            mode="python",
            exclude={"manifest_hash"},
        )
        values.update(updates)
        manifest = ArtifactManifest(
            **values,
            manifest_hash=canonical_sha256(values),
        )
        receipt_values = result.receipt.model_dump(
            mode="python",
            exclude={"receipt_hash"},
        )
        receipt_values.update({
            "output_manifest_id": manifest.artifact_manifest_id,
            "output_manifest_hash": manifest.manifest_hash,
        })
        receipt = StageReceipt(
            **receipt_values,
            receipt_hash=canonical_sha256({
                key: value
                for key, value in receipt_values.items()
                if key not in {"started_at_utc", "completed_at_utc"}
            }),
        )

        with pytest.raises(ValueError, match="artifact manifest"):
            SealedL4ServingSource(
                root=result.run_root,
                projection=result.serving_projection,
                receipt=receipt,
                manifest=manifest,
                input_manifest=result.source.output_manifest,
            )


@pytest.mark.unit
def test_schema2_source_requires_l3_manifest_in_identity_ancestry(
    tmp_path: Path,
) -> None:
    l1_root, domain_path, _ = _pipeline(tmp_path, "records")
    result = run_l4(
        _l3(tmp_path, l1_root, domain_path),
        state_root=tmp_path / ".fkg" / "l4",
    )
    foreign_receipt_identity = result.receipt.identity.model_copy(update={
        "parent_artifact_ids": ("artifact-manifest:foreign",),
    })
    manifest_values = result.output_manifest.model_dump(
        mode="python",
        exclude={"manifest_hash"},
    )
    manifest_values["identity"] = foreign_receipt_identity.model_copy(update={
        "contract_kind": "c0.artifact_manifest",
    })
    manifest = ArtifactManifest(
        **manifest_values,
        manifest_hash=canonical_sha256(manifest_values),
    )
    serving_values = result.serving_projection.model_dump(
        mode="python",
        exclude={"projection_hash"},
    )
    serving_values["identity"] = foreign_receipt_identity.model_copy(update={
        "contract_kind": "c0.semantic_serving_projection",
    })
    serving = type(result.serving_projection)(
        **serving_values,
        projection_hash=canonical_sha256({
            key: value
            for key, value in serving_values.items()
            if key != "sealed_at_utc"
        }),
    )
    receipt_values = result.receipt.model_dump(
        mode="python",
        exclude={"receipt_hash"},
    )
    receipt_values.update({
        "identity": foreign_receipt_identity,
        "output_manifest_hash": manifest.manifest_hash,
    })
    receipt = StageReceipt(
        **receipt_values,
        receipt_hash=canonical_sha256({
            key: value
            for key, value in receipt_values.items()
            if key not in {"started_at_utc", "completed_at_utc"}
        }),
    )

    with pytest.raises(ValueError, match="artifact manifest"):
        SealedL4ServingSource(
            root=result.run_root,
            projection=serving,
            receipt=receipt,
            manifest=manifest,
            input_manifest=result.source.output_manifest,
        )


@pytest.mark.unit
def test_schema2_source_rejects_resealed_invalid_parquet_row_hash(
    tmp_path: Path,
) -> None:
    l1_root, domain_path, _ = _pipeline(tmp_path, "records")
    result = run_l4(
        _l3(tmp_path, l1_root, domain_path),
        state_root=tmp_path / ".fkg" / "l4",
    )
    table_name = "semantic_asserted_entities"
    path = result.run_root / f"{table_name}.parquet"
    rows = pq.read_table(path).to_pylist()
    rows[0]["row_hash"] = "0" * 64
    pq.write_table(
        pa.Table.from_pylist(
            rows,
            schema=L4_PROJECTION_TABLE_SCHEMAS[table_name],
        ),
        path,
        compression="zstd",
        version="2.6",
        use_dictionary=False,
        write_statistics=True,
    )
    manifest, _metrics, receipt = _rewrite_l4_artifact(
        result,
        artifact_id=f"l4-table:{table_name}",
        file_name=f"{table_name}.parquet",
        payload=path.read_bytes(),
    )

    with pytest.raises(ValueError, match="row hash"):
        SealedL4ServingSource(
            root=result.run_root,
            projection=result.serving_projection,
            receipt=receipt,
            manifest=manifest,
            input_manifest=result.source.output_manifest,
        )


@pytest.mark.unit
def test_l4_relationship_projection_uses_entity_type_index() -> None:
    source = inspect.getsource(lifecycle_projection._serving_rows)

    assert "entity_type_by_id = {" in source
    assert "source_type = entity_type_by_id[source_id]" in source
    assert "target_type = entity_type_by_id[target_id]" in source
    assert source.count("for row in entity_rows") == 1
    assert "source_type = next(" not in source
    assert "target_type = next(" not in source


@pytest.mark.unit
def test_schema2_source_rejects_duplicate_entity_type_keys(
    tmp_path: Path,
) -> None:
    l1_root, domain_path, _ = _pipeline(tmp_path, "records")
    result = run_l4(
        _l3(tmp_path, l1_root, domain_path),
        state_root=tmp_path / ".fkg" / "l4",
    )
    source = result.sealed_source()
    tables = {
        name: tuple(
            pq.read_table(result.run_root / f"{name}.parquet").to_pylist()
        )
        for name in L4_PROJECTION_TABLE_SCHEMAS
    }
    type_rows = list(tables["semantic_entity_type_assertions"])
    type_rows.append(dict(type_rows[0]))
    tables["semantic_entity_type_assertions"] = tuple(type_rows)

    with pytest.raises(ValueError, match="type assertion IDs"):
        source._validate_cross_artifact_invariants(
            result.audit_projection,
            result.serving_projection,
            result.projection_equivalences,
            tables,
        )


@pytest.mark.unit
def test_schema2_source_rejects_contradictory_type_markers_and_depths(
    tmp_path: Path,
) -> None:
    l1_root, domain_path, _ = _pipeline(tmp_path, "records")
    result = run_l4(
        _l3(tmp_path, l1_root, domain_path),
        state_root=tmp_path / ".fkg" / "l4",
    )
    source = result.sealed_source()
    base_tables = {
        name: tuple(
            pq.read_table(result.run_root / f"{name}.parquet").to_pylist()
        )
        for name in L4_PROJECTION_TABLE_SCHEMAS
    }
    type_rows = list(base_tables["semantic_entity_type_assertions"])
    marker_rows = [dict(row) for row in type_rows]
    marker_rows[0]["is_most_specific"] = False
    marker_rows[0]["row_hash"] = canonical_sha256({
        key: value
        for key, value in marker_rows[0].items()
        if key != "row_hash"
    })
    marker_tables = dict(base_tables)
    marker_tables["semantic_entity_type_assertions"] = tuple(marker_rows)
    with pytest.raises(ValueError, match="entity type assertions"):
        source._validate_cross_artifact_invariants(
            result.audit_projection,
            result.serving_projection,
            result.projection_equivalences,
            marker_tables,
        )

    depth_rows = [dict(row) for row in type_rows]
    depth_rows[1]["semantic_type_id"] = depth_rows[0]["semantic_type_id"]
    depth_rows[1]["hierarchy_depth"] = depth_rows[0]["hierarchy_depth"] + 1
    depth_rows[1]["row_hash"] = canonical_sha256({
        key: value
        for key, value in depth_rows[1].items()
        if key != "row_hash"
    })
    depth_tables = dict(base_tables)
    depth_tables["semantic_entity_type_assertions"] = tuple(depth_rows)
    with pytest.raises(
        ValueError,
        match="hierarchy depths|entity type assertions",
    ):
        source._validate_cross_artifact_invariants(
            result.audit_projection,
            result.serving_projection,
            result.projection_equivalences,
            depth_tables,
        )


@pytest.mark.unit
def test_schema2_source_binds_audit_rows_to_disposition_targets(
    tmp_path: Path,
) -> None:
    l1_root, domain_path, _ = _pipeline(
        tmp_path,
        "records",
        mutate=_all_lifecycle_mutation,
    )
    result = run_l4(
        _l3(tmp_path, l1_root, domain_path),
        state_root=tmp_path / ".fkg" / "l4",
    )
    source = result.sealed_source()
    tables = {
        name: tuple(
            pq.read_table(result.run_root / f"{name}.parquet").to_pylist()
        )
        for name in L4_PROJECTION_TABLE_SCHEMAS
    }
    audit_rows = [dict(row) for row in tables["audit_candidates"]]
    deduplicated_index = next(
        index
        for index, row in enumerate(audit_rows)
        if row["disposition"] == "deduplicated"
    )
    wrong_target = next(
        row["candidate_id"]
        for row in audit_rows
        if row["disposition"] == "retained"
        and row["candidate_id"]
        != audit_rows[deduplicated_index]["candidate_id"]
    )
    audit_rows[deduplicated_index]["candidate_id"] = wrong_target
    audit_rows[deduplicated_index]["row_hash"] = canonical_sha256({
        key: value
        for key, value in audit_rows[deduplicated_index].items()
        if key != "row_hash"
    })
    tables["audit_candidates"] = tuple(audit_rows)

    with pytest.raises(ValueError, match="disposition target"):
        source._validate_cross_artifact_invariants(
            result.audit_projection,
            result.serving_projection,
            result.projection_equivalences,
            tables,
        )


@pytest.mark.unit
def test_schema2_source_reconciles_relationship_hierarchy_authority(
    tmp_path: Path,
) -> None:
    l1_root, domain_path, _ = _pipeline(tmp_path, "records")
    result = run_l4(
        _l3(tmp_path, l1_root, domain_path),
        state_root=tmp_path / ".fkg" / "l4",
    )
    source = result.sealed_source()
    tables = {
        name: tuple(
            pq.read_table(result.run_root / f"{name}.parquet").to_pylist()
        )
        for name in L4_PROJECTION_TABLE_SCHEMAS
    }
    entities = tables["semantic_asserted_entities"]
    relationship = {
        "relationship_id": "relationship:forged",
        "semantic_relationship_id": "semantic-relationship:forged",
        "source_entity_id": entities[0]["entity_id"],
        "target_entity_id": entities[1]["entity_id"],
        "candidate_ids": ["candidate:forged"],
        "evidence_span_ids": list(entities[0]["evidence_span_ids"]),
        "source_inheritance_path": [],
        "target_inheritance_path": [],
        "hierarchy_hash": "f" * 64,
        "domain_contract_hash": result.serving_projection.sealed_domain_contract_hash,
        "semantic_contract_hash": (
            result.serving_projection.sealed_semantic_contract_hash
        ),
    }
    relationship["row_hash"] = canonical_sha256(relationship)
    tables["semantic_asserted_relationships"] = (relationship,)
    serving_values = result.serving_projection.model_dump(
        mode="python",
        exclude={"projection_hash"},
    )
    id_hashes = dict(result.serving_projection.canonical_id_set_hashes)
    row_hashes = dict(result.serving_projection.canonical_row_hashes)
    id_hashes["relationship"] = canonical_sha256(["relationship:forged"])
    row_hashes["relationship"] = canonical_sha256([relationship])
    serving_values.update({
        "relationship_assertion_ids": ("relationship:forged",),
        "canonical_id_set_hashes": id_hashes,
        "canonical_row_hashes": row_hashes,
    })
    serving = type(result.serving_projection)(
        **serving_values,
        projection_hash=canonical_sha256({
            key: value
            for key, value in serving_values.items()
            if key != "sealed_at_utc"
        }),
    )
    object.__setattr__(source, "projection", serving)

    with pytest.raises(ValueError, match="hierarchy authority"):
        source._validate_cross_artifact_invariants(
            result.audit_projection,
            serving,
            result.projection_equivalences,
            tables,
        )


@pytest.mark.unit
def test_schema2_source_reconciles_required_member_authority_hashes(
    tmp_path: Path,
) -> None:
    result = run_l4(
        _l3_with_sealed_manifest(tmp_path),
        state_root=tmp_path / ".fkg" / "l4",
    )
    source = result.sealed_source()
    tables = {
        name: tuple(
            pq.read_table(result.run_root / f"{name}.parquet").to_pylist()
        )
        for name in L4_PROJECTION_TABLE_SCHEMAS
    }
    for table_name in (
        "semantic_required_member_manifests",
        "semantic_required_members",
    ):
        rows = [dict(row) for row in tables[table_name]]
        for row in rows:
            row["hierarchy_hash"] = "f" * 64
            row["identity_policy_hash"] = "e" * 64
            row["row_hash"] = canonical_sha256({
                key: value for key, value in row.items() if key != "row_hash"
            })
        tables[table_name] = tuple(rows)

    with pytest.raises(ValueError, match="hierarchy authority"):
        source._validate_cross_artifact_invariants(
            result.audit_projection,
            result.serving_projection,
            result.projection_equivalences,
            tables,
        )


@pytest.mark.unit
def test_l4_serving_gates_evidence_identity_hierarchy_endpoints_and_contract(
    tmp_path: Path,
) -> None:
    l1_root, domain_path, _ = _pipeline(tmp_path, "records")
    l3 = _l3(tmp_path, l1_root, domain_path)
    results, _, classifications, observations = lifecycle_projection._candidate_indexes(
        l3
    )
    asserted_entity_id = next(
        candidate_id
        for candidate_id, item in results.items()
        if item.candidate_kind == "entity"
        and item.current_state == AssertionState.ASSERTED.value
    )

    bad_identity = dict(results)
    bad_identity[asserted_entity_id] = dataclasses.replace(
        bad_identity[asserted_entity_id],
        identity_recomputed=False,
    )
    with pytest.raises(L4ProjectionError, match="L4_ASSERTED_IDENTITY_INVALID"):
        lifecycle_projection._serving_rows(
            l3, bad_identity, classifications, observations
        )

    bad_evidence = dict(results)
    bad_evidence[asserted_entity_id] = dataclasses.replace(
        bad_evidence[asserted_entity_id],
        evidence_span_ids=(),
    )
    with pytest.raises(L4ProjectionError, match="L4_ASSERTED_EVIDENCE_INVALID"):
        lifecycle_projection._serving_rows(
            l3, bad_evidence, classifications, observations
        )

    bad_classifications = {
        candidate_id: list(items)
        for candidate_id, items in classifications.items()
    }
    bad_classifications[asserted_entity_id][0] = dataclasses.replace(
        bad_classifications[asserted_entity_id][0],
        hierarchy_hash="f" * 64,
    )
    with pytest.raises(L4ProjectionError, match="L4_ASSERTED_HIERARCHY_INVALID"):
        lifecycle_projection._serving_rows(
            l3, results, bad_classifications, observations
        )

    relationship_id = next(
        candidate_id
        for candidate_id, item in results.items()
        if item.candidate_kind == "relationship"
    )
    bad_endpoint = dict(results)
    bad_endpoint[relationship_id] = dataclasses.replace(
        bad_endpoint[relationship_id],
        current_state=AssertionState.ASSERTED.value,
        source_inheritance_path=(),
    )
    with pytest.raises(L4ProjectionError, match="L4_ASSERTED_ENDPOINT_INVALID"):
        lifecycle_projection._serving_rows(
            l3, bad_endpoint, classifications, observations
        )

    bad_hierarchy = dataclasses.replace(
        l3.inputs.hierarchy,
        domain_contract_hash="e" * 64,
    )
    bad_source = dataclasses.replace(
        l3,
        inputs=dataclasses.replace(l3.inputs, hierarchy=bad_hierarchy),
    )
    with pytest.raises(L4ProjectionError, match="L4_INPUT_MANIFEST_INVALID"):
        lifecycle_projection._validate_l3_artifacts(bad_source)
    bad_approval = l3.inputs.domain_contract.approval.model_copy(
        update={"contract_hash": "d" * 64}
    )
    bad_contract = l3.inputs.domain_contract.model_copy(
        update={"approval": bad_approval}
    )
    bad_source = dataclasses.replace(
        l3,
        inputs=dataclasses.replace(l3.inputs, domain_contract=bad_contract),
    )
    with pytest.raises(L4ProjectionError, match="L4_DOMAIN_HASH_MISMATCH"):
        lifecycle_projection._validate_l3_artifacts(bad_source)


@pytest.mark.unit
def test_l4_reclassification_keeps_one_node_and_explicit_ancestor_types(
    tmp_path: Path,
) -> None:
    def mutate(candidates, work_unit):
        first = work_unit.text.find("governed record")
        specialized = dict(candidates[0])
        specialized["observed_type"] = "Record A"
        specialized["anchors"] = [{
            "span_start": work_unit.slice_start + first + len("governed "),
            "span_end": work_unit.slice_start + first + len("governed record"),
            "quote": "record",
            "model_authored_evidence_id": None,
        }]
        return [candidates[0], specialized] + list(candidates[1:])

    l1_root, domain_path = _approved_l1(
        tmp_path,
        "records",
        extra_types=_subtypes("records"),
    )
    _run_l2(
        tmp_path,
        "records",
        _Service("records", mutate=mutate),
        l1_root,
        domain_path,
    )

    result = run_l4(
        _l3(tmp_path, l1_root, domain_path),
        state_root=tmp_path / ".fkg" / "l4",
    )
    stable_id = next(
        item.semantic_id
        for item in result.source.candidate_results
        if item.approved_semantic_id == "semantic-type:records.record-a"
    )
    entity_rows = [
        row for row in result.rows.semantic_asserted_entities
        if row["entity_id"] == stable_id
    ]
    assert len(entity_rows) == 1
    assert entity_rows[0]["most_specific_type_id"] == (
        "semantic-type:records.record-a"
    )
    type_rows = [
        row for row in result.rows.semantic_entity_type_assertions
        if row["entity_id"] == stable_id
    ]
    assert {row["semantic_type_id"] for row in type_rows} == {
        "semantic-type:records.record-a",
        "semantic-type:records.record",
    }


def _sealed_manifest(tmp_path: Path):
    fact_set = _fact_set(
        "manufacturing",
        ordered=False,
        roles=False,
        expected_count=None,
    )
    l1_root, domain_path, l2 = _pipeline(
        tmp_path,
        "manufacturing",
        fact_set=fact_set,
    )
    l3 = _l3(tmp_path, l1_root, domain_path)
    identity = schema2_validation_stage._validation_identity(
        l3.inputs.l2_receipt.identity,
        contract_kind="l3.stage",
    )
    return schema2_validation_stage._seal_manifest(
        proposal=l2.required_member_sets[0].proposal,
        identity=identity,
        sealed_at_utc=l3.inputs.l2_receipt.completed_at_utc,
    )


def _l3_with_sealed_manifest(
    tmp_path: Path,
    *,
    ordered: bool = False,
    roles: bool = False,
    member_count: int = 1,
):
    fact_set = _fact_set(
        "manufacturing",
        ordered=ordered,
        roles=roles,
        expected_count=None,
    )
    mutate = None
    if ordered or roles or member_count > 1:
        def mutate(candidates, _work_unit):
            values = [dict(candidate) for candidate in candidates]
            relationship = dict(values[2])
            relationship["member_role_id"] = (
                "role:manufacturing.subject" if roles else None
            )
            relationship["member_order"] = 0 if ordered else None
            values[2] = relationship
            if member_count > 1:
                second_member = {
                    **values[1],
                    "local_id": "subject-2",
                    "label": "Subject 2",
                }
                second_relationship = {
                    **relationship,
                    "target_local_id": "subject-2",
                    "member_order": 1 if ordered else None,
                }
                values.extend((second_member, second_relationship))
            return values
    l1_root, domain_path, l2 = _pipeline(
        tmp_path,
        "manufacturing",
        fact_set=fact_set,
        mutate=mutate,
    )
    l3 = _l3(tmp_path, l1_root, domain_path)
    manifest = schema2_validation_stage._seal_manifest(
        proposal=l2.required_member_sets[0].proposal,
        identity=schema2_validation_stage._validation_identity(
            l3.inputs.l2_receipt.identity,
            contract_kind="l3.stage",
        ),
        sealed_at_utc=l3.inputs.l2_receipt.completed_at_utc,
    )
    prior = l3.required_member_outcomes[0]
    member_ids = tuple(member.member_canonical_id for member in manifest.members)
    complete = dataclasses.replace(
        prior.outcome,
        completeness_state="complete",
        reason_codes=(),
        verified_member_ids=member_ids,
        verified_member_count=len(member_ids),
        membership_evidence_span_ids=tuple(sorted({
            evidence_id
            for member in manifest.members
            for evidence_id in member.supporting_evidence_span_ids
        })),
        recomputed_collection_hash=manifest.authoritative_collection_hash,
    )
    outcome = schema2_validation_stage.RequiredMemberOutcomeRecord(
        outcome=complete,
        manifest=manifest,
    )
    outcome_payload = {
        key: (list(value) if isinstance(value, tuple) else value)
        for key, value in complete.__dict__.items()
    }
    outcome_payload["role_coverage"] = [
        list(item) for item in complete.role_coverage
    ]
    outcome_payload["required_member_manifest_id"] = (
        manifest.required_member_manifest_id
    )
    proposal_id = complete.required_member_set_proposal_id
    updated_l3 = dataclasses.replace(
        l3,
        required_member_outcomes=(outcome,),
    )
    reason_index_payload = lifecycle_projection._l3_reason_code_index(updated_l3)
    entries = []
    for entry in l3.output_manifest.entries:
        if entry.artifact_id == f"{proposal_id}:outcome":
            entries.append(entry.model_copy(update={
                "content_hash": canonical_sha256(outcome_payload),
                "row_count": len(member_ids),
                "canonical_id_set_hash": canonical_sha256(sorted(member_ids)),
                "byte_count": len(
                    (canonical_json(outcome_payload) + "\n").encode("utf-8")
                ),
            }))
        elif entry.artifact_id == "l3-reason-code-index":
            entries.append(entry.model_copy(update={
                "content_hash": canonical_sha256(reason_index_payload),
                "byte_count": len(
                    (canonical_json(reason_index_payload) + "\n").encode("utf-8")
                ),
            }))
        else:
            entries.append(entry)
    manifest_payload = (canonical_json(manifest) + "\n").encode("utf-8")
    entries.append(schema2_validation_stage._artifact_entry(
        artifact_id=manifest.required_member_manifest_id,
        contract_kind="c0.required_member_manifest",
        contract_version="1.1.0",
        schema_hash=canonical_sha256(
            RequiredMemberManifestV1_1.model_json_schema()
        ),
        content_hash=manifest.manifest_hash,
        byte_count=len(manifest_payload),
        row_count=len(manifest.members),
        canonical_id_set_hash=manifest.member_set_hash,
    ))
    entries.sort(key=lambda entry: entry.artifact_id)
    manifest_values = {
        "identity": l3.output_manifest.identity,
        "artifact_manifest_id": l3.output_manifest.artifact_manifest_id,
        "entries": tuple(entries),
        "total_row_count": sum(entry.row_count or 0 for entry in entries),
        "total_byte_count": sum(entry.byte_count for entry in entries),
    }
    output_manifest = ArtifactManifest(
        **manifest_values,
        manifest_hash=canonical_sha256(manifest_values),
    )
    receipt_values = l3.receipt.model_dump(
        mode="python",
        exclude={"receipt_hash"},
    )
    receipt_values["output_manifest_hash"] = output_manifest.manifest_hash
    receipt = StageReceipt(
        **receipt_values,
        receipt_hash=canonical_sha256({
            key: value
            for key, value in receipt_values.items()
            if key not in {"started_at_utc", "completed_at_utc"}
        }),
    )
    return dataclasses.replace(
        l3,
        required_member_outcomes=(outcome,),
        output_manifest=output_manifest,
        receipt=receipt,
    )


@pytest.mark.unit
def test_l4_persists_and_reads_back_required_member_equivalence(
    tmp_path: Path,
) -> None:
    result = run_l4(
        _l3_with_sealed_manifest(tmp_path),
        state_root=tmp_path / ".fkg" / "l4",
    )
    manifest = result.source.required_member_manifests[0]
    physical = pq.read_table(
        result.run_root / "semantic_required_members.parquet"
    ).to_pylist()

    assert len(physical) == len(manifest.members)
    assert physical[0]["candidate_id"] == manifest.members[0].candidate_id
    assert physical[0]["member_hash"] == manifest.members[0].member_hash
    assert result.sealed_source().resolve(
        "semantic_required_member_manifests"
    ).is_file()
    assert result.projection_equivalences
    assert all(proof.equivalent for proof in result.projection_equivalences)
    proof_payload = json.loads(
        (result.run_root / "projection-equivalence.json").read_text("utf-8")
    )
    assert len(proof_payload) == len(result.projection_equivalences)
    assert proof_payload[0]["expected"] == proof_payload[0]["read_back"]
    proof_entry = next(
        entry for entry in result.output_manifest.entries
        if entry.artifact_id == "l4-parquet-projection-equivalence"
    )
    assert proof_entry.schema_hash == canonical_sha256({
        "type": "array",
        "items": ProjectionEquivalence.model_json_schema(),
    })


@pytest.mark.unit
def test_sealed_source_requires_receipt_anchored_l3_input_manifest(
    tmp_path: Path,
) -> None:
    result = run_l4(
        _l3_with_sealed_manifest(tmp_path),
        state_root=tmp_path / ".fkg" / "l4",
    )
    entry = next(
        item
        for item in result.source.output_manifest.entries
        if item.contract_kind == "c0.required_member_manifest"
    )
    wrong_input = _replace_manifest_entry(
        result.source.output_manifest,
        entry.artifact_id,
        content_hash="f" * 64,
    )

    with pytest.raises(ValueError, match="input manifest differs"):
        SealedL4ServingSource(
            root=result.run_root,
            projection=result.serving_projection,
            receipt=result.receipt,
            manifest=result.output_manifest,
            input_manifest=wrong_input,
        )


@pytest.mark.unit
def test_sealed_source_requires_exact_l3_required_member_entries(
    tmp_path: Path,
) -> None:
    result = run_l4(
        _l3_with_sealed_manifest(tmp_path),
        state_root=tmp_path / ".fkg" / "l4",
    )
    source = result.sealed_source()
    input_manifest = result.source.output_manifest
    required_entry = next(
        entry
        for entry in input_manifest.entries
        if entry.contract_kind == "c0.required_member_manifest"
    )
    wrong = _replace_manifest_entry(
        input_manifest,
        required_entry.artifact_id,
        content_hash="f" * 64,
    )
    missing = _manifest_with_entries(
        input_manifest,
        tuple(
            entry
            for entry in input_manifest.entries
            if entry.artifact_id != required_entry.artifact_id
        ),
    )
    extra_entry = required_entry.model_copy(update={
        "artifact_id": "required-member-manifest:extra",
    })
    extra = _manifest_with_entries(
        input_manifest,
        (*input_manifest.entries, extra_entry),
    )
    tables = {
        name: tuple(
            pq.read_table(result.run_root / f"{name}.parquet").to_pylist()
        )
        for name in (
            "semantic_required_member_manifests",
            "semantic_required_members",
        )
    }

    for changed in (wrong, missing, extra):
        object.__setattr__(source, "input_manifest", changed)
        with pytest.raises(ValueError, match="anchored L3 manifest"):
            source._validate_projection_equivalences(
                result.serving_projection,
                result.projection_equivalences,
                tables["semantic_required_member_manifests"],
                tables["semantic_required_members"],
            )


@pytest.mark.unit
def test_sealed_source_rejects_duplicate_l3_manifest_entry(
    tmp_path: Path,
) -> None:
    result = run_l4(
        _l3_with_sealed_manifest(tmp_path),
        state_root=tmp_path / ".fkg" / "l4",
    )
    input_manifest = result.source.output_manifest
    required_entry = next(
        entry
        for entry in input_manifest.entries
        if entry.contract_kind == "c0.required_member_manifest"
    )
    duplicate = ArtifactManifest.model_construct(
        identity=input_manifest.identity,
        artifact_manifest_id=input_manifest.artifact_manifest_id,
        entries=(*input_manifest.entries, required_entry),
        total_row_count=(
            input_manifest.total_row_count + (required_entry.row_count or 0)
        ),
        total_byte_count=(
            input_manifest.total_byte_count + required_entry.byte_count
        ),
        manifest_hash=input_manifest.manifest_hash,
    )

    with pytest.raises(ValueError, match="canonical L3 input manifest"):
        SealedL4ServingSource(
            root=result.run_root,
            projection=result.serving_projection,
            receipt=result.receipt,
            manifest=result.output_manifest,
            input_manifest=duplicate,
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("member_canonical_id", "entity:forged"),
        ("member_semantic_type_id", "semantic-type:forged"),
        ("member_role_id", "role:forged"),
        ("candidate_id", "candidate:forged"),
        ("member_order", 0),
        ("supporting_evidence_span_ids", ["evidence-span:forged"]),
    ),
)
def test_sealed_source_rejects_resealed_required_member_field_drift(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    result = run_l4(
        _l3_with_sealed_manifest(tmp_path),
        state_root=tmp_path / ".fkg" / "l4",
    )
    manifest_rows = [
        dict(row) for row in result.rows.semantic_required_member_manifests
    ]
    member_rows = [
        dict(row) for row in result.rows.semantic_required_members
    ]
    member_rows[0][field] = replacement
    member_rows[0]["row_hash"] = canonical_sha256({
        key: value
        for key, value in member_rows[0].items()
        if key != "row_hash"
    })
    manifest, _metrics, receipt = _rewrite_required_member_artifacts(
        result,
        manifest_rows,
        member_rows,
    )

    with pytest.raises(ValueError, match="carried C0 authority"):
        SealedL4ServingSource(
            root=result.run_root,
            projection=result.serving_projection,
            receipt=receipt,
            manifest=manifest,
            input_manifest=result.source.output_manifest,
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("member_set_hash", "d" * 64),
        ("ordered_member_tuple_hash", "e" * 64),
        ("authoritative_collection_hash", "f" * 64),
        ("expected_cardinality", 99),
        ("minimum_cardinality", -1),
        ("required_role_ids", ["role:z", "role:a"]),
    ),
)
def test_sealed_source_rejects_resealed_required_member_aggregate_drift(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    result = run_l4(
        _l3_with_sealed_manifest(tmp_path),
        state_root=tmp_path / ".fkg" / "l4",
    )
    manifest_rows = [
        dict(row) for row in result.rows.semantic_required_member_manifests
    ]
    member_rows = [
        dict(row) for row in result.rows.semantic_required_members
    ]
    manifest_rows[0][field] = replacement
    manifest_rows[0]["row_hash"] = canonical_sha256({
        key: value
        for key, value in manifest_rows[0].items()
        if key != "row_hash"
    })
    for row in member_rows:
        row[field] = replacement
        row["row_hash"] = canonical_sha256({
            key: value for key, value in row.items() if key != "row_hash"
        })
    manifest, _metrics, receipt = _rewrite_required_member_artifacts(
        result,
        manifest_rows,
        member_rows,
    )

    with pytest.raises(
        ValueError,
        match="carried C0 authority|anchored L3 manifest",
    ):
        SealedL4ServingSource(
            root=result.run_root,
            projection=result.serving_projection,
            receipt=receipt,
            manifest=manifest,
            input_manifest=result.source.output_manifest,
        )


@pytest.mark.unit
def test_sealed_source_binds_recomputed_member_hashes_to_l3_manifest_hash(
    tmp_path: Path,
) -> None:
    result = run_l4(
        _l3_with_sealed_manifest(tmp_path),
        state_root=tmp_path / ".fkg" / "l4",
    )
    carried = result.source.required_member_manifests[0]
    manifest_rows = [
        dict(row) for row in result.rows.semantic_required_member_manifests
    ]
    member_rows = [
        dict(row) for row in result.rows.semantic_required_members
    ]
    member_values = carried.members[0].model_dump(
        mode="python",
        exclude={"member_hash"},
    )
    member_values["member_canonical_id"] = "entity:forged"
    forged_member = RequiredMemberReferenceV1_1.seal(**member_values)
    members = (forged_member, *carried.members[1:])
    member_set_hash, ordered_tuple_hash = _member_hashes_v1_1(
        members,
        ordering_mode=carried.ordering_policy.mode,
    )
    collection_hash = authoritative_collection_hash_v1_1(
        authority=carried.authority,
        scope_canonical_id=carried.scope_canonical_id,
        membership_semantic_relationship_id=(
            carried.membership_semantic_relationship_id
        ),
        ordering_policy=carried.ordering_policy,
        expected_cardinality=carried.expected_cardinality,
        minimum_cardinality=carried.minimum_cardinality,
        maximum_cardinality=carried.maximum_cardinality,
        required_role_ids=carried.required_role_ids,
        members=members,
    )
    forged_values = carried.model_dump(
        mode="python",
        exclude={"manifest_hash"},
    )
    forged_values.update({
        "members": members,
        "member_set_hash": member_set_hash,
        "ordered_member_tuple_hash": ordered_tuple_hash,
        "authoritative_collection_hash": collection_hash,
    })
    semantic_values = dict(forged_values)
    semantic_values.pop("sealed_at_utc")
    forged_manifest = RequiredMemberManifestV1_1(
        **forged_values,
        manifest_hash=canonical_sha256(semantic_values),
    )
    member_rows[0].update({
        **forged_member.model_dump(mode="json"),
        "member_set_hash": member_set_hash,
        "ordered_member_tuple_hash": ordered_tuple_hash,
        "authoritative_collection_hash": collection_hash,
        "manifest_hash": forged_manifest.manifest_hash,
    })
    member_rows[0]["row_hash"] = canonical_sha256({
        key: value
        for key, value in member_rows[0].items()
        if key != "row_hash"
    })
    manifest_rows[0].update({
        "member_set_hash": member_set_hash,
        "ordered_member_tuple_hash": ordered_tuple_hash,
        "authoritative_collection_hash": collection_hash,
        "manifest_hash": forged_manifest.manifest_hash,
    })
    manifest_rows[0]["row_hash"] = canonical_sha256({
        key: value
        for key, value in manifest_rows[0].items()
        if key != "row_hash"
    })
    manifest, _metrics, receipt = _rewrite_required_member_artifacts(
        result,
        manifest_rows,
        member_rows,
    )

    with pytest.raises(ValueError, match="anchored L3 manifest entry"):
        SealedL4ServingSource(
            root=result.run_root,
            projection=result.serving_projection,
            receipt=receipt,
            manifest=manifest,
            input_manifest=result.source.output_manifest,
        )


@pytest.mark.unit
def test_sealed_source_rejects_resealed_required_member_index_swap(
    tmp_path: Path,
) -> None:
    result = run_l4(
        _l3_with_sealed_manifest(tmp_path, member_count=2),
        state_root=tmp_path / ".fkg" / "l4",
    )
    manifest_rows = [
        dict(row) for row in result.rows.semantic_required_member_manifests
    ]
    member_rows = [
        dict(row) for row in result.rows.semantic_required_members
    ]
    assert len(member_rows) >= 2
    first_index = member_rows[0]["manifest_member_index"]
    member_rows[0]["manifest_member_index"] = member_rows[1][
        "manifest_member_index"
    ]
    member_rows[1]["manifest_member_index"] = first_index
    for row in member_rows[:2]:
        row["row_hash"] = canonical_sha256({
            key: value for key, value in row.items() if key != "row_hash"
        })
    manifest, _metrics, receipt = _rewrite_required_member_artifacts(
        result,
        manifest_rows,
        member_rows,
    )

    with pytest.raises(ValueError, match="carried C0 authority"):
        SealedL4ServingSource(
            root=result.run_root,
            projection=result.serving_projection,
            receipt=receipt,
            manifest=manifest,
            input_manifest=result.source.output_manifest,
        )


@pytest.mark.unit
def test_sealed_source_rejects_resealed_noncanonical_required_roles(
    tmp_path: Path,
) -> None:
    result = run_l4(
        _l3_with_sealed_manifest(tmp_path, roles=True),
        state_root=tmp_path / ".fkg" / "l4",
    )
    manifest_rows = [
        dict(row) for row in result.rows.semantic_required_member_manifests
    ]
    member_rows = [
        dict(row) for row in result.rows.semantic_required_members
    ]
    roles = list(manifest_rows[0]["required_role_ids"])
    assert roles
    manifest_rows[0]["required_role_ids"] = [*roles, roles[0]]
    manifest_rows[0]["row_hash"] = canonical_sha256({
        key: value
        for key, value in manifest_rows[0].items()
        if key != "row_hash"
    })
    for row in member_rows:
        row["required_role_ids"] = [*roles, roles[0]]
        row["row_hash"] = canonical_sha256({
            key: value for key, value in row.items() if key != "row_hash"
        })
    manifest, _metrics, receipt = _rewrite_required_member_artifacts(
        result,
        manifest_rows,
        member_rows,
    )

    with pytest.raises(ValueError, match="carried C0 authority"):
        SealedL4ServingSource(
            root=result.run_root,
            projection=result.serving_projection,
            receipt=receipt,
            manifest=manifest,
            input_manifest=result.source.output_manifest,
        )


@pytest.mark.unit
def test_required_member_physical_projection_is_exact(
    tmp_path: Path,
) -> None:
    manifest = _sealed_manifest(tmp_path)
    projected = project_required_members((manifest,))
    manifest_rows = project_required_member_manifests((manifest,))
    assert projected
    mutations = (
        ("member_canonical_id", "entity:wrong"),
        ("manifest_member_index", 9),
        ("member_role_id", "role:wrong"),
        ("member_order", 9),
        ("candidate_id", "candidate:wrong"),
        ("member_hash", "d" * 64),
        ("expected_cardinality", 9),
        ("required_role_ids", ["role:wrong"]),
        ("authoritative_collection_hash", "f" * 64),
        ("source_unit_manifest_hash", "e" * 64),
    )
    for field, replacement in mutations:
        changed = [dict(row) for row in projected]
        changed[0][field] = replacement
        with pytest.raises(
            L4ProjectionError,
            match="L4_REQUIRED_MEMBER_EQUIVALENCE_FAILED",
        ):
            validate_required_member_projection((manifest,), manifest_rows, changed)


@pytest.mark.unit
def test_required_member_projection_rejects_missing_and_extra_manifests(
    tmp_path: Path,
) -> None:
    manifest = _sealed_manifest(tmp_path)
    projected = project_required_members((manifest,))
    manifest_rows = project_required_member_manifests((manifest,))

    with pytest.raises(L4ProjectionError):
        validate_required_member_projection((manifest,), manifest_rows, ())
    with pytest.raises(L4ProjectionError):
        validate_required_member_projection((manifest,), (), projected)
    extra = [dict(row) for row in projected]
    extra[0]["required_member_manifest_id"] = "required-member-manifest:extra"
    with pytest.raises(L4ProjectionError):
        validate_required_member_projection((manifest,), manifest_rows, extra)


@pytest.mark.unit
def test_required_member_projection_preserves_empty_manifest_authority(
    tmp_path: Path,
) -> None:
    fact_set = _fact_set(
        "manufacturing",
        ordered=False,
        roles=False,
        expected_count=None,
    )
    l1_root, domain_path, l2 = _pipeline(
        tmp_path,
        "manufacturing",
        fact_set=fact_set,
    )
    l3 = _l3(tmp_path, l1_root, domain_path)
    values = l2.required_member_sets[0].proposal.model_dump(
        mode="python",
        exclude={
            "proposal_hash",
            "member_set_hash",
            "ordered_member_tuple_hash",
            "authoritative_collection_hash",
        },
    )
    values.update({
        "members": (),
        "expected_cardinality": 0,
        "minimum_cardinality": None,
        "maximum_cardinality": None,
    })
    proposal = RequiredMemberSetProposalV1_1.seal(**values)
    manifest = schema2_validation_stage._seal_manifest(
        proposal=proposal,
        identity=schema2_validation_stage._validation_identity(
            l3.inputs.l2_receipt.identity,
            contract_kind="l3.stage",
        ),
        sealed_at_utc=l3.inputs.l2_receipt.completed_at_utc,
    )

    manifest_rows = project_required_member_manifests((manifest,))
    member_rows = project_required_members((manifest,))
    assert len(manifest_rows) == 1
    assert manifest_rows[0]["member_count"] == 0
    assert member_rows == ()
    validate_required_member_projection((manifest,), manifest_rows, member_rows)
