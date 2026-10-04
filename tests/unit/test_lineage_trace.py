"""Tests for lineage/trace.py — trace_record backward/forward traversal."""
from __future__ import annotations

import pytest

from fabric_kg_builder.lineage.trace import (
    BrokenEdge,
    TraceResult,
    _build_pk_index,
    _find_record_tables,
    _is_backward_terminal,
    _ordered_backward_edges,
    trace_record,
    trace_asset,
    trace_run,
)


# ---------------------------------------------------------------------------
# Minimal table fixtures for testing
# ---------------------------------------------------------------------------

def _make_tables() -> dict:
    """Minimal graph of: asset → asset_version → source_file → chunk → entity."""
    return {
        "assets": [
            {"asset_id": "asset-001", "domain_hash": "dhash"},
        ],
        "asset_versions": [
            {"asset_version_id": "av-001", "asset_id": "asset-001", "blob_uri": "blob://x"},
        ],
        "processing_runs": [
            {"run_id": "run-001", "asset_id": "asset-001", "parent_run_id": None},
        ],
        "source_files": [
            {
                "source_file_id": "sf-001",
                "asset_id": "asset-001",
                "asset_version_id": "av-001",
                "run_id": "run-001",
            }
        ],
        "chunks": [
            {
                "chunk_id": "chunk-001",
                "source_file_id": "sf-001",
                "asset_id": "asset-001",
                "asset_version_id": "av-001",
                "run_id": "run-001",
            }
        ],
        "entities": [
            {
                "entity_id": "ent-001",
                "asset_id": "asset-001",
                "asset_version_id": "av-001",
                "run_id": "run-001",
            }
        ],
    }


# ---------------------------------------------------------------------------
# _build_pk_index
# ---------------------------------------------------------------------------

class TestBuildPkIndex:
    def test_indexes_known_tables(self):
        tables = {"assets": [{"asset_id": "a1"}]}
        idx = _build_pk_index(tables)
        assert "assets" in idx
        assert "a1" in idx["assets"]

    def test_skips_unknown_tables(self):
        tables = {"unknown_table": [{"some_id": "x"}]}
        idx = _build_pk_index(tables)
        assert "unknown_table" not in idx

    def test_empty_tables(self):
        idx = _build_pk_index({})
        assert idx == {}


# ---------------------------------------------------------------------------
# _find_record_tables
# ---------------------------------------------------------------------------

class TestFindRecordTables:
    def test_finds_single_table(self):
        tables = {"assets": [{"asset_id": "a1"}], "asset_versions": [{"asset_version_id": "av1"}]}
        idx = _build_pk_index(tables)
        found = _find_record_tables("a1", idx)
        assert found == ["assets"]

    def test_not_found_returns_empty(self):
        idx = _build_pk_index({"assets": [{"asset_id": "a1"}]})
        assert _find_record_tables("nonexistent", idx) == []


# ---------------------------------------------------------------------------
# _is_backward_terminal
# ---------------------------------------------------------------------------

class TestIsBackwardTerminal:
    def test_assets_is_terminal(self):
        assert _is_backward_terminal("assets", {}) is True

    def test_asset_versions_is_terminal(self):
        assert _is_backward_terminal("asset_versions", {}) is True

    def test_processing_run_terminal_when_no_parent(self):
        assert _is_backward_terminal("processing_runs", {"parent_run_id": None}) is True

    def test_processing_run_not_terminal_when_has_parent(self):
        assert _is_backward_terminal("processing_runs", {"parent_run_id": "run-000"}) is False

    def test_chunk_not_terminal(self):
        assert _is_backward_terminal("chunks", {}) is False


# ---------------------------------------------------------------------------
# TraceResult
# ---------------------------------------------------------------------------

class TestTraceResult:
    def test_complete_empty_path(self):
        result = TraceResult(record_id="x", direction="backward")
        assert result.is_complete is True
        assert result.broken_edges == []

    def test_deprecated_aliases(self):
        result = TraceResult(record_id="x", direction="backward", table_name="entities")
        assert result.start_record_id == "x"
        assert result.start_table == "entities"
        assert result.complete is True

    def test_as_dict_structure(self):
        result = TraceResult(
            record_id="x",
            direction="backward",
            path=[("entities", "x"), ("assets", "a1")],
        )
        d = result.as_dict()
        assert d["record_id"] == "x"
        assert d["path_length"] == 2
        assert d["broken_edge"] is None

    def test_as_json(self):
        import json
        result = TraceResult(record_id="x", direction="backward")
        js = result.as_json()
        data = json.loads(js)
        assert data["record_id"] == "x"

    def test_broken_edges_property(self):
        edge = BrokenEdge(
            from_table="chunks", from_record_id="c1",
            fk_field="source_file_id", expected_table="source_files",
            missing_id="sf-missing",
        )
        result = TraceResult(record_id="c1", direction="backward")
        result.broken_edge = ("source_files", "sf-missing")
        result._broken_edge_detail = edge
        assert len(result.broken_edges) == 1
        assert result.broken_edges[0].missing_id == "sf-missing"


class TestBrokenEdgeStr:
    def test_str_representation(self):
        edge = BrokenEdge(
            from_table="chunks", from_record_id="c1",
            fk_field="source_file_id", expected_table="source_files",
            missing_id="sf-missing",
        )
        s = str(edge)
        assert "chunks" in s
        assert "source_files" in s
        assert "sf-missing" in s


# ---------------------------------------------------------------------------
# trace_record — basic backward traversal
# ---------------------------------------------------------------------------

class TestTraceRecord:
    def test_invalid_direction_raises(self):
        with pytest.raises(ValueError, match="direction must be"):
            trace_record("x", {}, direction="invalid")

    def test_unknown_table_raises(self):
        with pytest.raises(KeyError, match="Unknown table"):
            trace_record("x", {}, table_name="nonexistent_table")

    def test_missing_record_returns_incomplete(self):
        result = trace_record("nonexistent", {}, table_name="entities")
        assert result.is_complete is False
        assert result.broken_edge is not None

    def test_asset_is_terminal(self):
        tables = {"assets": [{"asset_id": "a1"}]}
        result = trace_record("a1", tables, table_name="assets")
        assert result.is_complete is True
        assert result.path == [("assets", "a1")]

    def test_backward_traversal_from_entity_to_asset(self):
        tables = _make_tables()
        result = trace_record("ent-001", tables, table_name="entities")
        assert result.is_complete is True
        tables_visited = {t for t, _ in result.path}
        assert "entities" in tables_visited
        # Entity traverses backward via asset_id → asset_versions or assets
        assert len(result.path) >= 1

    def test_backward_traversal_from_chunk(self):
        tables = _make_tables()
        result = trace_record("chunk-001", tables, table_name="chunks")
        assert result.is_complete is True
        tables_visited = {t for t, _ in result.path}
        assert "chunks" in tables_visited

    def test_broken_edge_missing_source_file(self):
        tables = _make_tables()
        # Add chunk with non-existent source_file_id
        tables["chunks"].append({
            "chunk_id": "chunk-broken",
            "source_file_id": "sf-missing",
            "asset_id": "asset-001",
            "asset_version_id": "av-001",
            "run_id": "run-001",
        })
        result = trace_record("chunk-broken", tables, table_name="chunks")
        assert result.is_complete is False
        assert result.broken_edge is not None

    def test_conflicting_table_hints_raise(self):
        tables = _make_tables()
        with pytest.raises(ValueError, match="Conflicting table hints"):
            trace_record("ent-001", tables, table_name="entities", table_hint="chunks")

    def test_ambiguous_record_raises(self):
        # Same ID in two tables
        tables = {
            "assets": [{"asset_id": "shared-id"}],
            "asset_versions": [{"asset_version_id": "shared-id", "asset_id": "x", "blob_uri": "b"}],
        }
        with pytest.raises(LookupError, match="ambiguous"):
            trace_record("shared-id", tables)

    def test_direction_forward(self):
        tables = _make_tables()
        result = trace_record("asset-001", tables, table_name="assets", direction="forward")
        assert result.direction == "forward"
        assert len(result.path) >= 1

    def test_as_dict_has_path(self):
        tables = _make_tables()
        result = trace_record("ent-001", tables, table_name="entities")
        d = result.as_dict()
        assert "path" in d
        assert d["path_length"] > 0


# ---------------------------------------------------------------------------
# trace_asset
# ---------------------------------------------------------------------------

class TestTraceAsset:
    def test_traces_from_asset(self):
        tables = _make_tables()
        result = trace_asset(tables, "asset-001")
        assert result.table_name == "assets"
        assert result.is_complete is True

    def test_missing_asset(self):
        result = trace_asset({}, "nonexistent")
        assert result.is_complete is False


# ---------------------------------------------------------------------------
# trace_run
# ---------------------------------------------------------------------------

class TestTraceRun:
    def test_traces_from_run(self):
        tables = _make_tables()
        result = trace_run(tables, "run-001")
        assert result.table_name == "processing_runs"
        # processing run with no parent is terminal in forward direction
        assert len(result.path) >= 1

    def test_missing_run(self):
        result = trace_run({}, "nonexistent")
        assert result.is_complete is False


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------

class TestCycleDetection:
    def test_cycle_in_processing_runs(self):
        # Create a self-referencing run
        tables = {
            "processing_runs": [
                {"run_id": "run-001", "parent_run_id": "run-002", "asset_id": "a1"},
                {"run_id": "run-002", "parent_run_id": "run-001", "asset_id": "a1"},
            ]
        }
        # Should detect the cycle and stop without infinite loop
        result = trace_record("run-001", tables, table_name="processing_runs")
        assert result.cycle_detected is True
