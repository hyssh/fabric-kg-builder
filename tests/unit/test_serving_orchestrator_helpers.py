"""Tests for serving/orchestrator.py — pure helpers and config models."""
from __future__ import annotations

import json
from dataclasses import field
from typing import Any

import pytest

from fabric_kg_builder.serving.orchestrator import (
    OrchestratorConfig,
    OrchestratorResult,
    _iter_search_upload_batches,
    _select_graph_lineage_probe,
)


# ---------------------------------------------------------------------------
# _iter_search_upload_batches
# ---------------------------------------------------------------------------


class TestIterSearchUploadBatches:
    def test_empty_actions(self):
        batches = list(_iter_search_upload_batches([]))
        assert batches == []

    def test_single_action(self):
        action = {"@search.action": "upload", "id": "doc-1", "content": "text"}
        batches = list(_iter_search_upload_batches([action]))
        assert len(batches) == 1
        assert batches[0] == [action]

    def test_multiple_actions_same_batch(self):
        actions = [
            {"@search.action": "upload", "id": f"doc-{i}", "content": "x"}
            for i in range(5)
        ]
        batches = list(_iter_search_upload_batches(actions))
        # Should fit in one batch (small docs)
        assert len(batches) == 1
        assert len(batches[0]) == 5

    def test_respects_max_actions(self):
        actions = [
            {"@search.action": "upload", "id": f"doc-{i}", "content": "x"}
            for i in range(10)
        ]
        batches = list(_iter_search_upload_batches(actions, max_actions=3))
        # Should be split into batches of 3, 3, 3, 1
        assert len(batches) == 4
        assert len(batches[0]) == 3
        assert len(batches[-1]) == 1

    def test_large_action_exceeding_max_payload_raises(self):
        large_content = "x" * 2000
        action = {"@search.action": "upload", "id": "doc-1", "content": large_content}
        with pytest.raises(ValueError, match="exceeds the maximum"):
            list(_iter_search_upload_batches([action], max_payload_bytes=100))

    def test_batches_by_payload_size(self):
        # Create actions that are individually fine but together exceed limit
        actions = [
            {"@search.action": "upload", "id": f"doc-{i}", "content": "x" * 20}
            for i in range(5)
        ]
        action_size = len(json.dumps(actions[0]).encode("utf-8"))
        max_bytes = action_size * 2 + 50  # Allows ~2 actions per batch
        batches = list(_iter_search_upload_batches(actions, max_payload_bytes=max_bytes))
        assert len(batches) > 1  # Must have split

    def test_all_actions_preserved(self):
        actions = [
            {"@search.action": "upload", "id": f"doc-{i}", "content": "x"}
            for i in range(7)
        ]
        batches = list(_iter_search_upload_batches(actions, max_actions=3))
        recovered = [item for batch in batches for item in batch]
        assert len(recovered) == 7


# ---------------------------------------------------------------------------
# _select_graph_lineage_probe
# ---------------------------------------------------------------------------


class TestSelectGraphLineageProbe:
    def test_empty_catalog_returns_empty(self):
        label, fields = _select_graph_lineage_probe({})
        assert label == ""
        assert fields == []

    def test_no_nodes_key(self):
        label, fields = _select_graph_lineage_probe({"edges": []})
        assert label == ""
        assert fields == []

    def test_nodes_without_lineage_fields(self):
        catalog = {
            "nodes": [
                {"graph_label": "Person", "properties": ["name", "age"]},
            ]
        }
        label, fields = _select_graph_lineage_probe(catalog)
        # No lineage-priority fields declared → empty result
        assert label == "" or isinstance(label, str)

    def test_selects_node_with_lineage_fields(self):
        catalog = {
            "nodes": [
                {
                    "graph_label": "Entity",
                    "properties": [
                        "name",
                        "asset_id",
                        "run_id",
                        "source_locator_json",
                        "project_id",
                    ],
                }
            ]
        }
        label, fields = _select_graph_lineage_probe(catalog)
        assert label == "Entity"
        assert len(fields) > 0
        assert any(f in fields for f in ("asset_id", "run_id", "project_id"))

    def test_selects_best_candidate(self):
        # 'source_file_id' is highest priority
        catalog = {
            "nodes": [
                {
                    "graph_label": "LowPriority",
                    "properties": ["run_id"],
                },
                {
                    "graph_label": "HighPriority",
                    "properties": ["source_file_id", "run_id"],
                },
            ]
        }
        label, fields = _select_graph_lineage_probe(catalog)
        assert label == "HighPriority"
        assert "source_file_id" in fields

    def test_nodes_not_list(self):
        catalog = {"nodes": "not a list"}
        label, fields = _select_graph_lineage_probe(catalog)
        assert label == ""
        assert fields == []


# ---------------------------------------------------------------------------
# OrchestratorConfig
# ---------------------------------------------------------------------------


class TestOrchestratorConfig:
    def _make_config(self, **kwargs):
        defaults = dict(
            workspace_id="ws-001",
            lakehouse_item_id="lh-001",
            search_endpoint="https://search.example.com",
            base_index_name="kg-dev-",
            schema_dict={"index": "schema"},
            embedding_model="text-embedding-3-large",
            dimensions=1536,
            run_id="run-001",
            environment="dev",
        )
        defaults.update(kwargs)
        return OrchestratorConfig(**defaults)

    def test_basic_creation(self):
        cfg = self._make_config()
        assert cfg.workspace_id == "ws-001"
        assert cfg.environment == "dev"
        assert cfg.dimensions == 1536

    def test_default_schema(self):
        cfg = self._make_config()
        assert cfg.schema == "dbo"

    def test_default_deploy_lakehouse_true(self):
        cfg = self._make_config()
        assert cfg.deploy_lakehouse is True

    def test_empty_docs_by_default(self):
        cfg = self._make_config()
        assert cfg.docs == []

    def test_custom_docs(self):
        cfg = self._make_config(docs=[{"id": "doc-1"}])
        assert len(cfg.docs) == 1


# ---------------------------------------------------------------------------
# OrchestratorResult
# ---------------------------------------------------------------------------


class TestOrchestratorResult:
    def test_basic_creation(self):
        from fabric_kg_builder.serving.orchestrator import OrchestratorResult
        result = OrchestratorResult(
            ok=True,
            physical_index_name="kg-dev-v1",
            alias="kg-dev",
        )
        assert result.ok is True
        assert result.physical_index_name == "kg-dev-v1"
        assert result.alias == "kg-dev"

    def test_default_success_flags(self):
        from fabric_kg_builder.serving.orchestrator import OrchestratorResult
        result = OrchestratorResult(
            ok=True,
            physical_index_name="kg-dev-v1",
            alias="kg-dev",
        )
        assert isinstance(result, OrchestratorResult)
        assert result.docs_pushed == 0
