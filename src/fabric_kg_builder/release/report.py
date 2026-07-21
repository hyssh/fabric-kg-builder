"""Deterministic release-candidate readiness report generator.

Reads a :class:`ReleaseManifest` and :class:`ResourceLedger` and produces a
:class:`ReadinessReport` that deterministically decides whether the release is
ready to ship.

Readiness criteria (all must be satisfied):
1. No required evidence record is NOT_RUN, BLOCKED, or FAILED.
2. No managed (CREATE-mode) resource remains active (billable leak check).
3. No teardown failed without being explicitly resolved.

PASSED evidence is accepted.  IMPLEMENTED_OFFLINE, PENDING_INTEGRATION, and
REQUIRES_LIVE_SMOKE are informational sub-statuses that may or may not be
blocking depending on the release gate being evaluated.

By default, ``generate_report`` treats only NOT_RUN / BLOCKED / FAILED as
blocking.  The caller can request a stricter mode that also blocks on
REQUIRES_LIVE_SMOKE.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from fabric_kg_builder.release.ledger import ResourceLedger
from fabric_kg_builder.release.manifest import (
    ALL_REQUIRED_IDS,
    EvidenceStatus,
    ReleaseManifest,
)


class ReadinessStatus(str, Enum):
    READY = "ready"
    NOT_READY = "not_ready"
    REQUIRES_LIVE_SMOKE = "requires_live_smoke"


class ReadinessReport(BaseModel):
    """Output of :func:`generate_report`."""

    report_id: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    readiness: ReadinessStatus
    manifest_id: str
    environment: Optional[str] = None

    # Evidence breakdown
    total_required: int
    not_run_count: int
    blocked_count: int
    failed_count: int
    implemented_offline_count: int
    pending_integration_count: int
    requires_live_smoke_count: int
    passed_count: int
    missing_ids: list[str]
    blocking_ids: list[str]

    # Cleanup breakdown
    active_billable_resources: int
    teardown_failures: list[str]
    cleanup_complete: bool

    # Separation narrative
    offline_summary: str
    live_smoke_summary: str
    blocking_summary: str

    def to_dict(self) -> dict:
        return json.loads(self.model_dump_json())

    def to_json(self, indent: int = 2) -> str:
        return self.model_dump_json(indent=indent)


def generate_report(
    manifest: ReleaseManifest,
    ledger: Optional[ResourceLedger] = None,
    report_id: str = "rc-report",
    require_live_smoke: bool = False,
) -> ReadinessReport:
    """Generate a deterministic release-candidate readiness report.

    Parameters
    ----------
    manifest:
        Populated :class:`ReleaseManifest` for this candidate.
    ledger:
        Optional :class:`ResourceLedger`.  When supplied, active billable
        resources and teardown failures are checked.
    report_id:
        Identifier for this report run.
    require_live_smoke:
        When True, ``REQUIRES_LIVE_SMOKE`` evidence is treated as blocking.
        Default False (offline report mode).
    """
    # ------------------------------------------------------------------
    # Evidence analysis
    # ------------------------------------------------------------------
    missing_ids = manifest.missing_ids()
    not_run = [
        r for r in manifest.evidence.values() if r.status == EvidenceStatus.NOT_RUN
    ]
    blocked = [
        r for r in manifest.evidence.values() if r.status == EvidenceStatus.BLOCKED
    ]
    failed = [
        r for r in manifest.evidence.values() if r.status == EvidenceStatus.FAILED
    ]
    impl_offline = [
        r
        for r in manifest.evidence.values()
        if r.status == EvidenceStatus.IMPLEMENTED_OFFLINE
    ]
    pending_integration = [
        r
        for r in manifest.evidence.values()
        if r.status == EvidenceStatus.PENDING_INTEGRATION
    ]
    req_live = [
        r
        for r in manifest.evidence.values()
        if r.status == EvidenceStatus.REQUIRES_LIVE_SMOKE
    ]
    passed = [
        r for r in manifest.evidence.values() if r.status == EvidenceStatus.PASSED
    ]

    # Determine blocking IDs
    blocking_ids: list[str] = []
    blocking_ids.extend(missing_ids)
    blocking_ids.extend(r.evidence_id for r in not_run)
    blocking_ids.extend(r.evidence_id for r in blocked)
    blocking_ids.extend(r.evidence_id for r in failed)
    if require_live_smoke:
        blocking_ids.extend(r.evidence_id for r in req_live)

    # ------------------------------------------------------------------
    # Cleanup analysis
    # ------------------------------------------------------------------
    if ledger is not None:
        active_billable = ledger.active_billable_count()
        teardown_failures = [r.resource_id for r in ledger.teardown_failures()]
        cleanup_complete = ledger.is_cleanup_complete()
    else:
        active_billable = 0
        teardown_failures = []
        cleanup_complete = True

    # Active billable resources always block readiness.
    if active_billable > 0:
        blocking_ids.append(f"CLEANUP:active_billable_resources={active_billable}")
    for rid in teardown_failures:
        blocking_ids.append(f"CLEANUP:teardown_failed:{rid}")

    # ------------------------------------------------------------------
    # Overall readiness
    # ------------------------------------------------------------------
    if blocking_ids:
        readiness = ReadinessStatus.NOT_READY
    elif req_live and not require_live_smoke:
        # Some evidence requires live smoke but isn't blocking in offline mode.
        readiness = ReadinessStatus.REQUIRES_LIVE_SMOKE
    else:
        readiness = ReadinessStatus.READY

    # ------------------------------------------------------------------
    # Narrative summaries — separated offline vs live smoke
    # ------------------------------------------------------------------
    offline_parts: list[str] = []
    if impl_offline:
        offline_parts.append(
            f"{len(impl_offline)} gate(s) verified offline: "
            + ", ".join(r.evidence_id for r in impl_offline[:5])
            + ("..." if len(impl_offline) > 5 else "")
        )
    if passed:
        offline_parts.append(f"{len(passed)} gate(s) explicitly PASSED.")
    offline_summary = "; ".join(offline_parts) if offline_parts else "No offline evidence recorded."

    live_parts: list[str] = []
    if req_live:
        live_parts.append(
            f"{len(req_live)} gate(s) require live smoke (opt-in, not a merge gate): "
            + ", ".join(r.evidence_id for r in req_live[:5])
            + ("..." if len(req_live) > 5 else "")
        )
    if pending_integration:
        live_parts.append(
            f"{len(pending_integration)} gate(s) pending integration (M5/M8 execution): "
            + ", ".join(r.evidence_id for r in pending_integration[:5])
            + ("..." if len(pending_integration) > 5 else "")
        )
    live_smoke_summary = "; ".join(live_parts) if live_parts else "No live-smoke gates identified."

    blocking_summary = (
        f"{len(blocking_ids)} blocking item(s): {blocking_ids[:10]}"
        if blocking_ids
        else "No blocking items."
    )

    return ReadinessReport(
        report_id=report_id,
        readiness=readiness,
        manifest_id=manifest.manifest_id,
        environment=ledger.environment if ledger else None,
        total_required=len(ALL_REQUIRED_IDS),
        not_run_count=len(not_run),
        blocked_count=len(blocked),
        failed_count=len(failed),
        implemented_offline_count=len(impl_offline),
        pending_integration_count=len(pending_integration),
        requires_live_smoke_count=len(req_live),
        passed_count=len(passed),
        missing_ids=missing_ids,
        blocking_ids=sorted(set(blocking_ids)),
        active_billable_resources=active_billable,
        teardown_failures=teardown_failures,
        cleanup_complete=cleanup_complete,
        offline_summary=offline_summary,
        live_smoke_summary=live_smoke_summary,
        blocking_summary=blocking_summary,
    )
