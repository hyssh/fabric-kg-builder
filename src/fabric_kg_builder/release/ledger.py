"""Typed resource ledger for M9 release hardening.

Records every provisioned or adopted resource with ownership, identity,
resource type, environment, tags, estimated cost-relevant units, and
lifecycle status.

Key invariant: adopted resources (AdoptionMode.CONNECT) can **never** be
scheduled for teardown by this tool.  Any attempt raises ``ValueError``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


class ResourceKind(str, Enum):
    """Fabric or Azure resource type."""

    AZURE_STORAGE = "azure_storage"
    AZURE_DOCUMENT_INTELLIGENCE = "azure_document_intelligence"
    AZURE_AI_SEARCH = "azure_ai_search"
    AZURE_FOUNDRY_PROJECT = "azure_foundry_project"
    AZURE_FOUNDRY_MODEL_DEPLOYMENT = "azure_foundry_model_deployment"
    AZURE_CONTAINER_APP = "azure_container_app"
    AZURE_CONTAINER_APP_ENV = "azure_container_app_env"
    AZURE_RESOURCE_GROUP = "azure_resource_group"
    FABRIC_WORKSPACE = "fabric_workspace"
    FABRIC_LAKEHOUSE = "fabric_lakehouse"
    FABRIC_ONTOLOGY = "fabric_ontology"
    FABRIC_GRAPH_MODEL = "fabric_graph_model"
    FABRIC_DATA_AGENT = "fabric_data_agent"
    FOUNDRY_KNOWLEDGE_BASE = "foundry_knowledge_base"
    FOUNDRY_KNOWLEDGE_SOURCE = "foundry_knowledge_source"
    FOUNDRY_AGENT = "foundry_agent"
    OTHER = "other"


class AdoptionMode(str, Enum):
    """Whether the resource was created by this tool or adopted (connected)."""

    CREATE = "create"
    CONNECT = "connect"


class ResourceStatus(str, Enum):
    """Lifecycle status of a resource record."""

    PROVISIONING = "provisioning"
    ACTIVE = "active"
    TEARDOWN_PENDING = "teardown_pending"
    TORN_DOWN = "torn_down"
    TEARDOWN_FAILED = "teardown_failed"
    UNKNOWN = "unknown"


class ResourceRecord(BaseModel):
    """One entry in the resource ledger."""

    resource_id: str = Field(description="Ledger-scoped unique ID (e.g. UUID or slug).")
    arm_or_fabric_id: Optional[str] = Field(
        default=None,
        description="ARM resource ID or Fabric item ID. None until provisioning completes.",
    )
    resource_kind: ResourceKind
    environment: str = Field(description="Logical environment name (dev/test/prod).")
    display_name: Optional[str] = None
    adoption_mode: AdoptionMode
    owner: str = Field(description="Identity or system that provisioned this resource.")
    tags: dict[str, str] = Field(default_factory=dict)
    estimated_cost_units: Optional[str] = Field(
        default=None,
        description="Human-readable cost-relevant info (e.g. 'standard SKU, 1 SU').",
    )
    status: ResourceStatus = ResourceStatus.UNKNOWN
    created_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None
    cleanup_error: Optional[str] = None
    notes: Optional[str] = None

    @model_validator(mode="after")
    def _adopted_never_teardown(self) -> "ResourceRecord":
        if (
            self.adoption_mode == AdoptionMode.CONNECT
            and self.status in (ResourceStatus.TEARDOWN_PENDING, ResourceStatus.TORN_DOWN)
        ):
            raise ValueError(
                f"Adopted resource '{self.resource_id}' (kind={self.resource_kind}) "
                "cannot be scheduled for teardown. Only managed (CREATE) resources "
                "may be torn down."
            )
        return self

    def is_billable_active(self) -> bool:
        """Return True when the resource is managed AND still active/provisioning."""
        return (
            self.adoption_mode == AdoptionMode.CREATE
            and self.status in (ResourceStatus.PROVISIONING, ResourceStatus.ACTIVE)
        )

    def mark_active(self, arm_or_fabric_id: Optional[str] = None) -> None:
        self.status = ResourceStatus.ACTIVE
        if arm_or_fabric_id:
            self.arm_or_fabric_id = arm_or_fabric_id
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc)

    def schedule_teardown(self) -> None:
        if self.adoption_mode == AdoptionMode.CONNECT:
            raise ValueError(
                f"Cannot schedule teardown for adopted resource '{self.resource_id}'. "
                "Adopted resources are never destroyed by this tool."
            )
        self.status = ResourceStatus.TEARDOWN_PENDING

    def mark_torn_down(self) -> None:
        if self.adoption_mode == AdoptionMode.CONNECT:
            raise ValueError(
                f"Cannot mark adopted resource '{self.resource_id}' as torn down."
            )
        self.status = ResourceStatus.TORN_DOWN
        self.deleted_at = datetime.now(timezone.utc)

    def mark_teardown_failed(self, error: str) -> None:
        self.status = ResourceStatus.TEARDOWN_FAILED
        self.cleanup_error = error


class ResourceLedger(BaseModel):
    """Ordered collection of ResourceRecord entries.

    Provides high-level accessors for billable-active resource detection and
    cleanup completeness checks used by the release readiness report.
    """

    environment: str
    resources: list[ResourceRecord] = Field(default_factory=list)

    def register(self, record: ResourceRecord) -> None:
        """Add a resource to the ledger.  Raises if the resource_id already exists."""
        ids = {r.resource_id for r in self.resources}
        if record.resource_id in ids:
            raise ValueError(
                f"Resource '{record.resource_id}' is already registered in the ledger."
            )
        self.resources.append(record)

    def get(self, resource_id: str) -> ResourceRecord:
        for r in self.resources:
            if r.resource_id == resource_id:
                return r
        raise KeyError(f"Resource '{resource_id}' not found in ledger.")

    # ------------------------------------------------------------------
    # Adoption-mode helpers
    # ------------------------------------------------------------------

    def managed_resources(self) -> list[ResourceRecord]:
        """Return only CREATE-mode (managed) resources."""
        return [r for r in self.resources if r.adoption_mode == AdoptionMode.CREATE]

    def adopted_resources(self) -> list[ResourceRecord]:
        """Return only CONNECT-mode (adopted) resources."""
        return [r for r in self.resources if r.adoption_mode == AdoptionMode.CONNECT]

    # ------------------------------------------------------------------
    # Readiness checks
    # ------------------------------------------------------------------

    def active_billable_count(self) -> int:
        """Number of managed resources that are still active/provisioning."""
        return sum(1 for r in self.resources if r.is_billable_active())

    def cleanup_incomplete(self) -> list[ResourceRecord]:
        """Managed resources that are NOT fully torn down."""
        return [
            r
            for r in self.managed_resources()
            if r.status not in (ResourceStatus.TORN_DOWN,)
        ]

    def teardown_failures(self) -> list[ResourceRecord]:
        return [
            r for r in self.resources if r.status == ResourceStatus.TEARDOWN_FAILED
        ]

    def is_cleanup_complete(self) -> bool:
        """True when no managed resource remains active or in a failed teardown."""
        return len(self.cleanup_incomplete()) == 0

    def to_safe_dict(self) -> dict[str, Any]:
        """Return a serialisable dict safe for logging (no secrets; resource IDs kept)."""
        return {
            "environment": self.environment,
            "resources": [
                {
                    "resource_id": r.resource_id,
                    "resource_kind": r.resource_kind,
                    "adoption_mode": r.adoption_mode,
                    "environment": r.environment,
                    "status": r.status,
                    "arm_or_fabric_id": r.arm_or_fabric_id,
                    "display_name": r.display_name,
                    "estimated_cost_units": r.estimated_cost_units,
                    "tags": r.tags,
                    "cleanup_error": r.cleanup_error,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "deleted_at": r.deleted_at.isoformat() if r.deleted_at else None,
                }
                for r in self.resources
            ],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_safe_dict(), indent=indent, default=str)
