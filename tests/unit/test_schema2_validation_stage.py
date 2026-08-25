"""Isolated L3 evidence-validation stage tests over real persisted L2 output."""

from __future__ import annotations

import dataclasses
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.contracts.evidence import EvidenceSpanV1_1
from fabric_kg_builder.contracts.extraction import RequiredMemberManifestV1_1
from fabric_kg_builder.contracts.lifecycle import AssertionState
from fabric_kg_builder.contracts.receipts import ArtifactManifest, StageReceipt
from fabric_kg_builder.domain.models import CompletenessRequirementV2
from fabric_kg_builder.domain.stage import (
    finalize_l1_stage,
    preflight_l1_inputs,
    prepare_l1_stage,
)
from fabric_kg_builder.enrichment.schema2_evidence import (
    L3_EXTRACTION_PURPOSE,
    L3_EXTRACTION_PURPOSE_VERSION,
    L3_EXTRACTION_VERIFIER_NAME,
    L3_STAGE_NAME,
    L3StageError,
    SourceUnitIndex,
)
from fabric_kg_builder.enrichment.schema2_sources import IndexedSourceCorpusReader
from fabric_kg_builder.enrichment.schema2_stage import run_l2
from fabric_kg_builder.enrichment import schema2_validation_stage
from fabric_kg_builder.enrichment.schema2_validation_stage import (
    L3_ACCEPTED_VERSIONS,
    REMOTE_METRIC_DIMENSIONS,
    RequiredMemberOutcomeRecord,
    assert_l2_did_not_mint_l3_artifacts,
    l3_input_fingerprint,
    l3_leaf_checkpoint_path,
    l3_run_root,
    load_l3_inputs,
    run_l3,
)
from fabric_kg_builder.model.schemas import AssetRow, AssetVersionRow
from tests.unit.test_l1_stage import _candidates, _intake

_SENTENCE = "A governed record describes a governed subject."
_MEMBER_TYPES = ("subject", "witness")


def _fact_set(
    domain: str,
    *,
    ordered: bool,
    roles: bool,
    expected_count: int | None,
) -> dict:
    ordering = (
        {
            "mode": "ordered",
            "ordinal_property_id": f"property:{domain}.member-order",
            "ordinal_value_type": "integer",
            "direction": "ascending",
            "unique_ordinals": True,
            "contiguous": True,
        }
        if ordered
        else {
            "mode": "unordered",
            "ordinal_property_id": None,
            "ordinal_value_type": None,
            "direction": None,
            "unique_ordinals": None,
            "contiguous": None,
        }
    )
    cardinality = (
        None
        if expected_count is None
        else {
            "expected_count": expected_count,
            "minimum_count": None,
            "maximum_count": None,
            "count_basis": "distinct_members_per_aggregate",
            "source_kind": "competency_question",
            "source_question_ids": ["cq:q1"],
            "source_evidence_span_ids": [],
            "reviewed_rationale": "The approved question fixes the member count.",
        }
    )
    return {
        "aggregate_type_id": f"semantic-type:{domain}.record",
        "membership_relationship_type_id": (
            f"relationship-type:{domain}.record-subject"
        ),
        "allowed_member_type_ids": [f"semantic-type:{domain}.subject"],
        "member_role_ids": [f"role:{domain}.subject"] if roles else [],
        "ordering_policy": ordering,
        "cardinality": cardinality,
        "collection_identity_policy": {
            "aggregate_identity_included": True,
            "membership_relationship_included": True,
            "member_identities_included": True,
            "member_roles_included": roles,
            "ordinals_included": ordered,
            "preserve_member_order": ordered,
            "hash_algorithm": "sha256",
        },
        "membership_source_kind": "competency_question",
        "membership_evidence_span_ids": [],
        "membership_rationale": "The approved question governs membership.",
    }


def _approved_l1(
    tmp_path: Path,
    domain: str,
    *,
    fact_set: dict | None = None,
    fact_set_from_design_evidence=None,
    member_properties: tuple[dict, ...] = (),
    type_properties: Mapping[str, tuple[dict, ...]] | None = None,
    extra_types: tuple[dict, ...] = (),
    extra_relationship_targets: bool = False,
    include_visual: bool = False,
) -> tuple[Path, Path]:
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    visual = (
        f'<img src="figure.png" alt="{_SENTENCE}"/>' if include_visual else ""
    )
    (source / "record.html").write_text(
        f"<p>{_SENTENCE}</p>{visual}",
        encoding="utf-8",
    )
    preflight = preflight_l1_inputs(
        source_path=source,
        intake_raw=_intake(domain),
        project_id=f"project:{domain}",
        run_id=f"run:{domain}",
        model_version="fixture/1.0.0",
        model_hash=canonical_sha256({"fixture": domain}),
    )
    candidates = _candidates(domain)
    if extra_types:
        candidates["semantic_type_candidates"] = list(
            candidates["semantic_type_candidates"]
        ) + list(extra_types)
        extra_ids = [item["proposed_type"]["type_id"] for item in extra_types]
        relationship = candidates["relationship_candidates"][0]
        relationship["source_type_ids"] = list(relationship["source_type_ids"]) + (
            extra_ids
        )
        if extra_relationship_targets:
            relationship["target_type_ids"] = list(
                relationship["target_type_ids"]
            ) + extra_ids
        for index, type_id in enumerate(extra_ids, start=1):
            candidates["question_routes"][index]["start_type_id"] = type_id
    if member_properties:
        for candidate in candidates["semantic_type_candidates"]:
            if candidate["proposed_type"]["type_id"].endswith(".subject"):
                candidate["proposed_type"]["declared_properties"] = list(
                    member_properties
                )
    if type_properties:
        for candidate in candidates["semantic_type_candidates"]:
            type_id = candidate["proposed_type"]["type_id"]
            if type_id in type_properties:
                candidate["proposed_type"]["declared_properties"] = list(
                    type_properties[type_id]
                )
    if fact_set is not None:
        candidates["completeness_candidates"] = [
            {
                "candidate_id": "candidate:completeness",
                "proposed_requirement": {
                    "requirement_id": (
                        f"completeness-requirement:{domain}.record-subjects"
                    ),
                    "competency_question_ids": ["cq:q1"],
                    "requirement_kind": "structured_fact_set",
                    "scope_type_id": f"semantic-type:{domain}.record",
                    "scoped_subtype_id": None,
                    "scoped_filter": None,
                    "rationale": "Question q1 requires observed record members.",
                    "source_kind": "competency_question",
                    "source_question_ids": ["cq:q1"],
                    "governance_references": [],
                    "evidence_span_ids": [],
                    "coverage_status": "covered",
                    "unsupported_reason": None,
                    "required_roles": None,
                    "structured_fact_set": fact_set,
                },
                "score_inputs": {
                    "accepted_evidence_span_count": 0,
                    "required_evidence_span_count": 0,
                    "covered_competency_question_count": 1,
                    "total_relevant_competency_question_count": 1,
                    "ambiguity_conflict_count": 0,
                    "classification_fit": "exact",
                    "ip_governance_status": "eligible",
                },
            }
        ]
    prepared = prepare_l1_stage(preflight, candidates=candidates)
    if fact_set_from_design_evidence is not None:
        # Bounded L1 design evidence is only knowable after preparation, so the
        # approved contract is rebuilt once its exact span IDs are available.
        design_evidence_ids = tuple(
            sorted(span.evidence_span_id for span in prepared.evidence_spans)
        )
        assert design_evidence_ids
        return _approved_l1(
            tmp_path,
            domain,
            fact_set=fact_set_from_design_evidence(design_evidence_ids),
            member_properties=member_properties,
            type_properties=type_properties,
            extra_types=extra_types,
            extra_relationship_targets=extra_relationship_targets,
            include_visual=include_visual,
        )
    state_root = tmp_path / ".fkg" / "l1"
    domain_path = tmp_path / "domain.yaml"
    result = finalize_l1_stage(
        prepared,
        decision="approve",
        actor="reviewer@example.test",
        state_root=state_root,
        domain_path=domain_path,
    )
    assert result.status == "succeeded"
    return state_root, domain_path


def _reader(tmp_path: Path, l1_state_root: Path, domain_path: Path, domain: str):
    from fabric_kg_builder.enrichment.schema2_sources import load_l2_inputs

    inputs = load_l2_inputs(l1_state_root=l1_state_root, domain_path=domain_path)
    now = datetime.now(timezone.utc)
    assets = []
    versions = []
    for entry in inputs.corpus_manifest.entries:
        if entry.disposition != "eligible":
            continue
        assets.append(
            AssetRow(
                asset_id=entry.asset_id,
                project_id=f"project:{domain}",
                original_name=Path(entry.relative_source_ref).name,
                media_type=entry.media_type,
                source_uri=f"https://sharepoint.example/{entry.asset_id}",
                created_at=now,
                created_by="test",
            )
        )
        versions.append(
            AssetVersionRow(
                asset_version_id=entry.asset_version_id,
                asset_id=entry.asset_id,
                version_identity="v1",
                content_hash=entry.original_byte_hash,
                size_bytes=entry.byte_count,
                original_name=Path(entry.relative_source_ref).name,
                media_type=entry.media_type,
                source_uri=assets[-1].source_uri,
                blob_uri=f"https://storage.example/{entry.asset_version_id}",
                blob_version_id="v1",
                landing_path=entry.relative_source_ref,
                registered_at=now,
                landing_timestamp=now,
                ingestion_status="ready",
            )
        )
    return IndexedSourceCorpusReader(
        source_root=tmp_path / "source",
        assets=tuple(assets),
        versions=tuple(versions),
    )


class _Service:
    """Deterministic proposal service; anchors are untrusted L2 offsets only."""

    def __init__(
        self,
        domain: str,
        *,
        mutate=None,
    ) -> None:
        self.domain = domain
        self.calls = 0
        self._mutate = mutate

    def complete(self, *, prompt: str, work_unit) -> dict:
        self.calls += 1
        text = work_unit.text
        offset = work_unit.slice_start
        record_start = text.find("governed record")
        subject_start = text.find("governed subject")
        if record_start < 0 or subject_start < 0:
            return {"candidates": []}
        record_anchor = {
            "span_start": offset + record_start,
            "span_end": offset + record_start + len("governed record"),
            "quote": "governed record",
            "model_authored_evidence_id": None,
        }
        subject_anchor = {
            "span_start": offset + subject_start,
            "span_end": offset + subject_start + len("governed subject"),
            "quote": "governed subject",
            "model_authored_evidence_id": None,
        }
        sentence_anchor = {
            "span_start": offset,
            "span_end": offset + len(text.rstrip()),
            "quote": text.rstrip(),
            "model_authored_evidence_id": "model-evidence-must-not-be-trusted",
        }
        candidates = [
            {
                "candidate_kind": "entity",
                "local_id": "record-1",
                "observed_type": "Record",
                "label": "Record 1",
                "aliases": [],
                "identity_key": {},
                "stable_source_identity": None,
                "anchors": [record_anchor],
            },
            {
                "candidate_kind": "entity",
                "local_id": "subject-1",
                "observed_type": "Subject",
                "label": "Subject 1",
                "aliases": [],
                "identity_key": {},
                "stable_source_identity": None,
                "anchors": [subject_anchor],
            },
            {
                "candidate_kind": "relationship",
                "source_local_id": "record-1",
                "target_local_id": "subject-1",
                "observed_predicate": "describes",
                "direction": "source_to_target",
                "governed_context": "approved test context",
                "member_role_id": None,
                "member_order": None,
                "anchor": sentence_anchor,
            },
        ]
        if self._mutate is not None:
            candidates = self._mutate(candidates, work_unit)
        return {"candidates": candidates}


def _run_l2(tmp_path: Path, domain: str, service, l1_state_root, domain_path):
    return run_l2(
        reader=_reader(tmp_path, l1_state_root, domain_path, domain),
        service=service,
        state_root=tmp_path / ".fkg" / "l2",
        l1_state_root=l1_state_root,
        domain_path=domain_path,
        prompt_hash=canonical_sha256({"prompt": domain}),
        model_version="fixture/1.0.0",
        model_hash=canonical_sha256({"model": domain}),
    )


def _pipeline(
    tmp_path: Path,
    domain: str,
    *,
    fact_set: dict | None = None,
    fact_set_from_design_evidence=None,
    mutate=None,
    member_properties: tuple[dict, ...] = (),
    type_properties: Mapping[str, tuple[dict, ...]] | None = None,
    extra_types: tuple[dict, ...] = (),
    extra_relationship_targets: bool = False,
):
    l1_state_root, domain_path = _approved_l1(
        tmp_path,
        domain,
        fact_set=fact_set,
        fact_set_from_design_evidence=fact_set_from_design_evidence,
        member_properties=member_properties,
        type_properties=type_properties,
        extra_types=extra_types,
        extra_relationship_targets=extra_relationship_targets,
    )
    service = _Service(domain, mutate=mutate)
    l2 = _run_l2(tmp_path, domain, service, l1_state_root, domain_path)
    assert l2.receipt.status == "succeeded"
    return l1_state_root, domain_path, l2


def _l3(tmp_path: Path, l1_state_root: Path, domain_path: Path):
    return run_l3(
        state_root=tmp_path / ".fkg" / "l3",
        l2_state_root=tmp_path / ".fkg" / "l2",
        l1_state_root=l1_state_root,
        domain_path=domain_path,
    )


# ---------------------------------------------------------------------------
# Entry gate
# ---------------------------------------------------------------------------


def test_l3_requires_an_intact_l2_receipt(tmp_path: Path) -> None:
    with pytest.raises(L3StageError) as excinfo:
        load_l3_inputs(
            l2_state_root=tmp_path / "missing-l2",
            l1_state_root=tmp_path / "missing-l1",
            domain_path=tmp_path / "domain.yaml",
        )

    assert excinfo.value.code == "L3_INPUT_RECEIPT_INVALID"


def test_l3_entry_binds_every_sealed_authority(tmp_path: Path) -> None:
    l1_state_root, domain_path, _ = _pipeline(tmp_path, "records")

    inputs = load_l3_inputs(
        l2_state_root=tmp_path / ".fkg" / "l2",
        l1_state_root=l1_state_root,
        domain_path=domain_path,
    )

    assert inputs.l2_receipt.stage_id == "L2"
    assert inputs.corpus_manifest.inventory_scope == "complete"
    assert len(inputs.source_units) >= 1
    assert inputs.leaf_batch_ids
    assert set(inputs.authority_hashes) == {
        "domain_contract_hash",
        "hierarchy_hash",
        "identity_policy_hash",
        "completeness_requirement_hash",
        "external_reference_decision_hash",
    }
    # Hierarchy depth is reported on its own and never derived from K.
    assert inputs.hierarchy.hierarchy_depth == 1
    assert inputs.domain_contract.reasoning_policy.max_hops >= 1


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("receipt_status", "L3_INPUT_RECEIPT_INVALID"),
        ("output_manifest", "L3_INPUT_MANIFEST_INVALID"),
        ("source_unit", "L3_SOURCE_UNIT_MISSING"),
    ],
)
def test_l3_entry_fails_closed_on_tampered_handoff(
    tmp_path: Path,
    mutation: str,
    code: str,
) -> None:
    l1_state_root, domain_path, l2 = _pipeline(tmp_path, "records")
    state_root = tmp_path / ".fkg" / "l2"
    if mutation == "receipt_status":
        raw = json.loads((state_root / "stage-receipt.json").read_text("utf-8"))
        raw["stage_id"] = "L4"
        (state_root / "stage-receipt.json").write_text(
            json.dumps(raw), encoding="utf-8"
        )
    elif mutation == "output_manifest":
        raw = json.loads((state_root / "output-manifest.json").read_text("utf-8"))
        raw["manifest_hash"] = "0" * 64
        (state_root / "output-manifest.json").write_text(
            json.dumps(raw), encoding="utf-8"
        )
    else:
        unit_id = l2.materialized.source_units[0].source_unit_id
        (
            state_root / "source-units" / f"{unit_id.replace(':', '-', 1)}.json"
        ).unlink()

    with pytest.raises(L3StageError) as excinfo:
        load_l3_inputs(
            l2_state_root=state_root,
            l1_state_root=l1_state_root,
            domain_path=domain_path,
        )

    assert excinfo.value.code == code


def test_l3_requires_the_exact_l2_accepted_contract_versions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    l1_state_root, domain_path, _ = _pipeline(tmp_path, "records")
    legacy = dict(schema2_validation_stage.L2_ACCEPTED_VERSIONS)
    legacy["c0.required_member_set_proposal"] = "1.0.0"
    monkeypatch.setattr(schema2_validation_stage, "L2_ACCEPTED_VERSIONS", legacy)

    with pytest.raises(L3StageError) as excinfo:
        load_l3_inputs(
            l2_state_root=tmp_path / ".fkg" / "l2",
            l1_state_root=l1_state_root,
            domain_path=domain_path,
        )

    assert excinfo.value.code == "L3_CONTRACT_VERSION_UNSUPPORTED"


def test_l2_may_not_hand_off_l3_owned_artifacts(tmp_path: Path) -> None:
    _, _, l2 = _pipeline(tmp_path, "records")
    kinds = {entry.contract_kind for entry in l2.output_manifest.entries}
    assert not kinds & {"c0.evidence_span", "c0.required_member_manifest"}

    forged = l2.output_manifest.entries[0].model_copy(
        update={"contract_kind": "c0.evidence_span"}
    )
    tampered = ArtifactManifest.model_validate(
        {
            **l2.output_manifest.model_dump(mode="python", round_trip=True),
            "entries": (forged,),
            "total_row_count": forged.row_count or 0,
            "total_byte_count": forged.byte_count,
            "manifest_hash": canonical_sha256(
                {
                    "identity": l2.output_manifest.identity,
                    "artifact_manifest_id": (
                        l2.output_manifest.artifact_manifest_id
                    ),
                    "entries": (forged,),
                    "total_row_count": forged.row_count or 0,
                    "total_byte_count": forged.byte_count,
                }
            ),
        }
    )

    with pytest.raises(L3StageError) as excinfo:
        assert_l2_did_not_mint_l3_artifacts(tampered)

    assert excinfo.value.code == "L3_INPUT_MANIFEST_INVALID"


# ---------------------------------------------------------------------------
# Local-only validation, evidence, and lifecycle
# ---------------------------------------------------------------------------


def test_l3_asserts_grounded_candidates_with_local_1_1_evidence(
    tmp_path: Path,
) -> None:
    l1_state_root, domain_path, l2 = _pipeline(tmp_path, "records")

    result = _l3(tmp_path, l1_state_root, domain_path)

    assert result.receipt.status == "succeeded"
    assert result.receipt.stage_id == "L3"
    assert result.receipt.stage_name == L3_STAGE_NAME
    assert result.receipt.accepted_contract_versions == L3_ACCEPTED_VERSIONS
    states_by_kind: dict[str, set[str]] = {}
    for item in result.candidate_results:
        states_by_kind.setdefault(item.candidate_kind, set()).add(item.current_state)
    # Entity identity is recomputed from the persisted witness, so entities may
    # assert. Relationship direction is not persisted by the frozen L2 carrier,
    # so a relationship is never asserted as if its direction were proven.
    assert states_by_kind["entity"] == {"asserted"}
    assert states_by_kind["relationship"] == {"unsupported"}
    assert result.evidence_spans
    for span in result.evidence_spans:
        assert isinstance(span, EvidenceSpanV1_1)
        assert span.identity.contract_version == "1.1.0"
        assert span.purpose == L3_EXTRACTION_PURPOSE
        assert span.verifier_purpose_version == L3_EXTRACTION_PURPOSE_VERSION
        assert span.verifier_name == L3_EXTRACTION_VERIFIER_NAME
    relationships = [
        item for item in result.candidate_results if item.candidate_kind == "relationship"
    ]
    assert relationships
    for item in relationships:
        assert item.resolved_source_entity_id and item.resolved_target_entity_id
        assert item.source_inheritance_path and item.target_inheritance_path
        assert item.ignored_model_evidence_id == "model-evidence-must-not-be-trusted"
        assert "MODEL_EVIDENCE_ID_IGNORED" in item.reason_codes
        assert "EVIDENCE_MODALITY_UNSUPPORTED" in item.reason_codes
    assert not {
        item.ignored_model_evidence_id for item in relationships
    } & {span.evidence_span_id for span in result.evidence_spans}
    # Every retained candidate keeps exactly one appended current transition.
    appended = [
        record for leaf in result.leaves for record in leaf.lifecycle_records
    ]
    assert len(appended) == len(result.candidate_results)
    for record in appended:
        assert record.sequence == 1
        assert record.from_state is AssertionState.PROPOSED
        assert record.prior_lifecycle_record_id is not None
        assert record.validator_name == "l3-evidence-validator"
    assert l2.receipt.output_manifest_id != result.receipt.output_manifest_id


def test_l3_records_deterministic_reason_states_for_broken_proposals(
    tmp_path: Path,
) -> None:
    def mutate(candidates, work_unit):
        extra = [
            {
                "candidate_kind": "entity",
                "local_id": "unknown-1",
                "observed_type": "InventedThing",
                "label": "Invented",
                "aliases": [],
                "identity_key": {},
                "stable_source_identity": None,
                "anchors": [
                    {
                        "span_start": work_unit.slice_start,
                        "span_end": work_unit.slice_start + 1,
                        "quote": work_unit.text[:1],
                        "model_authored_evidence_id": None,
                    }
                ],
            },
            {
                "candidate_kind": "entity",
                "local_id": "no-evidence-1",
                "observed_type": "Subject",
                "label": "No Evidence",
                "aliases": [],
                "identity_key": {},
                "stable_source_identity": None,
                "anchors": [],
            },
            {
                "candidate_kind": "entity",
                "local_id": "bad-quote-1",
                "observed_type": "Subject",
                "label": "Bad Quote",
                "aliases": [],
                "identity_key": {},
                "stable_source_identity": None,
                "anchors": [
                    {
                        "span_start": work_unit.slice_start,
                        "span_end": work_unit.slice_start + 5,
                        "quote": "zzzzz",
                        "model_authored_evidence_id": None,
                    }
                ],
            },
        ]
        return candidates + extra

    l1_state_root, domain_path, _ = _pipeline(tmp_path, "records", mutate=mutate)

    result = _l3(tmp_path, l1_state_root, domain_path)

    by_state: dict[str, set[str]] = {}
    for item in result.candidate_results:
        by_state.setdefault(item.current_state, set()).update(item.reason_codes)
    assert "UNKNOWN_ENTITY_TYPE" in by_state["discovery"]
    assert "DOMAIN_REREVIEW_REQUESTED" in by_state["discovery"]
    assert "EVIDENCE_MISSING" in by_state["unresolved"]
    assert "EVIDENCE_QUOTE_MISMATCH" in by_state["rejected"]
    assert result.receipt.status == "succeeded"
    reason_index = json.loads(
        (result.run_root / "reason-code-index.json").read_text("utf-8")
    )
    assert reason_index["domain_rereview_requested"]
    assert reason_index["candidate_reason_counts"] == sorted(
        reason_index["candidate_reason_counts"]
    )


def test_l3_rejects_reverse_endpoints_and_unresolved_local_references(
    tmp_path: Path,
) -> None:
    def mutate(candidates, work_unit):
        reversed_edge = dict(candidates[2])
        reversed_edge["source_local_id"] = "subject-1"
        reversed_edge["target_local_id"] = "record-1"
        dangling = dict(candidates[2])
        dangling["source_local_id"] = "record-1"
        dangling["target_local_id"] = "missing-1"
        dangling["governed_context"] = "dangling context"
        return candidates + [reversed_edge, dangling]

    l1_state_root, domain_path, _ = _pipeline(tmp_path, "records", mutate=mutate)

    result = _l3(tmp_path, l1_state_root, domain_path)

    relationships = [
        item for item in result.candidate_results if item.candidate_kind == "relationship"
    ]
    reasons = {code for item in relationships for code in item.reason_codes}
    assert "DIRECTION_MISMATCH" in reasons
    assert "ENDPOINT_UNRESOLVED" in reasons
    states = {item.current_state for item in relationships}
    # A reversed endpoint signature stays rejected, a dangling local reference
    # stays unresolved, and a well-formed proposal is still not asserted because
    # the frozen carrier never persists the direction the model claimed.
    assert {"unsupported", "rejected", "unresolved"} == states
    assert all(item.current_state != "asserted" for item in relationships)


def test_l3_emits_no_remote_resource_usage_or_projection(tmp_path: Path) -> None:
    l1_state_root, domain_path, _ = _pipeline(tmp_path, "records")

    result = _l3(tmp_path, l1_state_root, domain_path)

    for dimension in REMOTE_METRIC_DIMENSIONS:
        assert getattr(result.metrics, dimension) == 0
    assert result.metrics.retry_count == 0
    assert not result.receipt.remote_operation_refs
    kinds = {entry.contract_kind for entry in result.output_manifest.entries}
    assert "c0.audit_projection" not in kinds
    assert "c0.semantic_serving_projection" not in kinds
    state_root = tmp_path / ".fkg" / "l3"
    assert not list(state_root.rglob("*projection*"))


def test_l3_is_not_activated_in_the_product_cli() -> None:
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "l3" not in result.output.casefold()
    assert "validation-stage" not in result.output.casefold()


# ---------------------------------------------------------------------------
# Manifest reconciliation, resume, and corruption
# ---------------------------------------------------------------------------


def test_l3_output_manifest_reconciles_and_replays_deterministically(
    tmp_path: Path,
) -> None:
    l1_state_root, domain_path, _ = _pipeline(tmp_path, "records")

    first = _l3(tmp_path, l1_state_root, domain_path)
    second = _l3(tmp_path, l1_state_root, domain_path)

    assert first.receipt.receipt_hash == second.receipt.receipt_hash
    assert first.output_manifest.manifest_hash == second.output_manifest.manifest_hash
    assert first.recomputed_leaf_count == len(first.leaves)
    assert second.reused_leaf_count == len(second.leaves)
    assert second.recomputed_leaf_count == 0
    first_ids = {span.evidence_span_id for span in first.evidence_spans}
    assert first_ids == {span.evidence_span_id for span in second.evidence_spans}
    kinds = {entry.contract_kind for entry in first.output_manifest.entries}
    assert {
        "c0.extraction_candidate_batch",
        "c0.candidate_lifecycle_record",
        "c0.evidence_span",
        "l2.proposed_candidate_partition",
        "l3.classification_assertion",
        "l3.current_state_index",
        "l3.identity_index",
        "l3.property_observation",
        "l3.reason_code_index",
    } <= kinds
    manifest = ArtifactManifest.model_validate_json(
        (first.run_root / "output-manifest.json").read_text("utf-8")
    )
    assert manifest.manifest_hash == first.output_manifest.manifest_hash


def test_l3_reruns_only_a_corrupt_leaf_and_reuses_intact_leaves(
    tmp_path: Path,
) -> None:
    l1_state_root, domain_path, _ = _pipeline(tmp_path, "records")
    first = _l3(tmp_path, l1_state_root, domain_path)
    leaf_dir = first.state_root / "leaves"
    corrupted = sorted(leaf_dir.rglob("*.json"))[0]
    corrupted.write_text("{ not json", encoding="utf-8")

    second = _l3(tmp_path, l1_state_root, domain_path)

    assert second.recomputed_leaf_count == 1
    assert second.reused_leaf_count == len(second.leaves) - 1
    assert second.receipt.receipt_hash == first.receipt.receipt_hash

    (first.run_root / "stage-receipt.json").unlink()
    recovered = _l3(tmp_path, l1_state_root, domain_path)
    assert recovered.receipt.receipt_hash == first.receipt.receipt_hash


def test_l3_blocks_when_the_domain_authority_drifts(tmp_path: Path) -> None:
    l1_state_root, domain_path, _ = _pipeline(tmp_path, "records")
    _l3(tmp_path, l1_state_root, domain_path)
    text = domain_path.read_text(encoding="utf-8")
    domain_path.write_text(
        text.replace("A governed record.", "A governed record edited."),
        encoding="utf-8",
    )

    with pytest.raises(L3StageError) as excinfo:
        _l3(tmp_path, l1_state_root, domain_path)

    assert excinfo.value.code in {
        "L3_DOMAIN_HASH_MISMATCH",
        "L3_INPUT_MANIFEST_INVALID",
    }


# ---------------------------------------------------------------------------
# Domain-neutral structured collections
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("domain", "ordered", "roles"),
    [
        ("manufacturing", False, False),
        ("clinical", False, True),
        ("logistics", True, True),
    ],
)
def test_l3_keeps_generic_collections_unresolved_without_membership_proof(
    tmp_path: Path,
    domain: str,
    ordered: bool,
    roles: bool,
) -> None:
    def mutate(candidates, work_unit):
        edge = dict(candidates[2])
        edge["member_role_id"] = f"role:{domain}.subject" if roles else None
        edge["member_order"] = 0 if ordered else None
        return candidates[:2] + [edge]

    fact_set = _fact_set(domain, ordered=ordered, roles=roles, expected_count=None)
    member_properties = (
        (
            {
                "property_id": f"property:{domain}.member-order",
                "display_name": "Member Order",
                "value_type": "integer",
                "required": False,
            },
        )
        if ordered
        else ()
    )
    l1_state_root, domain_path, l2 = _pipeline(
        tmp_path,
        domain,
        fact_set=fact_set,
        mutate=mutate,
        member_properties=member_properties,
    )
    assert len(l2.required_member_sets) == 1

    result = _l3(tmp_path, l1_state_root, domain_path)

    # Membership is carried by a relationship candidate, and the frozen L2
    # carrier cannot prove its direction, so membership never asserts and the
    # collection stays audit-addressable instead of being sealed as complete.
    assert len(result.required_member_outcomes) == 1
    record = result.required_member_outcomes[0]
    assert record.outcome.completeness_state == "unresolved"
    assert record.manifest is None
    assert "MEMBERSHIP_EVIDENCE_INVALID" in record.outcome.reason_codes
    assert record.outcome.verified_member_ids == ()
    assert not result.required_member_manifests
    assert not list((result.run_root / "required-member-manifests").glob("*.json"))
    outcome_files = sorted(
        (result.run_root / "required-member-outcomes").glob("*.json")
    )
    assert len(outcome_files) == 1
    payload = json.loads(outcome_files[0].read_text("utf-8"))
    assert payload["required_member_manifest_id"] is None
    assert payload["reason_codes"]


def test_l3_seals_a_complete_collection_only_through_the_c0_factory(
    tmp_path: Path,
) -> None:
    """The complete carrier is sealed by C0 alone and repeats its proposal."""

    fact_set = _fact_set(
        "manufacturing",
        ordered=False,
        roles=False,
        expected_count=None,
    )
    l1_state_root, domain_path, l2 = _pipeline(
        tmp_path,
        "manufacturing",
        fact_set=fact_set,
    )
    result = _l3(tmp_path, l1_state_root, domain_path)
    proposal = l2.required_member_sets[0].proposal
    identity = schema2_validation_stage._validation_identity(
        result.inputs.l2_receipt.identity,
        contract_kind="l3.stage",
    )

    manifest = schema2_validation_stage._seal_manifest(
        proposal=proposal,
        identity=identity,
        sealed_at_utc=result.inputs.l2_receipt.completed_at_utc,
    )

    assert isinstance(manifest, RequiredMemberManifestV1_1)
    assert manifest.identity.contract_version == "1.1.0"
    assert manifest.required_member_set_proposal_id == (
        proposal.required_member_set_proposal_id
    )
    assert manifest.required_member_set_proposal_hash == proposal.proposal_hash
    assert manifest.validator_name == "l3-evidence-validator"
    assert (
        manifest.authoritative_collection_hash
        == proposal.authoritative_collection_hash
    )
    manifest.validate_against_proposal(proposal)
    # A sealed manifest and an unresolved outcome are the only two carriers, and
    # they partition the proposal set exactly once each.
    sealed = RequiredMemberOutcomeRecord(
        outcome=result.required_member_outcomes[0].outcome,
        manifest=manifest,
    )
    schema2_validation_stage._reconcile_collection_partition(
        proposals=result.inputs.required_member_proposals,
        outcomes=(sealed,),
    )
    schema2_validation_stage._reconcile_collection_partition(
        proposals=result.inputs.required_member_proposals,
        outcomes=result.required_member_outcomes,
    )
    with pytest.raises(L3StageError) as excinfo:
        schema2_validation_stage._reconcile_collection_partition(
            proposals=result.inputs.required_member_proposals,
            outcomes=(sealed,) + tuple(result.required_member_outcomes),
        )

    assert excinfo.value.code == "L3_VALIDATION_RESULT_INCOMPLETE"


def test_l3_keeps_an_incomplete_collection_unresolved_without_padding(
    tmp_path: Path,
) -> None:
    def mutate(candidates, work_unit):
        broken = dict(candidates[2])
        broken["anchor"] = {
            "span_start": work_unit.slice_start,
            "span_end": work_unit.slice_start + 5,
            "quote": "zzzzz",
            "model_authored_evidence_id": None,
        }
        return candidates[:2] + [broken]

    fact_set = _fact_set(
        "manufacturing",
        ordered=False,
        roles=False,
        expected_count=None,
    )
    l1_state_root, domain_path, _ = _pipeline(
        tmp_path,
        "manufacturing",
        fact_set=fact_set,
        mutate=mutate,
    )

    result = _l3(tmp_path, l1_state_root, domain_path)

    assert len(result.required_member_outcomes) == 1
    record = result.required_member_outcomes[0]
    assert record.manifest is None
    assert record.outcome.completeness_state == "unresolved"
    assert "MEMBERSHIP_EVIDENCE_INVALID" in record.outcome.reason_codes
    assert not result.required_member_manifests
    assert not list((result.run_root / "required-member-manifests").glob("*.json"))
    outcome_files = sorted(
        (result.run_root / "required-member-outcomes").glob("*.json")
    )
    assert len(outcome_files) == 1
    payload = json.loads(outcome_files[0].read_text("utf-8"))
    assert payload["required_member_manifest_id"] is None
    assert payload["reason_codes"]


# ---------------------------------------------------------------------------
# Stable identity across classification versions
# ---------------------------------------------------------------------------


def _subtypes(domain: str) -> tuple[dict, ...]:
    parent = f"semantic-type:{domain}.record"
    return tuple(
        {
            "candidate_id": f"candidate:type-record-{suffix}",
            "proposed_type": {
                "type_id": f"{parent}-{suffix}",
                "semantic_key": f"record_{suffix}",
                "display_name": f"Record {suffix.upper()}",
                "description": f"A governed record specialization {suffix}.",
                "aliases": [],
                "classification": "domain_specialization",
                "parent_type_id": parent,
                "abstract": False,
                "identity_root_type_id": parent,
                "identity_key_policy": None,
                "declared_properties": [],
                "declared_constraints": [],
                "sibling_classification_policy": {
                    "mode": "unresolved",
                    "discriminator_property_id": None,
                    "rationale": "Competing siblings stay unresolved.",
                },
                "generalization_basis": {
                    "competency_question_ids": [],
                    "evidence_span_ids": [],
                    "governance_rationale": "Reviewed generalization basis.",
                },
                "evidence_span_ids": [],
                "competency_question_ids": [f"cq:q{index}" for index in range(1, 6)],
                "governance_rationale": "Required by approved competency questions.",
                "tombstoned": False,
            },
            "score_inputs": {
                "accepted_evidence_span_count": 0,
                "required_evidence_span_count": 0,
                "covered_competency_question_count": 5,
                "total_relevant_competency_question_count": 5,
                "ambiguity_conflict_count": 0,
                "classification_fit": "exact",
                "ip_governance_status": "eligible",
            },
        }
        for suffix in ("a", "b")
    )


def test_l3_keeps_one_stable_entity_id_across_competing_siblings(
    tmp_path: Path,
) -> None:
    def mutate(candidates, work_unit):
        offset = work_unit.slice_start
        text = work_unit.text
        first = text.find("governed record")
        second = text.find("record", first + 1) if first >= 0 else -1
        sibling_a = dict(candidates[0])
        sibling_a["observed_type"] = "Record A"
        sibling_b = dict(candidates[0])
        sibling_b["observed_type"] = "Record B"
        sibling_b["anchors"] = [
            {
                "span_start": offset + first + len("governed "),
                "span_end": offset + first + len("governed record"),
                "quote": "record",
                "model_authored_evidence_id": None,
            }
        ]
        del second
        return [sibling_a, sibling_b] + list(candidates[1:])

    l1_state_root, domain_path = _approved_l1(
        tmp_path,
        "records",
        extra_types=_subtypes("records"),
    )
    _run_l2(
        tmp_path,
        "records",
        _Service("records", mutate=mutate),
        l1_state_root,
        domain_path,
    )

    result = _l3(tmp_path, l1_state_root, domain_path)

    entities = [
        item for item in result.candidate_results if item.candidate_kind == "entity"
    ]
    record_entities = [
        item
        for item in entities
        if item.approved_semantic_id
        in {
            "semantic-type:records.record-a",
            "semantic-type:records.record-b",
        }
    ]
    assert len(record_entities) == 2
    assert len({item.semantic_id for item in record_entities}) == 1
    assert all(item.current_state == "unresolved" for item in record_entities)
    assert all(
        "AMBIGUOUS_SIBLING_CLASSIFICATION" in item.reason_codes
        for item in record_entities
    )
    assert all(item.identity_recomputed for item in record_entities)
    index = json.loads(
        (result.run_root / "identity-index.json").read_text("utf-8")
    )
    stable = [
        item
        for item in index["entities"]
        if item["entity_id"] == record_entities[0].semantic_id
    ]
    assert len(stable) == 1
    assert len(stable[0]["classification_version_ids"]) == 2
    assert sorted(stable[0]["semantic_type_ids"]) == [
        "semantic-type:records.record-a",
        "semantic-type:records.record-b",
    ]


def test_l3_prefers_the_most_specific_concrete_classification(tmp_path: Path) -> None:
    def mutate(candidates, work_unit):
        offset = work_unit.slice_start
        text = work_unit.text
        first = text.find("governed record")
        specialized = dict(candidates[0])
        specialized["observed_type"] = "Record A"
        specialized["anchors"] = [
            {
                "span_start": offset + first + len("governed "),
                "span_end": offset + first + len("governed record"),
                "quote": "record",
                "model_authored_evidence_id": None,
            }
        ]
        return [candidates[0], specialized] + list(candidates[1:])

    l1_state_root, domain_path = _approved_l1(
        tmp_path,
        "records",
        extra_types=_subtypes("records"),
    )
    _run_l2(
        tmp_path,
        "records",
        _Service("records", mutate=mutate),
        l1_state_root,
        domain_path,
    )

    result = _l3(tmp_path, l1_state_root, domain_path)

    by_type = {
        item.approved_semantic_id: item
        for item in result.candidate_results
        if item.candidate_kind == "entity"
    }
    specialized = by_type["semantic-type:records.record-a"]
    generic = by_type["semantic-type:records.record"]
    assert specialized.semantic_id == generic.semantic_id
    assert specialized.current_state == "asserted"
    assert generic.current_state == "unresolved"
    classifications = [
        item for leaf in result.leaves for item in leaf.classifications
    ]
    assert len({item.classification_version_id for item in classifications}) == len(
        classifications
    )
    ancestor_paths = {
        item.semantic_type_id: item.ancestor_path for item in classifications
    }
    assert ancestor_paths["semantic-type:records.record-a"] == (
        "semantic-type:records.record-a",
        "semantic-type:records.record",
    )


def test_l3_resolves_case_folded_local_endpoint_references(tmp_path: Path) -> None:
    def mutate(candidates, work_unit):
        edge = dict(candidates[2])
        edge["source_local_id"] = "RECORD-1"
        edge["target_local_id"] = "Subject-1"
        return candidates[:2] + [edge]

    l1_state_root, domain_path, _ = _pipeline(tmp_path, "records", mutate=mutate)

    result = _l3(tmp_path, l1_state_root, domain_path)

    entity_ids = {
        item.approved_semantic_id: item.semantic_id
        for item in result.candidate_results
        if item.candidate_kind == "entity"
    }
    relationships = [
        item
        for item in result.candidate_results
        if item.candidate_kind == "relationship"
    ]
    assert len(relationships) == 1
    # Case-folded local references still resolve to exactly one retained entity,
    # but an unprovable direction keeps the edge out of the asserted set.
    assert relationships[0].current_state == "unsupported"
    assert relationships[0].resolved_source_entity_id == (
        entity_ids["semantic-type:records.record"]
    )
    assert relationships[0].resolved_target_entity_id == (
        entity_ids["semantic-type:records.subject"]
    )


def test_l3_marks_unverifiable_modalities_unsupported_not_rejected(
    tmp_path: Path,
) -> None:
    l1_state_root, domain_path = _approved_l1(
        tmp_path,
        "records",
        include_visual=True,
    )
    _run_l2(
        tmp_path,
        "records",
        _Service("records"),
        l1_state_root,
        domain_path,
    )

    result = _l3(tmp_path, l1_state_root, domain_path)

    visual_units = {
        unit.source_unit_id
        for unit in result.inputs.source_units.units
        if unit.unit_kind == "visual_description"
    }
    assert visual_units
    unsupported = [
        item
        for item in result.candidate_results
        if item.source_unit_id in visual_units
    ]
    assert unsupported
    for item in unsupported:
        assert item.current_state == "unsupported"
        assert "EVIDENCE_MODALITY_UNSUPPORTED" in item.reason_codes
    assert any(
        item.current_state == "asserted"
        for item in result.candidate_results
        if item.source_unit_id not in visual_units
    )


def test_l3_separates_missing_evidence_from_ungrounded_endpoints(
    tmp_path: Path,
) -> None:
    def mutate(candidates, work_unit):
        offset = work_unit.slice_start
        text = work_unit.text
        unevidenced = dict(candidates[2])
        unevidenced["anchor"] = None
        narrow_start = text.find("describes")
        ungrounded = dict(candidates[2])
        ungrounded["anchor"] = {
            "span_start": offset + narrow_start,
            "span_end": offset + narrow_start + len("describes"),
            "quote": "describes",
            "model_authored_evidence_id": None,
        }
        return candidates + [unevidenced, ungrounded]

    l1_state_root, domain_path, _ = _pipeline(tmp_path, "records", mutate=mutate)

    result = _l3(tmp_path, l1_state_root, domain_path)

    relationships = [
        item
        for item in result.candidate_results
        if item.candidate_kind == "relationship"
    ]
    assert len(relationships) == 3
    by_state = {item.current_state: item for item in relationships}
    assert set(by_state) == {"unsupported", "unresolved", "rejected"}
    # A missing anchor stays unresolved and keeps its precise reason instead of
    # being masked by the unprovable-direction capability gap.
    assert by_state["unresolved"].reason_codes == ("EVIDENCE_MISSING",)
    assert by_state["unresolved"].evidence_span_ids == ()
    assert "ENDPOINT_EVIDENCE_UNGROUNDED" in by_state["rejected"].reason_codes
    assert by_state["rejected"].evidence_span_ids
    assert by_state["unsupported"].evidence_span_ids
    assert by_state["unsupported"].reason_codes == (
        "EVIDENCE_MODALITY_UNSUPPORTED",
        "MODEL_EVIDENCE_ID_IGNORED",
    )


def test_l3_governs_property_observations_against_effective_properties(
    tmp_path: Path,
) -> None:
    def mutate(candidates, work_unit):
        offset = work_unit.slice_start
        text = work_unit.text
        subject_start = text.find("governed subject")
        anchor = {
            "span_start": offset + subject_start,
            "span_end": offset + subject_start + len("governed subject"),
            "quote": "governed subject",
            "model_authored_evidence_id": None,
        }
        approved = {
            "candidate_kind": "property",
            "owner_local_id": "subject-1",
            "observed_property": "Member Order",
            "value": 0,
            "normalized_value": 0,
            "temporal_key": None,
            "anchor": anchor,
        }
        unknown = {
            "candidate_kind": "property",
            "owner_local_id": "subject-1",
            "observed_property": "Invented Property",
            "value": "x",
            "normalized_value": "x",
            "temporal_key": None,
            "anchor": dict(anchor, span_end=anchor["span_start"] + 8, quote="governed"),
        }
        return candidates + [approved, unknown]

    member_properties = (
        {
            "property_id": "property:records.member-order",
            "display_name": "Member Order",
            "value_type": "integer",
            "required": False,
        },
    )
    l1_state_root, domain_path, _ = _pipeline(
        tmp_path,
        "records",
        mutate=mutate,
        member_properties=member_properties,
    )

    result = _l3(tmp_path, l1_state_root, domain_path)

    observations = [
        item for leaf in result.leaves for item in leaf.property_observations
    ]
    assert len(observations) == 2
    by_property = {item.effective_property_id: item for item in observations}
    approved = by_property["property:records.member-order"]
    # The frozen carrier persists neither the property owner nor the observed
    # value, so inherited-property validity and value conformance are recorded
    # as an explicit capability gap instead of being claimed as validated.
    assert approved.observation_state == "unsupported"
    assert approved.value_type == "integer"
    assert approved.constraint_outcome == ()
    assert "EVIDENCE_MODALITY_UNSUPPORTED" in approved.reason_codes
    assert approved.evidence_span_ids
    unknown = by_property[None]
    assert unknown.observation_state == "discovery"
    assert unknown.constraint_outcome == ("UNKNOWN_PROPERTY",)
    assert "EVIDENCE_MODALITY_UNSUPPORTED" not in unknown.reason_codes
    assert "DOMAIN_REREVIEW_REQUESTED" in unknown.reason_codes


def test_l3_modules_stay_isolated_from_cli_serving_and_schema1() -> None:
    from fabric_kg_builder.enrichment import schema2_evidence

    for module in (schema2_evidence, schema2_validation_stage):
        source = Path(module.__file__).read_text(encoding="utf-8")
        imports = [
            line.strip()
            for line in source.splitlines()
            if line.startswith(("import ", "from "))
        ]
        joined = "\n".join(imports)
        for forbidden in (
            "fabric_kg_builder.cli",
            "fabric_kg_builder.graph",
            "fabric_kg_builder.serving",
            "fabric_kg_builder.compile",
            "fabric_kg_builder.deploy",
            "fabric_kg_builder.runtime",
            "model.arrow_schemas",
            "model.ids",
            "contracts.projection",
            "AuditProjection",
            "SemanticServingProjection",
            "make_entity_id",
        ):
            assert forbidden not in joined, f"{module.__name__} imports {forbidden}"


def test_l3_never_infers_or_pads_an_unsatisfied_specified_count(
    tmp_path: Path,
) -> None:
    fact_set = _fact_set(
        "logistics",
        ordered=False,
        roles=False,
        expected_count=2,
    )
    l1_state_root, domain_path, l2 = _pipeline(
        tmp_path,
        "logistics",
        fact_set=fact_set,
    )
    proposal = l2.required_member_sets[0].proposal
    assert proposal.expected_cardinality == 2
    assert len(proposal.members) == 1

    result = _l3(tmp_path, l1_state_root, domain_path)

    record = result.required_member_outcomes[0]
    assert record.manifest is None
    assert record.outcome.completeness_state == "unresolved"
    assert "CARDINALITY_BOUND_VIOLATION" in record.outcome.reason_codes
    assert record.outcome.specified_expected_count == 2
    # An unsatisfied approved count is never padded, inferred, or downgraded.
    assert record.outcome.verified_member_count < 2
    assert not result.required_member_manifests


def test_l3_receipt_exposes_every_resolved_authority_hash(tmp_path: Path) -> None:
    l1_state_root, domain_path, _ = _pipeline(tmp_path, "records")

    result = _l3(tmp_path, l1_state_root, domain_path)

    binding = json.loads(
        json.dumps(
            {
                entry.artifact_id: entry.content_hash
                for entry in result.input_manifest.entries
            }
        )
    )
    assert "l3-authority-binding" in binding
    assert result.receipt.identity.domain_contract_hash == (
        result.inputs.hierarchy.domain_contract_hash
    )
    assert result.receipt.skip_key
    receipt = StageReceipt.model_validate_json(
        (result.run_root / "stage-receipt.json").read_text("utf-8")
    )
    assert receipt.receipt_hash == result.receipt.receipt_hash


# ---------------------------------------------------------------------------
# Checkpoint integrity and fail-closed carrier limits
# ---------------------------------------------------------------------------


def _leaf_checkpoints(result) -> list[Path]:
    paths = sorted((result.state_root / "leaves").rglob("*.json"))
    assert paths
    for leaf in result.leaves:
        expected = l3_leaf_checkpoint_path(
            result.state_root,
            leaf.extraction_candidate_batch_id,
            leaf.leaf_fingerprint,
        )
        assert expected.exists()
    assert result.run_root == l3_run_root(
        result.state_root,
        result.receipt.skip_key,
    )
    return paths


def _checkpoint_for(result, candidate_id: str) -> Path:
    for path in _leaf_checkpoints(result):
        raw = json.loads(path.read_text("utf-8"))
        if any(
            item["candidate_id"] == candidate_id
            for item in raw["candidate_results"]
        ):
            return path
    raise AssertionError(f"no leaf checkpoint carries {candidate_id}")


def _non_asserted_candidate(result):
    return next(
        item for item in result.candidate_results if item.current_state != "asserted"
    )


def _reseal_lifecycle_record(
    record: dict,
    *,
    to_state: str,
    reason_codes: list[str],
) -> None:
    """Recompute the unkeyed C0 transition hash over forged semantic content."""

    record["to_state"] = to_state
    record["reason_codes"] = reason_codes
    record["transition_hash"] = canonical_sha256(
        {
            key: value
            for key, value in record.items()
            if key not in {"transition_hash", "occurred_at_utc"}
        }
    )


def _rewrite_leaf(path: Path, mutate, *, reseal: bool) -> None:
    raw = json.loads(path.read_text("utf-8"))
    mutate(raw)
    if reseal:
        raw["leaf_payload_hash"] = canonical_sha256(
            {key: value for key, value in raw.items() if key != "leaf_payload_hash"}
        )
    path.write_text(json.dumps(raw), encoding="utf-8")


def test_l3_discards_a_leaf_whose_payload_hash_no_longer_recomputes(
    tmp_path: Path,
) -> None:
    l1_state_root, domain_path, _ = _pipeline(tmp_path, "records")
    first = _l3(tmp_path, l1_state_root, domain_path)
    target = _non_asserted_candidate(first)
    checkpoint = _checkpoint_for(first, target.candidate_id)

    def flip(raw: dict) -> None:
        for item in raw["candidate_results"]:
            if item["candidate_id"] == target.candidate_id:
                item["current_state"] = "asserted"
                item["reason_codes"] = []

    _rewrite_leaf(checkpoint, flip, reseal=False)

    second = _l3(tmp_path, l1_state_root, domain_path)

    # A stale payload hash is never trusted; the leaf recomputes byte-identically.
    assert second.recomputed_leaf_count == 1
    assert second.receipt.receipt_hash == first.receipt.receipt_hash
    assert {
        (item.candidate_id, item.current_state)
        for item in second.candidate_results
    } == {
        (item.candidate_id, item.current_state) for item in first.candidate_results
    }


def test_l3_rejects_a_structurally_valid_resealed_leaf_tampering(
    tmp_path: Path,
) -> None:
    l1_state_root, domain_path, _ = _pipeline(tmp_path, "records")
    first = _l3(tmp_path, l1_state_root, domain_path)
    tampered_candidate = _non_asserted_candidate(first)
    checkpoint = _checkpoint_for(first, tampered_candidate.candidate_id)

    def upgrade_state_only(raw: dict) -> None:
        for item in raw["candidate_results"]:
            if item["candidate_id"] == tampered_candidate.candidate_id:
                item["current_state"] = "asserted"

    def upgrade_state_and_reasons(raw: dict) -> None:
        for item in raw["candidate_results"]:
            if item["candidate_id"] == tampered_candidate.candidate_id:
                item["current_state"] = "asserted"
                item["reason_codes"] = []

    # A resealed payload passes the cache integrity gate, so reconciliation must
    # re-derive the state from the reason codes and the sealed transition.
    _rewrite_leaf(checkpoint, upgrade_state_only, reseal=True)
    with pytest.raises(L3StageError) as contradiction:
        _l3(tmp_path, l1_state_root, domain_path)
    assert contradiction.value.code == "L3_LIFECYCLE_CHAIN_INVALID"
    assert "contradicts its reasons" in str(contradiction.value)

    _rewrite_leaf(checkpoint, upgrade_state_and_reasons, reseal=True)
    with pytest.raises(L3StageError) as divergence:
        _l3(tmp_path, l1_state_root, domain_path)
    assert divergence.value.code == "L3_LIFECYCLE_CHAIN_INVALID"
    assert "diverges from its sealed transition" in str(divergence.value)


def test_l3_never_asserts_a_relationship_without_persisted_direction_proof(
    tmp_path: Path,
) -> None:
    def mutate(candidates, work_unit):
        reverse = dict(candidates[2])
        reverse["direction"] = "reverse"
        reverse["governed_context"] = "reverse context"
        unknown = dict(candidates[2])
        unknown["direction"] = "unknown"
        unknown["governed_context"] = "unknown context"
        return candidates + [reverse, unknown]

    l1_state_root, domain_path, _ = _pipeline(tmp_path, "records", mutate=mutate)

    result = _l3(tmp_path, l1_state_root, domain_path)

    relationships = [
        item
        for item in result.candidate_results
        if item.candidate_kind == "relationship"
    ]
    assert len(relationships) == 3
    # L2 folds the model-proposed direction into the relationship identity seed
    # without persisting it, so forward, reverse, and unknown proposals are
    # indistinguishable locally and none of them may assert.
    for item in relationships:
        assert item.current_state == "unsupported"
        assert "EVIDENCE_MODALITY_UNSUPPORTED" in item.reason_codes
    sealed = {
        record.candidate_id: record
        for leaf in result.leaves
        for record in leaf.lifecycle_records
    }
    for item in relationships:
        assert sealed[item.candidate_id].to_state is AssertionState.UNSUPPORTED
    assert not any(
        record.current_state == "asserted"
        for record in result.candidate_results
        if record.candidate_kind in {"relationship", "property"}
    )


def test_l3_never_asserts_a_property_without_owner_and_value_proof(
    tmp_path: Path,
) -> None:
    def mutate(candidates, work_unit):
        offset = work_unit.slice_start
        subject_start = work_unit.text.find("governed subject")
        anchor = {
            "span_start": offset + subject_start,
            "span_end": offset + subject_start + len("governed subject"),
            "quote": "governed subject",
            "model_authored_evidence_id": None,
        }
        return candidates + [
            {
                "candidate_kind": "property",
                "owner_local_id": "subject-1",
                "observed_property": "Member Order",
                "value": 0,
                "normalized_value": 0,
                "temporal_key": None,
                "anchor": anchor,
            }
        ]

    member_properties = (
        {
            "property_id": "property:records.member-order",
            "display_name": "Member Order",
            "value_type": "integer",
            "required": False,
        },
    )
    l1_state_root, domain_path, _ = _pipeline(
        tmp_path,
        "records",
        mutate=mutate,
        member_properties=member_properties,
    )

    result = _l3(tmp_path, l1_state_root, domain_path)

    properties = [
        item
        for item in result.candidate_results
        if item.candidate_kind == "property"
    ]
    assert len(properties) == 1
    observation = properties[0]
    assert observation.approved_semantic_id == "property:records.member-order"
    assert observation.evidence_span_ids
    # Owner attribution and observed value are not persisted, so inheritance and
    # value conformance are never claimed as validated.
    assert observation.current_state == "unsupported"
    assert "EVIDENCE_MODALITY_UNSUPPORTED" in observation.reason_codes
    assert "INHERITED_PROPERTY_INVALID" not in observation.reason_codes
    assert "PROPERTY_VALUE_INVALID" not in observation.reason_codes


def test_l3_rejects_design_sample_evidence_as_extraction_count_proof(
    tmp_path: Path,
) -> None:
    def build(design_evidence_ids: tuple[str, ...]) -> dict:
        fact_set = _fact_set(
            "manufacturing",
            ordered=False,
            roles=False,
            expected_count=1,
        )
        fact_set["cardinality"] = {
            "expected_count": 1,
            "minimum_count": None,
            "maximum_count": None,
            "count_basis": "distinct_members_per_aggregate",
            "source_kind": "source_evidence",
            "source_question_ids": [],
            "source_evidence_span_ids": [design_evidence_ids[0]],
            "reviewed_rationale": None,
        }
        return fact_set

    l1_state_root, domain_path, l2 = _pipeline(
        tmp_path,
        "manufacturing",
        fact_set_from_design_evidence=build,
    )
    requirement = l2.required_member_sets[0].proposal.completeness_requirement_id
    assert requirement

    result = _l3(tmp_path, l1_state_root, domain_path)

    design_evidence_ids = {
        evidence_id
        for entry in result.inputs.design_sample_manifest.entries
        for evidence_id in entry.evidence_span_ids
    }
    minted = {span.evidence_span_id for span in result.evidence_spans}
    assert design_evidence_ids and not design_evidence_ids & minted
    record = result.required_member_outcomes[0]
    # Bounded L1 design evidence is design context and can never prove an
    # extraction count, so the collection stays unresolved.
    assert record.manifest is None
    assert record.outcome.completeness_state == "unresolved"
    assert "CARDINALITY_EVIDENCE_INVALID" in record.outcome.reason_codes


def test_l3_rejects_a_model_controlled_stable_source_identity_collision(
    tmp_path: Path,
) -> None:
    def mutate(candidates, work_unit):
        offset = work_unit.slice_start
        text = work_unit.text
        first = text.find("governed record")
        colliding = dict(candidates[0])
        colliding["local_id"] = "record-2"
        colliding["stable_source_identity"] = "model-controlled-seed"
        colliding["anchors"] = [
            {
                "span_start": offset + first + len("governed "),
                "span_end": offset + first + len("governed record"),
                "quote": "record",
                "model_authored_evidence_id": None,
            }
        ]
        original = dict(candidates[0])
        original["stable_source_identity"] = "model-controlled-seed"
        return [original, colliding] + list(candidates[1:])

    l1_state_root, domain_path, _ = _pipeline(tmp_path, "records", mutate=mutate)

    result = _l3(tmp_path, l1_state_root, domain_path)

    collided = [
        item
        for item in result.candidate_results
        if item.candidate_kind == "entity"
        and item.approved_semantic_id == "semantic-type:records.record"
    ]
    assert len(collided) == 2
    # Two distinct local references collapsed onto one stable ID because the
    # model controlled the identity seed; L3 refuses to reproduce that ID.
    assert len({item.semantic_id for item in collided}) == 1
    for item in collided:
        assert item.current_state == "rejected"
        assert "IDENTITY_POLICY_VIOLATION" in item.reason_codes
        assert item.identity_recomputed is False
        assert item.identity_witness_kind == "opaque_source_identity"
    index = json.loads((result.run_root / "identity-index.json").read_text("utf-8"))
    entry = next(
        item
        for item in index["entities"]
        if item["entity_id"] == collided[0].semantic_id
    )
    assert entry["identity_recomputed"] is False
    assert entry["identity_witness_kinds"] == ["opaque_source_identity"]


def test_l3_reruns_after_a_validator_source_or_candidate_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "l3"
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first_l1, first_domain, _ = _pipeline(first_root, "records")
    second_l1, second_domain, _ = _pipeline(
        second_root,
        "logistics",
        mutate=lambda candidates, work_unit: candidates[:2],
    )

    first = run_l3(
        state_root=state_root,
        l2_state_root=first_root / ".fkg" / "l2",
        l1_state_root=first_l1,
        domain_path=first_domain,
    )
    changed_source_and_candidates = run_l3(
        state_root=state_root,
        l2_state_root=second_root / ".fkg" / "l2",
        l1_state_root=second_l1,
        domain_path=second_domain,
    )

    # A changed source, domain, and candidate set reruns into its own directory
    # instead of colliding with the earlier run's artifacts.
    assert changed_source_and_candidates.run_root != first.run_root
    assert changed_source_and_candidates.reused_leaf_count == 0
    assert changed_source_and_candidates.recomputed_leaf_count == len(
        changed_source_and_candidates.leaves
    )
    assert (first.run_root / "stage-receipt.json").exists()
    assert (
        StageReceipt.model_validate_json(
            (first.run_root / "stage-receipt.json").read_text("utf-8")
        ).receipt_hash
        == first.receipt.receipt_hash
    )

    monkeypatch.setattr(schema2_validation_stage, "L3_VALIDATOR_VERSION", "1.0.1")
    changed_validator = run_l3(
        state_root=state_root,
        l2_state_root=first_root / ".fkg" / "l2",
        l1_state_root=first_l1,
        domain_path=first_domain,
    )

    # A changed validator invalidates every leaf fingerprint, so no stale leaf is
    # reused and no immutable artifact collision blocks the rerun.
    assert changed_validator.run_root != first.run_root
    assert changed_validator.reused_leaf_count == 0
    assert changed_validator.recomputed_leaf_count == len(changed_validator.leaves)
    assert changed_validator.receipt.receipt_hash != first.receipt.receipt_hash
    assert changed_validator.receipt.skip_key != first.receipt.skip_key
    for leaf in changed_validator.leaves:
        assert leaf.leaf_fingerprint not in {
            item.leaf_fingerprint for item in first.leaves
        }
    replayed = run_l3(
        state_root=state_root,
        l2_state_root=first_root / ".fkg" / "l2",
        l1_state_root=first_l1,
        domain_path=first_domain,
    )
    assert replayed.reused_leaf_count == len(replayed.leaves)
    assert replayed.receipt.receipt_hash == changed_validator.receipt.receipt_hash


def test_l3_rejects_a_fully_resealed_leaf_that_forges_a_proven_identity(
    tmp_path: Path,
) -> None:
    def mutate(candidates, work_unit):
        offset = work_unit.slice_start
        first = work_unit.text.find("governed record")
        opaque = dict(candidates[0])
        opaque["stable_source_identity"] = "model-controlled-seed"
        opaque["anchors"] = [
            {
                "span_start": offset + first + len("governed "),
                "span_end": offset + first + len("governed record"),
                "quote": "record",
                "model_authored_evidence_id": None,
            }
        ]
        return [opaque] + list(candidates[1:])

    l1_state_root, domain_path, _ = _pipeline(tmp_path, "records", mutate=mutate)
    first = _l3(tmp_path, l1_state_root, domain_path)
    forged = next(
        item
        for item in first.candidate_results
        if item.identity_witness_kind == "opaque_source_identity"
    )
    assert forged.current_state == "rejected"
    assert forged.evidence_span_ids
    checkpoint = _checkpoint_for(first, forged.candidate_id)

    def reseal_lifecycle(record: dict) -> None:
        _reseal_lifecycle_record(record, to_state="asserted", reason_codes=[])

    def forge(raw: dict) -> None:
        for item in raw["candidate_results"]:
            if item["candidate_id"] == forged.candidate_id:
                item["current_state"] = "asserted"
                item["reason_codes"] = []
                item["identity_recomputed"] = True
                item["identity_witness_kind"] = "derived_source_identity"
        for item in raw["classifications"]:
            if item["candidate_id"] == forged.candidate_id:
                item["classification_state"] = "asserted"
                item["reason_codes"] = []
        for record in raw["lifecycle_records"]:
            if record["candidate_id"] == forged.candidate_id:
                reseal_lifecycle(record)

    _rewrite_leaf(checkpoint, forge, reseal=True)

    # Every self-hashing carrier in the leaf is now internally consistent, so the
    # identity witness itself must be re-derived rather than read back.
    with pytest.raises(L3StageError) as excinfo:
        _l3(tmp_path, l1_state_root, domain_path)

    assert excinfo.value.code == "L3_VALIDATION_RESULT_INCOMPLETE"
    assert "identity witness does not re-derive" in str(excinfo.value)


def test_l3_rebinds_every_result_to_its_persisted_l2_proposal(
    tmp_path: Path,
) -> None:
    l1_state_root, domain_path, _ = _pipeline(tmp_path, "records")
    first = _l3(tmp_path, l1_state_root, domain_path)
    target = next(
        item
        for item in first.candidate_results
        if item.approved_semantic_id == "semantic-type:records.record"
    )
    checkpoint = _checkpoint_for(first, target.candidate_id)

    def swap_semantic_id(raw: dict) -> None:
        for item in raw["candidate_results"]:
            if item["candidate_id"] == target.candidate_id:
                item["approved_semantic_id"] = "semantic-type:records.subject"

    _rewrite_leaf(checkpoint, swap_semantic_id, reseal=True)

    with pytest.raises(L3StageError) as excinfo:
        _l3(tmp_path, l1_state_root, domain_path)

    assert excinfo.value.code == "L3_VALIDATION_RESULT_INCOMPLETE"
    assert "diverges from its L2 proposal" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Sealed collection-policy binding
# ---------------------------------------------------------------------------


def _requirement_with(base: CompletenessRequirementV2, fact_set: dict):
    """Return the same approved requirement carrying a different sealed policy."""

    return CompletenessRequirementV2.model_validate(
        {
            **base.model_dump(mode="json"),
            "structured_fact_set": fact_set,
        }
    )


def test_l3_binds_collection_policy_to_the_sealed_domain_authority(
    tmp_path: Path,
) -> None:
    fact_set = _fact_set(
        "manufacturing",
        ordered=False,
        roles=False,
        expected_count=None,
    )
    l1_state_root, domain_path, l2 = _pipeline(
        tmp_path,
        "manufacturing",
        fact_set=fact_set,
    )
    inputs = load_l3_inputs(
        l2_state_root=tmp_path / ".fkg" / "l2",
        l1_state_root=l1_state_root,
        domain_path=domain_path,
    )
    proposal = l2.required_member_sets[0].proposal
    requirement = inputs.hierarchy.requirement_by_id[
        proposal.completeness_requirement_id
    ]

    # The approved authority the proposal actually repeats is accepted.
    schema2_validation_stage._validate_required_member_policy_binding(
        proposal=proposal,
        requirement=requirement,
    )
    assert proposal.ordering_policy.mode == "unordered"
    assert proposal.expected_cardinality is None
    assert proposal.required_role_ids == ()


@pytest.mark.parametrize(
    ("divergence", "fragment"),
    [
        ("ordered", "ordering policy"),
        ("expected_count", "cardinality bounds"),
        ("membership_predicate", "membership relationship"),
        ("roles", "required roles"),
    ],
)
def test_l3_rejects_a_proposal_that_does_not_repeat_its_sealed_policy(
    tmp_path: Path,
    divergence: str,
    fragment: str,
) -> None:
    fact_set = _fact_set(
        "manufacturing",
        ordered=False,
        roles=False,
        expected_count=None,
    )
    l1_state_root, domain_path, l2 = _pipeline(
        tmp_path,
        "manufacturing",
        fact_set=fact_set,
    )
    inputs = load_l3_inputs(
        l2_state_root=tmp_path / ".fkg" / "l2",
        l1_state_root=l1_state_root,
        domain_path=domain_path,
    )
    proposal = l2.required_member_sets[0].proposal
    approved = inputs.hierarchy.requirement_by_id[
        proposal.completeness_requirement_id
    ]

    if divergence == "ordered":
        # Domain approves an ordered collection; the proposal claims unordered.
        sealed = _fact_set(
            "manufacturing",
            ordered=True,
            roles=False,
            expected_count=None,
        )
    elif divergence == "expected_count":
        # Domain approves expected=3; the proposal omits every bound.
        sealed = _fact_set(
            "manufacturing",
            ordered=False,
            roles=False,
            expected_count=3,
        )
    elif divergence == "membership_predicate":
        sealed = dict(fact_set)
        sealed["membership_relationship_type_id"] = (
            "relationship-type:manufacturing.record-witness"
        )
    else:
        sealed = _fact_set(
            "manufacturing",
            ordered=False,
            roles=True,
            expected_count=None,
        )

    with pytest.raises(L3StageError) as excinfo:
        schema2_validation_stage._validate_required_member_policy_binding(
            proposal=proposal,
            requirement=_requirement_with(approved, sealed),
        )

    # Proposal self-consistency proves nothing about the approved authority.
    assert excinfo.value.code == "L3_COMPLETENESS_HASH_MISMATCH"
    assert fragment in str(excinfo.value)
    assert proposal.proposal_hash == canonical_sha256(
        proposal.model_dump(mode="json", exclude={"proposal_hash"})
    )


def test_l3_entry_fails_closed_when_the_sealed_policy_drifts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fact_set = _fact_set(
        "manufacturing",
        ordered=False,
        roles=False,
        expected_count=None,
    )
    l1_state_root, domain_path, _ = _pipeline(
        tmp_path,
        "manufacturing",
        fact_set=fact_set,
    )
    original = schema2_validation_stage._validate_required_member_policy_binding

    def drifted(*, proposal, requirement):
        sealed = _fact_set(
            "manufacturing",
            ordered=True,
            roles=False,
            expected_count=None,
        )
        return original(
            proposal=proposal,
            requirement=_requirement_with(requirement, sealed),
        )

    monkeypatch.setattr(
        schema2_validation_stage,
        "_validate_required_member_policy_binding",
        drifted,
    )

    with pytest.raises(L3StageError) as excinfo:
        _l3(tmp_path, l1_state_root, domain_path)

    # The stage blocks at the entry gate, before any candidate is validated.
    assert excinfo.value.code == "L3_COMPLETENESS_HASH_MISMATCH"
    assert not (tmp_path / ".fkg" / "l3" / "runs").exists()


# ---------------------------------------------------------------------------
# Interrupted-run forgery for every candidate kind
# ---------------------------------------------------------------------------


def _published_state(result, candidate_id: str) -> str | None:
    index_path = result.run_root / "current-state-index.json"
    if not index_path.exists():
        return None
    index = json.loads(index_path.read_text("utf-8"))
    states = [
        state
        for state, ids in index["candidate_ids_by_state"].items()
        if candidate_id in ids
    ]
    assert len(states) <= 1
    return states[0] if states else None


def _interrupt_run(result) -> None:
    """Model a run interrupted after checkpointing but before publication."""

    shutil.rmtree(result.run_root)
    assert not result.run_root.exists()
    assert list((result.state_root / "leaves").rglob("*.json"))


def test_l3_rejects_a_fully_resealed_relationship_upgrade(tmp_path: Path) -> None:
    l1_state_root, domain_path, _ = _pipeline(tmp_path, "records")
    first = _l3(tmp_path, l1_state_root, domain_path)
    target = next(
        item
        for item in first.candidate_results
        if item.candidate_kind == "relationship"
    )
    assert target.current_state == "unsupported"
    assert _published_state(first, target.candidate_id) == "unsupported"
    checkpoint = _checkpoint_for(first, target.candidate_id)

    def forge(raw: dict) -> None:
        for item in raw["candidate_results"]:
            if item["candidate_id"] == target.candidate_id:
                item["current_state"] = "asserted"
                item["reason_codes"] = ["MODEL_EVIDENCE_ID_IGNORED"]
        for record in raw["lifecycle_records"]:
            if record["candidate_id"] == target.candidate_id:
                _reseal_lifecycle_record(
                    record,
                    to_state="asserted",
                    reason_codes=["MODEL_EVIDENCE_ID_IGNORED"],
                )

    _interrupt_run(first)
    _rewrite_leaf(checkpoint, forge, reseal=True)

    # Candidate result, lifecycle transition hash, and leaf payload hash are all
    # internally consistent, and nothing is published to collide with, so only
    # re-derivation from the sealed inputs catches the forgery.
    with pytest.raises(L3StageError) as excinfo:
        _l3(tmp_path, l1_state_root, domain_path)

    assert excinfo.value.code == "L3_VALIDATION_RESULT_INCOMPLETE"
    assert "does not re-derive from its sealed inputs" in str(excinfo.value)
    assert target.candidate_id in str(excinfo.value)
    assert _published_state(first, target.candidate_id) != "asserted"

    checkpoint.unlink()
    recovered = _l3(tmp_path, l1_state_root, domain_path)
    # Recovery republishes the same derived content; only the run's own resource
    # measurements differ, so the semantic output manifest replays exactly.
    assert recovered.output_manifest.manifest_hash == (
        first.output_manifest.manifest_hash
    )
    assert recovered.receipt.skip_key == first.receipt.skip_key
    assert (
        next(
            item
            for item in recovered.candidate_results
            if item.candidate_id == target.candidate_id
        ).current_state
        == "unsupported"
    )


def test_l3_rejects_a_fully_resealed_property_upgrade(tmp_path: Path) -> None:
    def mutate(candidates, work_unit):
        offset = work_unit.slice_start
        subject_start = work_unit.text.find("governed subject")
        anchor = {
            "span_start": offset + subject_start,
            "span_end": offset + subject_start + len("governed subject"),
            "quote": "governed subject",
            "model_authored_evidence_id": None,
        }
        return candidates + [
            {
                "candidate_kind": "property",
                "owner_local_id": "subject-1",
                "observed_property": "Member Order",
                "value": 0,
                "normalized_value": 0,
                "temporal_key": None,
                "anchor": anchor,
            }
        ]

    member_properties = (
        {
            "property_id": "property:records.member-order",
            "display_name": "Member Order",
            "value_type": "integer",
            "required": False,
        },
    )
    l1_state_root, domain_path, _ = _pipeline(
        tmp_path,
        "records",
        mutate=mutate,
        member_properties=member_properties,
    )
    first = _l3(tmp_path, l1_state_root, domain_path)
    target = next(
        item
        for item in first.candidate_results
        if item.candidate_kind == "property"
    )
    assert target.current_state == "unsupported"
    assert _published_state(first, target.candidate_id) == "unsupported"
    checkpoint = _checkpoint_for(first, target.candidate_id)

    def forge(raw: dict) -> None:
        for item in raw["candidate_results"]:
            if item["candidate_id"] == target.candidate_id:
                item["current_state"] = "asserted"
                item["reason_codes"] = []
        for item in raw["property_observations"]:
            if item["candidate_id"] == target.candidate_id:
                item["observation_state"] = "asserted"
                item["reason_codes"] = []
        for record in raw["lifecycle_records"]:
            if record["candidate_id"] == target.candidate_id:
                _reseal_lifecycle_record(
                    record,
                    to_state="asserted",
                    reason_codes=[],
                )

    _interrupt_run(first)
    _rewrite_leaf(checkpoint, forge, reseal=True)

    with pytest.raises(L3StageError) as excinfo:
        _l3(tmp_path, l1_state_root, domain_path)

    assert excinfo.value.code == "L3_VALIDATION_RESULT_INCOMPLETE"
    assert "does not re-derive from its sealed inputs" in str(excinfo.value)
    assert _published_state(first, target.candidate_id) != "asserted"

    checkpoint.unlink()
    recovered = _l3(tmp_path, l1_state_root, domain_path)
    observation = next(
        item
        for leaf in recovered.leaves
        for item in leaf.property_observations
        if item.candidate_id == target.candidate_id
    )
    assert observation.observation_state == "unsupported"


# ---------------------------------------------------------------------------
# Leaf fingerprints bind the complete SourceUnit artifact
# ---------------------------------------------------------------------------


def test_l3_leaf_fingerprint_binds_the_whole_source_unit_not_only_its_text(
    tmp_path: Path,
) -> None:
    l1_state_root, domain_path, _ = _pipeline(tmp_path, "records")
    first = _l3(tmp_path, l1_state_root, domain_path)
    inputs = first.inputs
    batch_id = inputs.leaf_batch_ids[0]
    original_unit = next(
        unit
        for unit in inputs.source_units.units
        if unit.unit_kind in {"heading", "paragraph", "table", "cell"}
    )

    # Same source_unit_id, same exact text, different verified unit kind.
    reclassified = original_unit.model_copy(
        update={"unit_kind": "visual_description"}
    )
    assert reclassified.source_unit_id == original_unit.source_unit_id
    assert reclassified.text == original_unit.text
    assert reclassified.text_content_hash == original_unit.text_content_hash

    drifted = dataclasses.replace(
        inputs,
        source_units=SourceUnitIndex(
            tuple(
                reclassified if unit.source_unit_id == original_unit.source_unit_id
                else unit
                for unit in inputs.source_units.units
            )
        ),
    )
    shared = schema2_validation_stage._build_shared_context(drifted)
    original_fingerprint = schema2_validation_stage._leaf_fingerprint(
        inputs=inputs,
        shared=schema2_validation_stage._build_shared_context(inputs),
        batch_id=batch_id,
    )
    drifted_fingerprint = schema2_validation_stage._leaf_fingerprint(
        inputs=drifted,
        shared=shared,
        batch_id=batch_id,
    )

    assert original_fingerprint == first.leaves[0].leaf_fingerprint
    assert drifted_fingerprint != original_fingerprint
    assert l3_input_fingerprint(drifted) != l3_input_fingerprint(inputs)
    assert (
        drifted.source_units.source_unit_content_hash
        != inputs.source_units.source_unit_content_hash
    )
    assert (
        drifted.source_units.source_unit_id_set_hash
        == inputs.source_units.source_unit_id_set_hash
    )

    # The earlier checkpoint is unreachable, so the stale leaf cannot be reused.
    stale_path = l3_leaf_checkpoint_path(
        first.state_root,
        batch_id,
        original_fingerprint,
    )
    fresh_path = l3_leaf_checkpoint_path(
        first.state_root,
        batch_id,
        drifted_fingerprint,
    )
    assert stale_path.exists() and not fresh_path.exists()
    assert schema2_validation_stage._reuse_leaf(
        fresh_path,
        batch_id,
        drifted_fingerprint,
    ) is None
    assert schema2_validation_stage._reuse_leaf(
        stale_path,
        batch_id,
        drifted_fingerprint,
    ) is None

    # Reusing it would have published a wrong state: the reclassified unit is an
    # unverifiable modality, so every candidate on it becomes unsupported.
    recomputed = schema2_validation_stage._validate_leaf(
        batch=drifted.batch_by_id[batch_id],
        records=drifted.proposed_partitions[batch_id],
        lifecycle_by_candidate={
            record.candidate_id: record
            for record in drifted.lifecycle_partitions[batch_id]
        },
        inputs=drifted,
        shared=shared,
        lifecycle_identity=schema2_validation_stage._validation_identity(
            drifted.l2_receipt.identity,
            contract_kind="c0.candidate_lifecycle_record",
        ),
        occurred_at_utc=drifted.l2_receipt.completed_at_utc,
        leaf_fingerprint=drifted_fingerprint,
    )
    affected = [
        item
        for item in recomputed.candidate_results
        if item.source_unit_id == original_unit.source_unit_id
    ]
    assert affected
    for item in affected:
        assert item.current_state == "unsupported"
        assert "EVIDENCE_MODALITY_UNSUPPORTED" in item.reason_codes
    assert any(
        item.current_state == "asserted"
        for item in first.candidate_results
        if item.source_unit_id == original_unit.source_unit_id
    )


def test_l3_leaf_fingerprint_binds_cross_batch_grounding_context(
    tmp_path: Path,
) -> None:
    l1_state_root, domain_path, _ = _pipeline(tmp_path, "records")
    first = _l3(tmp_path, l1_state_root, domain_path)
    inputs = first.inputs
    shared = schema2_validation_stage._build_shared_context(inputs)
    batch_id = next(
        candidate_batch_id
        for candidate_batch_id in inputs.leaf_batch_ids
        if any(
            record.candidate_kind == "relationship"
            for record in inputs.proposed_partitions[candidate_batch_id]
        )
    )
    original_fingerprint = schema2_validation_stage._leaf_fingerprint(
        inputs=inputs,
        shared=shared,
        batch_id=batch_id,
    )
    records = inputs.proposed_partitions[batch_id]
    source_unit_id = records[0].source_unit_id
    scoped_entity_ids = {
        endpoint
        for record in records
        for endpoint in (
            record.semantic_id if record.candidate_kind == "entity" else None,
            record.proposed_source_entity_id,
            record.proposed_target_entity_id,
        )
        if endpoint is not None
    }
    anchor_key = next(
        key
        for key in shared.entity_anchor_by_key
        if key[0] in scoped_entity_ids and key[1] == source_unit_id
    )
    original_anchor = shared.entity_anchor_by_key[anchor_key]
    changed_anchor = dataclasses.replace(
        original_anchor,
        span_start=original_anchor.span_start + 1,
    )
    changed_anchors = dict(shared.entity_anchor_by_key)
    changed_anchors[anchor_key] = changed_anchor
    anchor_drift = dataclasses.replace(
        shared,
        entity_anchor_by_key=changed_anchors,
    )

    assert (
        schema2_validation_stage._leaf_fingerprint(
            inputs=inputs,
            shared=anchor_drift,
            batch_id=batch_id,
        )
        != original_fingerprint
    )

    local_key = next(
        key
        for key in shared.local_reference_index
        if key[0] == source_unit_id
    )
    changed_local_index = dict(shared.local_reference_index)
    changed_local_index[local_key] = tuple(
        sorted((*changed_local_index[local_key], "entity:sibling-context-drift"))
    )
    local_reference_drift = dataclasses.replace(
        shared,
        local_reference_index=changed_local_index,
    )

    assert (
        schema2_validation_stage._leaf_fingerprint(
            inputs=inputs,
            shared=local_reference_drift,
            batch_id=batch_id,
        )
        != original_fingerprint
    )
