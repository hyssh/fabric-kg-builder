"""knowledge.competency — competency suite runner for knowledge base validation.

AGK-008: Runs a structured competency suite against a knowledge base, checking
that:

  * The expected source(s) are selected for each question (routing check).
  * Expected facts or path patterns appear in the response content (fact check).
  * Citations are well-formed and normalised (citation check).

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
    """

    case: CompetencyCase
    citations: list[Citation]
    routing_result: object  # RoutingResult
    passed: bool
    failures: list[str] = field(default_factory=list)
    lineage_error: LineageCallbackError | None = None


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

        # 3. Fact pattern check (must all be present in at least one citation)
        for pattern in case.expected_fact_patterns:
            matched = _pattern_matches_any(pattern, citations)
            if not matched:
                failures.append(
                    f"Fact pattern {pattern!r} not found in any citation content."
                )

        # 4. Source name check
        found_sources = {c.source_name for c in citations}
        for src in case.expected_source_names:
            if src not in found_sources:
                failures.append(
                    f"Expected source {src!r} not found in citations "
                    f"(found: {sorted(found_sources)!r})."
                )

        # 5. Citation path check (prefix or exact match)
        citation_ids = [c.citation_id for c in citations]
        for expected_path in case.expected_citation_paths:
            matched = any(
                cid == expected_path or cid.startswith(expected_path)
                for cid in citation_ids
            )
            if not matched:
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
        logger.info(
            "[competency] %s -- %r",
            status,
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
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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

    lines: list[str] = [
        f"## Competency suite: {passed}/{total} passed, {failed} failed",
        "",
    ]
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
        if not result.passed:
            for failure in result.failures:
                lines.append(f"   - ✗ {failure}")
        lines.append("")
    return "\n".join(lines)
