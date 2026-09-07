"""knowledge.competency — competency suite runner for knowledge base validation.

AGK-008: Runs a structured competency suite against a knowledge base, checking
that:

  * The expected source(s) are selected for each question (routing check).
  * Expected facts or path patterns appear in the response content (fact check).
  * Citations are well-formed and normalised (citation check).

Alongside the per-case pass/fail verdict, the suite reports **retrieval
coverage** — the fraction of everything it looked for that it actually found
(:class:`RetrievalCoverage`, :func:`aggregate_coverage`).  ``passed`` alone
cannot distinguish a suite that missed one expectation from one that missed
every expectation; both are ``False``.  Coverage keeps the numerator and the
denominator, and names the specific misses.  It is purely observational and
never changes ``passed``.

The suite is driven by :class:`CompetencyCase` dataclasses — no external YAML
or config file dependency.  A :class:`CompetencySuiteRunner` executes each
case via :class:`~fabric_kg_builder.knowledge.retrieve.KnowledgeBaseRetriever`
and collects :class:`CompetencyResult` objects.

All I/O is injected (transport + retriever factory) so the suite runs fully
offline with :class:`~fabric_kg_builder.knowledge.transport.FakeTransport`.

Usage::

    from fabric_kg_builder.knowledge.competency import (
        CompetencyCase, CompetencySuiteRunner
    )
    from fabric_kg_builder.knowledge.retrieve import KnowledgeBaseRetriever
    from fabric_kg_builder.knowledge.routing import RouteCategory
    from fabric_kg_builder.knowledge.transport import FakeTransport, HttpResponse

    transport = FakeTransport()
    transport.register("POST", "/knowledgebases/my-kb/retrieve",
        HttpResponse(200, body={"value": [{
            "id": "doc1",
            "content": "The part number is M1234567-001",
            "source": {"name": "search-src", "docId": "doc1"},
            "score": 0.95,
        }]}))

    retriever = KnowledgeBaseRetriever(
        endpoint="https://svc.search.windows.net",
        kb_name="my-kb",
        api_version="2026-04-01",
        transport=transport,
        token="fake",
    )

    cases = [
        CompetencyCase(
            question="What is the part number of the battery?",
            expected_route=RouteCategory.SEARCH,
            expected_fact_patterns=["M1234567-001"],
            expected_source_names=["search-src"],
        ),
    ]
    runner = CompetencySuiteRunner(retriever=retriever)
    results = runner.run(cases)
    assert results[0].passed
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Sequence

from .retrieve import Citation, KnowledgeBaseRetriever, LineageCallbackError
from .routing import RouteCategory, classify_question

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Case definition
# ---------------------------------------------------------------------------


@dataclass
class CompetencyCase:
    """A single competency test case.

    Attributes
    ----------
    question : str
        The question to submit to the knowledge base.
    expected_route : RouteCategory | None
        The expected routing category.  ``None`` skips the routing check.
    expected_fact_patterns : list[str]
        Regex patterns that must appear in at least one citation's content.
        All patterns must match.
    expected_source_names : list[str]
        Source names that must appear in at least one citation.  All must match.
    expected_citation_paths : list[str]
        Expected ``citation_id`` prefixes or exact values.  Each must appear in
        the citation list.
    max_docs : int
        Maximum docs to request from the retrieval endpoint (default 20).
    description : str
        Optional human-readable description of what this case validates.
    """

    question: str
    expected_route: RouteCategory | None = None
    expected_fact_patterns: list[str] = field(default_factory=list)
    expected_source_names: list[str] = field(default_factory=list)
    expected_citation_paths: list[str] = field(default_factory=list)
    max_docs: int = 20
    description: str = ""
    require_lineage_callback: bool = False


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DimensionCoverage:
    """Retrieval coverage for one expectation dimension of a single case.

    A dimension is one of ``fact_patterns``, ``source_names`` or
    ``citation_paths`` — each a set of things the caller declared the
    retrieval *should* surface.  This records which of them it actually did.

    ``recall`` is deliberately ``None`` (not ``1.0``) when nothing was
    expected.  A case that asserts nothing has an *undefined* recall, and
    scoring it as perfect would silently inflate any aggregate built from it.
    """

    dimension: str
    expected: tuple[str, ...] = ()
    found: tuple[str, ...] = ()
    missed: tuple[str, ...] = ()

    @property
    def expected_count(self) -> int:
        return len(self.expected)

    @property
    def found_count(self) -> int:
        return len(self.found)

    @property
    def recall(self) -> float | None:
        """Fraction of expected items that were retrieved, or ``None``."""
        if not self.expected:
            return None
        return len(self.found) / len(self.expected)


@dataclass(frozen=True)
class RetrievalCoverage:
    """Retrieval coverage across every expectation dimension of one case.

    This is the *rate* companion to :attr:`CompetencyResult.passed`.  ``passed``
    is a single bit: it collapses "9 of 10 expected facts were retrieved" and
    "0 of 10 were retrieved" into the same ``False``.  This type keeps the
    numerator and the denominator, and names the specific items that were
    missed, so a suite can report how much of what it looked for it actually
    found rather than only whether it found all of it.
    """

    dimensions: tuple[DimensionCoverage, ...] = ()

    @property
    def expected_count(self) -> int:
        return sum(d.expected_count for d in self.dimensions)

    @property
    def found_count(self) -> int:
        return sum(d.found_count for d in self.dimensions)

    @property
    def missed(self) -> tuple[str, ...]:
        """Every missed expectation, prefixed by its dimension."""
        return tuple(
            f"{d.dimension}:{item}" for d in self.dimensions for item in d.missed
        )

    @property
    def recall(self) -> float | None:
        """Micro-averaged recall over all dimensions, or ``None``.

        Micro-averaging (total found / total expected) rather than averaging
        the per-dimension rates keeps a dimension with one expectation from
        outweighing a dimension with fifty.
        """
        expected = self.expected_count
        if expected == 0:
            return None
        return self.found_count / expected

    def dimension(self, name: str) -> DimensionCoverage | None:
        """Return the :class:`DimensionCoverage` named *name*, if present."""
        for d in self.dimensions:
            if d.dimension == name:
                return d
        return None


@dataclass
class CompetencyResult:
    """The outcome of running a single :class:`CompetencyCase`.

    Attributes
    ----------
    case : CompetencyCase
        The case that was run.
    citations : list[Citation]
        The citations returned by the retriever.
    routing_result : RoutingResult
        The routing classification for the question.
    passed : bool
        ``True`` if all checks passed.
    failures : list[str]
        List of failure messages (empty when ``passed=True``).
    coverage : RetrievalCoverage
        How much of what the case expected was actually retrieved, as a rate
        rather than a bit.  Purely observational — it does not affect
        ``passed``.
    """

    case: CompetencyCase
    citations: list[Citation]
    routing_result: object  # RoutingResult
    passed: bool
    failures: list[str] = field(default_factory=list)
    lineage_error: LineageCallbackError | None = None
    coverage: RetrievalCoverage = field(default_factory=RetrievalCoverage)


# ---------------------------------------------------------------------------
# Suite runner
# ---------------------------------------------------------------------------


class CompetencySuiteRunner:
    """Runs a sequence of :class:`CompetencyCase` objects against a retriever.

    Parameters
    ----------
    retriever : KnowledgeBaseRetriever
        The retriever to use for all cases.
    on_result : Callable[[CompetencyResult], None] | None
        Optional callback invoked for each result as it is produced.
    """

    def __init__(
        self,
        retriever: KnowledgeBaseRetriever,
        on_result: Callable[[CompetencyResult], None] | None = None,
    ) -> None:
        self._retriever = retriever
        self._on_result = on_result

    def run(self, cases: Sequence[CompetencyCase]) -> list[CompetencyResult]:
        """Execute all *cases* and return a list of :class:`CompetencyResult`.

        Each case is run independently; a failure in one case does not stop
        subsequent cases.

        Parameters
        ----------
        cases : Sequence[CompetencyCase]
            The cases to run.

        Returns
        -------
        list[CompetencyResult]
            One result per case, in the same order.
        """
        results: list[CompetencyResult] = []
        for case in cases:
            result = self._run_case(case)
            results.append(result)
            if self._on_result:
                self._on_result(result)
        return results

    def _run_case(self, case: CompetencyCase) -> CompetencyResult:
        """Run a single case and return a :class:`CompetencyResult`."""
        failures: list[str] = []
        lineage_error: LineageCallbackError | None = None

        # 1. Routing check
        routing_result = classify_question(case.question)
        if case.expected_route is not None:
            if routing_result.category != case.expected_route:
                failures.append(
                    f"Routing: expected {case.expected_route.value!r}, "
                    f"got {routing_result.category.value!r} "
                    f"(signals: graph={routing_result.graph_signals[:3]}, "
                    f"search={routing_result.search_signals[:3]})"
                )

        # 2. Retrieve
        citations: list[Citation] = []
        try:
            citations = self._retriever.retrieve(case.question)
        except LineageCallbackError as exc:
            # Lineage callback failure is an explicit typed error
            lineage_error = exc
            if case.require_lineage_callback:
                # Hard failure -- propagate immediately
                raise
            failures.append(f"Lineage callback error: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"Retrieval error: {exc}")

        # 3-5. Expectation checks.  Coverage and failures are derived from the
        # same evaluation so the two can never disagree about what was found.
        found_sources = {c.source_name for c in citations}
        citation_ids = [c.citation_id for c in citations]

        fact_cov = _split_expectations(
            "fact_patterns",
            case.expected_fact_patterns,
            lambda pattern: _pattern_matches_any(pattern, citations),
        )
        source_cov = _split_expectations(
            "source_names",
            case.expected_source_names,
            lambda src: src in found_sources,
        )
        path_cov = _split_expectations(
            "citation_paths",
            case.expected_citation_paths,
            lambda expected_path: any(
                cid == expected_path or cid.startswith(expected_path)
                for cid in citation_ids
            ),
        )
        coverage = RetrievalCoverage(dimensions=(fact_cov, source_cov, path_cov))

        # 3. Fact pattern check (must all be present in at least one citation)
        for pattern in fact_cov.missed:
            failures.append(
                f"Fact pattern {pattern!r} not found in any citation content."
            )

        # 4. Source name check
        for src in source_cov.missed:
            failures.append(
                f"Expected source {src!r} not found in citations "
                f"(found: {sorted(found_sources)!r})."
            )

        # 5. Citation path check (prefix or exact match)
        for expected_path in path_cov.missed:
            failures.append(
                f"Expected citation path {expected_path!r} not found "
                f"(found: {citation_ids[:5]!r})."
            )

        # 6. Citation schema validation
        for citation in citations:
            schema_failures = _validate_citation(citation)
            failures.extend(schema_failures)

        passed = len(failures) == 0
        status = "PASS" if passed else f"FAIL ({len(failures)} failure(s))"
        recall = coverage.recall
        logger.info(
            "[competency] %s -- recall=%s -- %r",
            status,
            "n/a" if recall is None else f"{recall:.1%}",
            case.question[:80],
        )
        if not passed:
            for f in failures:
                logger.debug("[competency]   x %s", f)

        return CompetencyResult(
            case=case,
            citations=citations,
            routing_result=routing_result,
            passed=passed,
            failures=failures,
            lineage_error=lineage_error,
            coverage=coverage,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split_expectations(
    dimension: str,
    expected: Sequence[str],
    is_found: Callable[[str], bool],
) -> DimensionCoverage:
    """Partition *expected* into found/missed using *is_found*.

    Input order is preserved so that reported misses are stable and
    reproducible across runs.
    """
    expected_tuple = tuple(expected)
    found: list[str] = []
    missed: list[str] = []
    for item in expected_tuple:
        (found if is_found(item) else missed).append(item)
    return DimensionCoverage(
        dimension=dimension,
        expected=expected_tuple,
        found=tuple(found),
        missed=tuple(missed),
    )


def _pattern_matches_any(pattern: str, citations: list[Citation]) -> bool:
    """Return ``True`` if *pattern* (regex) matches any citation's content."""
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error:
        # Treat invalid regex as literal substring search
        return any(pattern.lower() in c.content.lower() for c in citations)
    return any(compiled.search(c.content) for c in citations)


def _validate_citation(citation: Citation) -> list[str]:
    """Return a list of schema-violation messages for *citation*, or empty list."""
    issues: list[str] = []
    if not citation.citation_id:
        issues.append(f"Citation has empty citation_id (source={citation.source_name!r})")
    if citation.score is not None and not (0.0 <= citation.score <= 1.0):
        issues.append(
            f"Citation {citation.citation_id!r} has out-of-range score {citation.score}"
        )
    return issues


# ---------------------------------------------------------------------------
# Summary helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoverageSummary:
    """Suite-level retrieval coverage, micro-averaged across cases.

    ``pass_rate`` answers "how many questions were fully answered".
    ``recall`` answers the different and, for retrieval, more useful question:
    "of everything the suite looked for, what fraction did it find".  A suite
    can have a 0% pass rate and 95% recall — that is a near-miss suite, and it
    is not the same situation as 0% pass rate with 5% recall.
    """

    case_count: int = 0
    passed_count: int = 0
    expected_count: int = 0
    found_count: int = 0
    per_dimension: tuple[DimensionCoverage, ...] = ()
    missed: tuple[tuple[str, str], ...] = ()
    """``(question, "dimension:item")`` for every unmet expectation."""

    @property
    def pass_rate(self) -> float | None:
        if self.case_count == 0:
            return None
        return self.passed_count / self.case_count

    @property
    def recall(self) -> float | None:
        if self.expected_count == 0:
            return None
        return self.found_count / self.expected_count


def aggregate_coverage(results: Sequence[CompetencyResult]) -> CoverageSummary:
    """Micro-average retrieval coverage over *results*.

    Cases that assert nothing contribute nothing to the denominator rather
    than counting as perfect recall, so adding an assertion-free case cannot
    move the number.
    """
    dimension_names: list[str] = []
    by_dimension: dict[str, tuple[list[str], list[str], list[str]]] = {}
    missed: list[tuple[str, str]] = []

    for result in results:
        for dim in result.coverage.dimensions:
            if dim.dimension not in by_dimension:
                by_dimension[dim.dimension] = ([], [], [])
                dimension_names.append(dim.dimension)
            expected, found, dim_missed = by_dimension[dim.dimension]
            expected.extend(dim.expected)
            found.extend(dim.found)
            dim_missed.extend(dim.missed)
        for item in result.coverage.missed:
            missed.append((result.case.question, item))

    per_dimension = tuple(
        DimensionCoverage(
            dimension=name,
            expected=tuple(by_dimension[name][0]),
            found=tuple(by_dimension[name][1]),
            missed=tuple(by_dimension[name][2]),
        )
        for name in dimension_names
    )

    return CoverageSummary(
        case_count=len(results),
        passed_count=sum(1 for r in results if r.passed),
        expected_count=sum(d.expected_count for d in per_dimension),
        found_count=sum(d.found_count for d in per_dimension),
        per_dimension=per_dimension,
        missed=tuple(missed),
    )


def _format_rate(value: float | None) -> str:
    """Render a rate as a percentage, or ``n/a`` when undefined."""
    return "n/a" if value is None else f"{value:.1%}"


def summarise_results(results: list[CompetencyResult]) -> str:
    """Return a compact Markdown summary of suite results.

    Parameters
    ----------
    results : list[CompetencyResult]
        Results from :meth:`CompetencySuiteRunner.run`.

    Returns
    -------
    str
        Multi-line Markdown string.
    """
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = total - passed
    summary = aggregate_coverage(results)

    lines: list[str] = [
        f"## Competency suite: {passed}/{total} passed, {failed} failed",
        "",
        f"Retrieval recall: **{_format_rate(summary.recall)}** "
        f"({summary.found_count}/{summary.expected_count} expectations met)",
        "",
    ]
    if summary.expected_count:
        for dim in summary.per_dimension:
            if not dim.expected_count:
                continue
            lines.append(
                f"- `{dim.dimension}`: {_format_rate(dim.recall)} "
                f"({dim.found_count}/{dim.expected_count})"
            )
        lines.append("")
    for i, result in enumerate(results, 1):
        icon = "✅" if result.passed else "❌"
        lines.append(f"{icon} **{i}. {result.case.question[:80]}**")
        if result.case.description:
            lines.append(f"   *{result.case.description}*")
        rr = result.routing_result
        lines.append(
            f"   Route: **{rr.category.value.upper()}**"  # type: ignore[attr-defined]
        )
        lines.append(f"   Citations: {len(result.citations)}")
        if result.coverage.expected_count:
            lines.append(
                f"   Recall: {_format_rate(result.coverage.recall)} "
                f"({result.coverage.found_count}/"
                f"{result.coverage.expected_count})"
            )
        if not result.passed:
            for failure in result.failures:
                lines.append(f"   - ✗ {failure}")
        lines.append("")
    return "\n".join(lines)
