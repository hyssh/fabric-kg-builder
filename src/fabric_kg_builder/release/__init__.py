"""M9 release-hardening package.

Provides offline-verifiable release evidence contracts for fabric-kg-builder:

- :mod:`ledger`   — typed resource ledger (adopted-resource protection, cleanup tracking)
- :mod:`phase`    — disposable E2E phase/state machine (teardown guarantee)
- :mod:`manifest` — evidence manifest mapping PRD 1-13 / VAL-029..050 / REL-001..009
- :mod:`report`   — deterministic release-candidate readiness report
- :mod:`redact`   — secret/source-content redaction for serialised evidence
- :mod:`diagnostics` — SPEC-008A §10/§11 local, privacy-safe Fabric Data
  Agent diagnostic export inspector (S8A-DIA-001..003, VAL-087)

No module in this package may import cloud SDK clients or make network calls.
Live tests are in ``tests/integration/`` and are always opt-in.
"""

from __future__ import annotations

from fabric_kg_builder.release.diagnostics import (
    DiagnosticsInspectionError,
    DiagnosticsPrivacyViolation,
    REPORT_SCHEMA_VERSION as DIAGNOSTICS_REPORT_SCHEMA_VERSION,
    assert_report_is_redacted,
    fingerprint,
    inspect_files,
    redact_record,
)
from fabric_kg_builder.release.ledger import (
    AdoptionMode,
    ResourceKind,
    ResourceLedger,
    ResourceRecord,
    ResourceStatus,
)
from fabric_kg_builder.release.manifest import (
    EvidenceRecord,
    EvidenceStatus,
    ReleaseManifest,
    build_empty_manifest,
)
from fabric_kg_builder.release.phase import (
    E2EPhase,
    E2EPhaseStatus,
    E2ESession,
    PhaseResult,
)
from fabric_kg_builder.release.report import (
    ReadinessReport,
    ReadinessStatus,
    generate_report,
)

__all__ = [
    # diagnostics
    "DIAGNOSTICS_REPORT_SCHEMA_VERSION",
    "DiagnosticsInspectionError",
    "DiagnosticsPrivacyViolation",
    "assert_report_is_redacted",
    "fingerprint",
    "inspect_files",
    "redact_record",
    # ledger
    "AdoptionMode",
    "ResourceKind",
    "ResourceLedger",
    "ResourceRecord",
    "ResourceStatus",
    # manifest
    "EvidenceRecord",
    "EvidenceStatus",
    "ReleaseManifest",
    "build_empty_manifest",
    # phase
    "E2EPhase",
    "E2EPhaseStatus",
    "E2ESession",
    "PhaseResult",
    # report
    "ReadinessReport",
    "ReadinessStatus",
    "generate_report",
]
