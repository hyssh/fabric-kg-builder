"""Preflight must reject predictable companion paging failures before creation."""

import json

import pyarrow as pa
import pytest

from fabric_kg_builder.deploy import schema2_prototype as prototype


@pytest.mark.parametrize("page_size,blocked", [(1, True), (2, False)])
def test_dry_run_checks_companion_endpoint_buckets(tmp_path, monkeypatch, page_size, blocked):
    table = pa.table({
        "__canonical_id": ["relationship:first", "relationship:second"],
        "__source_entity_id": ["entity:source", "entity:source"],
        "__target_entity_id": ["entity:target", "entity:target"],
    })
    compilation = prototype._Compilation(
        definitions={"ontology": {
            "entity_types": [],
            "relationship_types": [{
                "physical_table_id": "relationships",
                "allowed_source_semantic_type_ids": ["semantic-type:source"],
                "allowed_target_semantic_type_ids": ["semantic-type:target"],
            }],
        }},
        tables={"relationships": table}, graph_parts=[],
        graph_catalog={"expected_node_count": 0, "expected_edge_count": 2},
        limitations=[], blockers=[], provenance={},
    )
    monkeypatch.setattr(prototype, "_compile", lambda *args: compilation)
    monkeypatch.setattr(prototype, "_materialize", lambda *args: None)
    monkeypatch.setattr(
        prototype, "_native_definitions",
        lambda *args, **kwargs: ({"graph": {"parts": []}}, []),
    )
    independent_check = prototype._GraphReadbackCheck(
        "(s)-[e]->(t)", "e.id", table.to_pylist(), [],
        [("e.id", "__canonical_id")],
    )
    monkeypatch.setattr(
        prototype, "_graph_readback_checks",
        lambda *args, **kwargs: [independent_check],
    )

    def forbid_live(*args, **kwargs):
        pytest.fail("Dry-run attempted live publication")

    monkeypatch.setattr(prototype, "_Run", forbid_live)
    plan_path = tmp_path / "plan.json"
    journal = tmp_path / "journal.json"
    result = prototype.publish_schema2_prototype(
        l4_run=tmp_path / "l4", l3_root=tmp_path / "l3",
        workspace_id="00000000-0000-0000-0000-000000000001",
        name_prefix="test", dry_run=True, approve_live=None,
        plan_path=plan_path, materialize_dir=tmp_path / "materialized",
        journal_path=journal, readback_page_size=page_size,
    )
    assert bool(result["blockers"]) is blocked
    if blocked:
        assert "Ontology companion readback for relationships" in result["blockers"][0]
        assert "endpoint-pair bucket exceeds the page size" in result["blockers"][0]
    assert json.loads(plan_path.read_text())["blockers"] == result["blockers"]
    assert not journal.exists()
