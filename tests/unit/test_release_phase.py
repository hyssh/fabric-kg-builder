"""Tests for release/phase.py — E2ESession phase management."""
from __future__ import annotations

import pytest

from fabric_kg_builder.release.phase import (
    E2EPhase,
    E2EPhaseStatus,
    E2ESession,
    PhaseResult,
    _PHASE_ORDER,
)


def _make_session(session_id: str = "sess-001", environment: str = "test") -> E2ESession:
    return E2ESession(session_id=session_id, environment=environment)


# ---------------------------------------------------------------------------
# PhaseResult
# ---------------------------------------------------------------------------

class TestPhaseResult:
    def test_default_status_not_run(self):
        result = PhaseResult(phase=E2EPhase.PROVISION)
        assert result.status == E2EPhaseStatus.NOT_RUN

    def test_duration_none_without_timestamps(self):
        result = PhaseResult(phase=E2EPhase.PROVISION)
        assert result.duration_seconds() is None

    def test_duration_with_timestamps(self):
        from datetime import datetime, timezone, timedelta
        start = datetime.now(timezone.utc)
        end = start + timedelta(seconds=5.0)
        result = PhaseResult(phase=E2EPhase.PROVISION, started_at=start, completed_at=end)
        assert abs(result.duration_seconds() - 5.0) < 0.01

    def test_is_terminal_not_run(self):
        result = PhaseResult(phase=E2EPhase.PROVISION)
        assert result.is_terminal() is False

    def test_is_terminal_passed(self):
        result = PhaseResult(phase=E2EPhase.PROVISION, status=E2EPhaseStatus.PASSED)
        assert result.is_terminal() is True

    def test_is_terminal_failed(self):
        result = PhaseResult(phase=E2EPhase.PROVISION, status=E2EPhaseStatus.FAILED)
        assert result.is_terminal() is True

    def test_is_terminal_blocked(self):
        result = PhaseResult(phase=E2EPhase.PROVISION, status=E2EPhaseStatus.BLOCKED)
        assert result.is_terminal() is True

    def test_is_terminal_running(self):
        result = PhaseResult(phase=E2EPhase.PROVISION, status=E2EPhaseStatus.RUNNING)
        assert result.is_terminal() is False


# ---------------------------------------------------------------------------
# E2ESession
# ---------------------------------------------------------------------------

class TestE2ESession:
    def test_initial_state(self):
        session = _make_session()
        for phase in _PHASE_ORDER:
            assert session.results[phase].status == E2EPhaseStatus.NOT_RUN
        assert session.any_resource_created is False

    def test_start_phase(self):
        session = _make_session()
        result = session.start_phase(E2EPhase.PROVISION)
        assert result.status == E2EPhaseStatus.RUNNING
        assert result.started_at is not None

    def test_start_phase_already_running_raises(self):
        session = _make_session()
        session.start_phase(E2EPhase.PROVISION)
        with pytest.raises(RuntimeError, match="already in status"):
            session.start_phase(E2EPhase.PROVISION)

    def test_complete_phase_passed(self):
        session = _make_session()
        session.start_phase(E2EPhase.PROVISION)
        result = session.complete_phase(E2EPhase.PROVISION, passed=True)
        assert result.status == E2EPhaseStatus.PASSED

    def test_complete_phase_failed(self):
        session = _make_session()
        session.start_phase(E2EPhase.PROVISION)
        result = session.complete_phase(E2EPhase.PROVISION, passed=False, error="Resource failed")
        assert result.status == E2EPhaseStatus.FAILED
        assert result.error == "Resource failed"

    def test_failed_phase_blocks_downstream(self):
        session = _make_session()
        session.start_phase(E2EPhase.PROVISION)
        session.complete_phase(E2EPhase.PROVISION, passed=False)
        # Ingest, Deploy, Query, Trace should be blocked
        for phase in [E2EPhase.INGEST, E2EPhase.DEPLOY, E2EPhase.QUERY, E2EPhase.TRACE]:
            assert session.results[phase].status == E2EPhaseStatus.BLOCKED
        # Teardown is never blocked
        assert session.results[E2EPhase.TEARDOWN].status == E2EPhaseStatus.NOT_RUN

    def test_teardown_never_blocked_by_failure(self):
        session = _make_session()
        session.start_phase(E2EPhase.PROVISION)
        session.complete_phase(E2EPhase.PROVISION, passed=False)
        assert session.results[E2EPhase.TEARDOWN].status == E2EPhaseStatus.NOT_RUN

    def test_mark_resource_created(self):
        session = _make_session()
        session.mark_resource_created()
        assert session.any_resource_created is True

    def test_ensure_teardown_marks_blocked_if_resource_created(self):
        session = _make_session()
        session.mark_resource_created()
        session.ensure_teardown()
        td = session.results[E2EPhase.TEARDOWN]
        assert td.status == E2EPhaseStatus.BLOCKED
        assert "Teardown was not explicitly completed" in td.notes

    def test_ensure_teardown_no_op_if_no_resource(self):
        session = _make_session()
        session.ensure_teardown()
        assert session.results[E2EPhase.TEARDOWN].status == E2EPhaseStatus.NOT_RUN

    def test_all_passed_false_initially(self):
        session = _make_session()
        assert session.all_passed() is False

    def test_any_failed(self):
        session = _make_session()
        session.start_phase(E2EPhase.PROVISION)
        session.complete_phase(E2EPhase.PROVISION, passed=False)
        assert session.any_failed() is True

    def test_any_failed_false_when_all_pass(self):
        session = _make_session()
        for phase in _PHASE_ORDER[:-1]:  # skip teardown
            session.results[phase].status = E2EPhaseStatus.PASSED
        assert session.any_failed() is False

    def test_teardown_succeeded_false_initially(self):
        session = _make_session()
        assert session.teardown_succeeded() is False

    def test_teardown_succeeded_after_pass(self):
        session = _make_session()
        session.start_phase(E2EPhase.TEARDOWN)
        session.complete_phase(E2EPhase.TEARDOWN, passed=True)
        assert session.teardown_succeeded() is True

    def test_summary_structure(self):
        session = _make_session()
        s = session.summary()
        assert s["session_id"] == "sess-001"
        assert s["environment"] == "test"
        assert "phases" in s
        assert E2EPhase.PROVISION.value in s["phases"]

    def test_complete_phase_with_notes_and_artifacts(self):
        session = _make_session()
        session.start_phase(E2EPhase.INGEST)
        result = session.complete_phase(
            E2EPhase.INGEST,
            passed=True,
            notes="Ingested 100 files",
            artifacts=["batch-001"],
        )
        assert result.notes == "Ingested 100 files"
        assert "batch-001" in result.artifacts

    def test_context_manager(self):
        session = _make_session()
        session.mark_resource_created()
        with session.managed():
            pass
        assert session.completed_at is not None
        # Teardown should be noted as blocked since no explicit teardown ran
        assert session.results[E2EPhase.TEARDOWN].status == E2EPhaseStatus.BLOCKED


# ---------------------------------------------------------------------------
# Phase order
# ---------------------------------------------------------------------------

class TestPhaseOrder:
    def test_provision_is_first(self):
        assert _PHASE_ORDER[0] == E2EPhase.PROVISION

    def test_teardown_is_last(self):
        assert _PHASE_ORDER[-1] == E2EPhase.TEARDOWN

    def test_six_phases(self):
        assert len(_PHASE_ORDER) == 6
