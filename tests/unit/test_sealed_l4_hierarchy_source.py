"""Authority-preserving tests for the read-only sealed-L4 hierarchy input adapter.

Scope: exercises `fabric_kg_builder.semantic.sealed_l4_hierarchy_source` only.
These tests do NOT re-test `SealedL4ServingSource`'s own validation logic --
that has its own dedicated suites (test_schema2_projection_stage.py,
test_qualified_handoff_l4_witnesses.py, test_approved_partial_handoff.py).
They prove the adapter is a faithful pass-through: it surfaces exactly what
the sealed source already validated or rejected, adds no reinterpretation,
invents no success path, and does not define a hierarchy provider/result
contract (that remains W2's graph/community_contracts.py sole ownership).
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fabric_kg_builder.graph.community_hierarchy_v2 import (
    build_derived_community_hierarchy_safe,
)
from fabric_kg_builder.graph.community_projection import _compute_input_graph_hash
from fabric_kg_builder.graph.community_provider_testing import (
    DeterministicConnectedComponentsProvider,
)
from fabric_kg_builder.model.arrow_schemas import L4_PROJECTION_TABLE_SCHEMAS
from fabric_kg_builder.semantic.derived_hierarchy_artifact import (
    write_derived_hierarchy_artifact,
)
from fabric_kg_builder.semantic.sealed_l4_hierarchy_source import (
    SealedL4HierarchyCoreInput,
    SealedL4HierarchyRows,
    build_hierarchy_core_rows,
    load_sealed_l4_hierarchy_rows,
    read_sealed_l4_hierarchy_rows,
)
from fabric_kg_builder.semantic.source_tables import SealedL4ServingSource
from fabric_kg_builder.serving.lifecycle_projection import run_l4
from tests.unit.test_schema2_validation_stage import _l3, _pipeline


def _run(tmp_path: Path, domain: str = "records", *, mutate=None):
    """Drive a real, minimal L1->L2->L3->L4 pipeline and return (l3, l4)."""
    l1_state_root, domain_path, _l2 = _pipeline(tmp_path, domain, mutate=mutate)
    l3 = _l3(tmp_path, l1_state_root, domain_path)
    l4 = run_l4(l3, state_root=tmp_path / ".fkg" / "l4")
    return l3, l4


def _drop_relationship_candidates(candidates, _work_unit):
    """Mutator that yields a sealed run with entities but zero relationships.

    This is a legitimately sparse/empty-table serving run, not a failure --
    the adapter must surface an empty tuple, not raise or fabricate rows.
    """
    return [c for c in candidates if c["candidate_kind"] != "relationship"]


@pytest.mark.unit
def test_adapter_reads_complete_sealed_l4_hierarchy_rows(tmp_path: Path) -> None:
    l3, l4 = _run(tmp_path)

    rows = load_sealed_l4_hierarchy_rows(
        l4.run_root,
        input_manifest_search_roots=(l3.state_root,),
    )

    assert isinstance(rows, SealedL4HierarchyRows)
    assert rows.root == l4.run_root
    assert rows.receipt == l4.receipt
    assert rows.manifest == l4.output_manifest
    assert rows.input_manifest == l4.source.output_manifest
    assert rows.projection == l4.serving_projection

    assert rows.entities
    assert rows.entity_type_assertions
    assert rows.relationships

    entity_ids = {row["entity_id"] for row in rows.entities}
    assert len(entity_ids) == 2

    # Evidence provenance must be preserved verbatim on both entities and
    # relationships -- nothing is down-projected away by this adapter.
    for row in rows.entities:
        assert row["evidence_span_ids"]
    for row in rows.relationships:
        assert row["evidence_span_ids"]
        assert row["source_entity_id"] in entity_ids
        assert row["target_entity_id"] in entity_ids

    for row in rows.entity_type_assertions:
        assert row["entity_id"] in entity_ids

    # No partial-extraction handoff was involved in this run.
    assert rows.partial_scope is None
    assert rows.partial_scope_context is None


@pytest.mark.unit
def test_adapter_surfaces_legitimately_empty_relationships_table(tmp_path: Path) -> None:
    l3, l4 = _run(tmp_path, mutate=_drop_relationship_candidates)

    rows = load_sealed_l4_hierarchy_rows(
        l4.run_root,
        input_manifest_search_roots=(l3.state_root,),
    )

    assert rows.entities
    assert rows.relationships == ()


@pytest.mark.unit
def test_read_sealed_l4_hierarchy_rows_accepts_preconstructed_source(tmp_path: Path) -> None:
    l3, l4 = _run(tmp_path)
    source = SealedL4ServingSource.from_run(
        l4.run_root,
        input_manifest_search_roots=[l3.state_root],
    )

    rows = read_sealed_l4_hierarchy_rows(source)

    assert rows.projection is source.projection
    assert rows.receipt is source.receipt
    assert rows.manifest is source.manifest
    assert rows.entities


@pytest.mark.unit
def test_adapter_rejects_tampered_sealed_entities_table(tmp_path: Path) -> None:
    l3, l4 = _run(tmp_path)
    path = l4.run_root / "semantic_asserted_entities.parquet"
    tampered_rows = pq.read_table(path).to_pylist()
    tampered_rows[0] = {**tampered_rows[0], "label": "TAMPERED"}
    pq.write_table(
        pa.Table.from_pylist(
            tampered_rows,
            schema=L4_PROJECTION_TABLE_SCHEMAS["semantic_asserted_entities"],
        ),
        path,
        compression="zstd",
        version="2.6",
        use_dictionary=False,
        write_statistics=True,
    )

    with pytest.raises(ValueError, match="differs from its artifact manifest"):
        load_sealed_l4_hierarchy_rows(
            l4.run_root,
            input_manifest_search_roots=(l3.state_root,),
        )


@pytest.mark.unit
def test_adapter_rejects_stale_run_with_missing_sealed_table(tmp_path: Path) -> None:
    l3, l4 = _run(tmp_path)
    (l4.run_root / "semantic_asserted_relationships.parquet").unlink()

    with pytest.raises(FileNotFoundError, match="sealed L4 table is missing"):
        load_sealed_l4_hierarchy_rows(
            l4.run_root,
            input_manifest_search_roots=(l3.state_root,),
        )


@pytest.mark.unit
def test_adapter_rejects_run_without_matching_l3_input_manifest(tmp_path: Path) -> None:
    _l3, l4 = _run(tmp_path)
    other_root = tmp_path / "unrelated-state-root"
    other_root.mkdir()

    with pytest.raises(Exception):
        load_sealed_l4_hierarchy_rows(
            l4.run_root,
            input_manifest_search_roots=(other_root,),
        )


@pytest.mark.unit
def test_adapter_never_resolves_raw_canonical_tables(tmp_path: Path) -> None:
    """Guard against ever widening the adapter beyond the 3 hierarchy inputs.

    `.resolve()` itself forbids raw "entities"/"relationships" canonical
    tables as schema-2 serving sources; this adapter must never route
    around that by reading canonical parquet files directly.
    """
    l3, l4 = _run(tmp_path)
    source = SealedL4ServingSource.from_run(
        l4.run_root,
        input_manifest_search_roots=[l3.state_root],
    )

    with pytest.raises(ValueError, match="raw canonical tables are forbidden"):
        source.resolve("entities")
    with pytest.raises(ValueError, match="raw canonical tables are forbidden"):
        source.resolve("relationships")

    rows = read_sealed_l4_hierarchy_rows(source)
    assert rows.entities and rows.relationships


@pytest.mark.unit
def test_build_hierarchy_core_rows_maps_sealed_facts_onto_real_w2_rows(
    tmp_path: Path,
) -> None:
    """Frozen field mapping produces real ``EntityRow``/``RelationshipRow``."""
    l3, l4 = _run(tmp_path)
    sealed = load_sealed_l4_hierarchy_rows(
        l4.run_root, input_manifest_search_roots=(l3.state_root,)
    )

    core_input = build_hierarchy_core_rows(sealed)

    assert isinstance(core_input, SealedL4HierarchyCoreInput)
    assert len(core_input.entities) == len(sealed.entities)
    assert len(core_input.relationships) == len(sealed.relationships)

    sealed_entities_by_id = {row["entity_id"]: row for row in sealed.entities}
    for entity_row in core_input.entities:
        sealed_row = sealed_entities_by_id[entity_row.entity_id]
        assert entity_row.entity_type == sealed_row["most_specific_type_id"]
        assert entity_row.canonical_key == sealed_row["entity_id"]
        assert entity_row.domain_hash == sealed_row["domain_contract_hash"]
        assert entity_row.content_hash == sealed_row["row_hash"]
        assert entity_row.evidence_ids == list(sealed_row["evidence_span_ids"])
        assert entity_row.created_at == sealed.receipt.completed_at_utc
        assert entity_row.updated_at == sealed.receipt.completed_at_utc

    sealed_relationships_by_id = {
        row["relationship_id"]: row for row in sealed.relationships
    }
    for relationship_row in core_input.relationships:
        sealed_row = sealed_relationships_by_id[relationship_row.relationship_id]
        assert relationship_row.relationship_type == sealed_row["semantic_relationship_id"]
        assert relationship_row.source_entity_id == sealed_row["source_entity_id"]
        assert relationship_row.target_entity_id == sealed_row["target_entity_id"]
        assert relationship_row.domain_hash == sealed_row["domain_contract_hash"]
        assert relationship_row.content_hash == sealed_row["row_hash"]
        assert relationship_row.evidence_ids == list(sealed_row["evidence_span_ids"])
        assert relationship_row.created_at == sealed.receipt.completed_at_utc


@pytest.mark.unit
def test_build_hierarchy_core_rows_input_graph_hash_matches_w2s_own_helper(
    tmp_path: Path,
) -> None:
    """``input_graph_hash`` must equal W2's own helper called directly -- the
    core hash-equality guarantee: this helper never reimplements the hash,
    it only calls through to ``community_projection._compute_input_graph_hash``.
    """
    l3, l4 = _run(tmp_path)
    sealed = load_sealed_l4_hierarchy_rows(
        l4.run_root, input_manifest_search_roots=(l3.state_root,)
    )

    core_input = build_hierarchy_core_rows(sealed)

    expected_hash = _compute_input_graph_hash(
        list(core_input.entities), list(core_input.relationships)
    )
    assert core_input.input_graph_hash == expected_hash


@pytest.mark.unit
def test_build_hierarchy_core_rows_hash_matches_real_build_receipt(
    tmp_path: Path,
) -> None:
    """End-to-end: the helper's ``input_graph_hash`` equals whatever a real
    W2 build against the very same rows reports back on its outcome/receipt
    path, using an injected dependency-free provider (not a native claim).
    """
    l3, l4 = _run(tmp_path)
    sealed = load_sealed_l4_hierarchy_rows(
        l4.run_root, input_manifest_search_roots=(l3.state_root,)
    )
    core_input = build_hierarchy_core_rows(sealed)

    entity_evidence = {
        row.entity_id: tuple(row.evidence_ids or ()) for row in core_input.entities
    }
    relationship_evidence = {
        row.relationship_id: tuple(row.evidence_ids or ())
        for row in core_input.relationships
    }

    outcome = build_derived_community_hierarchy_safe(
        list(core_input.entities),
        list(core_input.relationships),
        provider=DeterministicConnectedComponentsProvider(),
        policy_id="test-policy",
        run_id="test-run",
        entity_evidence=entity_evidence,
        relationship_evidence=relationship_evidence,
    )

    assert outcome.input_graph_hash == core_input.input_graph_hash

    # End-to-end: the same equality must still hold once the outcome is
    # actually published through the real writer, on the real
    # ``DerivedHierarchyReceipt.input_graph_hash`` field -- not just on the
    # in-memory outcome object constructed above.
    result = write_derived_hierarchy_artifact(
        sealed=sealed,
        outcome=outcome,
        output_root=tmp_path / "derived",
        run_id="test-run",
        config_id="default",
        policy_id="test-policy",
        seed=0,
        max_cluster_size=10,
        requested_provider_name="stub_connected_components",
        requested_provider_version="test-fake-1",
        entity_evidence=entity_evidence,
        relationship_evidence=relationship_evidence,
    )
    assert result.receipt.input_graph_hash == core_input.input_graph_hash
