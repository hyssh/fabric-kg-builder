"""Tests for infra/plan.py — plan helpers and plan generation."""
from __future__ import annotations

from pathlib import Path

import pytest

from fabric_kg_builder.infra.plan import (
    _collect_cost_bearing_skus,
    _collect_prereqs,
    _configured_resource_name,
    _state_resource_name,
    build_plan,
    load_plan,
    save_plan,
)
from fabric_kg_builder.infra.schema import (
    InfraManifest,
    InfraPlan,
    PlanAction,
    PlanItem,
)


# ---------------------------------------------------------------------------
# Helper: minimal manifest
# ---------------------------------------------------------------------------


def _make_manifest(**overrides) -> InfraManifest:
    base = {
        "environment": "dev",
        "azure": {"subscription_id": "sub-001"},
    }
    base.update(overrides)
    return InfraManifest.model_validate(base)


# ---------------------------------------------------------------------------
# _state_resource_name
# ---------------------------------------------------------------------------


class TestStateResourceName:
    def test_extracts_terminal_segment(self):
        arm_id = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/mystorage"
        assert _state_resource_name(arm_id) == "mystorage"

    def test_simple_name_unchanged(self):
        assert _state_resource_name("my-storage") == "my-storage"

    def test_trailing_slash_stripped(self):
        assert _state_resource_name("storage/") == "storage"

    def test_empty_string(self):
        assert _state_resource_name("") == ""


# ---------------------------------------------------------------------------
# _configured_resource_name
# ---------------------------------------------------------------------------


class TestConfiguredResourceName:
    def test_uses_configured_name_when_present(self):
        result = _configured_resource_name("my-storage", None, "storage", "dev")
        assert result == "my-storage"

    def test_extracts_from_resource_id(self):
        arm_id = "/subscriptions/sub/resourceGroups/rg/providers/Type/resources/actual-name"
        result = _configured_resource_name(None, arm_id, "storage", "dev")
        assert result == "actual-name"

    def test_generates_default_name_when_no_id(self):
        result = _configured_resource_name(None, None, "storage", "dev")
        assert isinstance(result, str)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# _collect_cost_bearing_skus
# ---------------------------------------------------------------------------


class TestCollectCostBearingSkus:
    def test_empty_items(self):
        assert _collect_cost_bearing_skus([]) == []

    def test_includes_cost_bearing_with_sku(self):
        item = PlanItem(
            resource_type="Microsoft.Search/searchServices",
            resource_name="mysearch",
            action=PlanAction.CREATE,
            cost_bearing=True,
            sku="S1",
        )
        result = _collect_cost_bearing_skus([item])
        assert len(result) == 1
        assert "S1" in result[0]

    def test_excludes_no_cost(self):
        item = PlanItem(
            resource_type="Microsoft.Resources/resourceGroups",
            resource_name="rg-dev",
            action=PlanAction.CREATE,
            cost_bearing=False,
            sku=None,
        )
        assert _collect_cost_bearing_skus([item]) == []

    def test_excludes_no_sku(self):
        item = PlanItem(
            resource_type="Some/Type",
            resource_name="name",
            action=PlanAction.CREATE,
            cost_bearing=True,
            sku=None,
        )
        assert _collect_cost_bearing_skus([item]) == []


# ---------------------------------------------------------------------------
# _collect_prereqs
# ---------------------------------------------------------------------------


class TestCollectPrereqs:
    def test_empty_items(self):
        assert _collect_prereqs([]) == []

    def test_deduplicates_prereqs(self):
        items = [
            PlanItem(
                resource_type="A",
                resource_name="r1",
                action=PlanAction.CREATE,
                cost_bearing=False,
                prereqs=["Microsoft.Storage", "Microsoft.Search"],
            ),
            PlanItem(
                resource_type="B",
                resource_name="r2",
                action=PlanAction.CREATE,
                cost_bearing=False,
                prereqs=["Microsoft.Storage"],  # duplicate
            ),
        ]
        result = _collect_prereqs(items)
        assert result.count("Microsoft.Storage") == 1
        assert "Microsoft.Search" in result

    def test_preserves_order(self):
        items = [
            PlanItem(
                resource_type="A",
                resource_name="r1",
                action=PlanAction.CREATE,
                cost_bearing=False,
                prereqs=["first", "second"],
            ),
        ]
        result = _collect_prereqs(items)
        assert result.index("first") < result.index("second")


# ---------------------------------------------------------------------------
# build_plan
# ---------------------------------------------------------------------------


class TestBuildPlan:
    def test_returns_infra_plan(self):
        manifest = _make_manifest()
        plan = build_plan(manifest)
        assert isinstance(plan, InfraPlan)

    def test_plan_has_items(self):
        manifest = _make_manifest()
        plan = build_plan(manifest)
        assert len(plan.items) > 0

    def test_environment_in_plan(self):
        manifest = _make_manifest()
        plan = build_plan(manifest)
        assert plan.environment == "dev"

    def test_has_rbac_assignments(self):
        manifest = _make_manifest()
        plan = build_plan(manifest)
        assert isinstance(plan.rbac_assignments, list)

    def test_has_prereqs(self):
        manifest = _make_manifest()
        plan = build_plan(manifest)
        assert isinstance(plan.prereqs, list)


# ---------------------------------------------------------------------------
# save_plan / load_plan
# ---------------------------------------------------------------------------


class TestSaveLoadPlan:
    def test_round_trip(self, tmp_path):
        manifest = _make_manifest()
        plan = build_plan(manifest)
        path = tmp_path / "plan.json"

        save_plan(plan, path)
        assert path.exists()

        loaded = load_plan(path)
        assert isinstance(loaded, InfraPlan)
        assert loaded.environment == plan.environment
        assert len(loaded.items) == len(plan.items)

    def test_load_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_plan(tmp_path / "nonexistent.json")

    def test_creates_parent_dir(self, tmp_path):
        manifest = _make_manifest()
        plan = build_plan(manifest)
        path = tmp_path / "subdir" / "plan.json"
        save_plan(plan, path)
        assert path.exists()
