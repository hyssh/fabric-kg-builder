"""Tests for knowledge/search_kb.py — spec classes and to_body() serialization."""
from __future__ import annotations

import pytest

from fabric_kg_builder.knowledge.search_kb import (
    FabricDataAgentKnowledgeSourceSpec,
    FabricOntologyKnowledgeSourceSpec,
    KnowledgeBaseSpec,
    SearchIndexKnowledgeSourceSpec,
    UpsertResult,
)


# ---------------------------------------------------------------------------
# SearchIndexKnowledgeSourceSpec
# ---------------------------------------------------------------------------


class TestSearchIndexKnowledgeSourceSpec:
    def test_basic_to_body(self):
        spec = SearchIndexKnowledgeSourceSpec(
            name="my-search-source",
            search_index_name="kg-dev-chunks",
        )
        body = spec.to_body()
        assert body["name"] == "my-search-source"
        assert body["kind"] == "searchIndex"
        assert body["searchIndexParameters"]["searchIndexName"] == "kg-dev-chunks"

    def test_with_semantic_config(self):
        spec = SearchIndexKnowledgeSourceSpec(
            name="src",
            search_index_name="kg-chunks",
            semantic_configuration_name="my-semantic-config",
        )
        body = spec.to_body()
        assert body["searchIndexParameters"]["semanticConfigurationName"] == "my-semantic-config"

    def test_without_semantic_config(self):
        spec = SearchIndexKnowledgeSourceSpec(
            name="src",
            search_index_name="kg-chunks",
        )
        body = spec.to_body()
        assert "semanticConfigurationName" not in body["searchIndexParameters"]

    def test_source_data_fields(self):
        spec = SearchIndexKnowledgeSourceSpec(
            name="src",
            search_index_name="kg-chunks",
            source_data_fields=["content", "source_name"],
        )
        body = spec.to_body()
        field_names = [f["name"] for f in body["searchIndexParameters"]["sourceDataFields"]]
        assert "content" in field_names
        assert "source_name" in field_names

    def test_search_fields(self):
        spec = SearchIndexKnowledgeSourceSpec(
            name="src",
            search_index_name="kg-chunks",
            search_fields=["content", "chunk_id"],
        )
        body = spec.to_body()
        field_names = [f["name"] for f in body["searchIndexParameters"]["searchFields"]]
        assert "content" in field_names

    def test_with_description(self):
        spec = SearchIndexKnowledgeSourceSpec(
            name="src",
            search_index_name="kg-chunks",
            description="Semantic search over chunked documents",
        )
        body = spec.to_body()
        assert body["description"] == "Semantic search over chunked documents"

    def test_without_description_omitted(self):
        spec = SearchIndexKnowledgeSourceSpec(name="src", search_index_name="kg-chunks")
        body = spec.to_body()
        assert "description" not in body


# ---------------------------------------------------------------------------
# FabricDataAgentKnowledgeSourceSpec
# ---------------------------------------------------------------------------


class TestFabricDataAgentKnowledgeSourceSpec:
    def test_basic_to_body(self):
        spec = FabricDataAgentKnowledgeSourceSpec(
            name="my-data-agent-source",
            workspace_id="ws-001",
            data_agent_id="da-001",
        )
        body = spec.to_body()
        assert body["name"] == "my-data-agent-source"
        assert body["kind"] == "fabricDataAgent"
        params = body["fabricDataAgentParameters"]
        assert params["workspaceId"] == "ws-001"
        assert params["dataAgentId"] == "da-001"

    def test_with_description(self):
        spec = FabricDataAgentKnowledgeSourceSpec(
            name="src",
            workspace_id="ws-001",
            data_agent_id="da-001",
            description="My data agent",
        )
        body = spec.to_body()
        assert body["description"] == "My data agent"

    def test_without_description_omitted(self):
        spec = FabricDataAgentKnowledgeSourceSpec(
            name="src",
            workspace_id="ws-001",
            data_agent_id="da-001",
        )
        body = spec.to_body()
        assert "description" not in body


# ---------------------------------------------------------------------------
# FabricOntologyKnowledgeSourceSpec
# ---------------------------------------------------------------------------


class TestFabricOntologyKnowledgeSourceSpec:
    def test_basic_to_body(self):
        spec = FabricOntologyKnowledgeSourceSpec(
            name="my-ontology-source",
            workspace_id="ws-001",
            ontology_id="ont-001",
        )
        body = spec.to_body()
        assert body["name"] == "my-ontology-source"
        assert body["kind"] == "fabricOntology"
        params = body["fabricOntologyParameters"]
        assert params["workspaceId"] == "ws-001"
        assert params["ontologyId"] == "ont-001"

    def test_with_description(self):
        spec = FabricOntologyKnowledgeSourceSpec(
            name="src",
            workspace_id="ws-001",
            ontology_id="ont-001",
            description="Fabric ontology knowledge",
        )
        body = spec.to_body()
        assert body["description"] == "Fabric ontology knowledge"


# ---------------------------------------------------------------------------
# KnowledgeBaseSpec (different from SearchIndexKnowledgeSourceSpec)
# ---------------------------------------------------------------------------


class TestKnowledgeBaseSpec:
    def test_basic_creation(self):
        spec = KnowledgeBaseSpec(
            name="my-kb",
            knowledge_source_names=["src-1", "src-2"],
        )
        assert spec.name == "my-kb"
        assert spec.knowledge_source_names == ["src-1", "src-2"]

    def test_default_fields(self):
        spec = KnowledgeBaseSpec(name="my-kb")
        assert spec.uses_llm is False
        assert spec.description == ""

    def test_to_body_includes_sources(self):
        spec = KnowledgeBaseSpec(
            name="my-kb",
            knowledge_source_names=["src-1"],
        )
        body = spec.to_body()
        assert body["name"] == "my-kb"
        assert isinstance(body, dict)


# ---------------------------------------------------------------------------
# UpsertResult
# ---------------------------------------------------------------------------


class TestUpsertResult:
    def test_basic_creation(self):
        result = UpsertResult(
            name="my-source",
            created=True,
            status_code=201,
            body={"id": "src-001"},
        )
        assert result.name == "my-source"
        assert result.created is True
        assert result.status_code == 201

    def test_updated_status(self):
        result = UpsertResult(
            name="my-source",
            created=False,
            status_code=200,
        )
        assert result.created is False
        assert result.status_code == 200
