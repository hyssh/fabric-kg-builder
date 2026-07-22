"""Tests for graph/summarizer.py — DeterministicSummarizer, SummaryVerifier, consolidate helpers."""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from fabric_kg_builder.graph.summarizer import (
    DeterministicSummarizer,
    LLMSummarizer,
    SummaryConsolidationResult,
    SummaryVerifier,
    SummarizerProtocol,
    VerificationResult,
    consolidate_description,
    consolidate_description_typed,
)


# ---------------------------------------------------------------------------
# SummaryConsolidationResult
# ---------------------------------------------------------------------------

class TestSummaryConsolidationResult:
    def test_as_string_returns_summary(self):
        r = SummaryConsolidationResult(summary="Hello world")
        assert r.as_string() == "Hello world"

    def test_defaults(self):
        r = SummaryConsolidationResult(summary="")
        assert r.distinct_facts == []
        assert r.supporting_occurrence_ids == []
        assert r.supporting_evidence_ids == []

    def test_with_facts_and_ids(self):
        r = SummaryConsolidationResult(
            summary="Test",
            distinct_facts=["fact1", "fact2"],
            supporting_occurrence_ids=["occ1"],
            supporting_evidence_ids=["ev1"],
        )
        assert len(r.distinct_facts) == 2
        assert r.supporting_occurrence_ids == ["occ1"]


# ---------------------------------------------------------------------------
# SummaryVerifier
# ---------------------------------------------------------------------------

class TestSummaryVerifier:
    def setup_method(self):
        self.verifier = SummaryVerifier()

    def test_passes_when_all_occurrences_present(self):
        result = SummaryConsolidationResult(
            summary="The company is profitable",
            distinct_facts=["company is profitable"],
            supporting_occurrence_ids=["occ1"],
        )
        occ_map = {"occ1": "The company is profitable in Q3"}
        vr = self.verifier.verify(result, occ_map)
        assert vr.passed is True
        assert vr.missing_occurrence_ids == []

    def test_fails_when_occurrence_missing(self):
        result = SummaryConsolidationResult(
            summary="The company is profitable",
            supporting_occurrence_ids=["occ_missing"],
        )
        vr = self.verifier.verify(result, {})
        assert vr.passed is False
        assert "occ_missing" in vr.missing_occurrence_ids

    def test_fails_when_fact_not_in_any_text(self):
        result = SummaryConsolidationResult(
            summary="The company is profitable",
            distinct_facts=["quantum teleportation"],
            supporting_occurrence_ids=["occ1"],
        )
        occ_map = {"occ1": "The company is profitable"}
        vr = self.verifier.verify(result, occ_map)
        assert vr.passed is False
        assert "quantum teleportation" in vr.unrepresented_facts

    def test_passes_when_no_facts(self):
        result = SummaryConsolidationResult(
            summary="Short summary",
            distinct_facts=[],
            supporting_occurrence_ids=[],
        )
        vr = self.verifier.verify(result, {})
        assert vr.passed is True

    def test_partial_token_match_counts_as_represented(self):
        result = SummaryConsolidationResult(
            summary="revenue growth",
            distinct_facts=["revenue growth"],
            supporting_occurrence_ids=["occ1"],
        )
        occ_map = {"occ1": "Q3 revenue and growth exceeded expectations"}
        vr = self.verifier.verify(result, occ_map)
        assert vr.passed is True


# ---------------------------------------------------------------------------
# DeterministicSummarizer
# ---------------------------------------------------------------------------

class TestDeterministicSummarizer:
    def setup_method(self):
        self.s = DeterministicSummarizer()

    def test_empty_list_returns_empty_string(self):
        assert self.s.summarize([], max_length=300) == ""

    def test_single_text_is_returned(self):
        result = self.s.summarize(["Acme is a profitable company."], max_length=300)
        assert "Acme" in result

    def test_duplicate_texts_are_deduped(self):
        texts = ["Same text.", "Same text.", "Same text."]
        result = self.s.summarize(texts, max_length=300)
        assert result.count("Same text.") == 1

    def test_max_length_truncates(self):
        long_text = "A" * 500
        result = self.s.summarize([long_text], max_length=100)
        assert len(result) <= 100

    def test_longest_text_is_base(self):
        texts = ["Short.", "This is a much longer description about the company."]
        result = self.s.summarize(texts, max_length=300)
        assert "longer description" in result

    def test_consolidate_returns_typed_result(self):
        result = self.s.consolidate(["Some text."])
        assert isinstance(result, SummaryConsolidationResult)
        assert result.summary != ""

    def test_consolidate_tracks_occurrence_ids(self):
        result = self.s.consolidate(["Text."], occurrence_ids=["occ1"])
        assert result.supporting_occurrence_ids == ["occ1"]

    def test_consolidate_tracks_evidence_ids(self):
        result = self.s.consolidate(["Text."], evidence_ids=["ev1", "ev2"])
        assert result.supporting_evidence_ids == ["ev1", "ev2"]

    def test_consolidate_empty_list(self):
        result = self.s.consolidate([])
        assert result.summary == ""

    def test_adds_new_tokens_texts(self):
        # Second text adds >= 2 new tokens — should be appended
        texts = [
            "Apple is a technology company.",
            "Apple manufactures innovative smartphone products.",
        ]
        result = self.s.consolidate(texts, max_length=300)
        # The result should contain tokens from both or just the longer
        assert len(result.summary) > 0

    def test_distinct_facts_populated(self):
        texts = [
            "Apple is a big company.",
            "Apple manufactures innovative products globally.",
        ]
        result = self.s.consolidate(texts)
        assert len(result.distinct_facts) >= 1

    def test_whitespace_only_texts_filtered(self):
        texts = ["   ", "", "Real text about something."]
        result = self.s.summarize(texts, max_length=300)
        assert "Real text" in result

    def test_satisfies_protocol(self):
        assert isinstance(self.s, SummarizerProtocol)


# ---------------------------------------------------------------------------
# LLMSummarizer
# ---------------------------------------------------------------------------

class TestLLMSummarizer:
    def _make_mock_client(self, response_json: str):
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value.choices[0].message.content = response_json
        return mock_client

    def test_summarize_calls_client(self):
        client = self._make_mock_client(
            '{"summary": "Acme is a company.", "distinct_facts": [], "grounded": true}'
        )
        s = LLMSummarizer(client)
        result = s.summarize(["Acme text"], max_length=300)
        assert result == "Acme is a company."

    def test_consolidate_returns_typed_result(self):
        client = self._make_mock_client(
            '{"summary": "Summary here.", "distinct_facts": ["fact1"], "grounded": true}'
        )
        s = LLMSummarizer(client)
        result = s.consolidate(["text"], occurrence_ids=["occ1"])
        assert isinstance(result, SummaryConsolidationResult)
        assert result.summary == "Summary here."
        assert result.supporting_occurrence_ids == ["occ1"]

    def test_empty_texts_returns_empty_without_api_call(self):
        client = MagicMock()
        s = LLMSummarizer(client)
        result = s.consolidate([])
        assert result.summary == ""
        client.chat.completions.create.assert_not_called()

    def test_hallucination_phrase_raises(self):
        client = self._make_mock_client(
            '{"summary": "As an AI, I cannot answer.", "distinct_facts": [], "grounded": true}'
        )
        s = LLMSummarizer(client)
        with pytest.raises(Exception):
            s.summarize(["text"])

    def test_max_length_truncates(self):
        client = self._make_mock_client(
            '{"summary": "' + "X" * 500 + '", "distinct_facts": [], "grounded": true}'
        )
        s = LLMSummarizer(client)
        result = s.summarize(["text"], max_length=50)
        assert len(result) <= 50


# ---------------------------------------------------------------------------
# consolidate_description
# ---------------------------------------------------------------------------

class TestConsolidateDescription:
    def test_uses_deterministic_by_default(self):
        result = consolidate_description(["Hello world."])
        assert isinstance(result, str)
        assert "Hello" in result

    def test_empty_texts_returns_empty_string(self):
        result = consolidate_description([])
        assert result == ""

    def test_custom_summarizer_with_consolidate(self):
        # If summarizer has 'consolidate', it should be called
        mock = MagicMock()
        mock.consolidate.return_value = SummaryConsolidationResult(summary="Consolidated")
        result = consolidate_description(["text"], summarizer=mock)
        assert result == "Consolidated"
        mock.consolidate.assert_called_once()

    def test_custom_summarizer_without_consolidate(self):
        # Uses summarize() if no consolidate attribute
        class SimpleSummarizer:
            def summarize(self, texts: list[str], *, max_length: int) -> str:
                return " ".join(texts)[:max_length]
        
        result = consolidate_description(["A.", "B."], summarizer=SimpleSummarizer())
        assert "A" in result

    def test_occurrence_ids_forwarded(self):
        mock = MagicMock()
        mock.consolidate.return_value = SummaryConsolidationResult(summary="X")
        consolidate_description(["text"], summarizer=mock, occurrence_ids=["occ1"])
        args = mock.consolidate.call_args
        # args[0] is positional tuple: (texts, occurrence_ids, evidence_ids)
        assert ["occ1"] in args[0]


# ---------------------------------------------------------------------------
# consolidate_description_typed
# ---------------------------------------------------------------------------

class TestConsolidateDescriptionTyped:
    def test_returns_typed_result(self):
        result = consolidate_description_typed(["Some text."])
        assert isinstance(result, SummaryConsolidationResult)

    def test_empty_returns_empty(self):
        result = consolidate_description_typed([])
        assert result.summary == ""

    def test_uses_consolidate_on_det_summarizer(self):
        s = DeterministicSummarizer()
        result = consolidate_description_typed(["Some long text."], summarizer=s)
        assert isinstance(result, SummaryConsolidationResult)

    def test_with_no_consolidate_summarizer(self):
        class SimpleSummarizer:
            def summarize(self, texts: list[str], *, max_length: int) -> str:
                return "short"
        
        result = consolidate_description_typed(["text"], summarizer=SimpleSummarizer())
        assert result.summary == "short"
        assert result.distinct_facts == ["short"]
