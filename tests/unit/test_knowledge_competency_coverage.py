"""Retrieval-coverage measurement for the competency suite.

Why this exists
---------------
``CompetencyResult.passed`` is a single bit.  It collapses "found 9 of the 10
things we looked for" and "found 0 of them" into the same ``False``, which
makes it impossible to say how much of what a suite looked for it actually
found — or to tell whether a change to the retrieval strategy helped.

These tests pin the *rate* alongside the bit, and pin the two properties that
make an aggregate rate trustworthy:

  * a case that asserts nothing has undefined recall, not perfect recall, so
    adding assertion-free cases cannot inflate the number;
  * coverage and the pass/fail failure list are derived from one evaluation,
    so they can never disagree about what was retrieved.

Fixtures are deliberately domain-neutral (placeholder tokens, no real corpus
vocabulary) and fully offline — the retriever is a stub, no network is used.
"""
from __future__ import annotations

from fabric_kg_builder.knowledge.competency import (
    CompetencyCase,
    CompetencySuiteRunner,
    DimensionCoverage,
    RetrievalCoverage,
    aggregate_coverage,
    summarise_results,
)
from fabric_kg_builder.knowledge.retrieve import Citation


# ---------------------------------------------------------------------------
# Offline fixtures
# ---------------------------------------------------------------------------


class _StubRetriever:
    """Returns a fixed citation list regardless of the question."""

    def __init__(self, citations: list[Citation]) -> None:
        self._citations = citations

    def retrieve(self, question: str) -> list[Citation]:  # noqa: ARG002
        return list(self._citations)


def _citation(
    citation_id: str = "c-001",
    source_name: str = "source-alpha",
    content: str = "",
) -> Citation:
    return Citation(
        citation_id=citation_id,
        source_name=source_name,
        doc_key="doc-001",
        content=content,
        score=0.9,
    )


def _run(case: CompetencyCase, citations: list[Citation]):
    runner = CompetencySuiteRunner(retriever=_StubRetriever(citations))
    return runner.run([case])[0]


# ---------------------------------------------------------------------------
# The core distinction: a rate, not a bit
# ---------------------------------------------------------------------------


class TestRecallIsARateNotABit:
    def test_partial_retrieval_reports_the_fraction_found(self):
        """Three of four expectations met is 75%, not merely 'failed'."""
        citations = [_citation(content="alpha beta gamma")]
        case = CompetencyCase(
            question="Q?",
            expected_fact_patterns=["alpha", "beta", "gamma", "delta"],
        )

        result = _run(case, citations)

        assert result.passed is False, "one expectation was unmet"
        assert result.coverage.recall == 0.75
        assert result.coverage.found_count == 3
        assert result.coverage.expected_count == 4

    def test_total_miss_and_near_miss_are_distinguishable(self):
        """The bit cannot tell these apart; the rate must."""
        near_miss = _run(
            CompetencyCase(
                question="Q?",
                expected_fact_patterns=["alpha", "beta", "gamma", "delta"],
            ),
            [_citation(content="alpha beta gamma")],
        )
        total_miss = _run(
            CompetencyCase(
                question="Q?",
                expected_fact_patterns=["alpha", "beta", "gamma", "delta"],
            ),
            [_citation(content="nothing relevant here")],
        )

        assert near_miss.passed == total_miss.passed is False
        assert near_miss.coverage.recall == 0.75
        assert total_miss.coverage.recall == 0.0

    def test_full_retrieval_is_one(self):
        result = _run(
            CompetencyCase(question="Q?", expected_fact_patterns=["alpha", "beta"]),
            [_citation(content="alpha beta")],
        )
        assert result.passed is True
        assert result.coverage.recall == 1.0

    def test_missed_expectations_are_named_with_their_dimension(self):
        result = _run(
            CompetencyCase(
                question="Q?",
                expected_fact_patterns=["alpha", "delta"],
                expected_source_names=["source-alpha", "source-omega"],
            ),
            [_citation(content="alpha")],
        )

        assert set(result.coverage.missed) == {
            "fact_patterns:delta",
            "source_names:source-omega",
        }


# ---------------------------------------------------------------------------
# Undefined, not perfect
# ---------------------------------------------------------------------------


class TestEmptyExpectationsAreUndefinedNotPerfect:
    def test_case_asserting_nothing_has_recall_none(self):
        result = _run(CompetencyCase(question="Q?"), [_citation(content="anything")])

        assert result.passed is True
        assert result.coverage.recall is None, (
            "a case that asserts nothing has undefined recall; scoring it 1.0 "
            "would let assertion-free cases inflate the suite average"
        )

    def test_dimension_with_no_expectations_has_recall_none(self):
        dim = DimensionCoverage(dimension="fact_patterns")
        assert dim.recall is None

    def test_adding_an_assertion_free_case_does_not_move_the_aggregate(self):
        """The regression this design exists to prevent."""
        measured = _run(
            CompetencyCase(
                question="Q1?", expected_fact_patterns=["alpha", "beta", "delta"]
            ),
            [_citation(content="alpha beta")],
        )
        vacuous = _run(CompetencyCase(question="Q2?"), [_citation(content="x")])

        before = aggregate_coverage([measured])
        after = aggregate_coverage([measured, vacuous])

        assert before.recall == after.recall
        assert after.expected_count == before.expected_count

    def test_aggregate_of_nothing_is_undefined(self):
        summary = aggregate_coverage([])
        assert summary.recall is None
        assert summary.pass_rate is None


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


class TestAggregateCoverage:
    def test_micro_averages_across_cases(self):
        first = _run(
            CompetencyCase(question="Q1?", expected_fact_patterns=["alpha", "beta"]),
            [_citation(content="alpha beta")],
        )
        second = _run(
            CompetencyCase(
                question="Q2?", expected_fact_patterns=["gamma", "delta"]
            ),
            [_citation(content="gamma")],
        )

        summary = aggregate_coverage([first, second])

        assert summary.found_count == 3
        assert summary.expected_count == 4
        assert summary.recall == 0.75

    def test_micro_average_is_not_the_mean_of_per_case_rates(self):
        """A case with many expectations must outweigh one with a single
        expectation; averaging per-case rates would treat them equally."""
        many = _run(
            CompetencyCase(
                question="Q1?",
                expected_fact_patterns=["a1", "a2", "a3", "a4", "a5", "a6"],
            ),
            [_citation(content="nothing")],
        )
        one = _run(
            CompetencyCase(question="Q2?", expected_fact_patterns=["beta"]),
            [_citation(content="beta")],
        )

        summary = aggregate_coverage([many, one])

        # micro-average: 1 found / 7 expected
        assert summary.recall == 1 / 7
        # the mean of per-case rates would have been (0.0 + 1.0) / 2 == 0.5
        assert summary.recall != 0.5

    def test_micro_average_is_not_the_mean_of_per_dimension_rates(self):
        """Weighting must survive aggregation *across dimensions* too.

        This is the case the per-case test above cannot see: when both groups
        land in the same dimension they are merged before any averaging, so a
        macro-average over dimensions coincidentally agrees.  Splitting them
        across dimensions with different denominators separates the two.
        """
        result = _run(
            CompetencyCase(
                question="Q?",
                # 0 of 6 in one dimension...
                expected_fact_patterns=["a1", "a2", "a3", "a4", "a5", "a6"],
                # ...and 1 of 1 in another.
                expected_source_names=["source-alpha"],
            ),
            [_citation(source_name="source-alpha", content="nothing matching")],
        )

        summary = aggregate_coverage([result])

        # micro: 1 found / 7 expected
        assert summary.recall == 1 / 7
        # macro over dimensions would be (0/6 + 1/1) / 2 == 0.5, hiding the
        # six misses behind one satisfied single-item dimension.
        assert summary.recall != 0.5

    def test_pass_rate_and_recall_are_independent_signals(self):
        """0% pass rate with high recall is a near-miss suite, and is not the
        same situation as 0% pass rate with low recall."""
        near_miss = [
            _run(
                CompetencyCase(
                    question=f"Q{i}?",
                    expected_fact_patterns=["alpha", "beta", "gamma", "delta"],
                ),
                [_citation(content="alpha beta gamma")],
            )
            for i in range(3)
        ]

        summary = aggregate_coverage(near_miss)

        assert summary.pass_rate == 0.0
        assert summary.recall == 0.75

    def test_per_dimension_breakdown_is_reported(self):
        result = _run(
            CompetencyCase(
                question="Q?",
                expected_fact_patterns=["alpha", "delta"],
                expected_source_names=["source-alpha"],
            ),
            [_citation(content="alpha")],
        )

        summary = aggregate_coverage([result])
        by_name = {d.dimension: d for d in summary.per_dimension}

        assert by_name["fact_patterns"].recall == 0.5
        assert by_name["source_names"].recall == 1.0
        assert by_name["citation_paths"].recall is None

    def test_missed_items_carry_their_question(self):
        first = _run(
            CompetencyCase(question="Q1?", expected_fact_patterns=["delta"]),
            [_citation(content="alpha")],
        )
        second = _run(
            CompetencyCase(question="Q2?", expected_fact_patterns=["omega"]),
            [_citation(content="alpha")],
        )

        summary = aggregate_coverage([first, second])

        assert ("Q1?", "fact_patterns:delta") in summary.missed
        assert ("Q2?", "fact_patterns:omega") in summary.missed


# ---------------------------------------------------------------------------
# Coverage and the verdict must agree
# ---------------------------------------------------------------------------


class TestCoverageAndFailuresCannotDisagree:
    def test_every_miss_produces_exactly_one_failure(self):
        result = _run(
            CompetencyCase(
                question="Q?",
                expected_fact_patterns=["alpha", "delta"],
                expected_source_names=["source-omega"],
                expected_citation_paths=["absent/"],
            ),
            [_citation(content="alpha")],
        )

        assert len(result.coverage.missed) == 3
        assert len(result.failures) == 3

    def test_full_coverage_means_no_expectation_failures(self):
        result = _run(
            CompetencyCase(
                question="Q?",
                expected_fact_patterns=["alpha"],
                expected_source_names=["source-alpha"],
                expected_citation_paths=["c-0"],
            ),
            [_citation(content="alpha")],
        )

        assert result.coverage.recall == 1.0
        assert result.failures == []
        assert result.passed is True

    def test_citation_path_prefix_matching_is_shared_with_the_verdict(self):
        """Coverage must use the same prefix rule the pass/fail check uses."""
        result = _run(
            CompetencyCase(question="Q?", expected_citation_paths=["c-"]),
            [_citation(citation_id="c-001", content="x")],
        )

        assert result.coverage.recall == 1.0
        assert result.passed is True


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


class TestVerdictSemanticsUnchanged:
    def test_retrieval_error_still_fails_without_crashing_coverage(self):
        class _Boom:
            def retrieve(self, question):  # noqa: ARG002
                raise RuntimeError("retrieval exploded")

        runner = CompetencySuiteRunner(retriever=_Boom())
        result = runner.run(
            [CompetencyCase(question="Q?", expected_fact_patterns=["alpha"])]
        )[0]

        assert result.passed is False
        # No citations were retrieved, so nothing could be found.
        assert result.coverage.recall == 0.0

    def test_result_can_still_be_built_without_coverage(self):
        """Existing callers constructing CompetencyResult positionally keep
        working; coverage defaults to an empty, undefined measurement."""
        from fabric_kg_builder.knowledge.competency import CompetencyResult

        result = CompetencyResult(
            case=CompetencyCase(question="Q?"),
            citations=[],
            routing_result=None,
            passed=True,
        )

        assert isinstance(result.coverage, RetrievalCoverage)
        assert result.coverage.recall is None


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


class TestSummaryReportsTheRate:
    def test_summary_includes_suite_recall(self):
        result = _run(
            CompetencyCase(
                question="Q?", expected_fact_patterns=["alpha", "beta", "delta"]
            ),
            [_citation(content="alpha beta")],
        )

        summary = summarise_results([result])

        assert "Retrieval recall" in summary
        assert "66.7%" in summary
        assert "2/3" in summary

    def test_summary_shows_n_a_when_nothing_was_asserted(self):
        result = _run(CompetencyCase(question="Q?"), [_citation(content="x")])
        summary = summarise_results([result])
        assert "n/a" in summary

    def test_empty_suite_summary_does_not_crash(self):
        summary = summarise_results([])
        assert "0/0 passed" in summary
        assert "n/a" in summary
