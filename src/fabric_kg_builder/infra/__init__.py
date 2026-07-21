"""fabric_kg_builder.infra — Infrastructure provisioning package.

Provides typed models, plan generation, apply orchestration, Fabric REST
clients, and preflight checks for M3 (INF-001..INF-019).

Public API (stable within this version):
- ``schema``         — Pydantic models for InfraManifest and typed results.
- ``manifest``       — YAML loader with env-var interpolation.
- ``names``          — Deterministic Azure-safe name generation.
- ``runner``         — CommandRunner protocol + FakeCommandRunner for tests.
- ``preflight``      — run_preflight() typed check suite.
- ``plan``           — build_plan(), save_plan(), load_plan().
- ``apply``          — apply_plan(), load_state(), save_state().
- ``model_discovery``— discover_model_capacity().
- ``fabric_client``  — FabricWorkspaceClient, FabricLakehouseClient, etc.
- ``destroy``        — build_destroy_plan(), execute_destroy().
"""

from fabric_kg_builder.infra.schema import (
    INFRA_SCHEMA_VERSION,
    AzureConfig,
    CompatibilityProbeResult,
    DocumentIntelligenceConfig,
    FabricConfig,
    FabricItemConfig,
    FabricLakehouseConfig,
    FeaturesConfig,
    FoundryModelsConfig,
    FoundryResourceConfig,
    IdentityConfig,
    IdentityMode,
    InfraManifest,
    InfraPlan,
    InfraState,
    ModelCapacityInfo,
    ModelDiscoveryResult,
    ModelDeploymentConfig,
    ModelSku,
    PlanAction,
    PlanItem,
    PreflightCheck,
    PreflightResult,
    PreflightStatus,
    RBACAssignment,
    ResourceGroupConfig,
    ResourceMode,
    ResourcesConfig,
    SearchResourceConfig,
    SearchSku,
    SemanticRanker,
    StorageResourceConfig,
    StorageSku,
)
from fabric_kg_builder.infra.manifest import (
    InfraManifestError,
    InfraManifestParseError,
    InfraManifestValidationError,
    load_manifest,
    default_manifest_path,
)

__all__ = [
    # schema
    "INFRA_SCHEMA_VERSION",
    "AzureConfig",
    "CompatibilityProbeResult",
    "DocumentIntelligenceConfig",
    "FabricConfig",
    "FabricItemConfig",
    "FabricLakehouseConfig",
    "FeaturesConfig",
    "FoundryModelsConfig",
    "FoundryResourceConfig",
    "IdentityConfig",
    "IdentityMode",
    "InfraManifest",
    "InfraPlan",
    "InfraState",
    "ModelCapacityInfo",
    "ModelDiscoveryResult",
    "ModelDeploymentConfig",
    "ModelSku",
    "PlanAction",
    "PlanItem",
    "PreflightCheck",
    "PreflightResult",
    "PreflightStatus",
    "RBACAssignment",
    "ResourceGroupConfig",
    "ResourceMode",
    "ResourcesConfig",
    "SearchResourceConfig",
    "SearchSku",
    "SemanticRanker",
    "StorageResourceConfig",
    "StorageSku",
    # manifest
    "InfraManifestError",
    "InfraManifestParseError",
    "InfraManifestValidationError",
    "load_manifest",
    "default_manifest_path",
]
