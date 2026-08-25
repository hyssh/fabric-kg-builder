"""Additive EvidenceSpan 1.1 contract and compatibility tests."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from fabric_kg_builder.contracts import (
    EvidencePurposeAmbiguousError,
    EvidencePurposePromotionError,
    EvidenceSpan,
    EvidenceSpanV1_1,
    REGISTERED_CONTRACT_VERSIONS,
    SUPPORTED_VERSIONS,
    SourceUnit,
    TrustedL1DesignEvidenceManifestContext,
    adapt_evidence_span_v1_0_to_v1_1,
    canonical_json,
    canonical_sha256,
    negotiate_contract,
    parse_contract,
)
from fabric_kg_builder.contracts.base import deterministic_contract_id, utf8_sha256

FIXTURES = Path(__file__).parent.parent / "fixtures" / "contracts"
NOW = datetime(2026, 8, 24, 17, 0, tzinfo=timezone.utc)
DESIGN_VERIFIER = "fabric-kg.local-evidence-verifier/domain_design"


def source_unit() -> SourceUnit:
    return parse_contract(
        (FIXTURES / "valid" / "source-unit.json").read_text(encoding="utf-8")
    )


def legacy_span(*, verifier_name: str = DESIGN_VERIFIER) -> EvidenceSpan:
    return EvidenceSpan.mint_verified(
        source_unit=source_unit(),
        span_start=0,
        span_end=6,
        verifier_name=verifier_name,
        verifier_version="1.0.0",
        verified_at_utc=NOW,
    )


def trusted_manifest(span: EvidenceSpan) -> TrustedL1DesignEvidenceManifestContext:
    return TrustedL1DesignEvidenceManifestContext(
        manifest_contract_kind="l1.design_sample_manifest",
        manifest_contract_version="1.0.0",
        design_sample_manifest_id="design-sample-manifest:trusted",
        design_sample_manifest_hash="d" * 64,
        evidence_span_ids=(span.evidence_span_id,),
    )


def mint_v1_1(
    purpose: str = "domain_design",
    *,
    verifier_purpose_version: str = "1.0.0",
) -> EvidenceSpanV1_1:
    return EvidenceSpanV1_1.mint_verified(
        source_unit=source_unit(),
        span_start=0,
        span_end=6,
        verifier_name=DESIGN_VERIFIER,
        verifier_version="1.0.0",
        purpose=purpose,
        verifier_purpose_version=verifier_purpose_version,
        verified_at_utc=NOW,
    )


@pytest.mark.contract
def test_v1_0_hash_and_id_seed_remain_unchanged() -> None:
    span = legacy_span()
    assert span.evidence_span_id == deterministic_contract_id(
        "evidence-span",
        {
            "source_unit_id": span.source_unit_id,
            "span_start": 0,
            "span_end": 6,
            "quote_hash": span.quote_hash,
            "verifier_name": DESIGN_VERIFIER,
            "verifier_version": "1.0.0",
        },
    )
    assert canonical_sha256(span) == (
        "754e0eea212b5864984592546a7f62e9d3ec7fc2d385b020a57e18e40a3fe3e6"
    )
    assert isinstance(parse_contract(canonical_json(span)), EvidenceSpan)


@pytest.mark.contract
def test_v1_1_requires_exact_version_and_structured_purpose_fields() -> None:
    span = mint_v1_1()
    assert span.identity.contract_version == "1.1.0"
    assert span.purpose == "domain_design"
    payload = span.model_dump(mode="python")
    for field_name in ("purpose", "verifier_purpose_version"):
        missing = copy.deepcopy(payload)
        missing.pop(field_name)
        with pytest.raises(ValidationError):
            EvidenceSpanV1_1.model_validate(missing)
    payload["purpose"] = "extraction"
    with pytest.raises(ValidationError):
        EvidenceSpanV1_1.model_validate(payload)
    payload = span.model_dump(mode="json")
    payload["identity"]["contract_version"] = "1.0.0"
    with pytest.raises(ValidationError):
        EvidenceSpanV1_1.model_validate(payload)


@pytest.mark.contract
def test_purpose_and_purpose_version_change_evidence_id_and_hash() -> None:
    design = mint_v1_1()
    extraction = mint_v1_1("extraction_assertion")
    revised = mint_v1_1(verifier_purpose_version="1.1.0")
    assert len(
        {
            design.evidence_span_id,
            extraction.evidence_span_id,
            revised.evidence_span_id,
        }
    ) == 3
    assert len(
        {
            canonical_sha256(design),
            canonical_sha256(extraction),
            canonical_sha256(revised),
        }
    ) == 3


@pytest.mark.contract
def test_trusted_design_adapter_preserves_exact_source_evidence() -> None:
    old = legacy_span()
    adapted = adapt_evidence_span_v1_0_to_v1_1(
        old,
        source_unit=source_unit(),
        trusted_manifest=trusted_manifest(old),
        purpose="domain_design",
        verifier_purpose_version="1.0.0",
    )
    adapted.verify_against(source_unit())
    assert adapted.purpose == "domain_design"
    assert adapted.quote == old.quote
    assert adapted.quote_hash == old.quote_hash
    assert adapted.source_text_content_hash == old.source_text_content_hash
    assert adapted.locator == old.locator


@pytest.mark.contract
@pytest.mark.parametrize("failure", ["ambiguous-verifier", "missing-manifest-entry"])
def test_design_adapter_fails_typed_when_purpose_is_ambiguous(failure: str) -> None:
    old = legacy_span(
        verifier_name=(
            "legacy-verifier" if failure == "ambiguous-verifier" else DESIGN_VERIFIER
        )
    )
    context = trusted_manifest(old)
    if failure == "missing-manifest-entry":
        other = legacy_span(verifier_name="other-verifier")
        context = trusted_manifest(other)
    with pytest.raises(
        EvidencePurposeAmbiguousError,
        match="C0_EVIDENCE_PURPOSE_AMBIGUOUS",
    ):
        adapt_evidence_span_v1_0_to_v1_1(
            old,
            source_unit=source_unit(),
            trusted_manifest=context,
            purpose="domain_design",
            verifier_purpose_version="1.0.0",
        )


@pytest.mark.contract
def test_legacy_extraction_promotion_is_prohibited() -> None:
    old = legacy_span()
    with pytest.raises(
        EvidencePurposePromotionError,
        match="C0_EVIDENCE_PURPOSE_PROMOTION_PROHIBITED",
    ):
        adapt_evidence_span_v1_0_to_v1_1(
            old,
            source_unit=source_unit(),
            trusted_manifest=trusted_manifest(old),
            purpose="extraction_assertion",
            verifier_purpose_version="1.0.0",
        )


@pytest.mark.contract
def test_adapter_rejects_non_legacy_evidence_span() -> None:
    span = mint_v1_1()
    with pytest.raises(ValueError, match="must be EvidenceSpan 1.0.0"):
        adapt_evidence_span_v1_0_to_v1_1(
            span,
            source_unit=source_unit(),
            trusted_manifest=TrustedL1DesignEvidenceManifestContext(
                manifest_contract_kind="l1.design_sample_manifest",
                manifest_contract_version="1.0.0",
                design_sample_manifest_id="design-sample-manifest:trusted",
                design_sample_manifest_hash="d" * 64,
                evidence_span_ids=(span.evidence_span_id,),
            ),
            purpose="domain_design",
            verifier_purpose_version="1.0.0",
        )


@pytest.mark.contract
def test_v1_1_rejects_secret_locator_and_detects_locator_or_quote_tampering() -> None:
    span = mint_v1_1()
    payload = span.model_dump(mode="python")
    secret = copy.deepcopy(payload)
    secret["locator"]["blob_uri"] = "https://storage.example.test/a?sig=secret"
    secret["locator"]["locator_hash"] = canonical_sha256(
        {key: value for key, value in secret["locator"].items() if key != "locator_hash"}
    )
    secret["identity"]["immutable_locator"] = secret["locator"]
    with pytest.raises(ValidationError):
        EvidenceSpanV1_1.model_validate(secret)

    moved = copy.deepcopy(payload)
    moved["locator"]["page"] = 1
    moved["locator"]["locator_hash"] = canonical_sha256(
        {key: value for key, value in moved["locator"].items() if key != "locator_hash"}
    )
    moved["identity"]["immutable_locator"] = moved["locator"]
    with pytest.raises(ValueError, match="source coordinates"):
        EvidenceSpanV1_1.model_validate(moved).verify_against(source_unit())

    changed_quote = copy.deepcopy(payload)
    changed_quote["quote"] = "Cafe X"
    changed_quote["quote_hash"] = utf8_sha256(changed_quote["quote"])
    changed_quote["evidence_span_id"] = deterministic_contract_id(
        "evidence-span",
        {
            "source_unit_id": changed_quote["source_unit_id"],
            "span_start": changed_quote["span_start"],
            "span_end": changed_quote["span_end"],
            "quote_hash": changed_quote["quote_hash"],
            "verifier_name": changed_quote["verifier_name"],
            "verifier_version": changed_quote["verifier_version"],
            "purpose": changed_quote["purpose"],
            "verifier_purpose_version": changed_quote["verifier_purpose_version"],
        },
    )
    with pytest.raises(ValueError, match="exact source substring"):
        EvidenceSpanV1_1.model_validate(changed_quote).verify_against(source_unit())


@pytest.mark.contract
def test_registry_negotiates_only_exact_evidence_versions() -> None:
    assert SUPPORTED_VERSIONS["c0.evidence_span"] == ("1.0.0", "1.1.0")
    assert REGISTERED_CONTRACT_VERSIONS[("c0.evidence_span", "1.0.0")] is EvidenceSpan
    assert (
        REGISTERED_CONTRACT_VERSIONS[("c0.evidence_span", "1.1.0")]
        is EvidenceSpanV1_1
    )
    assert negotiate_contract("c0.evidence_span", "1.0.0") is EvidenceSpan
    assert negotiate_contract("c0.evidence_span", "1.1.0") is EvidenceSpanV1_1
    with pytest.raises(ValueError, match="not registered"):
        negotiate_contract("c0.evidence_span", "1.2.0")


@pytest.mark.contract
def test_v1_1_fixture_round_trip_and_canonical_hash_golden() -> None:
    fixture = (FIXTURES / "valid" / "evidence-span-1.1.json").read_text(
        encoding="utf-8"
    )
    span = parse_contract(fixture)
    assert isinstance(span, EvidenceSpanV1_1)
    expected_json = (
        FIXTURES / "golden" / "evidence-span-1.1.canonical.json"
    ).read_text(encoding="utf-8").rstrip("\n")
    expected_hash = (
        FIXTURES / "golden" / "evidence-span-1.1.sha256"
    ).read_text(encoding="utf-8").strip()
    assert canonical_json(span) == expected_json
    assert canonical_sha256(span) == expected_hash
    assert parse_contract(expected_json) == span


@pytest.mark.contract
@pytest.mark.parametrize(
    "fixture_name",
    [
        "evidence-span-1.1-missing-purpose.json",
        "evidence-span-1.1-invalid-purpose.json",
    ],
)
def test_v1_1_invalid_fixtures_fail_closed(fixture_name: str) -> None:
    with pytest.raises((ValidationError, ValueError)):
        parse_contract(
            (FIXTURES / "invalid" / fixture_name).read_text(encoding="utf-8")
        )
