"""C0.Core schema, serialization, lifecycle, evidence, and receipt contracts."""

from __future__ import annotations

import copy
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from fabric_kg_builder.contracts import (
    ArtifactEntry,
    ArtifactManifest,
    AssertionState,
    AuditProjection,
    CandidateAccountingDisposition,
    CandidateLifecycleRecord,
    CanonicalIdentityEnvelope,
    EvidenceSpan,
    ImmutableSourceLocator,
    REGISTERED_CONTRACTS,
    REGISTERED_CONTRACT_VERSIONS,
    SemanticServingProjection,
    SourceUnit,
    StageReceipt,
    StageResourceMetrics,
    UnknownContractMajorError,
    canonical_json,
    canonical_sha256,
    negotiate_contract,
    parse_contract,
    validate_asserted_serving_subset,
    validate_receipt_resources,
    validate_skip_preconditions,
    write_registered_schemas,
)
from fabric_kg_builder.contracts.base import (
    CONTRACT_VERSION,
    deterministic_contract_id,
)
from fabric_kg_builder.contracts.lifecycle import allowed_lifecycle_transitions

FIXTURES = Path(__file__).parent.parent / "fixtures" / "contracts"
NOW = datetime(2026, 8, 24, 17, 0, tzinfo=timezone.utc)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def locator(*, char_start: int | None = None, char_end: int | None = None):
    values = {
        "locator_version": "1.0",
        "blob_uri": "https://storage.example.test/source/file.txt",
        "blob_version_id": "version-1",
        "source_uri": None,
        "page": 0,
        "sheet": None,
        "slide": None,
        "section_path": ("intro",),
        "cell_range": None,
        "char_start": char_start,
        "char_end": char_end,
        "polygon": None,
        "sheet_zone": None,
        "tile_id": None,
        "coordinate_system": None,
        "transform": None,
        "native_layer_id": None,
        "native_object_id": None,
    }
    return ImmutableSourceLocator(
        **values,
        locator_hash=canonical_sha256(values),
    )


def identity(
    kind: str,
    *,
    source_unit_id: str | None = None,
    semantic: bool = False,
    source_locator: ImmutableSourceLocator | None = None,
) -> CanonicalIdentityEnvelope:
    return CanonicalIdentityEnvelope(
        contract_kind=kind,
        contract_version=CONTRACT_VERSION,
        project_id="project-1",
        asset_id="asset-1",
        asset_version_id="asset-version-1",
        run_id="run-1",
        source_file_id="source-file-1",
        source_unit_id=source_unit_id,
        content_hash=HASH_A,
        domain_schema_version="2.0",
        domain_contract_hash=HASH_B,
        semantic_contract_hash=HASH_C if semantic else None,
        canonical_schema_version="2.0",
        prompt_version=None,
        prompt_hash=None,
        model_version=None,
        model_hash=None,
        extractor_name="fixture-adapter",
        extractor_version="1.0.0",
        parent_artifact_ids=("artifact-a",),
        parent_record_ids=(),
        immutable_locator=source_locator,
    )


def source_unit() -> SourceUnit:
    base_locator = locator()
    return SourceUnit.mint(
        identity=identity(
            "c0.source_unit",
            source_unit_id="replaced-by-mint",
            source_locator=base_locator,
        ),
        unit_kind="paragraph",
        text="Café 😀 pump requires service.",
        ordinal=0,
        locator=base_locator,
    )


def disposition(
    input_id: str,
    *,
    retained_id: str | None = None,
    dedup_target: str | None = None,
    state: AssertionState | None = None,
) -> CandidateAccountingDisposition:
    return CandidateAccountingDisposition(
        identity=identity("c0.candidate_accounting_disposition"),
        input_candidate_id=input_id,
        disposition="retained" if retained_id else "deduplicated",
        retained_candidate_id=retained_id,
        deduplicated_into_candidate_id=dedup_target,
        current_state=state,
        reason_codes=("diagnostic-b", "diagnostic-a", "diagnostic-a"),
    )


def audit_projection() -> AuditProjection:
    retained = disposition(
        "input-1",
        retained_id="candidate-1",
        state=AssertionState.ASSERTED,
    )
    deduplicated = disposition("input-2", dedup_target="candidate-1")
    values = {
        "identity": identity("c0.audit_projection", semantic=True),
        "projection_id": "audit-1",
        "projection_version": "1.0",
        "source_manifest_hash": HASH_A,
        "input_candidate_count": 2,
        "retained_candidate_count": 1,
        "deduplicated_input_count": 1,
        "candidate_dispositions": (retained, deduplicated),
        "lifecycle_state_counts": {AssertionState.ASSERTED: 1},
        "reason_code_counts": {"diagnostic-a": 2, "diagnostic-b": 1},
        "entity_assertion_ids": ("entity-assertion-1",),
        "relationship_assertion_ids": ("relationship-assertion-1",),
        "property_assertion_ids": (),
        "canonical_id_set_hashes": {"entity": HASH_A, "relationship": HASH_B},
        "canonical_row_hashes": {"entity": HASH_B, "relationship": HASH_C},
        "artifact_manifest_id": "manifest-1",
    }
    return AuditProjection(
        **values,
        projection_hash=canonical_sha256(values),
    )


def serving_projection() -> SemanticServingProjection:
    values = {
        "identity": identity("c0.semantic_serving_projection", semantic=True),
        "projection_id": "serving-1",
        "projection_version": "1.0",
        "audit_projection_id": "audit-1",
        "source_manifest_hash": HASH_A,
        "sealed_domain_contract_hash": HASH_B,
        "sealed_semantic_contract_hash": HASH_C,
        "included_states": (AssertionState.ASSERTED,),
        "entity_assertion_ids": ("entity-assertion-1",),
        "relationship_assertion_ids": ("relationship-assertion-1",),
        "property_assertion_ids": (),
        "evidence_span_ids": ("evidence-span-1",),
        "canonical_id_set_hashes": {"entity": HASH_A, "relationship": HASH_B},
        "canonical_row_hashes": {"entity": HASH_B, "relationship": HASH_C},
        "artifact_manifest_id": "manifest-1",
        "sealed_at_utc": NOW,
    }
    semantic_values = dict(values)
    semantic_values.pop("sealed_at_utc")
    return SemanticServingProjection(
        **values,
        projection_hash=canonical_sha256(semantic_values),
    )


def artifact_manifest() -> ArtifactManifest:
    entry = ArtifactEntry(
        artifact_id="artifact-1",
        contract_kind="c0.source_unit",
        contract_version=CONTRACT_VERSION,
        schema_hash=HASH_A,
        content_hash=HASH_B,
        canonical_id_set_hash=None,
        row_count=1,
        byte_count=10,
        partition_count=1,
        media_type="application/json",
        immutable_locator=None,
        blob_asset_ref_id=None,
    )
    values = {
        "identity": identity("c0.artifact_manifest"),
        "artifact_manifest_id": "manifest-1",
        "entries": (entry,),
        "total_row_count": 1,
        "total_byte_count": 10,
    }
    return ArtifactManifest(
        **values,
        manifest_hash=canonical_sha256(values),
    )


def resource_metrics() -> StageResourceMetrics:
    values = {
        "identity": identity("c0.stage_resource_metrics"),
        "resource_metrics_id": "metrics-1",
        "stage_id": "L2",
        "stage_name": "Schema-Constrained Extraction",
        "wall_ms": 0,
        "cpu_ms": 0,
        "peak_rss_bytes": 0,
        "storage_read_bytes": 0,
        "storage_write_bytes": 0,
        "network_request_bytes": 0,
        "network_response_bytes": 0,
        "source_units_read": 1,
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
        "cache_misses": 1,
        "max_observed_concurrency": 1,
        "budget_snapshot_hash": HASH_A,
        "exceeded_dimensions": (),
    }
    return StageResourceMetrics(
        **values,
        metrics_hash=canonical_sha256(values),
    )


def stage_receipt(
    status: str = "succeeded",
    *,
    output_manifest_hash: str = HASH_B,
) -> StageReceipt:
    metrics = resource_metrics()
    values = {
        "identity": identity("c0.stage_receipt"),
        "stage_receipt_id": f"receipt-{status}",
        "stage_id": "L2",
        "stage_name": "Schema-Constrained Extraction",
        "stage_contract_version": CONTRACT_VERSION,
        "status": status,
        "input_manifest_id": "input-manifest-1",
        "input_manifest_hash": HASH_A,
        "output_manifest_id": "manifest-1",
        "output_manifest_hash": output_manifest_hash,
        "skip_key": HASH_C,
        "accepted_contract_versions": {"c0.source_unit": "==1.0.0"},
        "resource_metrics_id": metrics.resource_metrics_id,
        "resource_metrics_hash": metrics.metrics_hash,
        "attempt_count": 1,
        "remote_operation_refs": (),
        "error_codes": (),
        "started_at_utc": NOW,
        "completed_at_utc": NOW,
    }
    semantic_values = dict(values)
    semantic_values.pop("started_at_utc")
    semantic_values.pop("completed_at_utc")
    return StageReceipt(
        **values,
        receipt_hash=canonical_sha256(semantic_values),
    )


@pytest.mark.contract
def test_valid_json_and_yaml_fixtures_round_trip() -> None:
    for path in (
        FIXTURES / "valid" / "source-unit.json",
        FIXTURES / "valid" / "source-unit.yaml",
    ):
        parsed = parse_contract(path.read_text(encoding="utf-8"))
        assert isinstance(parsed, SourceUnit)
        assert parse_contract(canonical_json(parsed)) == parsed


@pytest.mark.contract
@pytest.mark.parametrize(
    "fixture_name",
    ["unknown-field.json", "wrong-version.json", "secret-locator.json"],
)
def test_invalid_fixtures_fail_closed(fixture_name: str) -> None:
    with pytest.raises((ValidationError, ValueError)):
        parse_contract(
            (FIXTURES / "invalid" / fixture_name).read_text(encoding="utf-8")
        )


@pytest.mark.contract
def test_unknown_contract_major_fails_before_model_validation() -> None:
    with pytest.raises(UnknownContractMajorError):
        negotiate_contract("c0.source_unit", "2.0.0")
    payload = identity("c0.identity").model_dump(mode="json")
    payload["contract_version"] = "9.0.0"
    with pytest.raises(ValidationError):
        CanonicalIdentityEnvelope.model_validate_json(json.dumps(payload))


@pytest.mark.contract
def test_registered_identity_rejects_another_registered_kind() -> None:
    payload = identity("c0.source_unit").model_dump(mode="json")
    with pytest.raises(ValidationError, match="standalone identity"):
        REGISTERED_CONTRACTS["c0.identity"].model_validate(payload)


@pytest.mark.contract
def test_registry_contains_only_registered_c0_kinds() -> None:
    assert set(REGISTERED_CONTRACTS) == {
        "c0.identity",
        "c0.source_unit",
        "c0.evidence_span",
        "c0.extraction_candidate_batch",
        "c0.required_member_set_proposal",
        "c0.required_member_manifest",
        "c0.candidate_lifecycle_record",
        "c0.candidate_accounting_disposition",
        "c0.canonical_entity_assertion",
        "c0.canonical_relationship_assertion",
        "c0.canonical_property_assertion",
        "c0.audit_projection",
        "c0.semantic_serving_projection",
        "c0.publication_crosswalk",
        "c0.projection_equivalence",
        "c0.governed_asset_reference",
        "c0.access_policy",
        "c0.query_budget",
        "c0.ontology_scope_envelope",
        "c0.resolved_ontology_scope",
        "c0.resolved_retrieval_scope",
        "c0.agentic_retrieval_request_context",
        "c0.agentic_retrieval_coverage_receipt",
        "c0.search_citation_envelope",
        "c0.citation_presentation",
        "c0.artifact_manifest",
        "c0.stage_receipt",
        "c0.stage_resource_metrics",
        "c0.rdf_projection_manifest",
        "c0.rdf_projection_candidate_bundle",
        "c0.rdf_serialization_artifact",
        "c0.rdf_validation_receipt",
    }


@pytest.mark.contract
def test_contract_models_are_frozen_and_reject_wrong_types() -> None:
    unit = source_unit()
    with pytest.raises(ValidationError):
        unit.ordinal = 2
    payload = unit.model_dump(mode="json")
    payload["ordinal"] = "0"
    with pytest.raises(ValidationError):
        SourceUnit.model_validate(payload)


@pytest.mark.contract
def test_canonical_json_is_nfc_sorted_compact_and_finite() -> None:
    assert canonical_json({"z": "Cafe\u0301", "a": 1}) == '{"a":1,"z":"Café"}'
    with pytest.raises(ValueError):
        canonical_json({"value": float("nan")})


@pytest.mark.contract
def test_golden_canonical_json_and_hash() -> None:
    unit = parse_contract(
        (FIXTURES / "valid" / "source-unit.json").read_text(encoding="utf-8")
    )
    expected_json = (FIXTURES / "golden" / "source-unit.canonical.json").read_text(
        encoding="utf-8"
    ).rstrip("\n")
    expected_hash = (FIXTURES / "golden" / "source-unit.sha256").read_text(
        encoding="utf-8"
    ).strip()
    assert canonical_json(unit) == expected_json
    assert canonical_sha256(unit) == expected_hash


@pytest.mark.contract
def test_set_like_arrays_sort_and_dedupe_but_text_is_preserved() -> None:
    item = disposition(
        "input-1",
        retained_id="candidate-1",
        state=AssertionState.PROPOSED,
    )
    assert item.reason_codes == ("diagnostic-a", "diagnostic-b")
    unit = source_unit()
    assert unit.text == "Café 😀 pump requires service."


@pytest.mark.contract
def test_identity_has_no_generic_record_id_and_repeated_ids_must_match() -> None:
    assert "record_id" not in CanonicalIdentityEnvelope.model_fields
    payload = source_unit().model_dump(mode="json")
    payload["source_file_id"] = "different-source"
    with pytest.raises(ValidationError, match="source_file_id"):
        SourceUnit.model_validate_json(json.dumps(payload))


@pytest.mark.contract
@pytest.mark.parametrize(
    "uri",
    [
        "/tmp/file.txt",
        "file:///tmp/file.txt",
        r"C:\Users\alice\file.txt",
        "https://alice:password@storage.example.test/a",
        "https://storage.example.test/a?sig=secret",
        "https://storage.example.test/a?access_token=secret",
    ],
)
def test_locator_rejects_mutable_paths_and_tokens(uri: str) -> None:
    values = locator().model_dump(mode="python", exclude={"locator_hash"})
    values["blob_uri"] = uri
    values["locator_hash"] = canonical_sha256(values)
    with pytest.raises(ValidationError):
        ImmutableSourceLocator.model_validate(values)


@pytest.mark.contract
@pytest.mark.parametrize(
    "uri",
    [
        "abfss://container@account.dfs.core.windows.net/path/file.json",
        "wasbs://container@account.blob.core.windows.net/path/file.json",
    ],
)
def test_locator_accepts_azure_filesystem_authorities(uri: str) -> None:
    values = locator().model_dump(mode="python", exclude={"locator_hash"})
    values["blob_uri"] = uri
    values["locator_hash"] = canonical_sha256(values)
    parsed = ImmutableSourceLocator.model_validate(values)
    assert parsed.blob_uri == uri


@pytest.mark.contract
def test_unicode_evidence_offsets_quote_and_hash_are_exact() -> None:
    unit = source_unit()
    emoji_index = unit.text.index("😀")
    span = EvidenceSpan.mint_verified(
        source_unit=unit,
        span_start=emoji_index,
        span_end=emoji_index + 1,
        verifier_name="local-exact-span-verifier",
        verifier_version="1.0.0",
        verified_at_utc=NOW,
    )
    assert span.quote == "😀"
    assert span.span_end - span.span_start == 1
    span.verify_against(unit)
    payload = span.model_dump(mode="json")
    payload["evidence_span_id"] = "model-authored"
    with pytest.raises(ValidationError, match="not minted"):
        EvidenceSpan.model_validate_json(json.dumps(payload))
    payload = span.model_dump(mode="json")
    payload["locator"]["char_end"] = span.span_end + 1
    payload["locator"]["locator_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload["locator"].items()
            if key != "locator_hash"
        }
    )
    payload["identity"]["immutable_locator"] = payload["locator"]
    with pytest.raises(ValidationError, match="offsets"):
        EvidenceSpan.model_validate_json(json.dumps(payload))


@pytest.mark.contract
def test_source_unit_rejects_tampered_deterministic_id() -> None:
    payload = source_unit().model_dump(mode="json")
    payload["source_unit_id"] = "source-unit:tampered"
    payload["identity"]["source_unit_id"] = "source-unit:tampered"
    with pytest.raises(ValidationError, match="deterministic seed"):
        SourceUnit.model_validate(payload)


@pytest.mark.contract
def test_evidence_verification_binds_non_span_locator_coordinates() -> None:
    unit = source_unit()
    span = EvidenceSpan.mint_verified(
        source_unit=unit,
        span_start=0,
        span_end=4,
        verifier_name="locator-verifier",
        verifier_version="1.0.0",
        verified_at_utc=NOW,
    )
    payload = span.model_dump(mode="json")
    payload["locator"]["page"] = 1
    payload["locator"]["locator_hash"] = canonical_sha256(
        {
            key: value
            for key, value in payload["locator"].items()
            if key != "locator_hash"
        }
    )
    payload["identity"]["immutable_locator"] = payload["locator"]
    moved = EvidenceSpan.model_validate_json(json.dumps(payload))
    with pytest.raises(ValueError, match="source coordinates"):
        moved.verify_against(unit)


@pytest.mark.contract
def test_locator_preserves_nested_authority_coordinates_immutably() -> None:
    values = locator().model_dump(mode="json", exclude={"locator_hash"})
    values["polygon"] = [[1.0, 2.0], [3.0, 4.0]]
    values["transform"] = {"scale": 0.5, "origin": [0.0, 0.0]}
    nested = ImmutableSourceLocator(
        **values,
        locator_hash=canonical_sha256(values),
    )
    assert nested.to_authority()["polygon"] == [[1.0, 2.0], [3.0, 4.0]]
    with pytest.raises(TypeError):
        nested.polygon[0][0] = 9.0
    with pytest.raises(TypeError):
        nested.transform["scale"] = 2.0


@pytest.mark.contract
@pytest.mark.parametrize("start", range(0, 8))
@pytest.mark.parametrize("width", range(1, 5))
def test_unicode_span_ranges_exhaustively(start: int, width: int) -> None:
    unit = source_unit()
    end = start + width
    if end <= unit.codepoint_count:
        span = EvidenceSpan.mint_verified(
            source_unit=unit,
            span_start=start,
            span_end=end,
            verifier_name="range-verifier",
            verifier_version="1.0.0",
            verified_at_utc=NOW,
        )
        assert span.quote == unit.text[start:end]
        span.verify_against(unit)
    else:
        with pytest.raises(ValueError):
            EvidenceSpan.mint_verified(
                source_unit=unit,
                span_start=start,
                span_end=end,
                verifier_name="range-verifier",
                verifier_version="1.0.0",
                verified_at_utc=NOW,
            )


@pytest.mark.contract
def test_evidence_id_is_deterministic_and_verifier_minted() -> None:
    unit = source_unit()
    kwargs = {
        "source_unit": unit,
        "span_start": 0,
        "span_end": 4,
        "verifier_name": "verifier",
        "verifier_version": "1.0.0",
        "verified_at_utc": NOW,
    }
    first = EvidenceSpan.mint_verified(**kwargs)
    second = EvidenceSpan.mint_verified(**kwargs)
    assert first.evidence_span_id == second.evidence_span_id
    assert first.evidence_span_id == deterministic_contract_id(
        "evidence-span",
        {
            "source_unit_id": unit.source_unit_id,
            "span_start": 0,
            "span_end": 4,
            "quote_hash": first.quote_hash,
            "verifier_name": "verifier",
            "verifier_version": "1.0.0",
        },
    )


@pytest.mark.contract
@pytest.mark.parametrize("transition", sorted(
    allowed_lifecycle_transitions(),
    key=lambda pair: (pair[0].value if pair[0] else "", pair[1].value),
))
def test_every_allowed_lifecycle_transition(
    transition: tuple[AssertionState | None, AssertionState],
) -> None:
    from_state, to_state = transition
    record = CandidateLifecycleRecord.seal(
        identity=identity("c0.candidate_lifecycle_record"),
        lifecycle_record_id=f"lifecycle-{from_state}-{to_state}",
        candidate_id="candidate-1",
        candidate_version_id="candidate-version-1",
        candidate_kind="relationship",
        sequence=0 if from_state is None else 1,
        prior_lifecycle_record_id=None if from_state is None else "lifecycle-prior",
        from_state=from_state,
        to_state=to_state,
        reason_codes=("reason-b", "reason-a"),
        evidence_span_ids=("evidence-1",),
        governance_justification_id=None,
        resolved_source_entity_id="entity-1",
        resolved_target_entity_id="entity-2",
        source_inheritance_path=(),
        target_inheritance_path=(),
        validator_name="fixture-validator",
        validator_version="1.0.0",
        occurred_at_utc=NOW,
    )
    assert record.to_state == to_state


@pytest.mark.contract
def test_every_forbidden_lifecycle_transition() -> None:
    states = list(AssertionState)
    allowed = allowed_lifecycle_transitions()
    for from_state in [None, *states]:
        for to_state in states:
            if (from_state, to_state) in allowed:
                continue
            values = {
                "identity": identity("c0.candidate_lifecycle_record"),
                "lifecycle_record_id": "lifecycle-invalid",
                "candidate_id": "candidate-1",
                "candidate_version_id": "candidate-version-1",
                "candidate_kind": "relationship",
                "sequence": 0 if from_state is None else 1,
                "prior_lifecycle_record_id": (
                    None if from_state is None else "lifecycle-prior"
                ),
                "from_state": from_state,
                "to_state": to_state,
                "reason_codes": (),
                "evidence_span_ids": (),
                "governance_justification_id": None,
                "resolved_source_entity_id": None,
                "resolved_target_entity_id": None,
                "source_inheritance_path": (),
                "target_inheritance_path": (),
                "validator_name": "fixture-validator",
                "validator_version": "1.0.0",
                "occurred_at_utc": NOW,
            }
            values["transition_hash"] = canonical_sha256(
                {
                    key: value
                    for key, value in values.items()
                    if key not in {"transition_hash", "occurred_at_utc"}
                }
            )
            with pytest.raises(ValidationError, match="forbidden lifecycle transition"):
                CandidateLifecycleRecord(**values)


@pytest.mark.contract
def test_candidate_accounting_is_mutually_exclusive_and_non_additive() -> None:
    audit = audit_projection()
    assert (
        audit.input_candidate_count
        == audit.retained_candidate_count + audit.deduplicated_input_count
    )
    assert sum(audit.lifecycle_state_counts.values()) == audit.retained_candidate_count
    assert sum(audit.reason_code_counts.values()) > audit.input_candidate_count
    with pytest.raises(TypeError):
        audit.reason_code_counts["forged"] = 99
    with pytest.raises(ValidationError, match="projection_hash"):
        audit.model_copy(update={"reason_code_counts": {"forged": 99}})


@pytest.mark.contract
def test_invalid_dedup_target_or_duplicate_disposition_fails() -> None:
    audit = audit_projection()
    values = audit.model_dump(mode="python", exclude={"projection_hash"})
    duplicate = copy.deepcopy(values)
    duplicate["candidate_dispositions"] = (
        audit.candidate_dispositions[0],
        audit.candidate_dispositions[0],
    )
    duplicate["projection_hash"] = canonical_sha256(duplicate)
    with pytest.raises(ValidationError):
        AuditProjection.model_validate(duplicate)

    orphan = copy.deepcopy(values)
    orphan_disposition = disposition("input-2", dedup_target="missing-retained")
    orphan["candidate_dispositions"] = (
        audit.candidate_dispositions[0],
        orphan_disposition,
    )
    orphan["projection_hash"] = canonical_sha256(orphan)
    with pytest.raises(ValidationError, match="map to one retained"):
        AuditProjection.model_validate(orphan)


@pytest.mark.contract
def test_serving_projection_is_exact_asserted_subset() -> None:
    validate_asserted_serving_subset(
        audit_projection(),
        serving_projection(),
        asserted_entity_ids={"entity-assertion-1"},
        asserted_relationship_ids={"relationship-assertion-1"},
        asserted_property_ids=set(),
    )
    mismatched_values = serving_projection().model_dump(
        mode="python",
        exclude={"projection_hash"},
    )
    mismatched_values["source_manifest_hash"] = HASH_C
    semantic_values = dict(mismatched_values)
    semantic_values.pop("sealed_at_utc")
    mismatched = SemanticServingProjection(
        **mismatched_values,
        projection_hash=canonical_sha256(semantic_values),
    )
    with pytest.raises(ValueError, match="source manifest"):
        validate_asserted_serving_subset(
            audit_projection(),
            mismatched,
            asserted_entity_ids={"entity-assertion-1"},
            asserted_relationship_ids={"relationship-assertion-1"},
            asserted_property_ids=set(),
        )
    with pytest.raises(ValueError, match="exact asserted subset"):
        validate_asserted_serving_subset(
            audit_projection(),
            serving_projection(),
            asserted_entity_ids=set(),
            asserted_relationship_ids={"relationship-assertion-1"},
            asserted_property_ids=set(),
        )


@pytest.mark.contract
def test_raw_table_names_cannot_be_serving_membership() -> None:
    payload = serving_projection().model_dump(mode="python")
    payload["entity_assertion_ids"] = ("entities",)
    semantic = dict(payload)
    semantic.pop("projection_hash")
    semantic.pop("sealed_at_utc")
    payload["projection_hash"] = canonical_sha256(semantic)
    raw_serving = SemanticServingProjection.model_validate(payload)
    with pytest.raises(ValueError):
        validate_asserted_serving_subset(
            audit_projection(),
            raw_serving,
            asserted_entity_ids={"entity-assertion-1"},
            asserted_relationship_ids={"relationship-assertion-1"},
            asserted_property_ids=set(),
        )


@pytest.mark.contract
def test_manifest_totals_and_hash_reconcile() -> None:
    manifest = artifact_manifest()
    assert manifest.total_byte_count == sum(
        entry.byte_count for entry in manifest.entries
    )
    payload = manifest.model_dump(mode="python")
    payload["total_byte_count"] += 1
    with pytest.raises(ValidationError):
        ArtifactManifest.model_validate(payload)


@pytest.mark.contract
def test_receipt_skip_requires_prior_success_and_intact_output() -> None:
    intact = artifact_manifest()
    prior = stage_receipt(output_manifest_hash=intact.manifest_hash)
    skipped = stage_receipt("skipped", output_manifest_hash=intact.manifest_hash)
    validate_skip_preconditions(
        skipped,
        prior_succeeded=prior,
        intact_output_manifest=intact,
    )
    changed_values = skipped.model_dump(mode="python", exclude={"receipt_hash"})
    changed_values["skip_key"] = HASH_A
    semantic_values = dict(changed_values)
    semantic_values.pop("started_at_utc")
    semantic_values.pop("completed_at_utc")
    changed = StageReceipt(
        **changed_values,
        receipt_hash=canonical_sha256(semantic_values),
    )
    with pytest.raises(ValueError, match="skip key"):
        validate_skip_preconditions(
            changed,
            prior_succeeded=prior,
            intact_output_manifest=intact,
        )
    with pytest.raises(TypeError):
        prior.accepted_contract_versions["c0.source_unit"] = ">=9"


@pytest.mark.contract
def test_receipt_rejects_uri_user_info_in_remote_operation_refs() -> None:
    payload = stage_receipt().model_dump(mode="python")
    payload["remote_operation_refs"] = (
        "https://user:password@operations.example.test/job-1",
    )
    with pytest.raises(ValidationError, match="URI credentials"):
        StageReceipt.model_validate(payload)


@pytest.mark.contract
def test_metrics_measure_without_numeric_threshold_policy() -> None:
    metrics = resource_metrics()
    receipt = stage_receipt()
    validate_receipt_resources(receipt, metrics)
    assert not hasattr(metrics, "cache_ttl_seconds")
    assert not hasattr(metrics, "p95_regression_threshold")
    assert not hasattr(metrics, "peak_rss_regression_threshold")


@pytest.mark.contract
def test_succeeded_receipt_rejects_exceeded_hard_dimension() -> None:
    metrics = resource_metrics()
    payload = metrics.model_dump(mode="python", exclude={"metrics_hash"})
    payload["exceeded_dimensions"] = ("evidence_completeness",)
    exceeded = StageResourceMetrics(
        **payload,
        metrics_hash=canonical_sha256(payload),
    )
    receipt_values = stage_receipt().model_dump(
        mode="python",
        exclude={"receipt_hash"},
    )
    receipt_values["resource_metrics_id"] = exceeded.resource_metrics_id
    receipt_values["resource_metrics_hash"] = exceeded.metrics_hash
    semantic_values = dict(receipt_values)
    semantic_values.pop("started_at_utc")
    semantic_values.pop("completed_at_utc")
    receipt = StageReceipt(
        **receipt_values,
        receipt_hash=canonical_sha256(semantic_values),
    )
    with pytest.raises(ValueError, match="hard dimension"):
        validate_receipt_resources(receipt, exceeded)


@pytest.mark.contract
def test_generated_schema_registry_matches_registered_kinds() -> None:
    registry = json.loads(
        (
            Path(__file__).parents[2]
            / "src"
            / "fabric_kg_builder"
            / "contracts"
            / "schemas"
            / "registry.json"
        ).read_text(encoding="utf-8")
    )
    assert {
        (item["contract_kind"], item["contract_version"])
        for item in registry["schemas"]
    } == set(REGISTERED_CONTRACT_VERSIONS)
    for item in registry["schemas"]:
        path = (
            Path(__file__).parents[2]
            / "src"
            / "fabric_kg_builder"
            / "contracts"
            / "schemas"
            / item["path"]
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert canonical_sha256(payload) == item["schema_hash"]
        schema = payload["schema"]
        identity_schema = schema
        if item["contract_kind"] != "c0.identity":
            identity_schema = next(
                definition
                for name, definition in schema["$defs"].items()
                if name
                in {
                    "CanonicalIdentityEnvelope",
                    "EvidenceIdentityV1_1",
                    "RequiredMemberManifestIdentityV1_1",
                    "RequiredMemberSetProposalIdentityV1_1",
                    "PublicationCrosswalkIdentityV1_1",
                    "PublicationCrosswalkIdentityV1_2",
                    "ProjectionEquivalenceIdentityV1_1",
                    "QueryBudgetIdentityV1_1",
                    "AgenticRetrievalRequestContextIdentityV1_1",
                    "AgenticRetrievalCoverageReceiptIdentityV1_1",
                }
            )
        assert (
            identity_schema["properties"]["contract_kind"]["const"]
            == item["contract_kind"]
        )
        assert (
            identity_schema["properties"]["contract_version"]["const"]
            == item["contract_version"]
        )


@pytest.mark.contract
def test_schema_writer_retains_legacy_hash_lookup_keys(tmp_path: Path) -> None:
    hashes = write_registered_schemas(tmp_path)
    assert hashes["c0.source_unit"] == hashes["c0.source_unit@1.0.0"]
    assert hashes["c0.evidence_span"] == hashes["c0.evidence_span@1.0.0"]
    assert "c0.evidence_span@1.1.0" in hashes
