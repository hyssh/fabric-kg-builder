"""Disposable E2E phase/state contract for M9 release hardening.

Defines the ordered phases of a live end-to-end smoke run and a context-manager
that guarantees teardown is attempted in a ``finally`` path after any managed
resource has been created.

Phase order: PROVISION → INGEST → DEPLOY → QUERY → TRACE → TEARDOWN

Status transitions:
    NOT_RUN → RUNNING → PASSED | FAILED | BLOCKED

BLOCKED means an upstream phase failed and this phase was skipped as a
consequence.  A phase can only become PASSED when it executes without error.
Missing precondition phases or a FAILED upstream phase set the current phase
to BLOCKED (not PASSED).
"""

from __future__ import annotations

import contextlib
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Generator, Optional

from pydantic import BaseModel, Field


class E2EPhase(str, Enum):
    """Ordered phases of a disposable live E2E smoke run."""

    PROVISION = "provision"
    INGEST = "ingest"
    DEPLOY = "deploy"
    QUERY = "query"
    TRACE = "trace"
    TEARDOWN = "teardown"


# Canonical phase ordering used for BLOCKED propagation.
_PHASE_ORDER: list[E2EPhase] = [
    E2EPhase.PROVISION,
    E2EPhase.INGEST,
    E2EPhase.DEPLOY,
    E2EPhase.QUERY,
    E2EPhase.TRACE,
    E2EPhase.TEARDOWN,
]


class E2EPhaseStatus(str, Enum):
    NOT_RUN = "not_run"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


class PhaseResult(BaseModel):
    """Recorded outcome for one E2E phase."""

    phase: E2EPhase
    status: E2EPhaseStatus = E2EPhaseStatus.NOT_RUN
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    notes: Optional[str] = None
    artifacts: list[str] = Field(default_factory=list)

    def duration_seconds(self) -> Optional[float]:
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None

    def is_terminal(self) -> bool:
        return self.status in (
            E2EPhaseStatus.PASSED,
            E2EPhaseStatus.FAILED,
            E2EPhaseStatus.BLOCKED,
        )


class E2ESession(BaseModel):
    """Full state of one disposable E2E smoke session.

    Maintains one :class:`PhaseResult` per phase, in order.  Provides helpers
    to advance phase status and determine overall readiness.

    The session MUST be used as a context manager or the caller MUST invoke
    :meth:`ensure_teardown` explicitly.  When any managed resource has been
    created, teardown is attempted regardless of earlier failures.
    """

    session_id: str
    environment: str
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    budget_tag: Optional[str] = None
    timeout_seconds: Optional[int] = None
    any_resource_created: bool = False
    results: dict[E2EPhase, PhaseResult] = Field(
        default_factory=lambda: {p: PhaseResult(phase=p) for p in _PHASE_ORDER}
    )

    # ------------------------------------------------------------------
    # Phase control
    # ------------------------------------------------------------------

    def start_phase(self, phase: E2EPhase) -> PhaseResult:
        result = self.results[phase]
        if result.status != E2EPhaseStatus.NOT_RUN:
            raise RuntimeError(
                f"Phase {phase} is already in status {result.status}; "
                "cannot restart a terminal or running phase."
            )
        result.status = E2EPhaseStatus.RUNNING
        result.started_at = datetime.now(timezone.utc)
        return result

    def complete_phase(
        self,
        phase: E2EPhase,
        *,
        passed: bool,
        error: Optional[str] = None,
        notes: Optional[str] = None,
        artifacts: Optional[list[str]] = None,
    ) -> PhaseResult:
        result = self.results[phase]
        result.completed_at = datetime.now(timezone.utc)
        result.status = E2EPhaseStatus.PASSED if passed else E2EPhaseStatus.FAILED
        result.error = error
        if notes:
            result.notes = notes
        if artifacts:
            result.artifacts = artifacts
        # Propagate BLOCKED to downstream phases if this one failed.
        if not passed and phase != E2EPhase.TEARDOWN:
            self._block_downstream(phase)
        return result

    def _block_downstream(self, failed_phase: E2EPhase) -> None:
        idx = _PHASE_ORDER.index(failed_phase)
        for downstream in _PHASE_ORDER[idx + 1 :]:
            if downstream == E2EPhase.TEARDOWN:
                # Never block teardown; it must always be attempted.
                continue
            r = self.results[downstream]
            if r.status == E2EPhaseStatus.NOT_RUN:
                r.status = E2EPhaseStatus.BLOCKED

    def mark_resource_created(self) -> None:
        """Signal that at least one managed resource exists; teardown is now required."""
        self.any_resource_created = True

    # ------------------------------------------------------------------
    # Context manager — teardown always runs in finally
    # ------------------------------------------------------------------

    @contextmanager
    def managed(self) -> Generator["E2ESession", None, None]:
        """Context manager that ensures teardown is attempted.

        Usage::

            with session.managed():
                session.start_phase(E2EPhase.PROVISION)
                ...

        Teardown is attempted via :meth:`ensure_teardown` in the ``finally``
        block regardless of exceptions.  The caller is responsible for
        implementing the actual teardown logic and calling
        ``session.complete_phase(E2EPhase.TEARDOWN, passed=...)``.
        """
        self.started_at = datetime.now(timezone.utc)
        try:
            yield self
        finally:
            self.ensure_teardown()
            self.completed_at = datetime.now(timezone.utc)

    def ensure_teardown(self) -> None:
        """Mark teardown as attempted if any resource was created and it hasn't run yet.

        This does NOT perform real teardown — callers implement that logic.
        This method prevents a NOT_RUN teardown from being confused with
        no cleanup needed when resources exist.
        """
        td = self.results[E2EPhase.TEARDOWN]
        if self.any_resource_created and td.status == E2EPhaseStatus.NOT_RUN:
            td.status = E2EPhaseStatus.BLOCKED
            td.notes = (
                "Teardown was not explicitly completed; "
                "resources may remain active. Inspect ledger."
            )

    # ------------------------------------------------------------------
    # Readiness helpers
    # ------------------------------------------------------------------

    def all_passed(self, exclude_teardown: bool = False) -> bool:
        phases = _PHASE_ORDER if not exclude_teardown else _PHASE_ORDER[:-1]
        return all(self.results[p].status == E2EPhaseStatus.PASSED for p in phases)

    def any_failed(self) -> bool:
        return any(r.status == E2EPhaseStatus.FAILED for r in self.results.values())

    def teardown_succeeded(self) -> bool:
        return self.results[E2EPhase.TEARDOWN].status == E2EPhaseStatus.PASSED

    def summary(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "environment": self.environment,
            "budget_tag": self.budget_tag,
            "any_resource_created": self.any_resource_created,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "phases": {
                p.value: {
                    "status": r.status.value,
                    "duration_seconds": r.duration_seconds(),
                    "error": r.error,
                }
                for p, r in self.results.items()
            },
        }
