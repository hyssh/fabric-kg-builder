"""Pydantic models for the infrastructure manifest (infra/environments/<env>.yaml).

SPEC-006 §5.1 — INF-001.

All values are non-secret. Secrets are referenced via ${ENV_VAR} or Key Vault
URIs and must never appear in outputs or manifests.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .names import (
    validate_fabric_graph_model_name,
    validate_fabric_identifier_name,
)


INFRA_SCHEMA_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ResourceMode(str, Enum):
    """Whether the resource should be created or an existing one adopted."""
    CREATE = "create"
    CONNECT = "connect"


class IdentityMode(str, Enum):
    USER_ASSIGNED = "user-assigned"
    SYSTEM_ASSIGNED = "system-assigned"
    NONE = "none"


class StorageSku(str, Enum):
    STANDARD_LRS = "Standard_LRS"
    STANDARD_GRS = "Standard_GRS"
    STANDARD_ZRS = "Standard_ZRS"
    PREMIUM_LRS = "Premium_LRS"


class DocumentIntelligenceSku(str, Enum):
    S0 = "S0"
    F0 = "F0"


class SearchSku(str, Enum):
    FREE = "free"
    BASIC = "basic"
    STANDARD = "standard"
    STANDARD2 = "standard2"
    STANDARD3 = "standard3"
    STORAGE_OPTIMIZED_L1 = "storage_optimized_l1"
    STORAGE_OPTIMIZED_L2 = "storage_optimized_l2"


class SemanticRanker(str, Enum):
    DISABLED = "disabled"
    FREE = "free"
    STANDARD = "standard"


class ModelSku(str, Enum):
    GLOBAL_STANDARD = "GlobalStandard"
    STANDARD = "Standard"
    PROVISIONED_MANAGED = "ProvisionedManaged"


# ---------------------------------------------------------------------------
# Azure sub-configs
# ---------------------------------------------------------------------------


class ResourceGroupConfig(BaseModel):
    """Target resource group — create or connect."""
    mode: ResourceMode = ResourceMode.CREATE
    name: Optional[str] = None
    location: Optional[str] = None


class AzureConfig(BaseModel):
    """Azure subscription and top-level resource group configuration."""
    subscription_id: str = Field(
        description="Azure subscription ID (use ${AZURE_SUBSCRIPTION_ID})."
    )
    resource_group: ResourceGroupConfig = Field(default_factory=ResourceGroupConfig)
    default_location: str = Field(
        default="eastus2",
        description="Default Azure region for new resources.",
    )
    tags: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


class IdentityConfig(BaseModel):
    """User-assigned or system-assigned managed identity preference."""
    mode: IdentityMode = IdentityMode.USER_ASSIGNED
    name: Optional[str] = None
    resource_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


class StorageResourceConfig(BaseModel):
    """Azure Blob Storage with hierarchical namespace (ADLS Gen2)."""
    mode: ResourceMode = ResourceMode.CREATE
    name: Optional[str] = None
    resource_id: Optional[str] = None
    sku: StorageSku = StorageSku.STANDARD_LRS
    hierarchical_namespace: bool = True
    container: str = "kg-assets"
    retention_days: int = Field(default=365, ge=1, le=36500)

    @model_validator(mode="after")
    def _require_id_when_connect(self) -> "StorageResourceConfig":
        if self.mode == ResourceMode.CONNECT and not (self.resource_id or self.name):
            raise ValueError(
                "storage.connect requires either resource_id or name."
            )
        return self


# ---------------------------------------------------------------------------
# Document Intelligence
# ---------------------------------------------------------------------------


class DocumentIntelligenceConfig(BaseModel):
    """Azure AI Document Intelligence resource."""
    mode: ResourceMode = ResourceMode.CREATE
    name: Optional[str] = None
    resource_id: Optional[str] = None
    sku: DocumentIntelligenceSku = DocumentIntelligenceSku.S0

    @model_validator(mode="after")
    def _require_id_when_connect(self) -> "DocumentIntelligenceConfig":
        if self.mode == ResourceMode.CONNECT and not (self.resource_id or self.name):
            raise ValueError(
                "document_intelligence.connect requires either resource_id or name."
            )
        return self


# ---------------------------------------------------------------------------
# Foundry / Azure AI Services
# ---------------------------------------------------------------------------


class ModelDeploymentConfig(BaseModel):
    """Single model deployment configuration."""
    model: str = Field(description="Model name, e.g. gpt-4.1 or text-embedding-3-large.")
    deployment_name: Optional[str] = Field(
        default=None,
        description=(
            "Optional for create mode; required explicitly and non-empty when "
            "connecting to an existing Foundry account."
        ),
    )
    sku: ModelSku = ModelSku.GLOBAL_STANDARD
    target_tpm: Optional[int] = Field(
        default=None,
        ge=1000,
        description="Target tokens-per-minute capacity. Must be a multiple of 1000.",
    )
    dimensions: Optional[int] = Field(
        default=None,
        description="Embedding dimensions (embedding models only).",
    )

    @field_validator("target_tpm")
    @classmethod
    def _tpm_multiple_of_1000(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v % 1000 != 0:
            raise ValueError(f"target_tpm must be a multiple of 1000; got {v}.")
        return v


class FoundryModelsConfig(BaseModel):
    """Chat and embedding model deployment targets."""
    chat: ModelDeploymentConfig = Field(
        default_factory=lambda: ModelDeploymentConfig(
            model="gpt-4.1",
            sku=ModelSku.GLOBAL_STANDARD,
            target_tpm=200000,
        )
    )
    embedding: ModelDeploymentConfig = Field(
        default_factory=lambda: ModelDeploymentConfig(
            model="text-embedding-3-large",
            dimensions=1536,
        )
    )


class FoundryResourceConfig(BaseModel):
    """Azure AI Foundry (Microsoft.CognitiveServices/accounts kind=AIServices)."""
    mode: ResourceMode = ResourceMode.CREATE
    name: Optional[str] = None
    resource_id: Optional[str] = None
    project_name: str = "kg-dev"
    models: FoundryModelsConfig = Field(default_factory=FoundryModelsConfig)

    @model_validator(mode="after")
    def _require_id_when_connect(self) -> "FoundryResourceConfig":
        if self.mode == ResourceMode.CONNECT and not (self.resource_id or self.name):
            raise ValueError(
                "foundry.connect requires either resource_id or name."
            )
        if self.mode == ResourceMode.CONNECT:
            required_fields = {
                "project_name": self.project_name,
                "chat deployment_name": self.models.chat.deployment_name,
                "embedding deployment_name": self.models.embedding.deployment_name,
            }
            missing = [
                field for field, value in required_fields.items()
                if not isinstance(value, str) or not value.strip()
            ]
            explicitly_defaulted = (
                "project_name" not in self.model_fields_set
                or "models" not in self.model_fields_set
                or "chat" not in self.models.model_fields_set
                or "embedding" not in self.models.model_fields_set
            )
            if missing or explicitly_defaulted:
                raise ValueError(
                    "foundry.connect requires explicit project_name and explicit "
                    "non-empty chat and embedding deployment_name values."
                )
        return self


# ---------------------------------------------------------------------------
# AI Search
# ---------------------------------------------------------------------------


class SearchResourceConfig(BaseModel):
    """Azure AI Search service."""
    mode: ResourceMode = ResourceMode.CREATE
    name: Optional[str] = None
    resource_id: Optional[str] = None
    sku: SearchSku = SearchSku.STANDARD
    semantic_ranker: SemanticRanker = SemanticRanker.STANDARD

    @model_validator(mode="after")
    def _require_id_when_connect(self) -> "SearchResourceConfig":
        if self.mode == ResourceMode.CONNECT and not (self.resource_id or self.name):
            raise ValueError(
                "search.connect requires either resource_id or name."
            )
        return self


# ---------------------------------------------------------------------------
# Container registry
# ---------------------------------------------------------------------------


class ContainerRegistryResourceConfig(BaseModel):
    """Azure Container Registry for the deployed reference application."""

    mode: ResourceMode = ResourceMode.CREATE
    name: Optional[str] = None
    resource_id: Optional[str] = None
    sku: Literal["Basic", "Standard", "Premium"] = "Basic"

    @model_validator(mode="after")
    def _require_id_when_connect(self) -> "ContainerRegistryResourceConfig":
        if self.mode == ResourceMode.CONNECT and not (
            self.resource_id or self.name
        ):
            raise ValueError(
                "container_registry.connect requires either resource_id or name."
            )
        return self


# ---------------------------------------------------------------------------
# Resources aggregate
# ---------------------------------------------------------------------------


class ResourcesConfig(BaseModel):
    """All Azure infrastructure resources."""
    storage: StorageResourceConfig = Field(default_factory=StorageResourceConfig)
    document_intelligence: DocumentIntelligenceConfig = Field(
        default_factory=DocumentIntelligenceConfig
    )
    foundry: FoundryResourceConfig = Field(default_factory=FoundryResourceConfig)
    search: SearchResourceConfig = Field(default_factory=SearchResourceConfig)
    container_registry: ContainerRegistryResourceConfig = Field(
        default_factory=ContainerRegistryResourceConfig
    )


# ---------------------------------------------------------------------------
# Fabric
# ---------------------------------------------------------------------------


class FabricItemConfig(BaseModel):
    """Fabric workspace item that can be created or connected."""
    mode: ResourceMode = ResourceMode.CREATE
    name: Optional[str] = None
    item_id: Optional[str] = None
    display_name: Optional[str] = None

    @model_validator(mode="after")
    def _require_id_when_connect(self) -> "FabricItemConfig":
        if self.mode == ResourceMode.CONNECT and not (self.item_id or self.name or self.display_name):
            raise ValueError(
                "Fabric item connect requires item_id, name, or display_name."
            )
        return self


class FabricLakehouseConfig(FabricItemConfig):
    """Fabric Lakehouse with optional schema enablement."""
    enable_schemas: bool = True

    @model_validator(mode="after")
    def _validate_create_name(self) -> "FabricLakehouseConfig":
        if self.mode == ResourceMode.CREATE:
            name = self.name or self.display_name
            if name:
                validate_fabric_identifier_name(name, "Lakehouse")
        return self


class FabricOntologyConfig(FabricItemConfig):
    """Fabric Ontology item with identifier-style creation naming rules."""

    @model_validator(mode="after")
    def _validate_create_name(self) -> "FabricOntologyConfig":
        if self.mode == ResourceMode.CREATE:
            name = self.display_name or self.name
            if name:
                validate_fabric_identifier_name(name, "Ontology")
        return self


class FabricGraphModelConfig(FabricItemConfig):
    """Fabric Graph model item with display-name validation."""

    @model_validator(mode="after")
    def _validate_create_name(self) -> "FabricGraphModelConfig":
        if self.mode == ResourceMode.CREATE:
            name = self.display_name or self.name
            if name:
                validate_fabric_graph_model_name(name)
        return self


class FabricConfig(BaseModel):
    """Microsoft Fabric workspace and items."""
    capacity_id: Optional[str] = Field(
        default=None,
        description="Fabric capacity ID (use ${FABRIC_CAPACITY_ID}).",
    )
    workspace: FabricItemConfig = Field(
        default_factory=lambda: FabricItemConfig(mode=ResourceMode.CREATE, name="kg-dev")
    )
    lakehouse: FabricLakehouseConfig = Field(
        default_factory=lambda: FabricLakehouseConfig(
            mode=ResourceMode.CREATE, name="kg", enable_schemas=True
        )
    )
    ontology: FabricOntologyConfig = Field(
        default_factory=lambda: FabricOntologyConfig(
            mode=ResourceMode.CREATE, display_name="KG_Ontology"
        )
    )
    graph_model: FabricGraphModelConfig = Field(
        default_factory=lambda: FabricGraphModelConfig(
            mode=ResourceMode.CREATE, display_name="KG Graph"
        )
    )


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------


class FeaturesConfig(BaseModel):
    """Feature gates — all off by default; enable only in dev after validation."""
    foundry_iq: bool = False
    fabric_data_agent: bool = False
    graph: bool = False
    reference_app: bool = False


# ---------------------------------------------------------------------------
# Top-level manifest
# ---------------------------------------------------------------------------


class InfraManifest(BaseModel):
    """Non-secret infrastructure environment manifest.

    Loaded from infra/environments/<env>.yaml. Validated by Pydantic.
    Secrets are referenced as ${ENV_VAR} placeholders — never serialized.

    SPEC-006 §5.1 / INF-001.
    """
    schema_version: str = Field(default=INFRA_SCHEMA_VERSION)
    environment: str = Field(description="Environment name, e.g. dev, test, prod.")
    azure: AzureConfig
    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    resources: ResourcesConfig = Field(default_factory=ResourcesConfig)
    fabric: FabricConfig = Field(default_factory=FabricConfig)
    features: FeaturesConfig = Field(default_factory=FeaturesConfig)
    managed_by: str = Field(
        default="fabric-kg-builder",
        description="Ownership tag written to all created resources.",
    )

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, v: str) -> str:
        if v != INFRA_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported schema_version '{v}'; expected '{INFRA_SCHEMA_VERSION}'."
            )
        return v


# ---------------------------------------------------------------------------
# Typed result types for preflight and plan
# ---------------------------------------------------------------------------


class PreflightStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIP = "skip"


class PreflightCheck(BaseModel):
    """Result of a single preflight check."""
    name: str
    status: PreflightStatus
    message: str
    action: Optional[str] = Field(
        default=None,
        description="Recommended remediation action when status is fail or warn.",
    )
    details: Optional[dict] = None


class PreflightResult(BaseModel):
    """Aggregate result for all preflight checks."""
    environment: str
    checks: list[PreflightCheck] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True when no check has status=fail."""
        return all(c.status != PreflightStatus.FAIL for c in self.checks)

    @property
    def failed_checks(self) -> list[PreflightCheck]:
        return [c for c in self.checks if c.status == PreflightStatus.FAIL]

    @property
    def warned_checks(self) -> list[PreflightCheck]:
        return [c for c in self.checks if c.status == PreflightStatus.WARN]


class PlanAction(str, Enum):
    CREATE = "create"
    ADOPT = "adopt"
    UPDATE = "update"
    REPLACE = "replace"
    DESTROY = "destroy"
    NO_OP = "no-op"


class RBACAssignment(BaseModel):
    """A role assignment that will be applied during infra apply."""
    principal_type: str
    principal_id: Optional[str] = None
    role_name: str
    scope: str
    description: str


class PlanItem(BaseModel):
    """A single resource action in the infrastructure plan."""
    resource_type: str
    resource_name: str
    action: PlanAction
    cost_bearing: bool = False
    sku: Optional[str] = None
    location: Optional[str] = None
    prereqs: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    details: Optional[dict] = None


class InfraPlan(BaseModel):
    """Machine-readable infrastructure plan.

    SPEC-006 §4.1 / INF-012: lists creates, adopts, updates, replacements,
    RBAC, cost-bearing SKUs, and prerequisites.
    """
    schema_version: str = INFRA_SCHEMA_VERSION
    environment: str
    items: list[PlanItem] = Field(default_factory=list)
    rbac_assignments: list[RBACAssignment] = Field(default_factory=list)
    prereqs: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    cost_bearing_skus: list[str] = Field(default_factory=list)

    @property
    def has_creates(self) -> bool:
        return any(i.action == PlanAction.CREATE for i in self.items)

    @property
    def has_destroys(self) -> bool:
        return any(i.action == PlanAction.DESTROY for i in self.items)


class InfraState(BaseModel):
    """Persisted infrastructure operation state.

    Written to build/infra/<env>/state.json.  No secrets.
    SPEC-006 §4.1 / INF-013.
    """
    schema_version: str = INFRA_SCHEMA_VERSION
    environment: str
    last_operation: Optional[str] = None
    last_operation_id: Optional[str] = None
    last_operation_status: Optional[str] = None
    managed_resource_ids: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Map of resource_type -> exact ARM or Fabric item ID for resources "
            "created and owned by this environment."
        ),
    )
    adopted_resource_ids: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Map of resource_type -> exact ARM or Fabric item ID for connected "
            "resources that must not be deleted by this environment."
        ),
    )
    outputs: dict[str, str] = Field(
        default_factory=dict,
        description="Non-secret output values from the last successful apply.",
    )


class CompatibilityProbeResult(BaseModel):
    """Result of a create/connect compatibility probe."""
    resource_type: str
    resource_name: str
    mode: ResourceMode
    identity_ok: bool = False
    sku_ok: bool = False
    network_ok: bool = False
    rbac_ok: bool = False
    data_plane_ok: bool = False
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class ModelCapacityInfo(BaseModel):
    """Discovered capacity information for a model deployment."""
    model: str
    sku: str
    subscription_id: str
    location: str
    available_capacity: Optional[int] = None
    used_capacity: Optional[int] = None
    unit: Optional[str] = None
    deployable: bool = False
    reason: Optional[str] = None


class ModelDiscoveryResult(BaseModel):
    """Result of querying model availability and quota before deployment."""
    subscription_id: str
    location: str
    chat_model: Optional[ModelCapacityInfo] = None
    embedding_model: Optional[ModelCapacityInfo] = None
    all_deployable: bool = False
    errors: list[str] = Field(default_factory=list)
