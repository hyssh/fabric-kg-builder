"""Unit tests for deploy.onelake_writer — mock mode only (no network calls).

Verifies:
- mock=True returns STATUS_PLANNED for all tables (no network)
- mock=True when parquet files absent still returns planned (no network)
- live path with missing parquet returns STATUS_SKIPPED
- custom token_provider callable is used when supplied (live path)
- empty tables list returns empty dict
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fabric_kg_builder.deploy.onelake_writer import (
    STATUS_OK,
    STATUS_PLANNED,
    STATUS_SKIPPED,
    deploy_parquet_to_onelake,
)

_WS_ID = "ws-test-0000-1111-2222"
_LH_ID = "lh-test-aaaa-bbbb-cccc"
_SCHEMA = "dbo"
_TABLES = ["entities", "chunks"]


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _write_parquet(directory: Path, name: str) -> None:
    """Write a minimal valid Parquet file for testing."""
    directory.mkdir(parents=True, exist_ok=True)
    table = pa.table({"id": ["a", "b"], "val": [1, 2]})
    pq.write_table(table, str(directory / f"{name}.parquet"))


# ---------------------------------------------------------------------------
# Mock-mode tests — no network calls ever
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOneLakeWriterMock:
    def test_mock_returns_planned_for_all_tables(self, tmp_path: Path) -> None:
        """mock=True returns STATUS_PLANNED for every table regardless of files present."""
        results = deploy_parquet_to_onelake(
            parquet_dir=tmp_path,
            workspace_id=_WS_ID,
            lakehouse_item_id=_LH_ID,
            schema=_SCHEMA,
            tables=_TABLES,
            mock=True,
        )
        assert results == {"entities": STATUS_PLANNED, "chunks": STATUS_PLANNED}

    def test_mock_empty_tables_returns_empty(self, tmp_path: Path) -> None:
        """mock=True with empty tables list returns empty dict."""
        results = deploy_parquet_to_onelake(
            parquet_dir=tmp_path,
            workspace_id=_WS_ID,
            lakehouse_item_id=_LH_ID,
            schema=_SCHEMA,
            tables=[],
            mock=True,
        )
        assert results == {}

    def test_mock_no_network_call(self, tmp_path: Path) -> None:
        """mock=True must not touch the network."""
        import socket

        with patch.object(socket, "getaddrinfo", side_effect=AssertionError("NETWORK BLOCKED")):
            results = deploy_parquet_to_onelake(
                parquet_dir=tmp_path,
                workspace_id=_WS_ID,
                lakehouse_item_id=_LH_ID,
                schema=_SCHEMA,
                tables=_TABLES,
                mock=True,
            )
        assert all(v == STATUS_PLANNED for v in results.values())

    def test_mock_does_not_require_parquet_files(self, tmp_path: Path) -> None:
        """mock=True succeeds even if no .parquet files exist in parquet_dir."""
        results = deploy_parquet_to_onelake(
            parquet_dir=tmp_path / "nonexistent",
            workspace_id=_WS_ID,
            lakehouse_item_id=_LH_ID,
            schema=_SCHEMA,
            tables=["entities"],
            mock=True,
        )
        assert results["entities"] == STATUS_PLANNED

    def test_mock_custom_schema_name(self, tmp_path: Path) -> None:
        """schema parameter is accepted and does not affect mock output."""
        results = deploy_parquet_to_onelake(
            parquet_dir=tmp_path,
            workspace_id=_WS_ID,
            lakehouse_item_id=_LH_ID,
            schema="custom_schema",
            tables=["chunks"],
            mock=True,
        )
        assert results["chunks"] == STATUS_PLANNED

    def test_mock_all_eight_lakehouse_tables(self, tmp_path: Path) -> None:
        """mock=True writer accepts any 8-table list when no projection is given.

        Note: this tests the raw writer API (no projection).  The deploy-lakehouse
        command uses the lean 7-table LAKEHOUSE_TABLE_PROJECTION which excludes chunks.
        """
        all_tables = [
            "source_files", "document_elements", "chunks", "entities",
            "relationships", "evidence", "visual_assets", "visual_regions",
        ]
        results = deploy_parquet_to_onelake(
            parquet_dir=tmp_path,
            workspace_id=_WS_ID,
            lakehouse_item_id=_LH_ID,
            schema=_SCHEMA,
            tables=all_tables,
            mock=True,
            # No projection — raw writer accepts any table list
        )
        assert len(results) == 8
        assert all(v == STATUS_PLANNED for v in results.values())


# ---------------------------------------------------------------------------
# Live-mode tests — mock the deltalake + token calls
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOneLakeWriterLive:
    def test_live_skips_missing_parquet(self, tmp_path: Path) -> None:
        """Live mode returns STATUS_SKIPPED when .parquet file is absent."""
        results = deploy_parquet_to_onelake(
            parquet_dir=tmp_path,
            workspace_id=_WS_ID,
            lakehouse_item_id=_LH_ID,
            schema=_SCHEMA,
            tables=["chunks"],
            mock=False,
        )
        assert results["chunks"] == STATUS_SKIPPED

    def test_live_calls_write_deltalake_with_correct_path(self, tmp_path: Path) -> None:
        """Live mode calls write_deltalake with the abfss:// path."""
        _write_parquet(tmp_path, "entities")

        mock_write = MagicMock()
        mock_token_provider = MagicMock(return_value="fake-token")

        with patch("deltalake.write_deltalake", mock_write):
            results = deploy_parquet_to_onelake(
                parquet_dir=tmp_path,
                workspace_id=_WS_ID,
                lakehouse_item_id=_LH_ID,
                schema=_SCHEMA,
                tables=["entities"],
                token_provider=mock_token_provider,
                mock=False,
            )

        assert results["entities"] == STATUS_OK
        mock_write.assert_called_once()
        call_args = mock_write.call_args
        path_arg = call_args[0][0] if call_args[0] else call_args[1].get("table_or_uri", "")
        assert _WS_ID in path_arg
        assert _LH_ID in path_arg
        assert f"Tables/{_SCHEMA}/entities" in path_arg

    def test_live_uses_custom_token_provider(self, tmp_path: Path) -> None:
        """token_provider callable is invoked when provided."""
        _write_parquet(tmp_path, "chunks")
        mock_token_provider = MagicMock(return_value="test-bearer-token")

        with patch("deltalake.write_deltalake"):
            deploy_parquet_to_onelake(
                parquet_dir=tmp_path,
                workspace_id=_WS_ID,
                lakehouse_item_id=_LH_ID,
                schema=_SCHEMA,
                tables=["chunks"],
                token_provider=mock_token_provider,
                mock=False,
            )

        mock_token_provider.assert_called_once()

    def test_live_storage_options_include_fabric_endpoint(self, tmp_path: Path) -> None:
        """storage_options passed to write_deltalake include use_fabric_endpoint=true."""
        _write_parquet(tmp_path, "entities")
        captured = {}

        def _capture(*args, **kwargs):
            captured.update(kwargs)

        with patch("deltalake.write_deltalake", side_effect=_capture):
            deploy_parquet_to_onelake(
                parquet_dir=tmp_path,
                workspace_id=_WS_ID,
                lakehouse_item_id=_LH_ID,
                schema=_SCHEMA,
                tables=["entities"],
                token_provider=lambda: "tok",
                mock=False,
            )

        opts = captured.get("storage_options", {})
        assert opts.get("use_fabric_endpoint") == "true"
        assert "bearer_token" in opts

    def test_live_error_recorded_per_table(self, tmp_path: Path) -> None:
        """Live mode records error status when write_deltalake raises."""
        _write_parquet(tmp_path, "entities")

        with patch(
            "deltalake.write_deltalake",
            side_effect=RuntimeError("connection refused"),
        ):
            results = deploy_parquet_to_onelake(
                parquet_dir=tmp_path,
                workspace_id=_WS_ID,
                lakehouse_item_id=_LH_ID,
                schema=_SCHEMA,
                tables=["entities"],
                token_provider=lambda: "tok",
                mock=False,
            )

        assert results["entities"].startswith("error")
        assert "connection refused" in results["entities"]
