from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.cli import enrich_cmd as enrich_cmd_module
from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.domain.stage import (
    finalize_l1_stage,
    preflight_l1_inputs,
    prepare_l1_stage,
)
from fabric_kg_builder.enrichment.schema2_sources import (
    IndexedSourceCorpusReader,
    L2StageError,
    load_l2_inputs,
)
from fabric_kg_builder.enrichment import schema2_sources
from fabric_kg_builder.enrichment.schema2_extraction import (
    L2_EXTRACTOR_VERSION,
    L2_PROMPT_VERSION,
    RawCandidateResponse,
    raw_candidate_response_schema,
)
from fabric_kg_builder.enrichment.schema2_stage import (
    L2_RESPONSE_SCHEMA_HASH,
    dry_run_l2,
    run_l2,
)
from fabric_kg_builder.model.schemas import AssetRow, AssetVersionRow
from tests.unit.test_l1_stage import _candidates, _intake, _preflight


def _approved_l1(
    tmp_path: Path,
    *,
    include_supported_source: bool = False,
    include_completeness: bool = False,
) -> tuple[Path, Path]:
    state_root = tmp_path / ".fkg" / "l1"
    domain_path = tmp_path / "domain.yaml"
    preflight = _preflight(tmp_path, "l2-stage")
    if include_supported_source:
        (tmp_path / "source" / "record.html").write_text(
            "<p>A governed record describes a governed subject.</p>",
            encoding="utf-8",
        )
        preflight = preflight_l1_inputs(
            source_path=tmp_path / "source",
            intake_raw=_intake("l2-stage"),
            project_id="project:l2-stage",
            run_id="run:l2-stage",
            model_version="fixture/1.0.0",
            model_hash=canonical_sha256({"fixture": "l2-stage"}),
        )
    candidates = _candidates("l2-stage")
    if include_completeness:
        candidates["completeness_candidates"] = [
            {
                "candidate_id": "candidate:completeness",
                "proposed_requirement": {
                    "requirement_id": (
                        "completeness-requirement:l2-stage.record-subjects"
                    ),
                    "competency_question_ids": ["cq:q1"],
                    "requirement_kind": "structured_fact_set",
                    "scope_type_id": "semantic-type:l2-stage.record",
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
                    "structured_fact_set": {
                        "aggregate_type_id": "semantic-type:l2-stage.record",
                        "membership_relationship_type_id": (
                            "relationship-type:l2-stage.record-subject"
                        ),
                        "allowed_member_type_ids": [
                            "semantic-type:l2-stage.subject"
                        ],
                        "member_role_ids": [],
                        "ordering_policy": {
                            "mode": "unordered",
                            "ordinal_property_id": None,
                            "ordinal_value_type": None,
                            "direction": None,
                            "unique_ordinals": None,
                            "contiguous": None,
                        },
                        "cardinality": None,
                        "collection_identity_policy": {
                            "aggregate_identity_included": True,
                            "membership_relationship_included": True,
                            "member_identities_included": True,
                            "member_roles_included": False,
                            "ordinals_included": False,
                            "preserve_member_order": False,
                            "hash_algorithm": "sha256",
                        },
                        "membership_source_kind": "competency_question",
                        "membership_evidence_span_ids": [],
                        "membership_rationale": (
                            "The approved question governs collection membership."
                        ),
                    },
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
    prepared = prepare_l1_stage(
        preflight,
        candidates=candidates,
    )
    result = finalize_l1_stage(
        prepared,
        decision="approve",
        actor="reviewer@example.test",
        state_root=state_root,
        domain_path=domain_path,
    )
    assert result.status == "succeeded"
    return state_root, domain_path


def test_schema2_fails_closed_without_succeeded_l1_receipt(tmp_path: Path) -> None:
    with pytest.raises(L2StageError, match="invalid artifact stage-receipt.json"):
        load_l2_inputs(
            l1_state_root=tmp_path / "missing-l1",
            domain_path=tmp_path / "domain.yaml",
        )


def test_l2_dry_run_consumes_intact_l1_without_writes_or_remote_calls(
    tmp_path: Path,
) -> None:
    l1_state_root, domain_path = _approved_l1(tmp_path)
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    plan = dry_run_l2(
        l1_state_root=l1_state_root,
        domain_path=domain_path,
    )

    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert plan.status == "planned"
    assert plan.corpus_entry_count == 2
    assert plan.design_sample_entry_count < plan.corpus_entry_count
    assert plan.remote_calls == 0
    assert plan.writes == 0
    assert before == after


def test_l2_is_not_activated_in_product_cli() -> None:
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0
    assert "schema2" not in result.output.casefold()
    assert "l2" not in result.output.casefold()


def test_l2_foundry_response_schema_requires_candidate_envelope() -> None:
    schema = raw_candidate_response_schema()
    assert schema["required"] == ["candidates"]
    parsed = RawCandidateResponse.model_validate(
        {
            "candidates": [
                {
                    "candidate_kind": "entity",
                    "local_id": "device-1",
                    "observed_type": "Device",
                    "label": "Device 1",
                    "identity_key": {},
                    "stable_source_identity": "device-1",
                }
            ]
        }
    )
    assert parsed.candidates[0].candidate_kind == "entity"


def test_enrich_dispatches_approved_schema2_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _state_root, domain_path = _approved_l1(tmp_path)
    source = tmp_path / "source"
    monkeypatch.setattr(
        enrich_cmd_module,
        "_resolve_max_concurrent",
        lambda _ctx, _override: 1,
    )
    calls: list[dict[str, object]] = []

    def run_schema2(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            receipt=SimpleNamespace(stage_receipt_id="receipt:l2")
        )

    monkeypatch.setattr(
        enrich_cmd_module,
        "_run_schema2_enrichment",
        run_schema2,
    )
    result = CliRunner().invoke(
        cli,
        [
            "enrich",
            "--input",
            str(source),
            "--domain-file",
            str(domain_path),
            "--out",
            str(tmp_path / "enriched"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "schema-2 extraction succeeded" in result.output
    assert calls and calls[0]["domain_file"] == str(domain_path)


def test_l2_run_is_proposed_only_and_exact_rerun_skips_remote_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    l1_state_root, domain_path = _approved_l1(
        tmp_path,
        include_supported_source=True,
        include_completeness=True,
    )
    inputs = load_l2_inputs(
        l1_state_root=l1_state_root,
        domain_path=domain_path,
    )
    now = datetime.now(timezone.utc)
    assets = []
    versions = []
    for entry in inputs.corpus_manifest.entries:
        if entry.disposition != "eligible":
            continue
        assets.append(
            AssetRow(
                asset_id=entry.asset_id,
                project_id="project:l2-stage",
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
    reader = IndexedSourceCorpusReader(
        source_root=tmp_path / "source",
        assets=tuple(assets),
        versions=tuple(versions),
    )

    class _Service:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, *, prompt: str, work_unit) -> dict:
            self.calls += 1
            prompt_payload = json.loads(prompt)
            assert prompt_payload["source_text"] == work_unit.text
            assert prompt_payload["source_identity"]["source_text_hash"] == (
                work_unit.source_text_hash
            )
            return {
                "candidates": [
                    {
                        "candidate_kind": "entity",
                        "local_id": "record-1",
                        "observed_type": "Record",
                        "label": "Record 1",
                        "aliases": [],
                        "identity_key": {},
                        "stable_source_identity": "record-1",
                        "anchors": [],
                    },
                    {
                        "candidate_kind": "entity",
                        "local_id": "subject-1",
                        "observed_type": "Subject",
                        "label": "Subject 1",
                        "aliases": [],
                        "identity_key": {},
                        "stable_source_identity": "subject-1",
                        "anchors": [],
                    },
                    {
                        "candidate_kind": "relationship",
                        "source_local_id": "record-1",
                        "target_local_id": "subject-1",
                        "observed_predicate": "describes",
                        "direction": "source_to_target",
                        "governed_context": "approved test context",
                        "anchor": None,
                    },
                ]
            }

    state_root = tmp_path / ".fkg" / "l2"
    first_service = _Service()
    first = run_l2(
        reader=reader,
        service=first_service,
        state_root=state_root,
        l1_state_root=l1_state_root,
        domain_path=domain_path,
        prompt_hash=canonical_sha256({"prompt": "l2-test"}),
        model_version="fixture/1.0.0",
        model_hash=canonical_sha256({"model": "fixture"}),
    )
    second_service = _Service()
    second = run_l2(
        reader=reader,
        service=second_service,
        state_root=state_root,
        l1_state_root=l1_state_root,
        domain_path=domain_path,
        prompt_hash=canonical_sha256({"prompt": "l2-test"}),
        model_version="fixture/1.0.0",
        model_hash=canonical_sha256({"model": "fixture"}),
    )

    assert first.receipt.status == "succeeded"
    assert first.receipt.receipt_hash == second.receipt.receipt_hash
    assert first_service.calls == len(first.materialized.source_units)
    assert second_service.calls == 0
    assert first.materialized.report.ineligible_corpus_entry_count == 2
    assert first.materialized.source_units
    assert len(first.required_member_sets) == 1
    assert any(
        entry.contract_kind == "c0.required_member_set_proposal"
        and entry.contract_version == "1.1.0"
        for entry in first.output_manifest.entries
    )
    proposal = first.required_member_sets[0].proposal
    assert proposal.identity.contract_version == "1.1.0"
    assert proposal.ordering_policy.mode == "unordered"
    assert proposal.expected_cardinality is None
    assert proposal.minimum_cardinality is None
    assert proposal.maximum_cardinality is None
    assert proposal.required_role_ids == ()
    assert proposal.members[0].member_role_id is None
    assert proposal.members[0].member_order is None
    assert "role:unspecified" not in proposal.model_dump_json()
    assert (
        first.receipt.accepted_contract_versions[
            "c0.required_member_set_proposal"
        ]
        == "1.1.0"
    )
    legacy_versions = dict(schema2_sources.L2_ACCEPTED_VERSIONS)
    legacy_versions["c0.required_member_set_proposal"] = "1.0.0"
    with monkeypatch.context() as context:
        context.setattr(
            schema2_sources,
            "L2_ACCEPTED_VERSIONS",
            legacy_versions,
        )
        legacy_fingerprint = schema2_sources.l2_input_fingerprint(
            first.inputs,
            first.materialized.source_unit_manifest,
            prompt_version=L2_PROMPT_VERSION,
            prompt_hash=canonical_sha256({"prompt": "l2-test"}),
            model_version="fixture/1.0.0",
            model_hash=canonical_sha256({"model": "fixture"}),
            extractor_name="schema2-extractor",
            extractor_version=L2_EXTRACTOR_VERSION,
            response_schema_hash=L2_RESPONSE_SCHEMA_HASH,
            split_policy_version="paragraph-sentence-token/1.0.0",
        )
    assert legacy_fingerprint != first.receipt.skip_key
    assert all(
        record.to_state.value == "proposed"
        and not record.evidence_span_ids
        and record.resolved_source_entity_id is None
        and record.resolved_target_entity_id is None
        for leaf in first.leaves
        for record in leaf.lifecycle_records
    )
    assert not list(state_root.rglob("*evidence-span*"))
    assert not list(state_root.rglob("*required-member-manifest*"))

    (state_root / "stage-receipt.json").unlink()
    crash_resume_service = _Service()
    recovered = run_l2(
        reader=reader,
        service=crash_resume_service,
        state_root=state_root,
        l1_state_root=l1_state_root,
        domain_path=domain_path,
        prompt_hash=canonical_sha256({"prompt": "l2-test"}),
        model_version="fixture/1.0.0",
        model_hash=canonical_sha256({"model": "fixture"}),
    )
    assert recovered.receipt.receipt_hash == first.receipt.receipt_hash
    assert crash_resume_service.calls == 0
