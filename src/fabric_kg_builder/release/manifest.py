"""Release evidence manifest for M9 release hardening.

Maps every PRD v2 acceptance criterion (1-13), SPEC-006 validation gate
(VAL-029..VAL-050), and backlog task (REL-001..REL-009) to typed evidence
records.

Design invariants:
- Missing evidence starts as ``NOT_RUN``.  It must never silently default to
  ``PASS``.
- ``build_empty_manifest()`` always returns NOT_RUN/BLOCKED records for every
  required ID — callers fill in passing evidence at execution time.
- Status can only advance to ``PASSED`` when ``test_command`` and
  ``artifact_path`` are explicitly supplied.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Evidence IDs
# ---------------------------------------------------------------------------

# PRD v2 acceptance criteria (section 9, items 1-13)
PRD_CRITERIA_IDS: list[str] = [f"PRD-AC-{i:02d}" for i in range(1, 14)]

# SPEC-006 §12 validation gates VAL-029 through VAL-050
VAL_GATE_IDS: list[str] = [f"VAL-{i:03d}" for i in range(29, 51)]

# TASKS-001 M9 release tasks REL-001 through REL-009
REL_TASK_IDS: list[str] = [f"REL-{i:03d}" for i in range(1, 10)]

ALL_REQUIRED_IDS: list[str] = PRD_CRITERIA_IDS + VAL_GATE_IDS + REL_TASK_IDS


# ---------------------------------------------------------------------------
# Evidence types
# ---------------------------------------------------------------------------


class EvidenceStatus(str, Enum):
    NOT_RUN = "not_run"
    BLOCKED = "blocked"
    PASSED = "passed"
    FAILED = "failed"
    PENDING_INTEGRATION = "pending_integration"
    REQUIRES_LIVE_SMOKE = "requires_live_smoke"
    IMPLEMENTED_OFFLINE = "implemented_offline"


class EvidenceRecord(BaseModel):
    """One piece of release evidence bound to a requirement/gate ID."""

    evidence_id: str = Field(description="PRD-AC-NN, VAL-NNN, or REL-NNN")
    description: str = Field(description="Human-readable description of what this evidence proves.")
    status: EvidenceStatus = EvidenceStatus.NOT_RUN
    test_command: Optional[str] = Field(
        default=None,
        description="pytest selector or CLI command that produces the evidence artifact.",
    )
    artifact_path: Optional[str] = Field(
        default=None,
        description="Relative path to the evidence artifact (report, fixture, log, etc.).",
    )
    recorded_at: Optional[datetime] = None
    notes: Optional[str] = None
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_default_pass(self) -> "EvidenceRecord":
        """PASSED status requires explicit test_command and artifact_path."""
        if self.status == EvidenceStatus.PASSED and (
            not self.test_command or not self.artifact_path
        ):
            raise ValueError(
                f"Evidence '{self.evidence_id}' cannot be PASSED without both "
                "'test_command' and 'artifact_path'. "
                "Do not default evidence to PASSED."
            )
        return self

    def mark_passed(
        self,
        test_command: str,
        artifact_path: str,
        notes: Optional[str] = None,
    ) -> None:
        """Mark this evidence as passing.  Requires explicit command + artifact."""
        if not test_command or not artifact_path:
            raise ValueError(
                f"Evidence '{self.evidence_id}': test_command and artifact_path "
                "are required to mark evidence as PASSED."
            )
        self.test_command = test_command
        self.artifact_path = artifact_path
        self.status = EvidenceStatus.PASSED
        self.recorded_at = datetime.now(timezone.utc)
        if notes:
            self.notes = notes

    def mark_failed(self, error: Optional[str] = None) -> None:
        self.status = EvidenceStatus.FAILED
        self.recorded_at = datetime.now(timezone.utc)
        if error:
            self.notes = error

    def is_blocking(self) -> bool:
        """Return True when this evidence must pass before release is accepted."""
        return self.status in (
            EvidenceStatus.NOT_RUN,
            EvidenceStatus.BLOCKED,
            EvidenceStatus.FAILED,
        )


# ---------------------------------------------------------------------------
# Manifest descriptions for all required IDs
# ---------------------------------------------------------------------------

_PRD_DESCRIPTIONS: dict[str, str] = {
    "PRD-AC-01": (
        "User with subscription and owned resource group can run infra plan and "
        "infra apply to create or connect all baseline Azure resources."
    ),
    "PRD-AC-02": (
        "Quota and Fabric prerequisites are checked before deployment; GPT-4.1 "
        "200k TPM unavailability is reported without partial success."
    ),
    "PRD-AC-03": (
        "Non-troubleshooting fixture proves no Surface-specific type is introduced."
    ),
    "PRD-AC-04": (
        "Invalid or low-quality domain.yaml is rejected with actionable findings; "
        "approved YAML is versioned in the run manifest."
    ),
    "PRD-AC-05": (
        "Required input adapters produce canonical elements and immutable source lineage; "
        "unsupported PSD behavior is explicit."
    ),
    "PRD-AC-06": (
        "Token-based chunks remain within configured limits and measured overlap is approximately 5%."
    ),
    "PRD-AC-07": (
        "Entity/relationship extraction, merge, summarization, claims, and at least three "
        "hierarchy levels pass fixture and evaluation thresholds."
    ),
    "PRD-AC-08": (
        "Any Search document, ontology instance, claim, and returned citation can be "
        "traced to the original Blob asset."
    ),
    "PRD-AC-09": (
        "Versioned hybrid/semantic/vector Search index and Foundry IQ knowledge base are "
        "deployed and return cited results."
    ),
    "PRD-AC-10": (
        "Lakehouse and Ontology can be redeployed idempotently and refreshed; Graph model "
        "can be created or explicitly connected and graph competency questions return expected paths."
    ),
    "PRD-AC-11": (
        "Fabric Data Agent can query selected Ontology and Search sources within platform limits."
    ),
    "PRD-AC-12": (
        "Foundry agent, FastAPI backend, and Chainlit reference UI can be deployed to a test "
        "environment and pass the automated smoke suite."
    ),
    "PRD-AC-13": (
        "Existing CLI workflows and canonical data remain backward compatible or have a "
        "documented migration with tests."
    ),
}

_VAL_DESCRIPTIONS: dict[str, str] = {
    "VAL-029": "Domain YAML schema is valid and approved.",
    "VAL-030": "No implicit sample-domain type appears in a non-sample run.",
    "VAL-031": "Every asset version resolves to immutable original Blob metadata.",
    "VAL-032": "Every derived record has complete common lineage.",
    "VAL-033": "All eligible chunks fit token budget.",
    "VAL-034": "Eligible adjacent chunks meet 5% overlap tolerance.",
    "VAL-035": "Every entity/relationship occurrence has valid evidence.",
    "VAL-036": "Every merged summary cites all distinct contributing facts.",
    "VAL-037": "Every claim has status, review state, and evidence; time bounds are ordered.",
    "VAL-038": "Published hierarchy has at least three supported levels and no cycles.",
    "VAL-039": "Search vector dimension equals index schema dimension.",
    "VAL-040": "Every Search citation resolves to original asset.",
    "VAL-041": "Every Ontology instance sampled resolves to canonical record and asset.",
    "VAL-042": "Infrastructure plan contains no unauthorized or destructive implicit action.",
    "VAL-043": "Adopted resources pass compatibility and RBAC checks.",
    "VAL-044": "Ontology deployment refresh completes or reports a blocking manual action.",
    "VAL-045": "Knowledge base probe returns source references.",
    "VAL-046": "Data Agent passes Search and Ontology competency questions.",
    "VAL-047": "Foundry agent groundedness/citation thresholds pass.",
    "VAL-048": "FastAPI readiness and streaming contract pass.",
    "VAL-049": "No secrets or source content appear in logs/manifests.",
    "VAL-050": "Deployment reapply produces no unintended replacements or duplicate records.",
}

_REL_DESCRIPTIONS: dict[str, str] = {
    "REL-001": "Full disposable live end-to-end workflow: provision → ingest → deploy → query → trace → teardown succeeds and records costs/resources.",
    "REL-002": "Three-domain quality evaluations: approved graph/retrieval/agent thresholds pass for unrelated domains.",
    "REL-003": "v1/current-environment upgrade and rollback: upgrade preserves IDs; Search/Ontology/app rollback procedures pass.",
    "REL-004": "Rate-limit, retry, timeout, and partial-failure tests: 429/Retry-After, LRO resume, batch partial failure, and timeout behavior are explicit.",
    "REL-005": "Security/RBAC/privacy review: least privilege, prompt security, file controls, retention, logs, containers, and source ACL design approved.",
    "REL-006": "Operations runbooks and cost controls: monitoring, alerts, budgets, retention, recovery, quota, preview fallback, and cleanup documented.",
    "REL-007": "Updated quickstarts and migration guide: fresh create and connect-existing paths plus v1 migration are reproducible.",
    "REL-008": "PRD/SPEC traceability review: every PRD acceptance criterion maps to passing evidence; no orphan task/gate.",
    "REL-009": "Release candidate and cleanup report: CI/evals/live smoke pass and no untracked billable test resource remains.",
}


# ---------------------------------------------------------------------------
# Offline status pre-classifications
# The status here reflects what can be verified without live cloud execution.
# Do not set PASSED without evidence artifacts.
# ---------------------------------------------------------------------------

# (evidence_id -> offline_status)
_OFFLINE_CLASSIFICATION: dict[str, EvidenceStatus] = {
    # PRD acceptance criteria
    "PRD-AC-01": EvidenceStatus.REQUIRES_LIVE_SMOKE,
    "PRD-AC-02": EvidenceStatus.REQUIRES_LIVE_SMOKE,
    "PRD-AC-03": EvidenceStatus.IMPLEMENTED_OFFLINE,
    "PRD-AC-04": EvidenceStatus.IMPLEMENTED_OFFLINE,
    "PRD-AC-05": EvidenceStatus.PENDING_INTEGRATION,
    "PRD-AC-06": EvidenceStatus.IMPLEMENTED_OFFLINE,
    "PRD-AC-07": EvidenceStatus.PENDING_INTEGRATION,
    "PRD-AC-08": EvidenceStatus.IMPLEMENTED_OFFLINE,
    "PRD-AC-09": EvidenceStatus.REQUIRES_LIVE_SMOKE,
    "PRD-AC-10": EvidenceStatus.REQUIRES_LIVE_SMOKE,
    "PRD-AC-11": EvidenceStatus.REQUIRES_LIVE_SMOKE,
    "PRD-AC-12": EvidenceStatus.REQUIRES_LIVE_SMOKE,
    "PRD-AC-13": EvidenceStatus.IMPLEMENTED_OFFLINE,
    # VAL gates
    "VAL-029": EvidenceStatus.IMPLEMENTED_OFFLINE,
    "VAL-030": EvidenceStatus.IMPLEMENTED_OFFLINE,
    "VAL-031": EvidenceStatus.IMPLEMENTED_OFFLINE,
    "VAL-032": EvidenceStatus.IMPLEMENTED_OFFLINE,
    "VAL-033": EvidenceStatus.IMPLEMENTED_OFFLINE,
    "VAL-034": EvidenceStatus.IMPLEMENTED_OFFLINE,
    "VAL-035": EvidenceStatus.IMPLEMENTED_OFFLINE,
    "VAL-036": EvidenceStatus.PENDING_INTEGRATION,
    "VAL-037": EvidenceStatus.PENDING_INTEGRATION,
    "VAL-038": EvidenceStatus.PENDING_INTEGRATION,
    "VAL-039": EvidenceStatus.REQUIRES_LIVE_SMOKE,
    "VAL-040": EvidenceStatus.REQUIRES_LIVE_SMOKE,
    "VAL-041": EvidenceStatus.REQUIRES_LIVE_SMOKE,
    "VAL-042": EvidenceStatus.IMPLEMENTED_OFFLINE,
    "VAL-043": EvidenceStatus.REQUIRES_LIVE_SMOKE,
    "VAL-044": EvidenceStatus.REQUIRES_LIVE_SMOKE,
    "VAL-045": EvidenceStatus.REQUIRES_LIVE_SMOKE,
    "VAL-046": EvidenceStatus.REQUIRES_LIVE_SMOKE,
    "VAL-047": EvidenceStatus.REQUIRES_LIVE_SMOKE,
    "VAL-048": EvidenceStatus.REQUIRES_LIVE_SMOKE,
    "VAL-049": EvidenceStatus.IMPLEMENTED_OFFLINE,
    "VAL-050": EvidenceStatus.REQUIRES_LIVE_SMOKE,
    # REL tasks
    "REL-001": EvidenceStatus.REQUIRES_LIVE_SMOKE,
    "REL-002": EvidenceStatus.REQUIRES_LIVE_SMOKE,
    "REL-003": EvidenceStatus.IMPLEMENTED_OFFLINE,
    "REL-004": EvidenceStatus.IMPLEMENTED_OFFLINE,
    "REL-005": EvidenceStatus.IMPLEMENTED_OFFLINE,
    "REL-006": EvidenceStatus.IMPLEMENTED_OFFLINE,
    "REL-007": EvidenceStatus.IMPLEMENTED_OFFLINE,
    "REL-008": EvidenceStatus.IMPLEMENTED_OFFLINE,
    "REL-009": EvidenceStatus.REQUIRES_LIVE_SMOKE,
}

_ALL_DESCRIPTIONS: dict[str, str] = {
    **_PRD_DESCRIPTIONS,
    **_VAL_DESCRIPTIONS,
    **_REL_DESCRIPTIONS,
}


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


class ReleaseManifest(BaseModel):
    """Container for all release evidence records.

    The manifest is the single source of truth for release readiness.
    ``build_empty_manifest()`` guarantees every required ID is present.
    """

    manifest_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    evidence: dict[str, EvidenceRecord] = Field(default_factory=dict)

    def get(self, evidence_id: str) -> EvidenceRecord:
        if evidence_id not in self.evidence:
            raise KeyError(f"Evidence ID '{evidence_id}' not found in manifest.")
        return self.evidence[evidence_id]

    def missing_ids(self) -> list[str]:
        """IDs from the required set that are completely absent from the manifest."""
        return [eid for eid in ALL_REQUIRED_IDS if eid not in self.evidence]

    def blocking_evidence(self) -> list[EvidenceRecord]:
        """Records that would block release (NOT_RUN, BLOCKED, or FAILED)."""
        return [r for r in self.evidence.values() if r.is_blocking()]

    def coverage_fraction(self) -> float:
        """Fraction of required IDs that have non-blocking status."""
        if not ALL_REQUIRED_IDS:
            return 1.0
        passed_count = sum(
            1
            for eid in ALL_REQUIRED_IDS
            if eid in self.evidence and not self.evidence[eid].is_blocking()
        )
        return passed_count / len(ALL_REQUIRED_IDS)

    def to_safe_dict(self) -> dict:
        return {
            "manifest_id": self.manifest_id,
            "created_at": self.created_at.isoformat(),
            "total_required": len(ALL_REQUIRED_IDS),
            "coverage_fraction": round(self.coverage_fraction(), 4),
            "evidence": {
                eid: {
                    "status": rec.status.value,
                    "description": rec.description,
                    "test_command": rec.test_command,
                    "artifact_path": rec.artifact_path,
                    "notes": rec.notes,
                    "recorded_at": rec.recorded_at.isoformat() if rec.recorded_at else None,
                }
                for eid, rec in self.evidence.items()
            },
        }


def build_empty_manifest(manifest_id: str = "release-candidate") -> ReleaseManifest:
    """Build a manifest with all required IDs initialised to their truthful offline status.

    Status is never defaulted to PASSED.  Each ID starts at the offline
    classification defined in ``_OFFLINE_CLASSIFICATION``.
    """
    evidence: dict[str, EvidenceRecord] = {}
    for eid in ALL_REQUIRED_IDS:
        desc = _ALL_DESCRIPTIONS.get(eid, f"Evidence for {eid}")
        status = _OFFLINE_CLASSIFICATION.get(eid, EvidenceStatus.NOT_RUN)
        evidence[eid] = EvidenceRecord(
            evidence_id=eid,
            description=desc,
            status=status,
        )
    return ReleaseManifest(manifest_id=manifest_id, evidence=evidence)
