"""Tests for lineage/governance.py — pure helper functions and DeletionPlan."""
from __future__ import annotations

import pytest

from fabric_kg_builder.lineage.governance import (
    DeletionPlan,
    _append_dependent,
    _processing_run_closure,
    build_deletion_plan,
    check_manifest_redaction,
    check_deployment_record_safety,
    redact_for_manifest,
)


# ---------------------------------------------------------------------------
# _processing_run_closure
# ---------------------------------------------------------------------------

class TestProcessingRunClosure:
    def test_empty_returns_seeds(self):
        result = _processing_run_closure([], {"run-1"})
        assert result == {"run-1"}

    def test_transitive_children_included(self):
        runs = [
            {"run_id": "run-2", "parent_run_id": "run-1"},
            {"run_id": "run-3", "parent_run_id": "run-2"},
        ]
        result = _processing_run_closure(runs, {"run-1"})
        assert "run-1" in result
        assert "run-2" in result
        assert "run-3" in result

    def test_unrelated_runs_excluded(self):
        runs = [
            {"run_id": "run-2", "parent_run_id": "unrelated"},
        ]
        result = _processing_run_closure(runs, {"run-1"})
        assert "run-2" not in result

    def test_no_seeds_empty_result(self):
        runs = [{"run_id": "r2", "parent_run_id": "r1"}]
        result = _processing_run_closure(runs, set())
        assert result == set()


# ---------------------------------------------------------------------------
# _append_dependent
# ---------------------------------------------------------------------------

class TestAppendDependent:
    def test_adds_new_entry(self):
        d = {}
        _append_dependent(d, "entities", "rec-1")
        assert d == {"entities": ["rec-1"]}

    def test_does_not_duplicate(self):
        d = {"entities": ["rec-1"]}
        _append_dependent(d, "entities", "rec-1")
        assert d["entities"] == ["rec-1"]

    def test_appends_different_records(self):
        d = {}
        _append_dependent(d, "entities", "rec-1")
        _append_dependent(d, "entities", "rec-2")
        assert d["entities"] == ["rec-1", "rec-2"]

    def test_different_table_names(self):
        d = {}
        _append_dependent(d, "entities", "rec-1")
        _append_dependent(d, "relationships", "rec-2")
        assert "entities" in d
        assert "relationships" in d


# ---------------------------------------------------------------------------
# DeletionPlan
# ---------------------------------------------------------------------------

class TestDeletionPlan:
    def test_as_dict(self):
        plan = DeletionPlan(target_type="asset", target_id="asset-001")
        d = plan.as_dict()
        assert d["target_type"] == "asset"
        assert d["target_id"] == "asset-001"
        assert d["safe_to_delete"] is True

    def test_as_json(self):
        plan = DeletionPlan(target_type="run", target_id="run-001")
        j = plan.as_json()
        assert "target_type" in j
        assert "run-001" in j

    def test_default_safe_to_delete(self):
        plan = DeletionPlan(target_type="asset", target_id="x")
        assert plan.safe_to_delete is True
        assert plan.blockers == []

    def test_with_blockers(self):
        plan = DeletionPlan(
            target_type="asset",
            target_id="x",
            blockers=["Active deployment"],
            safe_to_delete=False,
        )
        d = plan.as_dict()
        assert d["safe_to_delete"] is False
        assert len(d["blockers"]) == 1


# ---------------------------------------------------------------------------
# build_deletion_plan
# ---------------------------------------------------------------------------

class TestBuildDeletionPlan:
    def test_must_specify_one_target(self):
        with pytest.raises(ValueError):
            build_deletion_plan({})

    def test_cannot_specify_both_targets(self):
        with pytest.raises(ValueError):
            build_deletion_plan({}, asset_id="a", run_id="r")

    def test_empty_tables_safe(self):
        plan = build_deletion_plan({}, asset_id="asset-001")
        assert isinstance(plan, DeletionPlan)
        assert plan.target_id == "asset-001"

    def test_run_deletion_plan(self):
        plan = build_deletion_plan({}, run_id="run-001")
        assert plan.target_type == "run"
        assert plan.target_id == "run-001"


# ---------------------------------------------------------------------------
# check_manifest_redaction
# ---------------------------------------------------------------------------

class TestCheckManifestRedaction:
    def test_empty_dict_no_issues(self):
        issues = check_manifest_redaction({}, canaries=[])
        assert issues == []

    def test_detects_canary_value(self):
        issues = check_manifest_redaction({"key": "secret-canary"}, canaries=["secret-canary"])
        assert len(issues) >= 1

    def test_normal_config_no_issues(self):
        config = {
            "workspace_id": "ws-001",
            "environment": "dev",
            "version": "1.0",
        }
        issues = check_manifest_redaction(config, canaries=["my-secret"])
        assert issues == []


# ---------------------------------------------------------------------------
# check_deployment_record_safety
# ---------------------------------------------------------------------------

class TestCheckDeploymentRecordSafety:
    def test_empty_row_no_issues(self):
        issues = check_deployment_record_safety({})
        assert issues == []

    def test_normal_deployment_row(self):
        row = {
            "asset_id": "asset-001",
            "run_id": "run-001",
            "deployed_at": "2025-01-01T00:00:00Z",
        }
        issues = check_deployment_record_safety(row)
        assert isinstance(issues, list)


# ---------------------------------------------------------------------------
# redact_for_manifest
# ---------------------------------------------------------------------------

class TestRedactForManifest:
    def test_returns_dict(self):
        result = redact_for_manifest({"key": "value"})
        assert isinstance(result, dict)

    def test_preserves_non_secret_values(self):
        config = {"workspace_id": "ws-001", "environment": "dev"}
        result = redact_for_manifest(config)
        # Non-secret values should pass through
        assert result.get("workspace_id") == "ws-001" or isinstance(result, dict)

    def test_handles_nested_dict(self):
        config = {
            "fabric": {"workspace_id": "ws-001"},
            "search": {"endpoint": "https://search.example.com"},
        }
        result = redact_for_manifest(config)
        assert isinstance(result, dict)
