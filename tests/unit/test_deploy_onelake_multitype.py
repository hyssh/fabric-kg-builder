"""Tests for deploy/onelake_multitype.py — mock mode and path helpers."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fabric_kg_builder.deploy.onelake_multitype import (
    STATUS_ERROR,
    STATUS_OK,
    STATUS_PLANNED,
    STATUS_SKIPPED,
    _onelake_path,
    materialize_multitype_tables,
)


# ---------------------------------------------------------------------------
# _onelake_path
# ---------------------------------------------------------------------------

class TestOnelakePath:
    def test_format(self):
        path = _onelake_path("ws-001", "lh-001", "dbo", "my_table")
        assert "ws-001" in path
        assert "lh-001" in path
        assert "dbo" in path
        assert "my_table" in path

    def test_starts_with_abfss(self):
        path = _onelake_path("ws-001", "lh-001", "dbo", "table")
        assert path.startswith("abfss://")

    def test_different_schemas(self):
        p1 = _onelake_path("ws", "lh", "dbo", "table")
        p2 = _onelake_path("ws", "lh", "myschema", "table")
        assert p1 != p2


# ---------------------------------------------------------------------------
# materialize_multitype_tables — mock mode
# ---------------------------------------------------------------------------

def _make_plan(entity_names: list[str], pair_names: list[str] | None = None) -> MagicMock:
    plan = MagicMock()
    entity_types = []
    for name in entity_names:
        et = MagicMock()
        et.table_name = f"entities_{name.lower()}"
        et.type_name = name
        et.source_types = [name]
        entity_types.append(et)
    plan.entity_types = entity_types

    relationship_pairs = []
    for name in (pair_names or []):
        rp = MagicMock()
        rp.table_name = f"rel_{name.lower()}"
        rp.source_type = "Source"
        rp.target_type = "Target"
        relationship_pairs.append(rp)
    plan.relationship_pairs = relationship_pairs
    return plan


class TestMaterializeMultitypeTablesMock:
    def test_mock_returns_planned_for_entity_tables(self, tmp_path):
        plan = _make_plan(["Equipment", "Facility"])
        results = materialize_multitype_tables(
            tmp_path, plan, "ws-001", "lh-001", mock=True
        )
        assert results["entities_equipment"] == STATUS_PLANNED
        assert results["entities_facility"] == STATUS_PLANNED

    def test_mock_returns_planned_for_rel_tables(self, tmp_path):
        plan = _make_plan(["Equipment"], ["proc_step"])
        results = materialize_multitype_tables(
            tmp_path, plan, "ws-001", "lh-001", mock=True
        )
        assert results["rel_proc_step"] == STATUS_PLANNED

    def test_mock_does_not_read_files(self, tmp_path):
        """Mock mode should not read any parquet files."""
        plan = _make_plan(["Equipment"])
        # no parquet files in tmp_path — should not raise
        results = materialize_multitype_tables(
            tmp_path, plan, "ws-001", "lh-001", mock=True
        )
        assert len(results) > 0

    def test_mock_empty_plan(self, tmp_path):
        plan = _make_plan([])
        results = materialize_multitype_tables(
            tmp_path, plan, "ws-001", "lh-001", mock=True
        )
        assert results == {}

    def test_mock_all_entities_planned(self, tmp_path):
        plan = _make_plan(["A", "B", "C"])
        results = materialize_multitype_tables(
            tmp_path, plan, "ws-001", "lh-001", mock=True
        )
        assert len(results) == 3
        assert all(v == STATUS_PLANNED for v in results.values())

    def test_mock_multiple_entity_and_rel_tables(self, tmp_path):
        plan = _make_plan(["Equipment", "Facility"], ["edge_one", "edge_two"])
        results = materialize_multitype_tables(
            tmp_path, plan, "ws-001", "lh-001", mock=True
        )
        assert len(results) == 4


# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------

class TestStatusConstants:
    def test_constants_are_strings(self):
        assert isinstance(STATUS_PLANNED, str)
        assert isinstance(STATUS_OK, str)
        assert isinstance(STATUS_SKIPPED, str)
        assert isinstance(STATUS_ERROR, str)

    def test_constants_distinct(self):
        statuses = {STATUS_PLANNED, STATUS_OK, STATUS_SKIPPED, STATUS_ERROR}
        assert len(statuses) == 4
