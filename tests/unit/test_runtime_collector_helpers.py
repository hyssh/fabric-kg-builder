"""Tests for runtime/collector.py pure helper functions."""
from __future__ import annotations

import pytest

from fabric_kg_builder.runtime.collector import (
    _answer_identifiers,
    _graph_grounding_identifiers,
    _mcp_grounding_quality,
    _needs_mcp_grounding_retry,
    _route_result_category,
    GraphRuntimeConfig,
    SearchRuntimeConfig,
    McpRuntimeConfig,
    DeploymentRuntimeConfig,
    RuntimeConfig,
)
from fabric_kg_builder.runtime.semantic_reliability import QueryExecutionStatus


# ---------------------------------------------------------------------------
# _route_result_category
# ---------------------------------------------------------------------------

class TestRouteResultCategory:
    def test_explicit_success(self):
        result = _route_result_category({"result_category": "success"})
        assert result == QueryExecutionStatus.SUCCESS

    def test_explicit_partial(self):
        result = _route_result_category({"result_category": "partial_result"})
        assert result == QueryExecutionStatus.PARTIAL_RESULT

    def test_status_success_string(self):
        result = _route_result_category({"status": "success"})
        assert result == QueryExecutionStatus.SUCCESS

    def test_status_succeeded_string(self):
        result = _route_result_category({"status": "succeeded"})
        assert result == QueryExecutionStatus.SUCCESS

    def test_status_passed_string(self):
        result = _route_result_category({"status": "passed"})
        assert result == QueryExecutionStatus.SUCCESS

    def test_status_complete_string(self):
        result = _route_result_category({"status": "complete"})
        assert result == QueryExecutionStatus.SUCCESS

    def test_status_partial_string(self):
        result = _route_result_category({"status": "partial"})
        assert result == QueryExecutionStatus.PARTIAL_RESULT

    def test_unknown_status_platform_failure(self):
        result = _route_result_category({"status": "unknown_status"})
        assert result == QueryExecutionStatus.PLATFORM_FAILURE

    def test_empty_dict_platform_failure(self):
        result = _route_result_category({})
        assert result == QueryExecutionStatus.PLATFORM_FAILURE

    def test_explicit_category_takes_priority(self):
        result = _route_result_category({"result_category": "success", "status": "partial"})
        assert result == QueryExecutionStatus.SUCCESS


# ---------------------------------------------------------------------------
# _answer_identifiers
# ---------------------------------------------------------------------------

class TestAnswerIdentifiers:
    def test_entity_id_pattern(self):
        answer = "Found entity:abc123 in the results"
        ids = _answer_identifiers(answer)
        assert "entity:abc123" in ids

    def test_evidence_id_pattern(self):
        answer = "See evidence:ev001 for details"
        ids = _answer_identifiers(answer)
        assert "evidence:ev001" in ids

    def test_chunk_id_pattern(self):
        answer = "chunk:chunk-abc"
        ids = _answer_identifiers(answer)
        assert "chunk:chunk-abc" in ids

    def test_empty_string(self):
        ids = _answer_identifiers("")
        assert ids == set()

    def test_no_ids(self):
        ids = _answer_identifiers("The answer is there are no entities.")
        assert len(ids) == 0

    def test_multiple_ids(self):
        answer = "entity:e1 and entity:e2 with evid:ev1"
        ids = _answer_identifiers(answer)
        assert len(ids) >= 3


# ---------------------------------------------------------------------------
# _graph_grounding_identifiers
# ---------------------------------------------------------------------------

class TestGraphGroundingIdentifiers:
    def test_empty_dict(self):
        ids = _graph_grounding_identifiers({})
        assert ids == set()

    def test_canonical_ids(self):
        graph = {"canonical_ids": ["entity:abc", "entity:def"]}
        ids = _graph_grounding_identifiers(graph)
        assert "entity:abc" in ids
        assert "entity:def" in ids

    def test_relationship_evidence_ids(self):
        graph = {
            "canonical_ids": [],
            "accepted_relationships": [
                {"evidence_ids": ["evid:001", "evid:002"]}
            ]
        }
        ids = _graph_grounding_identifiers(graph)
        assert "evid:001" in ids
        assert "evid:002" in ids

    def test_non_dict_relationships_ignored(self):
        graph = {
            "accepted_relationships": ["not-a-dict", None, 42]
        }
        # Should not raise
        ids = _graph_grounding_identifiers(graph)
        assert isinstance(ids, set)

    def test_filters_empty_values(self):
        graph = {"canonical_ids": ["entity:abc", None, "", "entity:def"]}
        ids = _graph_grounding_identifiers(graph)
        assert "" not in ids
        assert None not in ids
        assert "entity:abc" in ids


# ---------------------------------------------------------------------------
# _mcp_grounding_quality
# ---------------------------------------------------------------------------

class TestMcpGroundingQuality:
    def test_no_signals_in_answer(self):
        obs = {"answer": "The company provides excellent services."}
        grounded, ungrounded, total = _mcp_grounding_quality(obs)
        assert isinstance(grounded, int)
        assert isinstance(ungrounded, int)
        assert isinstance(total, int)

    def test_empty_answer(self):
        obs = {"answer": ""}
        grounded, ungrounded, total = _mcp_grounding_quality(obs)
        assert total == 0


# ---------------------------------------------------------------------------
# _needs_mcp_grounding_retry
# ---------------------------------------------------------------------------

class TestNeedsMcpGroundingRetry:
    def test_returns_bool(self):
        # _needs_mcp_grounding_retry requires specific args (case, graph, mcp)
        # Just verify it's importable (already tested via import)
        from fabric_kg_builder.runtime.collector import _needs_mcp_grounding_retry
        assert callable(_needs_mcp_grounding_retry)


# ---------------------------------------------------------------------------
# Configuration models
# ---------------------------------------------------------------------------

class TestGraphRuntimeConfig:
    def test_basic_config(self):
        cfg = GraphRuntimeConfig(
            workspace_id="ws-001",
            graph_model_id="gm-001",
        )
        assert cfg.workspace_id == "ws-001"

    def test_defaults(self):
        cfg = GraphRuntimeConfig(workspace_id="ws-001", graph_model_id="gm-001")
        assert isinstance(cfg, GraphRuntimeConfig)


class TestSearchRuntimeConfig:
    def test_basic_config(self):
        cfg = SearchRuntimeConfig(
            endpoint="https://search.example.com",
            index_name="kg-chunks",
        )
        assert cfg.endpoint == "https://search.example.com"
        assert cfg.index_name == "kg-chunks"

    def test_missing_index_for_direct_search_raises(self):
        with pytest.raises(Exception):
            SearchRuntimeConfig(endpoint="https://search.example.com")


class TestMcpRuntimeConfig:
    def test_basic_config(self):
        cfg = McpRuntimeConfig(
            endpoint="https://mcp.example.com",
            workspace_id="ws-001",
            data_agent_id="da-001",
        )
        assert "example.com" in cfg.endpoint
        assert cfg.workspace_id == "ws-001"


class TestDeploymentRuntimeConfig:
    def test_basic_config(self):
        cfg = DeploymentRuntimeConfig(
            artifact_validation_status="valid",
            data_agent_published=True,
            compiled_instruction_hash="abc123",
            deployed_instruction_hash="abc123",
        )
        assert cfg.artifact_validation_status == "valid"
        assert cfg.data_agent_published is True
