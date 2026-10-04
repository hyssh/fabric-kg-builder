"""knowledge.lineage_adapter -- thin lineage recording for M7 knowledge operations.

Records create/update/probe operations for knowledge sources, knowledge bases,
and Fabric Data Agents as deployment entries in the existing lineage/deployment
registry.

Persisted fields per operation
-------------------------------
- operation       : "knowledge_source", "knowledge_base", "fabric_data_agent"
- action          : "upsert", "get", "probe"
- api_version     : the Search or Fabric API version used
- capability_mode : "ga" or "preview"
- resource_name   : knowledge source/base or data agent display name
- remote_id       : ETag / Fabric item ID where available
- status          : "ok", "created", "updated", "partial", "error"
- timestamps      : started_at, completed_at (from lineage.common.now_utc)
- parent_run_id   : caller-supplied run context

Never persisted
---------------
- API keys, bearer tokens, OBO tokens, connection strings, or any credential.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# M7 pipeline stage identifier
_M7_PIPELINE_VERSION = "m7-knowledge-v1"


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class KnowledgeDeploymentRecord:
    """A single M7 knowledge operation recorded as deployment lineage.

    Attributes
    ----------
    operation : str
        One of ``"knowledge_source"``, ``"knowledge_base"``, ``"fabric_data_agent"``.
    action : str
        One of ``"upsert"``, ``"get"``, ``"probe"``.
    api_version : str
        The API version string used for the operation.
    capability_mode : str
        ``"preview"`` when the preview API was used, else ``"ga"``.
    resource_name : str
        The knowledge source / base / data agent name.
    remote_id : str | None
        ETag, Fabric item ID, or other server-assigned identifier.
    status : str
        ``"created"``, ``"updated"``, ``"ok"``, ``"partial"``, or ``"error"``.
    started_at : str
        ISO-8601 timestamp.
    completed_at : str
        ISO-8601 timestamp.
    parent_run_id : str | None
        Caller-supplied run ID from the lineage registry.
    endpoint : str
        Service endpoint hostname only (no path, no credentials).
    extra : dict
        Optional additional non-sensitive metadata.
    """

    operation: str
    action: str
    api_version: str
    capability_mode: str
    resource_name: str
    status: str
    started_at: str
    completed_at: str
    endpoint: str = ""
    remote_id: str | None = None
    parent_run_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pipeline_version": _M7_PIPELINE_VERSION,
            "operation": self.operation,
            "action": self.action,
            "api_version": self.api_version,
            "capability_mode": self.capability_mode,
            "resource_name": self.resource_name,
            "remote_id": self.remote_id,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "endpoint": self.endpoint,
            "parent_run_id": self.parent_run_id,
            **{k: v for k, v in self.extra.items()},
        }


class KnowledgeLineageRecorder:
    """Records M7 knowledge operations to a JSON file and optionally to
    the existing lineage/deployment registry.

    Parameters
    ----------
    store_path : Path | str | None
        Path to the JSON file where records are appended.  If ``None``,
        records are held in memory only (useful in tests).
    registry : AssetRegistry | None
        Optional existing registry to also append ``record_deployment`` to.
        Passing ``None`` skips the registry call.
    parent_run_id : str | None
        Optional run ID to attach to all records.
    """

    def __init__(
        self,
        store_path: Path | str | None = None,
        registry: Any | None = None,
        parent_run_id: str | None = None,
    ) -> None:
        self._store_path = Path(store_path) if store_path else None
        self._registry = registry
        self._parent_run_id = parent_run_id
        self._records: list[KnowledgeDeploymentRecord] = []

    def record(
        self,
        *,
        operation: str,
        action: str,
        api_version: str,
        capability_mode: str,
        resource_name: str,
        status: str,
        endpoint: str = "",
        remote_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> KnowledgeDeploymentRecord:
        """Record a single knowledge operation.

        Never call this with credential values.  Only pass non-sensitive
        metadata (names, IDs, versions, statuses).
        """
        ts = _now_utc_iso()
        rec = KnowledgeDeploymentRecord(
            operation=operation,
            action=action,
            api_version=api_version,
            capability_mode=capability_mode,
            resource_name=resource_name,
            status=status,
            started_at=ts,
            completed_at=ts,
            endpoint=_sanitise_endpoint(endpoint),
            remote_id=remote_id,
            parent_run_id=self._parent_run_id,
            extra=extra or {},
        )
        self._records.append(rec)
        self._persist(rec)

        if self._registry is not None:
            self._record_to_registry(rec)

        logger.debug(
            "[lineage] %s %s %s -> %s",
            operation, action, resource_name, status,
        )
        return rec

    def _persist(self, rec: KnowledgeDeploymentRecord) -> None:
        if self._store_path is None:
            return
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            existing: list[dict[str, Any]] = []
            if self._store_path.exists():
                try:
                    existing = json.loads(self._store_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    existing = []
            existing.append(rec.to_dict())
            self._store_path.write_text(
                json.dumps(existing, indent=2, sort_keys=True, default=str),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[lineage] failed to persist record: %s", exc)

    def _record_to_registry(self, rec: KnowledgeDeploymentRecord) -> None:
        """Append to the existing AssetRegistry deployments list (best-effort)."""
        try:
            from fabric_kg_builder.lineage.registry import record_deployment  # noqa: PLC0415
            from fabric_kg_builder.lineage.common import now_utc  # noqa: PLC0415
            from fabric_kg_builder.model.ids import make_run_id  # noqa: PLC0415

            store = self._registry.load()
            run_id = self._parent_run_id or make_run_id()
            record_deployment(
                store["deployments"],
                run_id=run_id,
                environment=getattr(self._registry, "environment", "dev"),
                artifact_type=f"knowledge/{rec.operation}",
                artifact_version=rec.api_version,
                target_resource_id=rec.remote_id,
                target_name=rec.resource_name,
                target_record_locator=rec.endpoint,
                status=rec.status,
            )
            self._registry.save(store)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[lineage] registry record_deployment failed: %s", exc)

    @property
    def records(self) -> list[KnowledgeDeploymentRecord]:
        """All records captured during this session (in-memory)."""
        return list(self._records)


def _sanitise_endpoint(endpoint: str) -> str:
    """Return the hostname only (no path, no query, no credentials)."""
    if not endpoint:
        return ""
    try:
        from urllib.parse import urlparse  # noqa: PLC0415
        parsed = urlparse(endpoint if "://" in endpoint else f"https://{endpoint}")
        return f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else endpoint.split("/")[0]
    except Exception:  # noqa: BLE001
        return endpoint


# ---------------------------------------------------------------------------
# Module-level default recorder (no-op until configured)
# ---------------------------------------------------------------------------

#: Process-wide default recorder.  Replace with a configured instance to
#: enable persistent lineage recording.  ``None`` means recording is disabled.
default_recorder: KnowledgeLineageRecorder | None = None


def record(
    *,
    operation: str,
    action: str,
    api_version: str,
    capability_mode: str,
    resource_name: str,
    status: str,
    endpoint: str = "",
    remote_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Record via the module-level :data:`default_recorder` if configured.

    This is a fire-and-forget call: if no recorder is configured, or if
    recording fails, the operation continues silently.
    """
    if default_recorder is not None:
        try:
            default_recorder.record(
                operation=operation,
                action=action,
                api_version=api_version,
                capability_mode=capability_mode,
                resource_name=resource_name,
                status=status,
                endpoint=endpoint,
                remote_id=remote_id,
                extra=extra,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[lineage] record() failed: %s", exc)
