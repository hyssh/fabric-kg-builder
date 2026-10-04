from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from fabric_kg_builder.contracts.base import canonical_sha256
from fabric_kg_builder.contracts.registry import REGISTERED_CONTRACTS
from fabric_kg_builder.domain.contexts import DomainDesignContext, DomainIntake
from fabric_kg_builder.domain.stage import (
    finalize_l1_stage,
    make_l1_identity,
    prepare_l1_stage,
    seal_domain_intake,
)
from fabric_kg_builder.sources.corpus import DesignSampleManifest
from fabric_kg_builder.sources.corpus import SourceCorpusEntry
from fabric_kg_builder.sources.evidence_verifier import (
    mint_source_unit,
    mint_verified_span,
    verify_span_for_purpose,
)
from tests.unit.test_l1_stage import _candidates, _intake, _preflight


def test_l1_contracts_reject_unknown_fields_and_versions() -> None:
    identity = make_l1_identity(project_id="project:test", run_id="run:test")
    intake = seal_domain_intake(_intake(), identity=identity)
    payload = intake.model_dump(mode="json")
    payload["unexpected"] = True

    with pytest.raises(ValidationError, match="extra_forbidden"):
        DomainIntake.model_validate(payload)

    payload.pop("unexpected")
    payload["contract_version"] = "2.0.0"
    with pytest.raises(ValidationError, match="literal_error"):
        DomainIntake.model_validate(payload)


def test_generated_l1_registry_hashes_match_schemas() -> None:
    root = Path("src/fabric_kg_builder/domain/schemas")
    registry = json.loads((root / "registry.json").read_text(encoding="utf-8"))

    assert set(registry["contracts"]) == {
        "l1.design_sample_manifest",
        "l1.domain_approval_context",
        "l1.domain_design_context",
        "l1.domain_intake",
        "l1.domain_proposal",
        "l1.domain_source_profile",
        "l1.source_corpus_manifest",
    }
    for entry in registry["contracts"].values():
        schema = json.loads((root / entry["schema"]).read_text(encoding="utf-8"))
        assert entry["schema_hash"] == canonical_sha256(schema)
    design_schema = json.loads(
        (
            root / "l1-domain-design-context-1.0.0.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert design_schema == DomainDesignContext.model_json_schema()


def test_l1_kinds_do_not_modify_c0_registry() -> None:
    assert not any(kind.startswith("l1.") for kind in REGISTERED_CONTRACTS)


def test_design_sample_is_exact_subset_of_complete_corpus(tmp_path: Path) -> None:
    prepared = prepare_l1_stage(
        _preflight(tmp_path),
        candidates=_candidates(),
    )
    prepared.sample_manifest.validate_subset_of(prepared.preflight.corpus)
    payload = prepared.sample_manifest.model_dump(mode="json")
    payload["source_corpus_manifest_hash"] = "f" * 64
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {"identity", "design_sample_manifest_id", "sample_hash"}
    }
    payload["sample_hash"] = canonical_sha256(semantic)
    payload["design_sample_manifest_id"] = (
        "design-sample-manifest:00000000000000000000000000000000"
    )

    with pytest.raises(ValidationError):
        DesignSampleManifest.model_validate(payload)


def test_design_evidence_verifies_exact_unicode_codepoints(tmp_path: Path) -> None:
    identity = make_l1_identity(
        project_id="project:unicode",
        run_id="run:unicode",
    )
    entry = SourceCorpusEntry(
        source_file_id="source-file:unicode",
        asset_id="asset:unicode",
        asset_version_id="asset-version:unicode",
        original_byte_hash=canonical_sha256("Café equipment – exact evidence."),
        byte_count=37,
        media_type="text/plain",
        relative_source_ref="unicode.txt",
        disposition="eligible",
        adapter_status="supported",
        adapter_name="text",
        reason_code=None,
    )
    unit = mint_source_unit(
        base_identity=identity,
        corpus_entry=entry,
        source_corpus_manifest_id="source-corpus-manifest:unicode",
        unit_kind="paragraph",
        text="Café equipment – exact evidence.",
        ordinal=0,
    )
    span = mint_verified_span(
        source_unit=unit,
        span_start=0,
        span_end=14,
        purpose="domain_design",
        verified_at_utc=datetime.now(timezone.utc),
        expected_quote="Café equipment",
    )

    verify_span_for_purpose(span, unit, purpose="domain_design")
    assert span.quote == unit.text[span.span_start:span.span_end]


def test_succeeded_and_skipped_receipts_bind_intact_output(tmp_path: Path) -> None:
    prepared = prepare_l1_stage(
        _preflight(tmp_path, "receipts"),
        candidates=_candidates("receipts"),
    )
    result = finalize_l1_stage(
        prepared,
        decision="approve",
        actor="reviewer@example.test",
        persist=False,
    )
    prior = result.receipt
    assert prior is not None
    assert prior.status == "succeeded"
    assert result.output_manifest is not None
    assert prior.output_manifest_hash == result.output_manifest.manifest_hash
    assert prior.input_manifest_hash == prepared.preflight.input_manifest.manifest_hash


def test_approval_context_binds_domain_hierarchy_identity_and_completeness(
    tmp_path: Path,
) -> None:
    prepared = prepare_l1_stage(
        _preflight(tmp_path, "approval"),
        candidates=_candidates("approval"),
    )
    result = finalize_l1_stage(
        prepared,
        decision="approve",
        actor="reviewer@example.test",
        persist=False,
    )

    context = result.approval_context
    contract = result.contract
    assert context is not None and contract is not None
    assert context.domain_contract_hash == prepared.proposal.domain_contract_hash
    assert context.hierarchy_hash == contract.hierarchy_closure.hierarchy_hash
    assert context.identity_policy_hash == contract.identity_policy_hash
    assert (
        context.completeness_requirement_hash
        == contract.completeness_requirement_hash
    )
