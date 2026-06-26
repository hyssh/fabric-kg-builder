"""Unit tests for the multi-type ontology plan and Fabric parts.

Covers:
- multitype_plan.build_plan: present types kept in order, absent types dropped,
  relationship verbs collapsed by (source,target) pair, threshold + cap applied.
- fabric_def.build_multitype_ontology_parts: one EntityType + DataBinding per
  type, one RelationshipType + Contextualization per pair, unique IDs and paths,
  bindings point at the per-type / per-pair tables.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fabric_kg_builder.ontology.fabric_def import build_multitype_ontology_parts
from fabric_kg_builder.ontology.multitype_plan import (
    build_plan,
    slugify_table,
)


@pytest.fixture
def parquet_dir(tmp_path: Path) -> Path:
    """Write tiny entities.parquet + relationships.parquet fixtures."""
    entities = pa.table(
        {
            "entity_id": ["c1", "c2", "p1", "s1", "s2", "x1"],
            "entity_type": [
                "Component",
                "Component",
                "Procedure",
                "Step",
                "Step",
                "Obscure",  # not in DEFAULT_CORE_TYPES → dropped
            ],
            "display_name": ["Battery", "Screen", "Replace", "Unscrew", "Lift", "Z"],
            "canonical_key": ["component:battery", "component:screen", "procedure:replace",
                              "step:unscrew", "step:lift", "obscure:z"],
        }
    )
    pq.write_table(entities, str(tmp_path / "entities.parquet"))

    # Procedure->Step appears 3x (above threshold). Procedure->Component once.
    rels = pa.table(
        {
            "relationship_id": ["r1", "r2", "r3", "r4"],
            "relationship_type": ["HAS_STEP", "has_step", "includes_step", "involves"],
            "source_entity_id": ["p1", "p1", "p1", "p1"],
            "target_entity_id": ["s1", "s2", "s1", "c1"],
        }
    )
    pq.write_table(rels, str(tmp_path / "relationships.parquet"))
    return tmp_path


def test_slugify_table():
    assert slugify_table("DeviceModel") == "devicemodel"
    assert slugify_table("Part Number") == "part_number"
    assert slugify_table("HAS_STEP") == "has_step"
    assert slugify_table("!!!") == "x"


def test_build_plan_keeps_present_types_in_order(parquet_dir: Path):
    plan = build_plan(parquet_dir, min_pair_count=1)
    names = plan.type_names
    # Obscure dropped; order follows DEFAULT_CORE_TYPES (Component before Procedure before Step).
    assert names == ["Component", "Procedure", "Step"]
    assert all(e.table_name == f"entities_{slugify_table(e.type_name)}" for e in plan.entity_types)
    comp = next(e for e in plan.entity_types if e.type_name == "Component")
    assert comp.count == 2


def test_build_plan_collapses_verbs_by_pair(parquet_dir: Path):
    plan = build_plan(parquet_dir, min_pair_count=1)
    pairs = {(r.source_type, r.target_type): r for r in plan.relationship_pairs}
    # Procedure->Step collapsed to one pair with the 3 verbs combined.
    assert ("Procedure", "Step") in pairs
    ps = pairs[("Procedure", "Step")]
    assert ps.count == 3
    # Dominant verb (has_step appears twice: HAS_STEP + has_step both slug to has_step).
    assert ps.name == "has_step"
    assert ps.table_name == "rel_procedure_step"
    # Procedure->Component present too (count 1).
    assert ("Procedure", "Component") in pairs


def test_build_plan_min_pair_count_threshold(parquet_dir: Path):
    plan = build_plan(parquet_dir, min_pair_count=3)
    pairs = {(r.source_type, r.target_type) for r in plan.relationship_pairs}
    assert ("Procedure", "Step") in pairs  # count 3 meets threshold
    assert ("Procedure", "Component") not in pairs  # count 1 below threshold


def test_build_multitype_parts_structure(parquet_dir: Path):
    plan = build_plan(parquet_dir, min_pair_count=1)
    parts = build_multitype_ontology_parts(
        workspace_id="WS",
        lakehouse_item_id="LH",
        entity_types=[{"type_name": e.type_name, "table_name": e.table_name}
                      for e in plan.entity_types],
        relationship_pairs=[{"name": r.name, "source_type": r.source_type,
                             "target_type": r.target_type, "table_name": r.table_name}
                            for r in plan.relationship_pairs],
        schema="dbo",
    )
    paths = [p["path"] for p in parts]
    # Root + platform present.
    assert "definition.json" in paths
    assert ".platform" in paths
    # All part paths unique.
    assert len(paths) == len(set(paths))

    et_defs = [p for p in parts if p["path"].startswith("EntityTypes/")
               and p["path"].endswith("definition.json")]
    et_binds = [p for p in parts if "/DataBindings/" in p["path"]]
    rt_defs = [p for p in parts if p["path"].startswith("RelationshipTypes/")
               and p["path"].endswith("definition.json")]
    rt_ctx = [p for p in parts if "/Contextualizations/" in p["path"]]

    assert len(et_defs) == 3          # one per present type
    assert len(et_binds) == 3
    assert len(rt_defs) == len(plan.relationship_pairs)
    assert len(rt_ctx) == len(plan.relationship_pairs)

    # Entity type IDs are unique.
    et_ids = [p["path"].split("/")[1] for p in et_defs]
    assert len(et_ids) == len(set(et_ids))


def test_build_multitype_parts_bindings_point_at_per_type_tables(parquet_dir: Path):
    plan = build_plan(parquet_dir, min_pair_count=1)
    parts = build_multitype_ontology_parts(
        workspace_id="WS",
        lakehouse_item_id="LH",
        entity_types=[{"type_name": e.type_name, "table_name": e.table_name}
                      for e in plan.entity_types],
        relationship_pairs=[{"name": r.name, "source_type": r.source_type,
                             "target_type": r.target_type, "table_name": r.table_name}
                            for r in plan.relationship_pairs],
    )
    binds = [p for p in parts if "/DataBindings/" in p["path"]]
    bound_tables = {
        p["payload_json"]["dataBindingConfiguration"]["sourceTableProperties"]["sourceTableName"]
        for p in binds
    }
    assert bound_tables == {"entities_component", "entities_procedure", "entities_step"}

    # Relationship contextualization binds to the per-pair edge table and uses
    # source/target entity_id key bindings.
    ctxs = [p for p in parts if "/Contextualizations/" in p["path"]]
    ps_ctx = next(
        p for p in ctxs
        if p["payload_json"]["dataBindingTable"]["sourceTableName"] == "rel_procedure_step"
    )
    assert ps_ctx["payload_json"]["sourceKeyRefBindings"][0]["sourceColumnName"] == "source_entity_id"
    assert ps_ctx["payload_json"]["targetKeyRefBindings"][0]["sourceColumnName"] == "target_entity_id"


def test_build_multitype_parts_skips_unmodelled_endpoints(parquet_dir: Path):
    # A relationship pair whose endpoint type is not in entity_types must be skipped.
    parts = build_multitype_ontology_parts(
        workspace_id="WS",
        lakehouse_item_id="LH",
        entity_types=[{"type_name": "Component", "table_name": "entities_component"}],
        relationship_pairs=[
            {"name": "has_step", "source_type": "Procedure", "target_type": "Step",
             "table_name": "rel_procedure_step"},
        ],
    )
    rt_defs = [p for p in parts if p["path"].startswith("RelationshipTypes/")]
    assert rt_defs == []  # endpoints not modelled → no relationship parts
