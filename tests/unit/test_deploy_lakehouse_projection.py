"""Unit tests for the Lakehouse lean projection.

Verifies:
- LAKEHOUSE_TABLE_PROJECTION excludes chunks entirely
- LAKEHOUSE_TABLES list does not contain chunks
- document_elements projection drops content, content_html, row_index, col_index
- included graph tables (entities, relationships, etc.) keep all columns (None projection)
- deploy_parquet_to_onelake with projection skips tables not in projection dict
- defensive select: column listed in projection but absent from parquet → no crash
- deploy-lakehouse --mock reports lean scope and chunks-skipped message
- live write applies lean column projection to document_elements
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli import cli
from fabric_kg_builder.deploy.onelake_writer import (
    LAKEHOUSE_TABLE_PROJECTION,
    LAKEHOUSE_TABLES,
    STATUS_OK,
    STATUS_PLANNED,
    STATUS_SKIPPED,
    deploy_parquet_to_onelake,
)

_WS_ID = "ws-proj-0000"
_LH_ID = "lh-proj-0000"
_SCHEMA = "dbo"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_parquet(directory: Path, name: str, schema: pa.Schema | None = None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if schema is None:
        tbl = pa.table({"id": ["a", "b"], "val": [1, 2]})
    else:
        arrays = {f.name: pa.array(["x"] if pa.types.is_string(f.type) else [1]) for f in schema}
        tbl = pa.table(arrays, schema=schema)
    pq.write_table(tbl, str(directory / f"{name}.parquet"))


def _doc_elements_schema() -> pa.Schema:
    """Arrow schema matching DocumentElementRow columns."""
    return pa.schema([
        pa.field("document_element_id", pa.string()),
        pa.field("source_file_id", pa.string()),
        pa.field("element_type", pa.string()),
        pa.field("parent_element_id", pa.string()),
        pa.field("content", pa.string()),           # should be DROPPED
        pa.field("content_html", pa.string()),      # should be DROPPED
        pa.field("page_number", pa.int32()),
        pa.field("section_path", pa.string()),
        pa.field("sort_order", pa.int32()),
        pa.field("row_index", pa.int32()),          # should be DROPPED
        pa.field("col_index", pa.int32()),          # should be DROPPED
        pa.field("blob_url", pa.string()),
        pa.field("content_hash", pa.string()),
    ])


# ---------------------------------------------------------------------------
# Projection constant checks — no I/O needed
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLakehouseProjectionConstants:
    def test_chunks_absent_from_projection(self) -> None:
        """chunks must not appear in LAKEHOUSE_TABLE_PROJECTION."""
        assert "chunks" not in LAKEHOUSE_TABLE_PROJECTION

    def test_chunks_absent_from_lakehouse_tables_list(self) -> None:
        """LAKEHOUSE_TABLES must not contain chunks."""
        assert "chunks" not in LAKEHOUSE_TABLES

    def test_lakehouse_tables_has_seven_tables(self) -> None:
        """Default scope is 7 tables (all 8 canonical minus chunks)."""
        assert len(LAKEHOUSE_TABLES) == 7

    def test_graph_tables_have_none_projection(self) -> None:
        """source_files, entities, relationships, evidence, visual_assets, visual_regions use None (all cols)."""
        full_tables = [
            "source_files", "entities", "relationships",
            "evidence", "visual_assets", "visual_regions",
        ]
        for t in full_tables:
            assert LAKEHOUSE_TABLE_PROJECTION[t] is None, (
                f"{t} should have None projection (all columns)"
            )

    def test_document_elements_projection_is_list(self) -> None:
        """document_elements must have an explicit column list (lean projection)."""
        proj = LAKEHOUSE_TABLE_PROJECTION["document_elements"]
        assert isinstance(proj, list)
        assert len(proj) > 0

    def test_document_elements_projection_excludes_content(self) -> None:
        """content must not be in the document_elements projection."""
        proj = LAKEHOUSE_TABLE_PROJECTION["document_elements"]
        assert "content" not in proj

    def test_document_elements_projection_excludes_content_html(self) -> None:
        """content_html must not be in the document_elements projection."""
        proj = LAKEHOUSE_TABLE_PROJECTION["document_elements"]
        assert "content_html" not in proj

    def test_document_elements_projection_excludes_row_index(self) -> None:
        """row_index must not be in the document_elements projection."""
        proj = LAKEHOUSE_TABLE_PROJECTION["document_elements"]
        assert "row_index" not in proj

    def test_document_elements_projection_excludes_col_index(self) -> None:
        """col_index must not be in the document_elements projection."""
        proj = LAKEHOUSE_TABLE_PROJECTION["document_elements"]
        assert "col_index" not in proj

    def test_document_elements_projection_keeps_structural_ids(self) -> None:
        """Core structural columns must be kept."""
        proj = LAKEHOUSE_TABLE_PROJECTION["document_elements"]
        assert proj is not None
        for col in ["document_element_id", "source_file_id", "element_type", "content_hash"]:
            assert col in proj, f"{col} should be in document_elements projection"

    def test_projection_and_tables_list_aligned(self) -> None:
        """LAKEHOUSE_TABLES must exactly match the keys of LAKEHOUSE_TABLE_PROJECTION."""
        assert LAKEHOUSE_TABLES == list(LAKEHOUSE_TABLE_PROJECTION.keys())


# ---------------------------------------------------------------------------
# Writer-level projection tests — mock deltalake, no network
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOneLakeWriterProjection:
    def test_projection_skips_table_not_in_dict(self, tmp_path: Path) -> None:
        """Tables absent from projection are returned as 'skipped'."""
        # chunks is not in LAKEHOUSE_TABLE_PROJECTION
        results = deploy_parquet_to_onelake(
            parquet_dir=tmp_path,
            workspace_id=_WS_ID,
            lakehouse_item_id=_LH_ID,
            schema=_SCHEMA,
            tables=["entities", "chunks"],
            projection=LAKEHOUSE_TABLE_PROJECTION,
            mock=True,
        )
        # entities is in projection → planned
        assert results["entities"] == STATUS_PLANNED
        # chunks not in projection → skipped
        assert "skipped" in results["chunks"]
        assert "projection" in results["chunks"]

    def test_mock_projection_returns_planned_for_included_tables(self, tmp_path: Path) -> None:
        """mock=True returns STATUS_PLANNED for every table in projection."""
        results = deploy_parquet_to_onelake(
            parquet_dir=tmp_path,
            workspace_id=_WS_ID,
            lakehouse_item_id=_LH_ID,
            schema=_SCHEMA,
            tables=LAKEHOUSE_TABLES,
            projection=LAKEHOUSE_TABLE_PROJECTION,
            mock=True,
        )
        assert len(results) == 7
        assert all(v == STATUS_PLANNED for v in results.values()), results

    def test_live_projection_drops_text_cols_from_document_elements(
        self, tmp_path: Path
    ) -> None:
        """Live write of document_elements removes content, content_html, row/col_index."""
        schema = _doc_elements_schema()
        # Write a parquet with one row for each field
        doc_el_dir = tmp_path
        tbl = pa.table(
            {
                "document_element_id": ["de-1"],
                "source_file_id": ["sf-1"],
                "element_type": ["paragraph"],
                "parent_element_id": ["de-0"],
                "content": ["heavy text body"],       # must be dropped
                "content_html": ["<p>html</p>"],      # must be dropped
                "page_number": [1],
                "section_path": ["/intro"],
                "sort_order": [1],
                "row_index": [0],                     # must be dropped
                "col_index": [0],                     # must be dropped
                "blob_url": ["https://blob/x"],
                "content_hash": ["abc123"],
            }
        )
        pq.write_table(tbl, str(doc_el_dir / "document_elements.parquet"))

        captured_tables: list[pa.Table] = []

        def _capture_write(path, arrow_table, **kwargs):
            captured_tables.append(arrow_table)

        with patch("deltalake.write_deltalake", side_effect=_capture_write):
            results = deploy_parquet_to_onelake(
                parquet_dir=doc_el_dir,
                workspace_id=_WS_ID,
                lakehouse_item_id=_LH_ID,
                schema=_SCHEMA,
                tables=["document_elements"],
                token_provider=lambda: "tok",
                mock=False,
                projection=LAKEHOUSE_TABLE_PROJECTION,
            )

        assert results["document_elements"] == STATUS_OK
        assert len(captured_tables) == 1
        written: pa.Table = captured_tables[0]
        written_cols = set(written.schema.names)

        # Dropped columns must be absent
        assert "content" not in written_cols
        assert "content_html" not in written_cols
        assert "row_index" not in written_cols
        assert "col_index" not in written_cols

        # Key structural columns must be present
        assert "document_element_id" in written_cols
        assert "source_file_id" in written_cols
        assert "element_type" in written_cols
        assert "content_hash" in written_cols

    def test_live_full_table_keeps_all_columns(self, tmp_path: Path) -> None:
        """Tables with None projection (e.g. entities) keep all columns."""
        ent_tbl = pa.table({
            "entity_id": ["e-1"],
            "entity_type": ["Component"],
            "display_name": ["Battery"],
            "canonical_key": ["battery"],
            "content_hash": ["xyz"],
        })
        pq.write_table(ent_tbl, str(tmp_path / "entities.parquet"))

        captured: list[pa.Table] = []

        def _capture(path, arrow_table, **kwargs):
            captured.append(arrow_table)

        with patch("deltalake.write_deltalake", side_effect=_capture):
            results = deploy_parquet_to_onelake(
                parquet_dir=tmp_path,
                workspace_id=_WS_ID,
                lakehouse_item_id=_LH_ID,
                schema=_SCHEMA,
                tables=["entities"],
                token_provider=lambda: "tok",
                mock=False,
                projection=LAKEHOUSE_TABLE_PROJECTION,
            )

        assert results["entities"] == STATUS_OK
        written = captured[0]
        # All 5 original columns must be present
        assert set(written.schema.names) == {
            "entity_id", "entity_type", "display_name", "canonical_key", "content_hash"
        }

    def test_defensive_select_when_projection_col_absent(self, tmp_path: Path) -> None:
        """Projection columns absent from the parquet file are silently ignored (no crash)."""
        # document_elements parquet with only minimal columns
        tbl = pa.table({
            "document_element_id": ["de-1"],
            "source_file_id": ["sf-1"],
            "element_type": ["section"],
            "content_hash": ["abc"],
            # table_id, figure_id, image_id are in projection but absent here
        })
        pq.write_table(tbl, str(tmp_path / "document_elements.parquet"))

        captured: list[pa.Table] = []

        def _capture(path, arrow_table, **kwargs):
            captured.append(arrow_table)

        with patch("deltalake.write_deltalake", side_effect=_capture):
            results = deploy_parquet_to_onelake(
                parquet_dir=tmp_path,
                workspace_id=_WS_ID,
                lakehouse_item_id=_LH_ID,
                schema=_SCHEMA,
                tables=["document_elements"],
                token_provider=lambda: "tok",
                mock=False,
                projection=LAKEHOUSE_TABLE_PROJECTION,
            )

        # Must succeed without crashing
        assert results["document_elements"] == STATUS_OK
        written = captured[0]
        # All 4 present columns should survive
        assert "document_element_id" in written.schema.names
        assert "content_hash" in written.schema.names

    def test_no_projection_writes_all_columns(self, tmp_path: Path) -> None:
        """When projection=None, all columns pass through unchanged."""
        tbl = pa.table({"a": [1], "b": [2], "c": [3]})
        pq.write_table(tbl, str(tmp_path / "entities.parquet"))

        captured: list[pa.Table] = []

        def _capture(path, arrow_table, **kwargs):
            captured.append(arrow_table)

        with patch("deltalake.write_deltalake", side_effect=_capture):
            deploy_parquet_to_onelake(
                parquet_dir=tmp_path,
                workspace_id=_WS_ID,
                lakehouse_item_id=_LH_ID,
                schema=_SCHEMA,
                tables=["entities"],
                token_provider=lambda: "tok",
                mock=False,
                projection=None,
            )

        assert set(captured[0].schema.names) == {"a", "b", "c"}


# ---------------------------------------------------------------------------
# CLI mock mode — deploy-lakehouse --mock output checks
# ---------------------------------------------------------------------------


def _make_env_json(tmp_path: Path) -> Path:
    """Write a minimal environments/dev.json for CLI tests."""
    envs = tmp_path / "ontology" / "environments"
    envs.mkdir(parents=True)
    cfg = {
        "fabric": {
            "workspace_id": "ws-test-abc",
            "lakehouse_item_id": "lh-test-xyz",
            "lakehouse_display_name": "kg_lakehouse",
            "schema_name": "dbo",
        }
    }
    import json
    (envs / "dev.json").write_text(json.dumps(cfg))
    return envs


@pytest.mark.unit
class TestDeployLakehouseMockOutput:
    def test_mock_reports_chunks_skipped(self, tmp_path: Path) -> None:
        """--mock output must mention chunks as excluded from the Lakehouse."""
        _make_env_json(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["deploy-lakehouse", "--env", "dev", "--mock",
             "--dist", str(tmp_path / "dist")],
            catch_exceptions=False,
            env={"PYTHONPATH": "src"},
        )
        # Must succeed
        assert result.exit_code == 0, result.output
        # chunks must be mentioned in the output (as excluded)
        assert "chunks" in result.output
        # Must clearly indicate chunks is excluded (not written)
        assert "excluded" in result.output or "SKIPPED" in result.output

    def test_mock_reports_lean_scope(self, tmp_path: Path) -> None:
        """--mock output mentions lean scope and AI Search."""
        _make_env_json(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["deploy-lakehouse", "--env", "dev", "--mock",
             "--dist", str(tmp_path / "dist")],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        # Lean scope messaging
        assert "lean" in result.output.lower() or "LEAN" in result.output
        # AI Search reference
        assert "AI Search" in result.output

    def test_mock_does_not_list_chunks_as_would_upload(self, tmp_path: Path) -> None:
        """--mock must NOT print 'WOULD upload chunks.parquet'."""
        _make_env_json(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["deploy-lakehouse", "--env", "dev", "--mock",
             "--dist", str(tmp_path / "dist")],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        # chunks should not appear as a "WOULD upload" target
        assert "WOULD upload chunks.parquet" not in result.output

    def test_mock_lists_seven_graph_tables(self, tmp_path: Path) -> None:
        """--mock must show WOULD upload for all 7 graph/ontology tables."""
        _make_env_json(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["deploy-lakehouse", "--env", "dev", "--mock",
             "--dist", str(tmp_path / "dist")],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        for table in LAKEHOUSE_TABLES:
            assert f"WOULD upload {table}.parquet" in result.output, (
                f"Expected 'WOULD upload {table}.parquet' in mock output"
            )

    def test_mock_mentions_document_elements_lean(self, tmp_path: Path) -> None:
        """--mock output mentions document_elements lean projection."""
        _make_env_json(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["deploy-lakehouse", "--env", "dev", "--mock",
             "--dist", str(tmp_path / "dist")],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "document_elements" in result.output
        # Should mention lean or content columns dropped
        assert "lean" in result.output.lower() or "content" in result.output
