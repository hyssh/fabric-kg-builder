"""Tests for knowledge/routing.py — question routing classifier."""
from __future__ import annotations

import pytest

from fabric_kg_builder.knowledge.routing import (
    RouteCategory,
    RoutingResult,
    classify_question,
    routing_hints_for_question,
)


class TestRouteCategory:
    def test_values(self):
        assert RouteCategory.SEARCH.value == "search"
        assert RouteCategory.GRAPH.value == "graph"
        assert RouteCategory.MIXED.value == "mixed"


class TestRoutingResult:
    def test_basic_creation(self):
        r = RoutingResult(
            question="Who is the CEO?",
            category=RouteCategory.SEARCH,
        )
        assert r.question == "Who is the CEO?"
        assert r.category == RouteCategory.SEARCH
        assert r.graph_signals == []
        assert r.search_signals == []


class TestClassifyQuestion:
    def test_graph_question_hierarchy(self):
        result = classify_question("Show me the hierarchy of suppliers.")
        assert result.category in (RouteCategory.GRAPH, RouteCategory.MIXED)
        assert len(result.graph_signals) > 0

    def test_graph_question_relationship(self):
        result = classify_question("Find relationships between entities.")
        assert result.category in (RouteCategory.GRAPH, RouteCategory.MIXED)

    def test_graph_question_nodes(self):
        result = classify_question("Show all nodes connected to this entity.")
        assert result.category in (RouteCategory.GRAPH, RouteCategory.MIXED)

    def test_search_question_documents(self):
        result = classify_question("Find documents about contract compliance.")
        assert result.category in (RouteCategory.SEARCH, RouteCategory.MIXED)

    def test_search_question_manual(self):
        result = classify_question("Show me the manual for this product.")
        assert result.category in (RouteCategory.SEARCH, RouteCategory.MIXED)

    def test_search_question_policy(self):
        result = classify_question("What is the policy for data retention?")
        assert result.category in (RouteCategory.SEARCH, RouteCategory.MIXED)

    def test_unknown_defaults_to_mixed(self):
        result = classify_question("abcxyz")
        assert result.category == RouteCategory.MIXED

    def test_result_has_rationale(self):
        result = classify_question("Any question here?")
        assert isinstance(result.rationale, str)
        assert len(result.rationale) > 0

    def test_mixed_question(self):
        result = classify_question(
            "Find documents describing the hierarchy of suppliers."
        )
        assert result.category == RouteCategory.MIXED

    def test_empty_question_mixed(self):
        result = classify_question("")
        assert result.category == RouteCategory.MIXED

    def test_cypher_keyword_routes_graph(self):
        result = classify_question("Write a Cypher query to find entities.")
        assert result.category in (RouteCategory.GRAPH, RouteCategory.MIXED)

    def test_returns_routing_result(self):
        result = classify_question("What entities exist?")
        assert isinstance(result, RoutingResult)


class TestRoutingHintsForQuestion:
    def test_is_alias_of_classify(self):
        q = "Show hierarchy of components."
        r1 = classify_question(q)
        r2 = routing_hints_for_question(q)
        assert r1.category == r2.category

    def test_returns_routing_result(self):
        result = routing_hints_for_question("What is the status?")
        assert isinstance(result, RoutingResult)
