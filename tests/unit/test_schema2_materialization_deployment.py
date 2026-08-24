from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fabric_kg_builder.semantic.canonical_hash import (
    arrow_schema_hash,
    arrow_table_hash,
    canonical_hash,
)
from fabric_kg_builder.semantic.persisted_projection import (
    PersistedProjectionError,
    deploy_schema2_materialization,
    load_schema2_projection_authority,
    load_materialization_receipt,
    prepare_semantic_tables,
    validate_bound_tables,
    write_safe_receipt,
)
from fabric_kg_builder.semantic.schemas import (
    ColumnSpec,
    DataAvailability,
    EntityTableSpec,
    MaterializationPlan,
    RelationshipTableSpec,
    SourceTableAuthority,
)
from fabric_kg_builder.semantic.source_tables import (
    SOURCE_TAXONOMY_VERSION,
    resolve_schema2_source_parquet,
)
from fabric_kg_builder.serving.graph_model import build_graph_model_parts
from fabric_kg_builder.serving.competency import OneLakeDeltaClient

_HASH_A = "sha256:" + "a" * 64
_HASH_B = "sha256:" + "b" * 64
_HASH_C = "sha256:" + "c" * 64


def _write_sources(root: Path) -> None:
    root.mkdir()
    entities = [
        {
            "entity_id": f"entity:{index}",
            "semantic_type_id": f"entity-type:type-{index}",
            "entity_type": f"Type{index}",
            "display_name": f"Entity {index}",
            "assertion_state": "asserted",
            "semantic_contract_hash": _HASH_A,
        }
        for index in range(19)
    ]
    entities.append({
        "entity_id": "chunk:published",
        "semantic_type_id": "entity-type:document-chunk",
        "entity_type": "DocumentChunk",
        "display_name": "Published chunk",
        "assertion_state": "asserted",
        "semantic_contract_hash": _HASH_A,
    })
    relationships = [
        {
            "relationship_id": f"relationship:{index}",
            "semantic_relationship_id": f"relationship-type:rel-{index}",
            "relationship_type": f"rel_{index}",
            "source_entity_id": f"entity:{index}",
            "target_entity_id": f"entity:{(index + 1) % 19}",
            "evidence_id": "evidence:1",
            "assertion_state": "asserted",
            "semantic_contract_hash": _HASH_A,
        }
        for index in range(13)
    ]
    relationships.append({
        "relationship_id": "relationship:evidenced-by",
        "semantic_relationship_id": "relationship-type:evidenced-by",
        "relationship_type": "evidenced_by",
        "source_entity_id": "entity:0",
        "target_entity_id": "chunk:published",
        "evidence_id": "evidence:1",
        "assertion_state": "asserted",
        "semantic_contract_hash": _HASH_A,
    })
    evidence = [{
        "evidence_id": "evidence:1",
        "chunk_id": "chunk:published",
        "text": "secret source text",
    }]
    chunks = [
        {
            "chunk_id": "chunk:published",
            "content": "published content",
            "chunk_type": "text",
        },
        {
            "chunk_id": "chunk:unpublished",
            "content": "must not materialize",
            "chunk_type": "text",
        },
    ]
    pq.write_table(pa.Table.from_pylist(entities), root / "semantic_entities.parquet")
    pq.write_table(
        pa.Table.from_pylist(relationships),
        root / "semantic_relationships.parquet",
    )
    pq.write_table(pa.Table.from_pylist(evidence), root / "evidence.parquet")
    pq.write_table(pa.Table.from_pylist(chunks), root / "chunks.parquet")

    raw = pa.table({
        "relationship_id": [f"raw:{index}" for index in range(1723)],
        "evidence_id": [None] * 1709 + ["evidence:1"] * 14,
        "assertion_state": ["rejected"] * 1709 + ["asserted"] * 14,
    })
    pq.write_table(raw, root / "relationships.parquet")


def _source_authority(root: Path, table_name: str, category: str, key: str):
    table = pq.read_table(root / f"{table_name}.parquet")
    return SourceTableAuthority(
        table_name=table_name,
        category=category,
        primary_key=key,
        row_count=table.num_rows,
        table_hash=arrow_table_hash(table, key),
        schema_hash=arrow_schema_hash(table.schema),
    )


def _base_plan() -> MaterializationPlan:
    entities = [
        EntityTableSpec(
            semantic_id=f"entity-type:type-{index}",
            table_name=f"kg_entity_type_{index}",
            source_table_name="semantic_entities",
            entity_id_column="entity_id",
            display_name_column="display_name",
            columns=[
                ColumnSpec(
                    column_name="display_name",
                    data_type="string",
                    nullable=False,
                ),
                ColumnSpec(
                    column_name="entity_id",
                    data_type="string",
                    nullable=False,
                ),
            ],
            source_category="semantic_entity_projection",
        )
        for index in range(19)
    ]
    entities.append(
        EntityTableSpec(
            semantic_id="entity-type:document-chunk",
            table_name="kg_entity_document_chunk",
            source_table_name="chunks",
            entity_id_column="chunk_id",
            display_name_column="content",
            columns=[
                ColumnSpec(
                    column_name="chunk_id",
                    semantic_property_id="property:document-chunk.entity-id",
                    data_type="string",
                    nullable=False,
                ),
                ColumnSpec(
                    column_name="content",
                    data_type="string",
                    nullable=False,
                ),
            ],
            source_category="canonical_support_entity",
        )
    )
    relationships = [
        RelationshipTableSpec(
            semantic_id=f"relationship-type:rel-{index}",
            table_name=f"kg_relationship_rel_{index}",
            source_table_name="semantic_relationships",
            relationship_id_column="relationship_id",
            source_column="source_entity_id",
            target_column="target_entity_id",
            evidence_column="evidence_id",
            columns=[
                ColumnSpec(
                    column_name="evidence_id",
                    data_type="string",
                    nullable=False,
                ),
                ColumnSpec(
                    column_name="relationship_id",
                    data_type="string",
                    nullable=False,
                ),
                ColumnSpec(
                    column_name="source_entity_id",
                    data_type="string",
                    nullable=False,
                ),
                ColumnSpec(
                    column_name="target_entity_id",
                    data_type="string",
                    nullable=False,
                ),
            ],
            source_category="semantic_relationship_projection",
        )
        for index in range(13)
    ]
    relationships.append(
        RelationshipTableSpec(
            semantic_id="relationship-type:evidenced-by",
            table_name="kg_relationship_evidenced_by",
            source_table_name="semantic_relationships",
            relationship_id_column="relationship_id",
            source_column="source_entity_id",
            target_column="target_entity_id",
            evidence_column="evidence_id",
            columns=[
                ColumnSpec(
                    column_name="evidence_id",
                    data_type="string",
                    nullable=False,
                ),
                ColumnSpec(
                    column_name="relationship_id",
                    data_type="string",
                    nullable=False,
                ),
                ColumnSpec(
                    column_name="source_entity_id",
                    data_type="string",
                    nullable=False,
                ),
                ColumnSpec(
                    column_name="target_entity_id",
                    data_type="string",
                    nullable=False,
                ),
            ],
            source_category="semantic_relationship_projection",
        )
    )
    availability = [
        DataAvailability(
            semantic_id=table.semantic_id,
            observed_rows=None,
            required_rows=0,
            status="not_observed",
        )
        for table in [*entities, *relationships]
    ]
    return MaterializationPlan(
        manifest_hash=_HASH_B,
        entity_tables=entities,
        relationship_tables=relationships,
        data_availability=availability,
    )


def _seal_plan(root: Path) -> MaterializationPlan:
    plan = _base_plan()
    prepared = prepare_semantic_tables(
        parquet_dir=root,
        plan=plan,
        enforce_planned_hashes=False,
        strict_schema2=True,
    )

    def seal(table: Any):
        arrow, _ = prepared[table.table_name]
        key = (
            table.entity_id_column
            if hasattr(table, "entity_id_column")
            else table.relationship_id_column
        )
        return table.model_copy(update={
            "planned_row_count": arrow.num_rows,
            "planned_row_hash": arrow_table_hash(arrow, key),
            "planned_schema_hash": arrow_schema_hash(arrow.schema),
        })

    entity_tables = [seal(table) for table in plan.entity_tables]
    relationship_tables = [seal(table) for table in plan.relationship_tables]
    counts = {
        table.semantic_id: int(table.planned_row_count or 0)
        for table in [*entity_tables, *relationship_tables]
    }
    return plan.model_copy(update={
        "semantic_contract_hash": _HASH_A,
        "projection_receipt_hash": _HASH_C,
        "source_taxonomy_version": SOURCE_TAXONOMY_VERSION,
        "source_tables": [
            _source_authority(
                root,
                "chunks",
                "canonical_support_entity",
                "chunk_id",
            ),
            _source_authority(
                root,
                "evidence",
                "validation_support",
                "evidence_id",
            ),
            _source_authority(
                root,
                "semantic_entities",
                "semantic_entity_projection",
                "entity_id",
            ),
            _source_authority(
                root,
                "semantic_relationships",
                "semantic_relationship_projection",
                "relationship_id",
            ),
        ],
        "entity_tables": entity_tables,
        "relationship_tables": relationship_tables,
        "data_availability": [
            item.model_copy(update={
                "observed_rows": counts[item.semantic_id],
                "status": "sufficient",
            })
            for item in plan.data_availability
        ],
    })


class _Reader:
    def __init__(
        self,
        tables: dict[str, Any],
        *,
        extra_tables: set[str] | None = None,
    ) -> None:
        self.tables = tables
        self.extra_tables = extra_tables or set()

    def read_table(
        self,
        _workspace_id: str,
        _lakehouse_item_id: str,
        _schema: str,
        table: str,
        columns: list[str] | None = None,
    ) -> Any:
        value = self.tables[table]
        return value.select(columns) if columns else value

    def list_tables(
        self,
        _workspace_id: str,
        _lakehouse_item_id: str,
        _schema: str,
    ) -> list[str]:
        return sorted(set(self.tables) | self.extra_tables)


def test_mixed_lifecycle_creates_34_tables_and_support_identity_is_exact(
    tmp_path: Path,
) -> None:
    root = tmp_path / "parquet"
    _write_sources(root)
    plan = _seal_plan(root)
    prepared = prepare_semantic_tables(
        parquet_dir=root,
        plan=plan,
        enforce_planned_hashes=True,
    )

    assert len(plan.entity_tables) == 20
    assert len(plan.relationship_tables) == 14
    assert len(prepared) == 34
    chunk_table = prepared["kg_entity_document_chunk"][0]
    assert chunk_table.column_names == ["chunk_id", "content"]
    assert chunk_table["chunk_id"].to_pylist() == ["chunk:published"]
    edge = prepared["kg_relationship_evidenced_by"][0].to_pylist()
    assert edge[0]["target_entity_id"] == chunk_table["chunk_id"][0].as_py()
    assert pq.read_table(root / "relationships.parquet").num_rows == 1723


def test_34_contract_tables_bind_graph_without_raw_candidates(tmp_path: Path) -> None:
    root = tmp_path / "parquet"
    _write_sources(root)
    plan = _seal_plan(root)
    contract_tables = [*plan.entity_tables, *plan.relationship_tables]
    assert len(contract_tables) == 34
    assert {
        table.source_table_name for table in contract_tables
    } == {"semantic_entities", "semantic_relationships", "chunks"}
    entity_names = [f"Type{index}" for index in range(19)] + [
        "DocumentChunk"
    ]
    node_bindings = {
        name: {
            "table": table.table_name,
            "entity_id_column": table.entity_id_column,
            "property_columns": [
                column.column_name for column in table.columns
            ],
        }
        for name, table in zip(entity_names, plan.entity_tables)
    }
    relationship_pairs = [
        {
            "name": f"rel_{index}",
            "source_type": f"Type{index}",
            "target_type": f"Type{(index + 1) % 19}",
            "table": plan.relationship_tables[index].table_name,
            "source_entity_id_column": "source_entity_id",
            "target_entity_id_column": "target_entity_id",
            "property_columns": ["evidence_id"],
        }
        for index in range(13)
    ]
    relationship_pairs.append({
        "name": "evidenced_by",
        "source_type": "Type0",
        "target_type": "DocumentChunk",
        "table": plan.relationship_tables[-1].table_name,
        "source_entity_id_column": "source_entity_id",
        "target_entity_id_column": "target_entity_id",
        "property_columns": ["evidence_id"],
    })
    graph_parts = build_graph_model_parts(
        entity_types=entity_names,
        relationship_pairs=relationship_pairs,
        workspace_id="workspace",
        lakehouse_item_id="lakehouse",
        schema="dbo",
        node_table_bindings=node_bindings,
    )
    data_sources = next(
        part["payload_json"]
        for part in graph_parts
        if part["path"] == "dataSources.json"
    )
    graph_definition = next(
        part["payload_json"]
        for part in graph_parts
        if part["path"] == "graphDefinition.json"
    )
    assert len(data_sources["dataSources"]) == 34
    assert len(graph_definition["nodeTables"]) == 20
    assert len(graph_definition["edgeTables"]) == 14
    with pytest.raises(ValueError, match="raw candidate"):
        resolve_schema2_source_parquet(root, "relationships")


def test_evidenced_by_requires_evidence_chunk_endpoint_equality(
    tmp_path: Path,
) -> None:
    root = tmp_path / "parquet"
    _write_sources(root)
    evidence = pa.table({
        "evidence_id": ["evidence:1"],
        "chunk_id": ["chunk:unpublished"],
        "text": ["wrong chunk"],
    })
    pq.write_table(evidence, root / "evidence.parquet")
    with pytest.raises(
        PersistedProjectionError,
        match="non-serving row",
    ):
        prepare_semantic_tables(
            parquet_dir=root,
            plan=_base_plan(),
            enforce_planned_hashes=False,
            strict_schema2=True,
        )


def test_partial_write_emits_failure_evidence_for_every_table(
    tmp_path: Path,
) -> None:
    root = tmp_path / "parquet"
    _write_sources(root)
    plan = _seal_plan(root)
    written: dict[str, Any] = {}
    ordered = sorted(
        table.table_name
        for table in [*plan.entity_tables, *plan.relationship_tables]
    )

    def writer(name: str, table: Any) -> None:
        if name == ordered[1]:
            raise RuntimeError("Bearer abcdefghijklmnopqrstuvwxyz")
        written[name] = table

    receipt = deploy_schema2_materialization(
        environment="test",
        parquet_dir=root,
        plan=plan,
        semantic_model_manifest_hash=_HASH_B,
        semantic_crosswalk_hash=_HASH_C,
        workspace_id="workspace",
        lakehouse_item_id="lakehouse",
        schema="dbo",
        table_writer=writer,
        table_reader=_Reader(written),
    )

    assert receipt.status == "failed"
    assert len(receipt.tables) == 34
    assert {table.status for table in receipt.tables} == {
        "failed",
        "not_attempted",
    }
    output = tmp_path / "materialization.json"
    write_safe_receipt(output, receipt)
    text = output.read_text(encoding="utf-8")
    assert "Bearer abcdefghijklmnopqrstuvwxyz" not in text
    assert "[REDACTED]" in text
    assert "secret source text" not in text


def test_stale_typed_table_cannot_receive_success_receipt(tmp_path: Path) -> None:
    root = tmp_path / "parquet"
    _write_sources(root)
    plan = _seal_plan(root)
    written: dict[str, Any] = {}

    def writer(name: str, table: Any) -> None:
        written[name] = table

    stale_name = sorted(
        table.table_name
        for table in [*plan.entity_tables, *plan.relationship_tables]
    )[0]

    class StaleReader(_Reader):
        def read_table(self, *args: Any, **kwargs: Any) -> Any:
            table = super().read_table(*args, **kwargs)
            name = args[3]
            return table.slice(0, 0) if name == stale_name else table

    receipt = deploy_schema2_materialization(
        environment="test",
        parquet_dir=root,
        plan=plan,
        semantic_model_manifest_hash=_HASH_B,
        semantic_crosswalk_hash=_HASH_C,
        workspace_id="workspace",
        lakehouse_item_id="lakehouse",
        schema="dbo",
        table_writer=writer,
        table_reader=StaleReader(written),
    )
    assert receipt.status == "failed"
    assert next(
        table for table in receipt.tables if table.table_name == stale_name
    ).failure_code == "TABLE_READBACK_FAILED"


def test_plan_shrink_fails_on_extra_owned_table_but_ignores_unrelated(
    tmp_path: Path,
) -> None:
    root = tmp_path / "parquet"
    _write_sources(root)
    plan = _seal_plan(root)
    written: dict[str, Any] = {}

    def writer(name: str, table: Any) -> None:
        written[name] = table

    reader = _Reader(
        written,
        extra_tables={"kg_entity_removed_type", "sales_fact"},
    )
    receipt = deploy_schema2_materialization(
        environment="test",
        parquet_dir=root,
        plan=plan,
        semantic_model_manifest_hash=_HASH_B,
        semantic_crosswalk_hash=_HASH_C,
        workspace_id="workspace",
        lakehouse_item_id="lakehouse",
        schema="dbo",
        table_writer=writer,
        table_reader=reader,
    )

    assert receipt.status == "failed"
    assert "kg_entity_removed_type" in receipt.actual_managed_tables
    assert "sales_fact" not in receipt.actual_managed_tables
    assert receipt.expected_managed_tables == sorted(written)
    with pytest.raises(
        PersistedProjectionError,
        match="namespace differs",
    ):
        validate_bound_tables(
            plan=plan,
            workspace_id="workspace",
            lakehouse_item_id="lakehouse",
            schema="dbo",
            table_reader=reader,
            managed_table_prefix="kg_",
        )


def test_missing_or_failed_materialization_receipt_cannot_authorize_ontology(
    tmp_path: Path,
) -> None:
    root = tmp_path / "parquet"
    _write_sources(root)
    plan = _seal_plan(root)
    with pytest.raises(PersistedProjectionError, match="receipt"):
        load_materialization_receipt(
            tmp_path / "missing.json",
            plan=plan,
            semantic_model_manifest_hash=_HASH_B,
            semantic_crosswalk_hash=_HASH_C,
            workspace_id="workspace",
            lakehouse_item_id="lakehouse",
            schema="dbo",
        )

    def fail_write(_name: str, _table: Any) -> None:
        raise RuntimeError("write failed")

    failed = deploy_schema2_materialization(
        environment="test",
        parquet_dir=root,
        plan=plan,
        semantic_model_manifest_hash=_HASH_B,
        semantic_crosswalk_hash=_HASH_C,
        workspace_id="workspace",
        lakehouse_item_id="lakehouse",
        schema="dbo",
        table_writer=fail_write,
        table_reader=None,
    )
    receipt_path = tmp_path / "failed.json"
    write_safe_receipt(receipt_path, failed)
    with pytest.raises(PersistedProjectionError, match="complete deployment"):
        load_materialization_receipt(
            receipt_path,
            plan=plan,
            semantic_model_manifest_hash=_HASH_B,
            semantic_crosswalk_hash=_HASH_C,
            workspace_id="workspace",
            lakehouse_item_id="lakehouse",
            schema="dbo",
        )


@pytest.mark.parametrize(
    ("status", "expected_message"),
    [
        ("failed", "status"),
        ("succeeded", "hash"),
    ],
)
def test_projection_receipt_missing_or_stale_fails_closed(
    tmp_path: Path,
    status: str,
    expected_message: str,
) -> None:
    root = tmp_path / "parquet"
    _write_sources(root)
    semantic_entities = pq.read_table(root / "semantic_entities.parquet")
    semantic_relationships = pq.read_table(
        root / "semantic_relationships.parquet"
    )
    evidence = pq.read_table(root / "evidence.parquet")
    receipt = {
        "receipt_schema_version": "1.0",
        "schema_mode": "schema2",
        "status": status,
        "active_contract_hash": _HASH_A,
        "serving_counts": {
            "semantic_entities": semantic_entities.num_rows,
            "semantic_relationships": semantic_relationships.num_rows,
        },
        "aggregate_table_hashes": {
            "semantic_entities": arrow_table_hash(
                semantic_entities, "entity_id"
            ),
            "semantic_relationships": (
                _HASH_B
                if status == "succeeded"
                else arrow_table_hash(
                    semantic_relationships, "relationship_id"
                )
            ),
            "input_evidence": arrow_table_hash(evidence, "evidence_id"),
        },
        "invariants": [
            {"gate": f"SEM-{number}", "passed": True}
            for number in range(100, 105)
        ],
    }
    receipt_path = root / "semantic-projection-receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(PersistedProjectionError) as exc_info:
        load_schema2_projection_authority(
            parquet_dir=root,
            receipt_path=receipt_path,
            expected_contract_hash=_HASH_A,
            support_tables={"chunks"},
        )
    assert expected_message in str(exc_info.value).lower()


def test_source_taxonomy_hashes_support_schema_and_count(tmp_path: Path) -> None:
    root = tmp_path / "parquet"
    _write_sources(root)
    chunk = pq.read_table(root / "chunks.parquet")
    authority = _source_authority(
        root,
        "chunks",
        "canonical_support_entity",
        "chunk_id",
    )
    assert authority.row_count == 2
    assert authority.table_hash == arrow_table_hash(chunk, "chunk_id")
    assert authority.schema_hash == arrow_schema_hash(chunk.schema)
    assert canonical_hash(authority.model_dump(mode="json")).startswith("sha256:")


def test_validation_support_cannot_become_an_entity_source(
    tmp_path: Path,
) -> None:
    root = tmp_path / "parquet"
    _write_sources(root)
    plan = _seal_plan(root)
    payload = plan.model_dump(mode="json")
    payload["entity_tables"][0]["source_table_name"] = "evidence"
    payload["entity_tables"][0]["source_category"] = "validation_support"
    with pytest.raises(
        ValueError,
        match="source categories are invalid",
    ):
        MaterializationPlan.model_validate(payload)


def test_onelake_table_listing_is_injectable_and_deterministic() -> None:
    client = OneLakeDeltaClient(
        token_provider=lambda: "unused",
        path_lister=lambda _workspace, _lakehouse, _schema: [
            "sales_fact",
            "kg_entity_b",
            "kg_entity_a",
            "kg_entity_a",
        ],
    )
    assert client.list_tables("workspace", "lakehouse", "dbo") == [
        "kg_entity_a",
        "kg_entity_b",
        "sales_fact",
    ]


def test_onelake_table_listing_uses_storage_bearer_token() -> None:
    response = MagicMock()
    response.ok = True
    response.json.return_value = {
        "paths": [
            {
                "name": "lakehouse/Tables/dbo/kg_entity_a",
                "isDirectory": True,
            }
        ]
    }
    response.headers = {}
    client = OneLakeDeltaClient(token_provider=lambda: "token-value")
    with patch("requests.get", return_value=response) as request:
        assert client.list_tables(
            "workspace",
            "lakehouse",
            "dbo",
        ) == ["kg_entity_a"]
    assert request.call_args.kwargs["headers"]["Authorization"] == (
        "Bearer token-value"
    )
