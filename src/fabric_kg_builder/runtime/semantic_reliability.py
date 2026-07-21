"""Standalone SPEC-008A H5 runtime reliability core.

Covers S8A-QRY-004 through S8A-QRY-008: a precise no-data/execution-failure
taxonomy, a required-source final-status resolver, a per-turn idempotency and
bounded-retry coordinator, a grounded-evidence-trace validator, a semantic
determinism signature/comparator, and a controlled benchmark evaluator.

This module is intentionally standalone. It does not import from
``runtime.executors``, ``runtime.collector``, or ``runtime.contract`` so it
can be wired into those modules independently. All classification and
resolution logic here is deterministic: identical inputs always produce
identical outputs, and no function silently swallows an exception.

Stable names preferred for parent integration call sites:

- :func:`classify_execution_status` — unified HTTP/exception/result
  classifier (delegates to :func:`classify_http_status`,
  :func:`classify_exception`, :func:`classify_result`).
- :func:`resolve_required_source_status` — required-source final-status
  resolver.
- :class:`TurnRetryCoordinator` / :func:`execute_with_retry` — per-turn
  idempotency and bounded-retry coordination.
- :func:`validate_grounded_answer` / :func:`resolve_evidence_trace` —
  grounded-evidence-trace validator (identical behavior, two names).
- :func:`semantic_determinism_signature` — semantic determinism signature
  factory (builds a :class:`SemanticDeterminismSignature`).
- :func:`evaluate_runtime_benchmark` — controlled benchmark evaluator.
"""

from __future__ import annotations

import hashlib
import math
import threading
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Mapping, Sequence

from pydantic import ConfigDict, Field, model_validator
from pydantic import BaseModel
from urllib.parse import unquote, urlparse


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# S8A-QRY-004 — No-data and execution-failure taxonomy
# ---------------------------------------------------------------------------


class QueryExecutionStatus(str, Enum):
    """The SPEC-008A §9.5 no-data and execution-failure taxonomy."""

    NO_MATCH = "no_match"
    OPTIONAL_DATA_ABSENT = "optional_data_absent"
    INVALID_SEMANTIC_PLAN = "invalid_semantic_plan"
    INVALID_PHYSICAL_QUERY = "invalid_physical_query"
    AUTHORIZATION_FAILURE = "authorization_failure"
    PLATFORM_FAILURE = "platform_failure"
    TIMEOUT = "timeout"
    CONCURRENCY_CONFLICT = "concurrency_conflict"
    PARTIAL_RESULT = "partial_result"
    SUCCESS = "success"


#: Source-execution-failure categories. A source execution failure MUST NOT
#: be represented as ``no_match`` (SPEC-008A §9.5).
FAILURE_STATUSES: frozenset[QueryExecutionStatus] = frozenset(
    {
        QueryExecutionStatus.INVALID_SEMANTIC_PLAN,
        QueryExecutionStatus.INVALID_PHYSICAL_QUERY,
        QueryExecutionStatus.AUTHORIZATION_FAILURE,
        QueryExecutionStatus.PLATFORM_FAILURE,
        QueryExecutionStatus.TIMEOUT,
        QueryExecutionStatus.CONCURRENCY_CONFLICT,
    }
)

#: Statuses that represent a semantically successful source execution, i.e.
#: the source ran to completion and any absence of data was itself expected.
SEMANTICALLY_SUCCESSFUL_STATUSES: frozenset[QueryExecutionStatus] = frozenset(
    {
        QueryExecutionStatus.SUCCESS,
        QueryExecutionStatus.OPTIONAL_DATA_ABSENT,
    }
)

#: Statuses that may be retried under a bounded, jittered retry policy.
RETRYABLE_STATUSES: frozenset[QueryExecutionStatus] = frozenset(
    {
        QueryExecutionStatus.PLATFORM_FAILURE,
        QueryExecutionStatus.TIMEOUT,
        QueryExecutionStatus.CONCURRENCY_CONFLICT,
    }
)


class QueryClassificationError(ValueError):
    """Raised when a classifier receives an input it cannot classify safely."""


_HTTP_EXACT_STATUS_MAP: dict[int, QueryExecutionStatus] = {
    400: QueryExecutionStatus.INVALID_PHYSICAL_QUERY,
    401: QueryExecutionStatus.AUTHORIZATION_FAILURE,
    403: QueryExecutionStatus.AUTHORIZATION_FAILURE,
    408: QueryExecutionStatus.TIMEOUT,
    409: QueryExecutionStatus.CONCURRENCY_CONFLICT,
    422: QueryExecutionStatus.INVALID_PHYSICAL_QUERY,
    423: QueryExecutionStatus.CONCURRENCY_CONFLICT,
    429: QueryExecutionStatus.CONCURRENCY_CONFLICT,
    504: QueryExecutionStatus.TIMEOUT,
}


def classify_http_status(status_code: int) -> QueryExecutionStatus | None:
    """Classify a transport HTTP status code deterministically.

    Returns ``None`` for a 2xx status, meaning the transport completed and
    the caller MUST continue to :func:`classify_result` to determine the
    semantic outcome. Transport completion alone (HTTP 200) never implies
    ``success`` (SPEC-008A §10.1).
    """
    if status_code in _HTTP_EXACT_STATUS_MAP:
        return _HTTP_EXACT_STATUS_MAP[status_code]
    if 200 <= status_code < 300:
        return None
    if 500 <= status_code < 600:
        return QueryExecutionStatus.PLATFORM_FAILURE
    if 400 <= status_code < 500:
        return QueryExecutionStatus.INVALID_PHYSICAL_QUERY
    raise QueryClassificationError(
        f"Unrecognized HTTP status code: {status_code}."
    )


class SemanticPlanInvalidError(RuntimeError):
    """Raised when a semantic plan itself is invalid before physical execution."""


class PhysicalQueryInvalidError(RuntimeError):
    """Raised when static physical-query validation fails."""


class SourceAuthorizationError(RuntimeError):
    """Raised when a source rejects the caller's authorization."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class SourcePlatformError(RuntimeError):
    """Raised for infrastructure/platform-level source execution failures."""


class SourceConcurrencyConflictError(RuntimeError):
    """Raised when a source reports a concurrency or version conflict."""


#: Ordered exception-to-status map. The first matching type wins, so more
#: specific exception types MUST be listed before their broader ancestors.
_EXCEPTION_STATUS_MAP: tuple[tuple[type[BaseException], QueryExecutionStatus], ...] = (
    (SemanticPlanInvalidError, QueryExecutionStatus.INVALID_SEMANTIC_PLAN),
    (PhysicalQueryInvalidError, QueryExecutionStatus.INVALID_PHYSICAL_QUERY),
    (SourceAuthorizationError, QueryExecutionStatus.AUTHORIZATION_FAILURE),
    (PermissionError, QueryExecutionStatus.AUTHORIZATION_FAILURE),
    (SourceConcurrencyConflictError, QueryExecutionStatus.CONCURRENCY_CONFLICT),
    (TimeoutError, QueryExecutionStatus.TIMEOUT),
    (SourcePlatformError, QueryExecutionStatus.PLATFORM_FAILURE),
    (ConnectionError, QueryExecutionStatus.PLATFORM_FAILURE),
    (OSError, QueryExecutionStatus.PLATFORM_FAILURE),
)


def classify_exception(exc: BaseException) -> QueryExecutionStatus:
    """Classify a raised exception into a taxonomy status, deterministically.

    Unrecognized exception types classify conservatively as
    ``platform_failure``: an execution failure MUST NEVER be represented as
    ``no_match`` or ``success`` (SPEC-008A §9.5, §10.1).
    """
    for exception_type, status in _EXCEPTION_STATUS_MAP:
        if isinstance(exc, exception_type):
            return status
    return QueryExecutionStatus.PLATFORM_FAILURE


def classify_result(
    *,
    row_count: int,
    optional: bool,
    execution_error: QueryExecutionStatus | None = None,
) -> QueryExecutionStatus:
    """Classify a completed source execution's result deterministically.

    ``execution_error`` (when provided) MUST already be one of
    :data:`FAILURE_STATUSES`; it takes precedence over row-count inspection.
    """
    if execution_error is not None:
        if execution_error not in FAILURE_STATUSES:
            raise QueryClassificationError(
                f"execution_error must be a failure status, got {execution_error!r}."
            )
        return execution_error
    if row_count < 0:
        raise QueryClassificationError("row_count cannot be negative.")
    if row_count == 0:
        return (
            QueryExecutionStatus.OPTIONAL_DATA_ABSENT
            if optional
            else QueryExecutionStatus.NO_MATCH
        )
    return QueryExecutionStatus.SUCCESS


def classify_execution_status(
    *,
    http_status: int | None = None,
    exception: BaseException | None = None,
    row_count: int | None = None,
    optional: bool = False,
) -> QueryExecutionStatus:
    """Unified, stable entry point combining the three signal classifiers.

    Precedence (all deterministic, none silently swallow ambiguity):

    1. ``exception`` — a raised execution failure always wins; classified
       via :func:`classify_exception`.
    2. ``http_status`` — a failing transport status (per
       :func:`classify_http_status`) wins over row-count inspection; a 2xx
       status defers to ``row_count``.
    3. ``row_count``/``optional`` — classified via :func:`classify_result`
       when neither of the above resolves the status.

    Raises :class:`QueryClassificationError` when no signal is sufficient to
    resolve a status (e.g. a 2xx/absent http_status with no ``row_count``).
    """
    if exception is not None:
        return classify_exception(exception)
    if http_status is not None:
        transport_status = classify_http_status(http_status)
        if transport_status is not None:
            return transport_status
    if row_count is None:
        raise QueryClassificationError(
            "classify_execution_status requires row_count when no exception "
            "and no failing http_status are provided."
        )
    return classify_result(row_count=row_count, optional=optional)


# ---------------------------------------------------------------------------
# S8A-QRY-005 — Required-source final-status resolver
# ---------------------------------------------------------------------------


class SourceRequirement(_StrictModel):
    """One route's requirement level for a source, per competency case."""

    source_id: str = Field(min_length=1)
    requirement: str = Field(pattern="^(required|optional)$")


class SourceExecutionOutcome(_StrictModel):
    """The classified execution outcome for one source in one turn.

    ``request_id``/``correlation_id`` are the *actual* service-issued
    identifiers observed for this attempt (e.g. from a response header or
    payload). When absent, the coordinator falls back to its own generated
    identifiers so a receipt is never left without an ID.
    """

    source_id: str = Field(min_length=1)
    status: QueryExecutionStatus
    unsupported_portion: str | None = None
    request_id: str | None = None
    correlation_id: str | None = None


class UnsupportedPortion(_StrictModel):
    """One explicit unsupported-portion record for a partial-result answer."""

    source_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class FinalStatusResolution(_StrictModel):
    """The resolved final semantic status for one turn's composed answer."""

    status: QueryExecutionStatus
    unsupported_portions: list[UnsupportedPortion] = Field(default_factory=list)
    blocked: bool = False


class MissingSourceOutcomeError(ValueError):
    """Raised when a declared required source has no matching execution outcome."""


#: Required-source outcome statuses that block an unqualified ``success``
#: final status: outright execution failures, and a source's own
#: ``partial_result`` (the source itself only partially answered).
_BLOCKING_REQUIRED_STATUSES: frozenset[QueryExecutionStatus] = FAILURE_STATUSES | {
    QueryExecutionStatus.PARTIAL_RESULT
}


def resolve_required_source_status(
    requirements: Sequence[SourceRequirement],
    outcomes: Sequence[SourceExecutionOutcome],
    *,
    answer_is_fact_bearing: bool,
) -> FinalStatusResolution:
    """Resolve the final semantic status from required-source outcomes.

    Invariants (SPEC-008A §9.5, §10.1, VAL-085):

    - ``success`` is possible only when every required source is
      semantically successful (:data:`SEMANTICALLY_SUCCESSFUL_STATUSES`).
    - ``no_match`` and ``optional_data_absent`` are never coalesced into a
      failure or success category.
    - A required source reporting its own ``partial_result`` never resolves
      to overall ``success``; the final status stays ``partial_result``.
    - A fact-bearing answer following a required-source failure or
      required-source ``partial_result`` is either ``blocked`` (the caller
      MUST NOT deliver it) or resolved as ``partial_result`` with an
      explicit unsupported-portion record for every such required source.
    """
    outcomes_by_source = {outcome.source_id: outcome for outcome in outcomes}
    required = [r for r in requirements if r.requirement == "required"]
    missing = sorted(
        r.source_id for r in required if r.source_id not in outcomes_by_source
    )
    if missing:
        raise MissingSourceOutcomeError(
            f"Missing execution outcome for required source(s): {missing}"
        )
    required_outcomes = [outcomes_by_source[r.source_id] for r in required]

    blocking = [
        o for o in required_outcomes if o.status in _BLOCKING_REQUIRED_STATUSES
    ]
    if blocking:
        first_blocking = blocking[0]
        unsupported = [
            UnsupportedPortion(source_id=o.source_id, reason=o.unsupported_portion)
            for o in blocking
            if o.unsupported_portion
        ]
        if answer_is_fact_bearing:
            if len(unsupported) == len(blocking):
                return FinalStatusResolution(
                    status=QueryExecutionStatus.PARTIAL_RESULT,
                    unsupported_portions=unsupported,
                    blocked=False,
                )
            # A fact-bearing answer cannot be delivered without an explicit
            # unsupported-portion record for every blocking required source.
            return FinalStatusResolution(
                status=first_blocking.status,
                unsupported_portions=unsupported,
                blocked=True,
            )
        # No fact-bearing answer is being delivered: report the blocking
        # status truthfully. A required source's own partial_result NEVER
        # resolves to success, even when no unsupported-portion record was
        # supplied.
        return FinalStatusResolution(
            status=first_blocking.status,
            unsupported_portions=unsupported,
            blocked=False,
        )

    no_match_outcomes = [
        o for o in required_outcomes if o.status == QueryExecutionStatus.NO_MATCH
    ]
    if no_match_outcomes:
        # A fact-bearing answer is invalid when a required source found no
        # matching data: unsupported factual answers SHALL equal zero.
        return FinalStatusResolution(
            status=QueryExecutionStatus.NO_MATCH,
            blocked=answer_is_fact_bearing,
        )

    return FinalStatusResolution(status=QueryExecutionStatus.SUCCESS)


# ---------------------------------------------------------------------------
# S8A-QRY-006 — Turn-level idempotency and bounded retry coordinator
# ---------------------------------------------------------------------------


class RetryPolicy(_StrictModel):
    """Bounded retry configuration for one user turn."""

    max_attempts: int = Field(default=3, ge=1, le=10)
    base_delay_seconds: float = Field(default=0.0, ge=0.0)
    retryable_statuses: frozenset[QueryExecutionStatus] = Field(
        default_factory=lambda: frozenset(RETRYABLE_STATUSES)
    )
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)


class RetryAttemptRecord(_StrictModel):
    """One recorded attempt within a turn's retry sequence."""

    attempt: int = Field(ge=1)
    request_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    status: QueryExecutionStatus
    started_at: datetime
    completed_at: datetime


class TurnReceipt(_StrictModel):
    """The retained receipt for one user turn's idempotent retry sequence."""

    idempotency_key: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    attempts: list[RetryAttemptRecord] = Field(min_length=1)
    first_failure: RetryAttemptRecord | None
    final_status: QueryExecutionStatus
    final_request_id: str = Field(min_length=1)


class ConcurrentTurnRetryError(RuntimeError):
    """Raised when a turn already has an in-flight retry sequence.

    This enforces "avoid overlapping retries for the same turn"
    (SPEC-008A §10.2) — a second concurrent attempt for the same turn ID is
    rejected rather than silently interleaved.
    """


def turn_idempotency_key(turn_id: str) -> str:
    """Compute one stable idempotency key for a user turn.

    The same ``turn_id`` always yields the same key, including across
    retries, so downstream sources can deduplicate repeated attempts.
    """
    digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
    return f"turn:{digest[:32]}"


class TurnRetryCoordinator:
    """Serializes bounded, jittered retry for one user turn at a time.

    ``operation`` is a caller-supplied callable that performs one attempt and
    returns a classified :class:`SourceExecutionOutcome` — it MUST NOT raise
    for expected failure categories (those must already be classified via
    :func:`classify_exception` / :func:`classify_result` by the caller).
    Unexpected exceptions from ``operation`` are allowed to propagate; this
    coordinator does not swallow them.
    """

    def __init__(
        self,
        *,
        retry_policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] | None = None,
        jitter: Callable[[], float] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._retry_policy = retry_policy or RetryPolicy()
        self._sleep: Callable[[float], None] = sleep or (lambda _seconds: None)
        self._jitter: Callable[[], float] = jitter or (lambda: 0.0)
        self._id_factory: Callable[[], str] = id_factory or (lambda: uuid.uuid4().hex)
        self._turn_locks: dict[str, threading.Lock] = {}
        self._turn_locks_guard = threading.Lock()

    def _lock_for_turn(self, turn_id: str) -> threading.Lock:
        with self._turn_locks_guard:
            lock = self._turn_locks.get(turn_id)
            if lock is None:
                lock = threading.Lock()
                self._turn_locks[turn_id] = lock
            return lock

    def execute_turn(
        self,
        turn_id: str,
        operation: Callable[[int, str, str], SourceExecutionOutcome],
    ) -> TurnReceipt:
        """Run ``operation`` with bounded, serialized, jittered retry.

        ``operation(attempt, idempotency_key, request_id)`` performs one
        attempt and returns its classified outcome. Retries stop as soon as
        the outcome is ``success`` or is not in
        :data:`RETRYABLE_STATUSES`, or the attempt budget is exhausted.

        The ``request_id`` passed into ``operation`` is a generated
        candidate the caller MAY send with its outbound request. When the
        returned :class:`SourceExecutionOutcome` carries its own
        ``request_id``/``correlation_id`` (the actual service/client IDs
        observed for that attempt), the receipt retains those instead of
        the unsent generated ones; otherwise it falls back to the generated
        IDs so a receipt is never left without one.
        """
        idempotency_key = turn_idempotency_key(turn_id)
        lock = self._lock_for_turn(turn_id)
        if not lock.acquire(blocking=False):
            raise ConcurrentTurnRetryError(
                f"Turn {turn_id!r} already has an in-flight retry sequence."
            )
        try:
            attempts: list[RetryAttemptRecord] = []
            first_failure: RetryAttemptRecord | None = None
            for attempt in range(1, self._retry_policy.max_attempts + 1):
                generated_request_id = self._id_factory()
                generated_correlation_id = self._id_factory()
                started_at = _utc_now()
                outcome = operation(attempt, idempotency_key, generated_request_id)
                completed_at = _utc_now()
                actual_request_id = outcome.request_id or generated_request_id
                actual_correlation_id = (
                    outcome.correlation_id or generated_correlation_id
                )
                record = RetryAttemptRecord(
                    attempt=attempt,
                    request_id=actual_request_id,
                    correlation_id=actual_correlation_id,
                    status=outcome.status,
                    started_at=started_at,
                    completed_at=completed_at,
                )
                attempts.append(record)
                if (
                    outcome.status in FAILURE_STATUSES
                    or outcome.status == QueryExecutionStatus.PARTIAL_RESULT
                ) and first_failure is None:
                    first_failure = record
                is_last_attempt = attempt == self._retry_policy.max_attempts
                should_retry = (
                    outcome.status in self._retry_policy.retryable_statuses
                    and not is_last_attempt
                )
                if outcome.status == QueryExecutionStatus.SUCCESS or not should_retry:
                    return TurnReceipt(
                        idempotency_key=idempotency_key,
                        turn_id=turn_id,
                        attempts=attempts,
                        first_failure=first_failure,
                        final_status=outcome.status,
                        final_request_id=actual_request_id,
                    )
                delay = self._retry_policy.base_delay_seconds + self._jitter()
                self._sleep(max(delay, 0.0))
            # Unreachable: the loop above always returns on its last attempt.
            raise AssertionError("Retry loop exited without a terminal attempt.")
        finally:
            lock.release()
            with self._turn_locks_guard:
                if self._turn_locks.get(turn_id) is lock and not lock.locked():
                    self._turn_locks.pop(turn_id, None)


def execute_with_retry(
    turn_id: str,
    operation: Callable[[int, str, str], SourceExecutionOutcome],
    *,
    coordinator: TurnRetryCoordinator | None = None,
    retry_policy: RetryPolicy | None = None,
    sleep: Callable[[float], None] | None = None,
    jitter: Callable[[], float] | None = None,
    id_factory: Callable[[], str] | None = None,
) -> TurnReceipt:
    """Functional convenience wrapper around :class:`TurnRetryCoordinator`.

    Pass a shared ``coordinator`` across calls in the same process so the
    per-turn keyed lock correctly serializes repeated calls for the same
    ``turn_id``; when omitted, a private single-use coordinator is created
    (only safe for one-off, non-overlapping turns).
    """
    active_coordinator = coordinator or TurnRetryCoordinator(
        retry_policy=retry_policy,
        sleep=sleep,
        jitter=jitter,
        id_factory=id_factory,
    )
    return active_coordinator.execute_turn(turn_id, operation)


# ---------------------------------------------------------------------------
# S8A-QRY-007 — Grounded evidence trace model and validator
# ---------------------------------------------------------------------------


class EvidenceLocator(_StrictModel):
    """One immutable source locator for a piece of grounding evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str = Field(min_length=1)
    asset_version_id: str = Field(min_length=1)
    blob_url: str = Field(min_length=1)

    @model_validator(mode="after")
    def _require_immutable_locator(self) -> "EvidenceLocator":
        parsed = urlparse(self.blob_url)
        if parsed.scheme.casefold() != "https" or not parsed.hostname:
            raise ValueError(
                "Evidence blob_url must be an absolute https:// URL."
            )
        normalized_path = unquote(parsed.path).replace("\\", "/").casefold()
        version_segment = f"/versions/{self.asset_version_id}/".casefold()
        if version_segment not in normalized_path:
            raise ValueError(
                "Evidence blob_url must contain an immutable "
                f"'/versions/{{asset_version_id}}/' segment; got path "
                f"{parsed.path!r}."
            )
        return self


class EvidenceTrace(_StrictModel):
    """A grounded evidence trace for one fact-bearing answer.

    ``reused`` distinguishes a trace produced by the current turn's
    execution from one explicitly reused from a prior, compatible turn so
    reused context is never indistinguishable from a fresh query
    (SPEC-008A §10.3).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    trace_id: str = Field(min_length=1)
    origin_status: QueryExecutionStatus
    source_ids: list[str] = Field(min_length=1)
    model_hash: str = Field(min_length=1)
    data_hash: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    locators: list[EvidenceLocator] = Field(min_length=1)
    reused: bool = False
    reused_from_trace_id: str | None = None

    @model_validator(mode="after")
    def _validate_trace_invariants(self) -> "EvidenceTrace":
        if any(not source_id for source_id in self.source_ids):
            raise ValueError("Evidence trace source IDs must be non-empty.")
        if any(not evidence_id for evidence_id in self.evidence_ids):
            raise ValueError("Evidence trace IDs must be non-empty.")
        if self.reused and not self.reused_from_trace_id:
            raise ValueError(
                "A reused trace must explicitly identify its source trace ID."
            )
        if not self.reused and self.reused_from_trace_id:
            raise ValueError(
                "reused_from_trace_id is only valid when reused is True."
            )
        if self.origin_status != QueryExecutionStatus.SUCCESS:
            raise ValueError(
                "An evidence trace must originate from a successful source "
                "execution."
            )
        return self


class GroundedAnswerViolationError(ValueError):
    """Raised when a fact-bearing answer lacks a compatible evidence trace.

    This is the fail-closed gate for VAL-086: every fact-bearing answer MUST
    resolve to a current successful trace, or an explicitly reused prior
    trace whose model and data hashes still match.
    """


def validate_grounded_answer(
    *,
    fact_bearing: bool,
    current_model_hash: str,
    current_data_hash: str,
    trace: EvidenceTrace | None,
) -> EvidenceTrace | None:
    """Fail closed unless a fact-bearing answer has a compatible trace."""
    if not fact_bearing:
        return None
    if trace is None:
        raise GroundedAnswerViolationError(
            "Fact-bearing answer has no evidence trace; failing closed."
        )
    if trace.model_hash != current_model_hash or trace.data_hash != current_data_hash:
        state = "reused" if trace.reused else "current-turn"
        raise GroundedAnswerViolationError(
            f"Evidence trace is {state} but its model/data hashes do not "
            "match the executing turn; failing closed."
        )
    return trace


def resolve_evidence_trace(
    *,
    fact_bearing: bool,
    current_model_hash: str,
    current_data_hash: str,
    trace: EvidenceTrace | None,
) -> EvidenceTrace | None:
    """Stable-name alias of :func:`validate_grounded_answer`.

    Provided for parent integration call sites that prefer a "resolve"
    verb; the behavior and fail-closed semantics are identical.
    """
    return validate_grounded_answer(
        fact_bearing=fact_bearing,
        current_model_hash=current_model_hash,
        current_data_hash=current_data_hash,
        trace=trace,
    )


# ---------------------------------------------------------------------------
# S8A-QRY-008 — Semantic determinism signature and comparator
# ---------------------------------------------------------------------------


class SemanticDeterminismSignature(_StrictModel):
    """A canonical semantic signature for one resolved competency answer.

    Two equivalent business questions MUST produce equal signatures even if
    their physical query syntax differs (SPEC-008A §10.5): source selection,
    intent, required/optional relationship sets, requested properties, the
    complexity budget, evidence policy, and result semantics are compared —
    physical query text is intentionally excluded.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_selection: tuple[str, ...]
    intent: str = Field(min_length=1)
    required_relationships: tuple[str, ...]
    optional_relationships: tuple[str, ...]
    requested_properties: tuple[str, ...]
    complexity_budget: tuple[tuple[str, int], ...]
    evidence_policy: str = Field(min_length=1)
    result_semantics: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _canonicalize(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        canonical = dict(data)
        for key in (
            "source_selection",
            "required_relationships",
            "optional_relationships",
            "requested_properties",
        ):
            if key in canonical and canonical[key] is not None:
                canonical[key] = tuple(sorted(set(canonical[key])))
        budget = canonical.get("complexity_budget")
        if budget is not None:
            if isinstance(budget, dict):
                canonical["complexity_budget"] = tuple(
                    sorted((str(k), int(v)) for k, v in budget.items())
                )
            else:
                canonical["complexity_budget"] = tuple(
                    sorted((str(k), int(v)) for k, v in budget)
                )
        return canonical


def semantic_signatures_equivalent(
    left: SemanticDeterminismSignature,
    right: SemanticDeterminismSignature,
) -> bool:
    """Compare two semantic signatures, ignoring physical query syntax."""
    return left == right


def semantic_determinism_signature(
    *,
    source_selection: Sequence[str],
    intent: str,
    required_relationships: Sequence[str],
    optional_relationships: Sequence[str],
    requested_properties: Sequence[str],
    complexity_budget: Mapping[str, int] | Sequence[tuple[str, int]],
    evidence_policy: str,
    result_semantics: str,
) -> SemanticDeterminismSignature:
    """Stable factory for :class:`SemanticDeterminismSignature`.

    Preferred parent-integration entry point over constructing the pydantic
    model directly; accepts plain sequences/mappings and canonicalizes them
    (order- and duplicate-independent) via the model's own validation.
    """
    return SemanticDeterminismSignature(
        source_selection=tuple(source_selection),
        intent=intent,
        required_relationships=tuple(required_relationships),
        optional_relationships=tuple(optional_relationships),
        requested_properties=tuple(requested_properties),
        complexity_budget=complexity_budget,
        evidence_policy=evidence_policy,
        result_semantics=result_semantics,
    )


def score_semantic_determinism(
    signatures: Sequence[SemanticDeterminismSignature],
) -> float:
    """Score the fraction of signatures equivalent to the first signature."""
    if not signatures:
        raise QueryClassificationError(
            "At least one semantic signature is required to score determinism."
        )
    baseline = signatures[0]
    matches = sum(1 for signature in signatures if signature == baseline)
    return matches / len(signatures)


# ---------------------------------------------------------------------------
# S8A-QRY-008 — Controlled benchmark evaluator
# ---------------------------------------------------------------------------


#: Release thresholds from SPEC-008A §13.
SEMANTIC_SUCCESS_MIN: float = 0.95
PRE_SOURCE_FAILURE_MAX: float = 0.01
CONCURRENCY_CONFLICT_MAX: int = 0
P95_LATENCY_MAX_SECONDS: float = 60.0
MAX_LATENCY_MAX_SECONDS: float = 120.0
DETERMINISM_MIN: float = 1.0

#: SPEC-008A §13 requires scoring "30 controlled competency runs"; fewer
#: outcomes cannot support that threshold and MUST fail closed rather than
#: silently score an under-powered run set.
MIN_CONTROLLED_RUN_COUNT: int = 30


class BenchmarkRunOutcome(_StrictModel):
    """One controlled benchmark run's recorded outcome.

    ``latency_seconds`` and all other fields are supplied by the caller from
    injected synthetic timings; this evaluator never sleeps.
    """

    run_id: str = Field(min_length=1)
    status: QueryExecutionStatus
    latency_seconds: float = Field(ge=0.0)
    pre_source_failure: bool = False
    concurrency_conflict: bool = False
    determinism_signature: SemanticDeterminismSignature | None = None


class SemanticReliabilityBenchmarkReport(_StrictModel):
    """The scored result of one controlled benchmark run set."""

    run_count: int = Field(ge=1)
    semantic_success_rate: float
    pre_source_failure_rate: float
    concurrency_conflict_count: int
    p95_latency_seconds: float
    max_latency_seconds: float
    determinism_score: float
    passed: bool
    failures: list[str] = Field(default_factory=list)


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    if not sorted_values:
        raise QueryClassificationError(
            "Cannot compute a percentile over an empty sequence."
        )
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = math.ceil(fraction * len(sorted_values)) - 1
    rank = min(max(rank, 0), len(sorted_values) - 1)
    return sorted_values[rank]


def evaluate_runtime_benchmark(
    outcomes: Sequence[BenchmarkRunOutcome],
) -> SemanticReliabilityBenchmarkReport:
    """Score a controlled benchmark run set against SPEC-008A §13 thresholds.

    Fails closed with :class:`QueryClassificationError` when fewer than
    :data:`MIN_CONTROLLED_RUN_COUNT` outcomes are supplied: the 30-run
    semantic-success and pre-source-failure thresholds cannot be honestly
    scored from an under-powered run set.
    """
    if len(outcomes) < MIN_CONTROLLED_RUN_COUNT:
        raise QueryClassificationError(
            "Benchmark evaluation requires at least "
            f"{MIN_CONTROLLED_RUN_COUNT} controlled run outcomes "
            f"(SPEC-008A §13); got {len(outcomes)}."
        )
    run_count = len(outcomes)
    successes = sum(
        1 for outcome in outcomes if outcome.status == QueryExecutionStatus.SUCCESS
    )
    pre_source_failures = sum(
        1
        for outcome in outcomes
        if outcome.pre_source_failure
        or outcome.status
        in {
            QueryExecutionStatus.INVALID_SEMANTIC_PLAN,
            QueryExecutionStatus.INVALID_PHYSICAL_QUERY,
        }
    )
    conflicts = sum(
        1
        for outcome in outcomes
        if outcome.concurrency_conflict
        or outcome.status == QueryExecutionStatus.CONCURRENCY_CONFLICT
    )
    latencies = sorted(outcome.latency_seconds for outcome in outcomes)
    p95_latency = _percentile(latencies, 0.95)
    max_latency = latencies[-1]

    signatures = [
        outcome.determinism_signature
        for outcome in outcomes
        if outcome.determinism_signature is not None
    ]
    signature_coverage = len(signatures) / run_count
    determinism_score = (
        score_semantic_determinism(signatures) if signatures else 0.0
    )

    semantic_success_rate = successes / run_count
    pre_source_failure_rate = pre_source_failures / run_count

    failures: list[str] = []
    if signature_coverage < 1.0:
        failures.append(
            f"determinism_signature_coverage {signature_coverage:.4f} < 1.0"
        )
    if semantic_success_rate < SEMANTIC_SUCCESS_MIN:
        failures.append(
            f"semantic_success_rate {semantic_success_rate:.4f} < "
            f"{SEMANTIC_SUCCESS_MIN}"
        )
    if pre_source_failure_rate > PRE_SOURCE_FAILURE_MAX:
        failures.append(
            f"pre_source_failure_rate {pre_source_failure_rate:.4f} > "
            f"{PRE_SOURCE_FAILURE_MAX}"
        )
    if conflicts > CONCURRENCY_CONFLICT_MAX:
        failures.append(
            f"concurrency_conflict_count {conflicts} > {CONCURRENCY_CONFLICT_MAX}"
        )
    if p95_latency > P95_LATENCY_MAX_SECONDS:
        failures.append(
            f"p95_latency_seconds {p95_latency:.4f} > {P95_LATENCY_MAX_SECONDS}"
        )
    if max_latency > MAX_LATENCY_MAX_SECONDS:
        failures.append(
            f"max_latency_seconds {max_latency:.4f} > {MAX_LATENCY_MAX_SECONDS}"
        )
    if determinism_score < DETERMINISM_MIN:
        failures.append(
            f"determinism_score {determinism_score:.4f} < {DETERMINISM_MIN}"
        )

    return SemanticReliabilityBenchmarkReport(
        run_count=run_count,
        semantic_success_rate=semantic_success_rate,
        pre_source_failure_rate=pre_source_failure_rate,
        concurrency_conflict_count=conflicts,
        p95_latency_seconds=p95_latency,
        max_latency_seconds=max_latency,
        determinism_score=determinism_score,
        passed=not failures,
        failures=failures,
    )
