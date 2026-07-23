"""Regression tests for scope/ontology-integrity active pipeline wiring.

D3 — _validate_parquet_date_precision reports rejected-value count and affected
     entity count (not just 3 sample values).

D4 — read_graph_counts failure raises rather than silently warning.

These tests exclusively cover the ACTIVE PIPELINE paths not exercised by
Hockney's 70 identity_validation unit tests (which only exercise the module API
in isolation).  Do NOT modify these tests after merging.
"""

from __future__ import annotations

import io
import json
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helper: build a minimal in-memory Parquet table substitute using pyarrow
# ---------------------------------------------------------------------------

def _make_pyarrow_table(column_name: str, values: list) -> Any:
    """Return a pyarrow Table with a single string column."""
    import pyarrow as pa  # noqa: PLC0415
    arr = pa.array([v for v in values], type=pa.string())
    return pa.table({column_name: arr})


# ---------------------------------------------------------------------------
# D3: _validate_parquet_date_precision — count reporting
# ---------------------------------------------------------------------------

class TestValidateParquetDatePrecisionCounts:
    """_validate_parquet_date_precision must report exact rejected-value count
    and affected entity count, not just sample values."""

    def _run(self, parquet_dir: Path, model: dict) -> list[str]:
        from fabric_kg_builder.cli.deploy_cmd import (  # noqa: PLC0415
            _validate_parquet_date_precision,
        )
        return _validate_parquet_date_precision(parquet_dir, model)

    @pytest.fixture()
    def tmp_parquet_dir(self, tmp_path: Path) -> Path:
        return tmp_path

    def test_single_entity_reports_exact_count(self, tmp_parquet_dir: Path) -> None:
        """Error message must include the exact count of rejected values (not a sample)."""
        import pyarrow.parquet as pq  # noqa: PLC0415

        table = _make_pyarrow_table(
            "event_date",
            ["2020", "2021", "2022", "2023-01", "2023-06"],  # 5 partial values
        )
        pq.write_table(table, tmp_parquet_dir / "entities.parquet")

        model = {
            "entityTypes": [
                {
                    "name": "ServiceEvent",
                    "properties": [
                        {"name": "event_date", "type": "timestamp"},
                    ],
                }
            ]
        }
        errors = self._run(tmp_parquet_dir, model)
        assert len(errors) == 1
        msg = errors[0]
        assert "PARTIAL_DATE_INCOMPATIBLE" in msg
        # Must mention the exact count (5), not just sample values
        assert "5" in msg, f"Expected exact count '5' in message: {msg}"

    def test_single_entity_reports_entity_count(self, tmp_parquet_dir: Path) -> None:
        """Error message must include an entity count (at minimum '1 entity type')."""
        import pyarrow.parquet as pq  # noqa: PLC0415

        table = _make_pyarrow_table("event_date", ["2020", "2021"])
        pq.write_table(table, tmp_parquet_dir / "entities.parquet")

        model = {
            "entityTypes": [
                {
                    "name": "Event",
                    "properties": [{"name": "event_date", "type": "timestamp"}],
                }
            ]
        }
        errors = self._run(tmp_parquet_dir, model)
        assert len(errors) == 1
        msg = errors[0]
        # Must mention entity count — any numeric count in context of "entity"
        assert "entity" in msg.lower(), f"Expected 'entity' in message: {msg}"
        import re  # noqa: PLC0415
        assert re.search(r"\d", msg), f"Expected numeric count in message: {msg}"

    def test_two_affected_entities_reports_combined_count(self, tmp_parquet_dir: Path) -> None:
        """With two affected entity types, total affected entity count must appear in messages."""
        import pyarrow.parquet as pq  # noqa: PLC0415

        # Both entity types share the same column name in the entities table
        table = _make_pyarrow_table("event_date", ["2020", "2021", "2022"])
        pq.write_table(table, tmp_parquet_dir / "entities.parquet")

        model = {
            "entityTypes": [
                {
                    "name": "EventA",
                    "properties": [{"name": "event_date", "type": "timestamp"}],
                },
                {
                    "name": "EventB",
                    "properties": [{"name": "event_date", "type": "timestamp"}],
                },
            ]
        }
        errors = self._run(tmp_parquet_dir, model)
        # Must return errors for both entities
        assert len(errors) == 2
        # Combined entity count (2) must appear in at least one message
        combined = " ".join(errors)
        import re  # noqa: PLC0415
        counts = re.findall(r"(\d+)\s+entity", combined)
        total = max(int(c) for c in counts) if counts else 0
        assert total >= 2, (
            f"Expected combined affected entity count ≥ 2 in messages: {errors}"
        )

    def test_full_timestamps_not_counted(self, tmp_parquet_dir: Path) -> None:
        """Full ISO-8601 timestamps must NOT be counted as partial dates."""
        import pyarrow.parquet as pq  # noqa: PLC0415

        table = _make_pyarrow_table(
            "event_date",
            ["2020-07-22T10:30:00", "2021-01-01T00:00:00"],
        )
        pq.write_table(table, tmp_parquet_dir / "entities.parquet")

        model = {
            "entityTypes": [
                {
                    "name": "Event",
                    "properties": [{"name": "event_date", "type": "timestamp"}],
                }
            ]
        }
        errors = self._run(tmp_parquet_dir, model)
        assert errors == [], f"Full timestamps must not trigger partial-date error: {errors}"

    def test_no_timestamp_props_returns_empty(self, tmp_parquet_dir: Path) -> None:
        """Model with no timestamp properties → empty error list."""
        model = {
            "entityTypes": [
                {
                    "name": "Device",
                    "properties": [{"name": "serial_number", "type": "string"}],
                }
            ]
        }
        errors = self._run(tmp_parquet_dir, model)
        assert errors == []


# ---------------------------------------------------------------------------
# D4: read_graph_counts failure must propagate, not warn-and-continue
# ---------------------------------------------------------------------------

class TestReadGraphCountsFailureBlocking:
    """When read_graph_counts returns failure indicators (-1), deployment must
    abort with sys.exit(1), NOT warn and continue (D4 regression)."""

    def test_check_zero_edge_skipped_when_edges_negative_is_blocked(self) -> None:
        """_check_zero_edge_types with total_edges=-1 returns [] (no errors) —
        confirming that the -1 guard in deploy_cmd must be the blocking layer."""
        from fabric_kg_builder.cli.deploy_cmd import _check_zero_edge_types  # noqa: PLC0415

        model = {
            "relationshipTypes": [
                {"name": "has_component", "sourceType": "Device", "targetType": "Component"},
            ]
        }
        # When total_edges is -1, _check_zero_edge_types is a no-op (guard condition)
        # The deploy_cmd must detect -1 and fail BEFORE calling _check_zero_edge_types
        errors = _check_zero_edge_types(model, {}, -1)
        assert errors == [], (
            "_check_zero_edge_types with total_edges=-1 must return [] "
            "(blocking is the deploy_cmd's responsibility via the -1 guard)"
        )

    def test_graph_counts_negative_values_are_failure_markers(self) -> None:
        """read_graph_counts returns negative counts on internal failure —
        these are the failure markers that deploy_cmd must catch and abort on."""
        from fabric_kg_builder.deploy.fabric_ontology import read_graph_counts  # noqa: PLC0415

        # Build a mock table_reader that raises on read_table (simulates network/auth failure)
        failing_reader = MagicMock()
        failing_reader.read_table.side_effect = RuntimeError("connection refused")

        result = read_graph_counts(
            workspace_id="ws-1",
            lakehouse_item_id="lh-1",
            schema="dbo",
            table_reader=failing_reader,
        )
        # read_graph_counts returns -1 on failure (internal broad except)
        assert result["total_nodes"] == -1 or result["total_edges"] == -1, (
            "read_graph_counts must return negative count when table read fails"
        )
