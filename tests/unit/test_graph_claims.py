"""Tests for graph/claims.py — DeterministicClaimExtractor and helpers."""
from __future__ import annotations

import json
import pytest

from fabric_kg_builder.graph.claims import (
    ClaimExtractionResult,
    ClaimExtractorProtocol,
    DeterministicClaimExtractor,
    LLMClaimExtractor,
    _parse_dt_optional,
    _parse_dt_strict,
    _sentence_status,
    extract_claims,
)


# ---------------------------------------------------------------------------
# _parse_dt_strict
# ---------------------------------------------------------------------------

class TestParseDtStrict:
    def test_parses_full_iso_z(self):
        dt = _parse_dt_strict("2024-01-15T10:30:00Z")
        assert dt.year == 2024
        assert dt.month == 1
        assert dt.day == 15
        assert dt.hour == 10

    def test_parses_full_iso_no_z(self):
        dt = _parse_dt_strict("2024-01-15T10:30:00")
        assert dt.year == 2024

    def test_parses_date_only(self):
        dt = _parse_dt_strict("2024-06-01")
        assert dt.year == 2024
        assert dt.month == 6

    def test_parses_year_only(self):
        dt = _parse_dt_strict("2023")
        assert dt.year == 2023

    def test_raises_on_invalid(self):
        with pytest.raises(ValueError, match="Unrecognised date string"):
            _parse_dt_strict("not-a-date")

    def test_raises_on_wrong_format(self):
        with pytest.raises(ValueError):
            _parse_dt_strict("15/01/2024")


# ---------------------------------------------------------------------------
# _parse_dt_optional
# ---------------------------------------------------------------------------

class TestParseDtOptional:
    def test_returns_none_for_none(self):
        assert _parse_dt_optional(None) is None

    def test_returns_none_for_empty_string(self):
        assert _parse_dt_optional("") is None

    def test_parses_date(self):
        dt = _parse_dt_optional("2024-03-10")
        assert dt is not None
        assert dt.year == 2024

    def test_raises_on_invalid_date(self):
        with pytest.raises(ValueError):
            _parse_dt_optional("invalid")


# ---------------------------------------------------------------------------
# _sentence_status
# ---------------------------------------------------------------------------

class TestSentenceStatus:
    def test_normal_sentence_is_asserted(self):
        assert _sentence_status("The company is profitable.") == "asserted"

    def test_no_longer_is_retracted(self):
        assert _sentence_status("The company is no longer profitable.") == "retracted"

    def test_not_is_retracted(self):
        assert _sentence_status("The product is not available.") == "retracted"

    def test_never_is_retracted(self):
        assert _sentence_status("The CEO never agreed.") == "retracted"

    def test_alleged_is_disputed(self):
        # "alleged" (word boundary) matches; "allegedly" does not
        assert _sentence_status("The alleged misconduct is severe.") == "disputed"

    def test_reported_is_disputed(self):
        assert _sentence_status("The firm reportedly has issues.") == "disputed"

    def test_contested_is_disputed(self):
        assert _sentence_status("The claim is contested.") == "disputed"

    def test_disputed_trumps_retracted(self):
        # disputed check is done first
        status = _sentence_status("The alleged cause is no longer valid.")
        assert status == "disputed"


# ---------------------------------------------------------------------------
# ClaimExtractionResult
# ---------------------------------------------------------------------------

class TestClaimExtractionResult:
    def test_empty_result(self):
        result = ClaimExtractionResult()
        assert result.claims == []
        assert result.evidence_links == []
        assert result.contradicting_pairs == []
        assert result.claim_ids == []

    def test_claim_ids_property(self):
        from fabric_kg_builder.model.schemas import ClaimRow
        from datetime import datetime, timezone
        claim = ClaimRow(
            claim_id="claim:abc123",
            subject_entity_id="entity:sub",
            predicate="is",
            status="asserted",
            observed_at=datetime.now(timezone.utc),
            review_state="not_reviewed",
        )
        result = ClaimExtractionResult(claims=[claim])
        assert result.claim_ids == ["claim:abc123"]


# ---------------------------------------------------------------------------
# DeterministicClaimExtractor
# ---------------------------------------------------------------------------

class TestDeterministicClaimExtractor:
    def setup_method(self):
        self.extractor = DeterministicClaimExtractor()

    def test_empty_text_returns_no_claims(self):
        result = self.extractor.extract("", "entity:sub")
        assert result.claims == []
        assert result.evidence_links == []

    def test_extracts_simple_claim(self):
        text = "Acme is a profitable company."
        result = self.extractor.extract(text, "entity:acme")
        assert len(result.claims) >= 1
        assert result.claims[0].subject_entity_id == "entity:acme"

    def test_asserted_confidence(self):
        text = "Acme provides excellent service."
        result = self.extractor.extract(text, "entity:acme")
        assert len(result.claims) >= 1
        for claim in result.claims:
            if claim.status == "asserted":
                assert claim.confidence == pytest.approx(0.7)

    def test_disputed_confidence(self):
        text = "The alleged fraud provides a false pretext."
        result = self.extractor.extract(text, "entity:co")
        disputed = [c for c in result.claims if c.status == "disputed"]
        if disputed:
            assert disputed[0].confidence == pytest.approx(0.5)

    def test_retracted_claim(self):
        text = "The company no longer provides support."
        result = self.extractor.extract(text, "entity:co")
        retracted = [c for c in result.claims if c.status == "retracted"]
        assert len(retracted) >= 1

    def test_evidence_id_creates_evidence_links(self):
        text = "Acme is a leader in its field."
        result = self.extractor.extract(text, "entity:acme", evidence_id="ev:001")
        assert len(result.evidence_links) >= 1
        for link in result.evidence_links:
            assert link.evidence_id == "ev:001"

    def test_no_evidence_id_creates_no_links(self):
        text = "Acme is a leader."
        result = self.extractor.extract(text, "entity:acme")
        assert result.evidence_links == []

    def test_occurrence_id_in_links(self):
        text = "Acme is a leader."
        result = self.extractor.extract(
            text, "entity:acme", evidence_id="ev:001", occurrence_id="occ:001"
        )
        for link in result.evidence_links:
            assert link.occurrence_id == "occ:001"

    def test_domain_hash_in_claims(self):
        text = "Acme is a company."
        result = self.extractor.extract(text, "entity:acme", domain_hash="dhash123")
        for claim in result.claims:
            assert claim.domain_hash == "dhash123"

    def test_run_id_in_claims(self):
        text = "Acme is a company."
        result = self.extractor.extract(text, "entity:acme", run_id="run-001")
        for claim in result.claims:
            assert claim.run_id == "run-001"

    def test_duplicate_claim_ids_unique(self):
        text = "Acme is large. Beta is small."
        result = self.extractor.extract(text, "entity:x")
        claim_ids = [c.claim_id for c in result.claims]
        assert len(claim_ids) == len(set(claim_ids))

    def test_date_extraction(self):
        text = "Acme has been a leader since 2020."
        result = self.extractor.extract(text, "entity:acme")
        dated_claims = [c for c in result.claims if c.valid_from is not None]
        assert len(dated_claims) >= 1

    def test_two_dates_valid_from_and_to(self):
        text = "Acme provides support from 2019 to 2023."
        result = self.extractor.extract(text, "entity:acme")
        two_date_claims = [c for c in result.claims if c.valid_from and c.valid_to]
        if two_date_claims:
            c = two_date_claims[0]
            assert c.valid_from < c.valid_to

    def test_contradictions_detected(self):
        text = "Acme is profitable. Acme is no longer profitable."
        result = self.extractor.extract(text, "entity:acme")
        assert len(result.contradicting_pairs) >= 1

    def test_no_contradictions_when_different_predicates(self):
        text = "Acme is profitable. Acme has many employees."
        result = self.extractor.extract(text, "entity:acme")
        assert len(result.contradicting_pairs) == 0

    def test_claim_id_stable_across_runs(self):
        text = "Acme is profitable."
        r1 = self.extractor.extract(text, "entity:acme")
        r2 = self.extractor.extract(text, "entity:acme")
        assert [c.claim_id for c in r1.claims] == [c.claim_id for c in r2.claims]

    def test_whitespace_only_text_returns_no_claims(self):
        result = self.extractor.extract("   \n   ", "entity:x")
        assert result.claims == []

    def test_multiple_sentences(self):
        text = "Acme is large. Acme provides services. Beta has great quality."
        result = self.extractor.extract(text, "entity:x")
        assert len(result.claims) >= 1


# ---------------------------------------------------------------------------
# extract_claims (public API)
# ---------------------------------------------------------------------------

class TestExtractClaims:
    def test_uses_deterministic_by_default(self):
        result = extract_claims("Acme is a company.", "entity:acme")
        assert isinstance(result, ClaimExtractionResult)

    def test_custom_extractor(self):
        class _Fake:
            def extract(self, text, subject_entity_id, *, evidence_id=None,
                        occurrence_id=None, domain_hash=None, run_id=""):
                return ClaimExtractionResult()

        result = extract_claims("Some text", "entity:x", extractor=_Fake())
        assert result.claims == []

    def test_protocol_check(self):
        # DeterministicClaimExtractor satisfies the protocol
        extractor = DeterministicClaimExtractor()
        assert isinstance(extractor, ClaimExtractorProtocol)

    def test_evidence_id_forwarded(self):
        result = extract_claims(
            "Acme is a company.", "entity:acme", evidence_id="ev:99"
        )
        assert any(l.evidence_id == "ev:99" for l in result.evidence_links)


# ---------------------------------------------------------------------------
# LLMClaimExtractor protocol compliance
# ---------------------------------------------------------------------------

class TestLLMClaimExtractor:
    def test_satisfies_protocol(self):
        class _FakeClient:
            pass
        extractor = LLMClaimExtractor(_FakeClient())
        assert isinstance(extractor, ClaimExtractorProtocol)

    def test_calls_client(self):
        from unittest.mock import MagicMock
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value.choices[0].message.content = (
            '{"claims": [{"predicate": "is", "object_text": "profitable", "status": "asserted"}]}'
        )
        extractor = LLMClaimExtractor(mock_client)
        result = extractor.extract("Acme is profitable.", "entity:acme", evidence_id="ev:1")
        assert len(result.claims) == 1
        assert result.claims[0].predicate == "is"

    def test_invalid_status_raises(self):
        from unittest.mock import MagicMock
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value.choices[0].message.content = (
            '{"claims": [{"predicate": "is", "object_text": "x", "status": "wrong_status"}]}'
        )
        extractor = LLMClaimExtractor(mock_client)
        with pytest.raises(Exception):
            extractor.extract("text", "entity:x")
