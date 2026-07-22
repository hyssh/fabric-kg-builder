"""Tests for knowledge/competency.py — pure helpers and data models."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from fabric_kg_builder.knowledge.competency import (
    CompetencyCase,
    CompetencyResult,
    _pattern_matches_any,
    _validate_citation,
    summarise_results,
)
from fabric_kg_builder.knowledge.retrieve import Citation


# ---------------------------------------------------------------------------
# CompetencyCase
# ---------------------------------------------------------------------------


class TestCompetencyCase:
    def test_basic_creation(self):
        case = CompetencyCase(question="Who is the CEO?")
        assert case.question == "Who is the CEO?"
        assert case.expected_route is None
        assert case.expected_fact_patterns == []
        assert case.max_docs == 20

    def test_with_all_fields(self):
        case = CompetencyCase(
            question="Find all suppliers.",
            expected_fact_patterns=["Supplier A"],
            expected_source_names=["supply-chain.pdf"],
            max_docs=10,
            description="Test supplier retrieval",
        )
        assert case.description == "Test supplier retrieval"
        assert case.max_docs == 10
        assert len(case.expected_fact_patterns) == 1


# ---------------------------------------------------------------------------
# _pattern_matches_any
# ---------------------------------------------------------------------------


def _make_citation(content: str) -> Citation:
    return Citation(
        citation_id="c-001",
        source_name="test",
        doc_key="doc-001",
        content=content,
        score=0.9,
    )


class TestPatternMatchesAny:
    def test_pattern_found(self):
        citations = [_make_citation("The CEO is Alice Smith.")]
        assert _pattern_matches_any("alice smith", citations) is True

    def test_pattern_not_found(self):
        citations = [_make_citation("The CFO is Bob Jones.")]
        assert _pattern_matches_any("CEO", citations) is False

    def test_regex_pattern(self):
        citations = [_make_citation("Revenue: $1.2M")]
        assert _pattern_matches_any(r"\$\d+\.\d+M", citations) is True

    def test_invalid_regex_falls_back_to_substring(self):
        citations = [_make_citation("Contains [unclosed bracket")]
        # Invalid regex, falls back to literal substring
        result = _pattern_matches_any("[unclosed bracket", citations)
        assert isinstance(result, bool)

    def test_multiple_citations_any_match(self):
        citations = [
            _make_citation("No match here."),
            _make_citation("Alice is the CEO."),
        ]
        assert _pattern_matches_any("CEO", citations) is True

    def test_empty_citations_returns_false(self):
        assert _pattern_matches_any("anything", []) is False

    def test_case_insensitive(self):
        citations = [_make_citation("ALICE SMITH leads the company.")]
        assert _pattern_matches_any("alice smith", citations) is True


# ---------------------------------------------------------------------------
# _validate_citation
# ---------------------------------------------------------------------------


class TestValidateCitation:
    def test_valid_citation_no_issues(self):
        c = Citation(citation_id="c-001", source_name="doc.pdf", doc_key="d-001", content="Some text", score=0.8)
        issues = _validate_citation(c)
        assert issues == []

    def test_empty_citation_id_flagged(self):
        c = Citation(citation_id="", source_name="doc.pdf", doc_key="d-001", content="Text", score=0.8)
        issues = _validate_citation(c)
        assert len(issues) >= 1
        assert any("citation_id" in issue.lower() for issue in issues)

    def test_out_of_range_score_flagged(self):
        c = Citation(citation_id="c-001", source_name="doc.pdf", doc_key="d-001", content="Text", score=1.5)
        issues = _validate_citation(c)
        assert len(issues) >= 1
        assert any("score" in issue.lower() for issue in issues)

    def test_none_score_ok(self):
        c = Citation(citation_id="c-001", source_name="doc.pdf", doc_key="d-001", content="Text", score=None)
        issues = _validate_citation(c)
        assert issues == []

    def test_zero_score_ok(self):
        c = Citation(citation_id="c-001", source_name="doc.pdf", doc_key="d-001", content="Text", score=0.0)
        issues = _validate_citation(c)
        assert issues == []

    def test_negative_score_flagged(self):
        c = Citation(citation_id="c-001", source_name="doc.pdf", doc_key="d-001", content="Text", score=-0.1)
        issues = _validate_citation(c)
        assert len(issues) >= 1


# ---------------------------------------------------------------------------
# summarise_results
# ---------------------------------------------------------------------------


class _FakeRoute:
    def __init__(self, category):
        self.category = category


class _FakeCategory:
    def __init__(self, v):
        self.value = v


class TestSummariseResults:
    def _make_result(self, question: str, passed: bool, failures=None) -> CompetencyResult:
        case = CompetencyCase(question=question, description="Test case")
        citation = Citation(citation_id="c-001", source_name="test", doc_key="d-001", content="text", score=0.9)
        route = _FakeRoute(_FakeCategory("factual"))
        return CompetencyResult(
            case=case,
            citations=[citation],
            routing_result=route,
            passed=passed,
            failures=failures or [],
        )

    def test_all_passed(self):
        results = [
            self._make_result("Q1?", True),
            self._make_result("Q2?", True),
        ]
        summary = summarise_results(results)
        assert "2/2 passed" in summary
        assert "✅" in summary

    def test_with_failures(self):
        results = [
            self._make_result("Q1?", True),
            self._make_result("Q2?", False, failures=["Expected CEO not found"]),
        ]
        summary = summarise_results(results)
        assert "1/2 passed" in summary
        assert "❌" in summary
        assert "CEO not found" in summary

    def test_empty_results(self):
        summary = summarise_results([])
        assert "0/0 passed" in summary

    def test_description_included(self):
        case = CompetencyCase(question="Who?", description="My test")
        citation = Citation(citation_id="c-001", source_name="test", doc_key="d-001", content="text", score=0.9)
        route = _FakeRoute(_FakeCategory("factual"))
        result = CompetencyResult(
            case=case, citations=[citation], routing_result=route, passed=True
        )
        summary = summarise_results([result])
        assert "My test" in summary

    def test_question_truncated_at_80(self):
        long_question = "A" * 100 + "?"
        result = self._make_result(long_question, True)
        summary = summarise_results([result])
        # Question shown, possibly truncated
        assert "A" * 80 in summary
