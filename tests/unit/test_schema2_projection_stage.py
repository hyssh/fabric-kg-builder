"""Isolated L4 audit and asserted-only serving projection tests."""

from __future__ import annotations

import dataclasses
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from fabric_kg_builder.contracts.base import canonical_json, canonical_sha256
from fabric_kg_builder.contracts.extraction import (
    RequiredMemberManifestV1_1,
    RequiredMemberSetProposalV1_1,
)
from fabric_kg_builder.contracts.lifecycle import AssertionState
from fabric_kg_builder.contracts.publication import ProjectionEquivalence
from fabric_kg_builder.contracts.receipts import ArtifactManifest, StageReceipt
from fabric_kg_builder.contracts.resources import StageResourceMetrics
from fabric_kg_builder.enrichment import schema2_validation_stage
from fabric_kg_builder.model.arrow_schemas import L4_PROJECTION_TABLE_SCHEMAS
from fabric_kg_builder.semantic.source_tables import (
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
    l1_root, domain_path, _ = _pipeline(tmp_path, "records")
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


def _l3_with_sealed_manifest(tmp_path: Path):
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
