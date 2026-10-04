"""Contract-focused tests for live Graph/Data Agent example validation (#11)."""
from __future__ import annotations

import pytest

from fabric_kg_builder.knowledge.data_agent import (
    compare_graph_few_shot_semantics,
    validate_graph_few_shot_examples,
)
from fabric_kg_builder.knowledge.validation import DataAgentExampleValidationFailed
from fabric_kg_builder.semantic.schemas import CompetencyExampleReceipt

_H1 = "sha256:" + "1" * 64
_H2 = "sha256:" + "2" * 64
_H3 = "sha256:" + "3" * 64


def _query_schema() -> dict:
    return {
        "manifest_hash": _H1,
        "schema_hash": _H2,
        "nodes": [
            {
                "semantic_id": "entity-type:asset",
                "label": "Asset",
                "owner_properties": {"asset_id": "asset_id", "evidence_id": "evidence_id"},
            },
            {
                "semantic_id": "entity-type:location",
                "label": "Location",
                "owner_properties": {"location_id": "location_id"},
            },
        ],
        "relationships": [
            {
                "semantic_id": "relationship-type:located_at",
                "label": "located_at",
                "source_type_id": "entity-type:asset",
                "target_type_id": "entity-type:location",
                "source_label": "Asset",
                "target_label": "Location",
                "direction": "source_to_target",
            }
        ],
    }


def _plan() -> dict:
    return {
        "plan_hash": _H3,
        "manifest_hash": _H1,
        "intent": "Locate an asset.",
        "required_types": ["entity-type:asset", "entity-type:location"],
        "required_relationships": ["relationship-type:located_at"],
        "optional_relationships": [],
        "requested_properties": ["asset_id", "evidence_id"],
        "evidence_required": True,
        "path_steps": [
            {
                "step_id": "s1",
                "from_type_id": "entity-type:asset",
                "via_relationship_id": "relationship-type:located_at",
                "to_type_id": "entity-type:location",
                "direction": "source_to_target",
                "optional": False,
                "max_depth": 1,
            }
        ],
        "budget": {
            "max_hops": 4,
            "max_nodes": 6,
            "max_relationships": 5,
            "max_rows_per_subquery": 25,
            "max_subqueries": 4,
        },
    }


def _case() -> dict:
    return {
        "id": "locate-asset",
        "question": "Where is Asset A?",
        "expected": {
            "relationship_types": ["relationship-type:located_at"],
            "evidence_required": True,
        },
        "probes": {
            "direct_graph": {
                "query": (
                    "MATCH (a:`Asset`)-[:`located_at`]->(l:`Location`) "
                    "RETURN a.asset_id AS asset_id, a.evidence_id AS evidence_id "
                    "LIMIT 10"
                ),
                "semantic_plan": _plan(),
                "static_validation_passed": True,
                "canonical_id_columns": ["asset_id"],
            }
        },
    }


def _graph_response(*, rows: list[dict], code: str = "00", description: str = "OK") -> dict:
    return {
        "status": {"code": code, "description": description, "requestId": "req-graph-1"},
        "result": {"kind": "TABLE", "data": rows},
    }


def test_required_example_published_and_semantically_matches() -> None:
    summary = validate_graph_few_shot_examples(
        {"cases": [_case()]},
        dry_run=False,
        execute_graph_query=lambda _: _graph_response(
            rows=[{"asset_id": "asset-1", "evidence_id": "ev-1"}]
        ),
        query_schema=_query_schema(),
        require_schema=True,
    )
    assert len(summary.examples) == 1
    published = summary.receipts[0]
    assert published.published is True
    compared = compare_graph_few_shot_semantics(
        {"cases": [_case()]},
        [published],
        direct_results=summary.direct_results,
        execute_data_agent_case=lambda _: {
            "result_category": "success",
            "request_ids": ["req-da-1"],
            "citations": [{"canonical_id": "asset-1"}],
        },
    )
    assert compared[0].semantic_match is True


def test_required_example_with_missing_evidence_is_blocked() -> None:
    with pytest.raises(DataAgentExampleValidationFailed):
        validate_graph_few_shot_examples(
            {"cases": [_case()]},
            dry_run=False,
            execute_graph_query=lambda _: _graph_response(rows=[{"asset_id": "asset-1"}]),
            query_schema=_query_schema(),
            require_schema=True,
        )


def test_required_semantic_mismatch_is_blocked() -> None:
    with pytest.raises(DataAgentExampleValidationFailed):
        compare_graph_few_shot_semantics(
            {"cases": [_case()]},
            [
                CompetencyExampleReceipt(
                    competency_id="locate-asset",
                    required=True,
                    required_relationship_ids=["relationship-type:located_at"],
                    observed_rows={"relationship-type:located_at": 1},
                    min_required_rows=1,
                    status="published",
                    remediation="",
                    published=True,
                )
            ],
            direct_results={
                "locate-asset": {
                    "canonical_ids": ["asset-1"],
                    "row_count": 1,
                    "result_category": "success",
                }
            },
            execute_data_agent_case=lambda _: {
                "result_category": "success",
                "citations": [{"canonical_id": "asset-2"}],
                "request_ids": ["req-da-2"],
            },
        )
