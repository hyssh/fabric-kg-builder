"""Golden canonical fixture test.

Verifies the compile-data CODE PATH (reader → writer → gates) against a small,
hand-crafted canonical JSON that mimics real Surface PDF enrichment output.
Runs in <1 s — no live PDFs, no LLM calls.

Why this exists: the real Surface PDF integration tests are slow and opt-in.
This test keeps the e2e data pipeline path covered on every commit without
depending on large sample files.

Fixture: tests/fixtures/golden/surface_mini_canonical.json
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli.compile_data_cmd import compile_data_cmd
from fabric_kg_builder.validate.data_gates import run_gates

_GOLDEN_FILE = Path(__file__).parent.parent / "fixtures" / "golden" / "surface_mini_canonical.json"


@pytest.fixture(scope="module")
def golden() -> dict:
    assert _GOLDEN_FILE.exists(), f"Golden fixture missing: {_GOLDEN_FILE}"
    return json.loads(_GOLDEN_FILE.read_text(encoding="utf-8"))


@pytest.mark.unit
class TestGoldenCanonicalFixture:
    """Fast golden-fixture tests — run on every commit, zero external I/O."""

    def test_fixture_has_required_top_level_keys(self, golden: dict) -> None:
        for key in ("source_file_id", "entities", "relationships", "chunks", "evidence"):
            assert key in golden, f"Golden fixture missing key: {key!r}"

    def test_fixture_has_two_entities(self, golden: dict) -> None:
        assert len(golden["entities"]) == 2

    def test_fixture_has_one_relationship(self, golden: dict) -> None:
        assert len(golden["relationships"]) == 1

    def test_fixture_has_one_chunk(self, golden: dict) -> None:
        assert len(golden["chunks"]) == 1

    def test_fixture_has_one_evidence(self, golden: dict) -> None:
        assert len(golden["evidence"]) == 1

    def test_relationship_fk_points_to_known_entities(self, golden: dict) -> None:
        entity_ids = {e["entity_id"] for e in golden["entities"]}
        for rel in golden["relationships"]:
            assert rel["source_entity_id"] in entity_ids, (
                f"source_entity_id {rel['source_entity_id']!r} not in entities"
            )
            assert rel["target_entity_id"] in entity_ids, (
                f"target_entity_id {rel['target_entity_id']!r} not in entities"
            )

    def test_evidence_chunk_fk_is_consistent(self, golden: dict) -> None:
        chunk_ids = {c["chunk_id"] for c in golden["chunks"]}
        for ev in golden["evidence"]:
            if ev.get("chunk_id") is not None:
                assert ev["chunk_id"] in chunk_ids, (
                    f"evidence.chunk_id {ev['chunk_id']!r} not in chunks"
                )

    def test_compile_data_exits_zero_on_golden_fixture(self, golden: dict, tmp_path: Path) -> None:
        """compile-data must exit 0 on the golden fixture (fast, no live data)."""
        in_dir = tmp_path / "enriched"
        in_dir.mkdir(parents=True)
        (in_dir / "surface_mini_canonical.json").write_text(
            json.dumps(golden, ensure_ascii=False), encoding="utf-8"
        )
        out_dir = tmp_path / "parquet"

        runner = CliRunner()
        result = runner.invoke(
            compile_data_cmd,
            ["--input", str(in_dir), "--out", str(out_dir)],
        )
        assert result.exit_code == 0, (
            f"compile-data exited {result.exit_code}.\nOutput:\n{result.output}"
        )

    def test_compile_data_writes_parquet_with_correct_row_counts(
        self, golden: dict, tmp_path: Path
    ) -> None:
        """Parquet tables from the golden fixture have the right row counts."""
        in_dir = tmp_path / "enriched"
        in_dir.mkdir(parents=True)
        (in_dir / "surface_mini_canonical.json").write_text(
            json.dumps(golden, ensure_ascii=False), encoding="utf-8"
        )
        out_dir = tmp_path / "parquet"

        runner = CliRunner()
        runner.invoke(
            compile_data_cmd,
            ["--input", str(in_dir), "--out", str(out_dir)],
        )

        assert pq.read_table(out_dir / "entities.parquet").num_rows == 2
        assert pq.read_table(out_dir / "relationships.parquet").num_rows == 1
        assert pq.read_table(out_dir / "chunks.parquet").num_rows == 1
        assert pq.read_table(out_dir / "evidence.parquet").num_rows == 1

    def test_data_gates_pass_on_golden_fixture(self, golden: dict) -> None:
        """All data integrity gates (VAL-001..012) must pass on the golden fixture."""
        tables = {
            "entities": golden["entities"],
            "relationships": golden["relationships"],
            "chunks": golden["chunks"],
            "evidence": golden["evidence"],
        }
        violations = run_gates(tables)
        assert violations == [], (
            "Data gate violations on golden fixture:\n"
            + "\n".join(str(v) for v in violations)
        )
