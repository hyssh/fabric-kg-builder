"""Tests for runtime/semantic_reliability.py — classifiers and retry logic."""
from __future__ import annotations

import pytest

from fabric_kg_builder.runtime.semantic_reliability import (
    FAILURE_STATUSES,
    RETRYABLE_STATUSES,
    SEMANTICALLY_SUCCESSFUL_STATUSES,
    MissingSourceOutcomeError,
    PhysicalQueryInvalidError,
    QueryClassificationError,
    QueryExecutionStatus,
    SemanticPlanInvalidError,
    SourceAuthorizationError,
    SourceConcurrencyConflictError,
    SourcePlatformError,
    classify_exception,
    classify_execution_status,
    classify_http_status,
    classify_result,
    resolve_required_source_status,
    SourceExecutionOutcome,
    SourceRequirement,
    FinalStatusResolution,
)


# ---------------------------------------------------------------------------
# classify_http_status
# ---------------------------------------------------------------------------

class TestClassifyHttpStatus:
    def test_200_returns_none(self):
        assert classify_http_status(200) is None

    def test_201_returns_none(self):
        assert classify_http_status(201) is None

    def test_400_invalid_physical_query(self):
        assert classify_http_status(400) == QueryExecutionStatus.INVALID_PHYSICAL_QUERY

    def test_401_authorization(self):
        assert classify_http_status(401) == QueryExecutionStatus.AUTHORIZATION_FAILURE

    def test_403_authorization(self):
        assert classify_http_status(403) == QueryExecutionStatus.AUTHORIZATION_FAILURE

    def test_408_timeout(self):
        assert classify_http_status(408) == QueryExecutionStatus.TIMEOUT

    def test_409_concurrency(self):
        assert classify_http_status(409) == QueryExecutionStatus.CONCURRENCY_CONFLICT

    def test_429_concurrency(self):
        assert classify_http_status(429) == QueryExecutionStatus.CONCURRENCY_CONFLICT

    def test_422_invalid_physical_query(self):
        assert classify_http_status(422) == QueryExecutionStatus.INVALID_PHYSICAL_QUERY

    def test_500_platform_failure(self):
        assert classify_http_status(500) == QueryExecutionStatus.PLATFORM_FAILURE

    def test_503_platform_failure(self):
        assert classify_http_status(503) == QueryExecutionStatus.PLATFORM_FAILURE

    def test_504_timeout(self):
        assert classify_http_status(504) == QueryExecutionStatus.TIMEOUT

    def test_unrecognized_raises(self):
        with pytest.raises(QueryClassificationError):
            classify_http_status(301)

    def test_all_2xx_return_none(self):
        for code in (200, 201, 202, 204):
            assert classify_http_status(code) is None


# ---------------------------------------------------------------------------
# classify_exception
# ---------------------------------------------------------------------------

class TestClassifyException:
    def test_semantic_plan_invalid(self):
        assert classify_exception(SemanticPlanInvalidError("bad plan")) == QueryExecutionStatus.INVALID_SEMANTIC_PLAN

    def test_physical_query_invalid(self):
        assert classify_exception(PhysicalQueryInvalidError("bad query")) == QueryExecutionStatus.INVALID_PHYSICAL_QUERY

    def test_authorization_error(self):
        assert classify_exception(SourceAuthorizationError("no access")) == QueryExecutionStatus.AUTHORIZATION_FAILURE

    def test_permission_error(self):
        assert classify_exception(PermissionError("denied")) == QueryExecutionStatus.AUTHORIZATION_FAILURE

    def test_concurrency_conflict(self):
        assert classify_exception(SourceConcurrencyConflictError("conflict")) == QueryExecutionStatus.CONCURRENCY_CONFLICT

    def test_timeout_error(self):
        assert classify_exception(TimeoutError("timed out")) == QueryExecutionStatus.TIMEOUT

    def test_platform_error(self):
        assert classify_exception(SourcePlatformError("infra failure")) == QueryExecutionStatus.PLATFORM_FAILURE

    def test_connection_error(self):
        assert classify_exception(ConnectionError("no connection")) == QueryExecutionStatus.PLATFORM_FAILURE

    def test_os_error(self):
        assert classify_exception(OSError("file not found")) == QueryExecutionStatus.PLATFORM_FAILURE

    def test_unknown_exception_is_platform_failure(self):
        class _WeirdError(Exception):
            pass
        assert classify_exception(_WeirdError("unknown")) == QueryExecutionStatus.PLATFORM_FAILURE


# ---------------------------------------------------------------------------
# classify_result
# ---------------------------------------------------------------------------

class TestClassifyResult:
    def test_success_when_rows(self):
        assert classify_result(row_count=5, optional=False) == QueryExecutionStatus.SUCCESS

    def test_no_match_when_zero_required(self):
        assert classify_result(row_count=0, optional=False) == QueryExecutionStatus.NO_MATCH

    def test_optional_absent_when_zero_optional(self):
        assert classify_result(row_count=0, optional=True) == QueryExecutionStatus.OPTIONAL_DATA_ABSENT

    def test_negative_row_count_raises(self):
        with pytest.raises(QueryClassificationError, match="negative"):
            classify_result(row_count=-1, optional=False)

    def test_execution_error_takes_precedence(self):
        result = classify_result(
            row_count=5,
            optional=False,
            execution_error=QueryExecutionStatus.TIMEOUT,
        )
        assert result == QueryExecutionStatus.TIMEOUT

    def test_non_failure_execution_error_raises(self):
        with pytest.raises(QueryClassificationError, match="failure status"):
            classify_result(
                row_count=0,
                optional=False,
                execution_error=QueryExecutionStatus.SUCCESS,
            )


# ---------------------------------------------------------------------------
# classify_execution_status
# ---------------------------------------------------------------------------

class TestClassifyExecutionStatus:
    def test_http_failure_takes_priority(self):
        result = classify_execution_status(
            http_status=401,
            row_count=10,
            optional=False,
        )
        assert result == QueryExecutionStatus.AUTHORIZATION_FAILURE

    def test_exception_takes_priority_over_rows(self):
        result = classify_execution_status(
            exception=TimeoutError("timeout"),
            row_count=5,
            optional=False,
        )
        assert result == QueryExecutionStatus.TIMEOUT

    def test_row_count_success(self):
        result = classify_execution_status(row_count=3, optional=False)
        assert result == QueryExecutionStatus.SUCCESS

    def test_row_count_no_match(self):
        result = classify_execution_status(row_count=0, optional=False)
        assert result == QueryExecutionStatus.NO_MATCH

    def test_row_count_optional_absent(self):
        result = classify_execution_status(row_count=0, optional=True)
        assert result == QueryExecutionStatus.OPTIONAL_DATA_ABSENT

    def test_http_200_with_zero_rows_is_no_match(self):
        result = classify_execution_status(
            http_status=200,
            row_count=0,
            optional=False,
        )
        assert result == QueryExecutionStatus.NO_MATCH

    def test_no_inputs_raises(self):
        with pytest.raises((QueryClassificationError, ValueError, TypeError)):
            classify_execution_status()


# ---------------------------------------------------------------------------
# FAILURE_STATUSES / RETRYABLE_STATUSES
# ---------------------------------------------------------------------------

class TestStatusSets:
    def test_failure_statuses_does_not_include_no_match(self):
        assert QueryExecutionStatus.NO_MATCH not in FAILURE_STATUSES

    def test_failure_statuses_does_not_include_success(self):
        assert QueryExecutionStatus.SUCCESS not in FAILURE_STATUSES

    def test_retryable_subset_of_failures(self):
        assert RETRYABLE_STATUSES <= FAILURE_STATUSES

    def test_success_is_semantically_successful(self):
        assert QueryExecutionStatus.SUCCESS in SEMANTICALLY_SUCCESSFUL_STATUSES

    def test_optional_absent_is_semantically_successful(self):
        assert QueryExecutionStatus.OPTIONAL_DATA_ABSENT in SEMANTICALLY_SUCCESSFUL_STATUSES

    def test_timeout_is_retryable(self):
        assert QueryExecutionStatus.TIMEOUT in RETRYABLE_STATUSES

    def test_platform_failure_is_retryable(self):
        assert QueryExecutionStatus.PLATFORM_FAILURE in RETRYABLE_STATUSES


# ---------------------------------------------------------------------------
# resolve_required_source_status
# ---------------------------------------------------------------------------

class TestResolveRequiredSourceStatus:
    def _make_outcome(self, source_id: str, status: QueryExecutionStatus) -> SourceExecutionOutcome:
        return SourceExecutionOutcome(source_id=source_id, status=status)

    def _make_requirement(self, source_id: str, required: bool = True) -> SourceRequirement:
        return SourceRequirement(
            source_id=source_id,
            requirement="required" if required else "optional",
        )

    def test_all_required_succeed(self):
        outcomes = [
            self._make_outcome("search", QueryExecutionStatus.SUCCESS),
            self._make_outcome("ontology", QueryExecutionStatus.SUCCESS),
        ]
        requirements = [
            self._make_requirement("search"),
            self._make_requirement("ontology"),
        ]
        result = resolve_required_source_status(
            outcomes=outcomes,
            requirements=requirements,
            answer_is_fact_bearing=False,
        )
        assert isinstance(result, FinalStatusResolution)
        assert result.status == QueryExecutionStatus.SUCCESS

    def test_required_source_fails_is_failure(self):
        outcomes = [
            self._make_outcome("search", QueryExecutionStatus.PLATFORM_FAILURE),
        ]
        requirements = [self._make_requirement("search", required=True)]
        result = resolve_required_source_status(
            outcomes=outcomes,
            requirements=requirements,
            answer_is_fact_bearing=False,
        )
        assert result.status in FAILURE_STATUSES

    def test_optional_source_absent_is_success(self):
        outcomes = [
            self._make_outcome("search", QueryExecutionStatus.SUCCESS),
            self._make_outcome("ontology", QueryExecutionStatus.OPTIONAL_DATA_ABSENT),
        ]
        requirements = [
            self._make_requirement("search", required=True),
            self._make_requirement("ontology", required=False),
        ]
        result = resolve_required_source_status(
            outcomes=outcomes,
            requirements=requirements,
            answer_is_fact_bearing=False,
        )
        assert result.status == QueryExecutionStatus.SUCCESS

    def test_missing_required_source_raises(self):
        outcomes = [
            self._make_outcome("search", QueryExecutionStatus.SUCCESS),
        ]
        requirements = [
            self._make_requirement("search"),
            self._make_requirement("missing-source"),
        ]
        with pytest.raises(MissingSourceOutcomeError):
            resolve_required_source_status(
                outcomes=outcomes,
                requirements=requirements,
                answer_is_fact_bearing=False,
            )

    def test_required_no_match_with_fact_bearing_is_blocked(self):
        outcomes = [
            self._make_outcome("search", QueryExecutionStatus.NO_MATCH),
        ]
        requirements = [self._make_requirement("search")]
        result = resolve_required_source_status(
            outcomes=outcomes,
            requirements=requirements,
            answer_is_fact_bearing=True,
        )
        assert result.status == QueryExecutionStatus.NO_MATCH
        assert result.blocked is True
