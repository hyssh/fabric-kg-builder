"""Tests for infra/schema.py — Pydantic models for infrastructure manifest."""
from __future__ import annotations

import pytest

from fabric_kg_builder.infra.schema import (
    AzureConfig,
    CompatibilityProbeResult,
    FabricConfig,
    FabricGraphModelConfig,
    FabricItemConfig,
    FabricLakehouseConfig,
    FabricOntologyConfig,
    FeaturesConfig,
    IdentityConfig,
    IdentityMode,
    InfraPlan,
    InfraManifest,
    InfraState,
    ModelCapacityInfo,
    ModelDiscoveryResult,
    PlanAction,
    PlanItem,
    PreflightCheck,
    PreflightResult,
    PreflightStatus,
    RBACAssignment,
    ResourceMode,
    ResourcesConfig,
    INFRA_SCHEMA_VERSION,
)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TestEnums:
    def test_resource_mode_values(self):
        assert ResourceMode.CREATE.value == "create"
        assert ResourceMode.CONNECT.value == "connect"

    def test_identity_mode_values(self):
        assert IdentityMode.USER_ASSIGNED.value == "user-assigned"
        assert IdentityMode.NONE.value == "none"

    def test_plan_action_values(self):
        assert PlanAction.CREATE.value == "create"
        assert PlanAction.DESTROY.value == "destroy"
        assert PlanAction.NO_OP.value == "no-op"

    def test_preflight_status_values(self):
        assert PreflightStatus.PASS.value == "pass"
        assert PreflightStatus.FAIL.value == "fail"
        assert PreflightStatus.WARN.value == "warn"


# ---------------------------------------------------------------------------
# AzureConfig
# ---------------------------------------------------------------------------

class TestAzureConfig:
    def test_required_fields(self):
        cfg = AzureConfig(subscription_id="sub-001")
        assert cfg.subscription_id == "sub-001"
        assert cfg.default_location == "eastus2"

    def test_resource_group_default(self):
        cfg = AzureConfig(subscription_id="sub-001")
        # resource_group has a default factory
        assert cfg.resource_group is not None

    def test_with_resource_group(self):
        from fabric_kg_builder.infra.schema import ResourceGroupConfig
        rg = ResourceGroupConfig(name="my-rg")
        cfg = AzureConfig(subscription_id="sub-001", resource_group=rg)
        assert cfg.resource_group.name == "my-rg"


# ---------------------------------------------------------------------------
# InfraManifest
# ---------------------------------------------------------------------------

def _minimal_manifest_dict() -> dict:
    return {
        "environment": "dev",
        "azure": {
            "subscription_id": "sub-abc",
        },
    }


class TestInfraManifest:
    def test_minimal_valid(self):
        manifest = InfraManifest.model_validate(_minimal_manifest_dict())
        assert manifest.environment == "dev"
        assert manifest.azure.subscription_id == "sub-abc"

    def test_schema_version_default(self):
        manifest = InfraManifest.model_validate(_minimal_manifest_dict())
        assert manifest.schema_version == INFRA_SCHEMA_VERSION

    def test_wrong_schema_version_raises(self):
        d = _minimal_manifest_dict()
        d["schema_version"] = "0.0"
        with pytest.raises(Exception, match="schema_version"):
            InfraManifest.model_validate(d)

    def test_managed_by_default(self):
        manifest = InfraManifest.model_validate(_minimal_manifest_dict())
        assert manifest.managed_by == "fabric-kg-builder"

    def test_features_defaults(self):
        manifest = InfraManifest.model_validate(_minimal_manifest_dict())
        assert isinstance(manifest.features, FeaturesConfig)

    def test_resources_defaults(self):
        manifest = InfraManifest.model_validate(_minimal_manifest_dict())
        assert isinstance(manifest.resources, ResourcesConfig)

    def test_fabric_defaults(self):
        manifest = InfraManifest.model_validate(_minimal_manifest_dict())
        assert isinstance(manifest.fabric, FabricConfig)


# ---------------------------------------------------------------------------
# PreflightCheck and PreflightResult
# ---------------------------------------------------------------------------

class TestPreflightCheck:
    def test_basic_check(self):
        check = PreflightCheck(
            name="azure-subscription",
            status=PreflightStatus.PASS,
            message="Subscription found.",
        )
        assert check.name == "azure-subscription"
        assert check.status == PreflightStatus.PASS
        assert check.action is None

    def test_failed_check_with_action(self):
        check = PreflightCheck(
            name="storage",
            status=PreflightStatus.FAIL,
            message="Storage not found.",
            action="Create storage account.",
        )
        assert check.action == "Create storage account."


class TestPreflightResult:
    def test_passed_when_no_fails(self):
        result = PreflightResult(
            environment="dev",
            checks=[
                PreflightCheck(name="c1", status=PreflightStatus.PASS, message="ok"),
                PreflightCheck(name="c2", status=PreflightStatus.WARN, message="warn"),
            ],
        )
        assert result.passed is True

    def test_not_passed_when_has_fail(self):
        result = PreflightResult(
            environment="dev",
            checks=[
                PreflightCheck(name="c1", status=PreflightStatus.FAIL, message="fail"),
            ],
        )
        assert result.passed is False

    def test_failed_checks_property(self):
        result = PreflightResult(
            environment="dev",
            checks=[
                PreflightCheck(name="c1", status=PreflightStatus.FAIL, message="fail1"),
                PreflightCheck(name="c2", status=PreflightStatus.PASS, message="ok"),
                PreflightCheck(name="c3", status=PreflightStatus.FAIL, message="fail2"),
            ],
        )
        assert len(result.failed_checks) == 2

    def test_warned_checks_property(self):
        result = PreflightResult(
            environment="dev",
            checks=[
                PreflightCheck(name="c1", status=PreflightStatus.WARN, message="w1"),
                PreflightCheck(name="c2", status=PreflightStatus.PASS, message="ok"),
            ],
        )
        assert len(result.warned_checks) == 1
        assert result.warned_checks[0].name == "c1"

    def test_empty_checks_passes(self):
        result = PreflightResult(environment="dev")
        assert result.passed is True
        assert result.failed_checks == []
        assert result.warned_checks == []


# ---------------------------------------------------------------------------
# PlanItem and InfraPlan
# ---------------------------------------------------------------------------

class TestPlanItem:
    def test_basic_item(self):
        item = PlanItem(
            resource_type="storage",
            resource_name="mystorage",
            action=PlanAction.CREATE,
        )
        assert item.action == PlanAction.CREATE
        assert item.cost_bearing is False
        assert item.prereqs == []

    def test_with_prereqs_and_sku(self):
        item = PlanItem(
            resource_type="foundry",
            resource_name="myfoundry",
            action=PlanAction.ADOPT,
            cost_bearing=True,
            sku="GlobalStandard",
            prereqs=["storage"],
        )
        assert item.cost_bearing is True
        assert "storage" in item.prereqs


class TestInfraPlan:
    def test_has_creates(self):
        plan = InfraPlan(
            environment="dev",
            items=[
                PlanItem(resource_type="storage", resource_name="s", action=PlanAction.CREATE),
            ],
        )
        assert plan.has_creates is True

    def test_has_no_creates(self):
        plan = InfraPlan(
            environment="dev",
            items=[
                PlanItem(resource_type="storage", resource_name="s", action=PlanAction.ADOPT),
            ],
        )
        assert plan.has_creates is False

    def test_has_destroys(self):
        plan = InfraPlan(
            environment="dev",
            items=[
                PlanItem(resource_type="storage", resource_name="s", action=PlanAction.DESTROY),
            ],
        )
        assert plan.has_destroys is True

    def test_empty_plan(self):
        plan = InfraPlan(environment="dev")
        assert plan.has_creates is False
        assert plan.has_destroys is False
        assert plan.items == []

    def test_rbac_assignments(self):
        rbac = RBACAssignment(
            principal_type="ServicePrincipal",
            role_name="Storage Blob Data Contributor",
            scope="/subscriptions/abc",
            description="Allow read/write to blobs",
        )
        plan = InfraPlan(environment="dev", rbac_assignments=[rbac])
        assert len(plan.rbac_assignments) == 1


# ---------------------------------------------------------------------------
# InfraState
# ---------------------------------------------------------------------------

class TestInfraState:
    def test_defaults(self):
        state = InfraState(environment="dev")
        assert state.managed_resource_ids == {}
        assert state.adopted_resource_ids == {}
        assert state.outputs == {}
        assert state.last_operation is None

    def test_with_outputs(self):
        state = InfraState(
            environment="dev",
            outputs={"storage_account_name": "mystorage"},
            last_operation="apply",
            last_operation_status="succeeded",
        )
        assert state.outputs["storage_account_name"] == "mystorage"
        assert state.last_operation == "apply"


# ---------------------------------------------------------------------------
# CompatibilityProbeResult
# ---------------------------------------------------------------------------

class TestCompatibilityProbeResult:
    def test_ok_when_no_errors(self):
        result = CompatibilityProbeResult(
            resource_type="storage",
            resource_name="mystorage",
            mode=ResourceMode.CREATE,
        )
        assert result.ok is True

    def test_not_ok_when_errors(self):
        result = CompatibilityProbeResult(
            resource_type="storage",
            resource_name="mystorage",
            mode=ResourceMode.CREATE,
            errors=["Connection refused"],
        )
        assert result.ok is False

    def test_with_warnings(self):
        result = CompatibilityProbeResult(
            resource_type="storage",
            resource_name="mystorage",
            mode=ResourceMode.CONNECT,
            warnings=["SKU not optimal"],
        )
        assert result.ok is True  # warnings don't block


# ---------------------------------------------------------------------------
# FabricConfig and sub-items
# ---------------------------------------------------------------------------

class TestFabricItemConfig:
    def test_lakehouse_name(self):
        cfg = FabricLakehouseConfig(name="MyLakehouse")
        assert cfg.name == "MyLakehouse"

    def test_ontology_name(self):
        cfg = FabricOntologyConfig(name="MyOntology")
        assert cfg.name == "MyOntology"

    def test_graph_model_name(self):
        cfg = FabricGraphModelConfig(name="MyGraph")
        assert cfg.name == "MyGraph"


# ---------------------------------------------------------------------------
# ModelCapacityInfo and ModelDiscoveryResult
# ---------------------------------------------------------------------------

class TestModelCapacityInfo:
    def test_basic(self):
        info = ModelCapacityInfo(
            model="gpt-4o",
            sku="GlobalStandard",
            subscription_id="sub-001",
            location="eastus",
            deployable=True,
        )
        assert info.model == "gpt-4o"
        assert info.deployable is True

    def test_not_deployable_default(self):
        info = ModelCapacityInfo(
            model="gpt-4o",
            sku="GlobalStandard",
            subscription_id="sub-001",
            location="eastus",
        )
        assert info.deployable is False


class TestModelDiscoveryResult:
    def test_all_deployable_false_by_default(self):
        result = ModelDiscoveryResult(subscription_id="sub-001", location="eastus")
        assert result.all_deployable is False
        assert result.errors == []

    def test_with_models(self):
        chat = ModelCapacityInfo(
            model="gpt-4o", sku="GlobalStandard",
            subscription_id="sub-001", location="eastus", deployable=True
        )
        result = ModelDiscoveryResult(
            subscription_id="sub-001", location="eastus",
            chat_model=chat, all_deployable=True
        )
        assert result.all_deployable is True
        assert result.chat_model.model == "gpt-4o"
