"""Sprint 2 unit tests: compile-search (full document generation) + deploy-search (mock).

Coverage:
  - compile-search with a small canonical fixture writes index.schema.json
    (1536 dims, entity_ids filterable, entity_aliases searchable) + docs.json
    with derived linkage fields
  - deploy-search mock reads dev.json (example-search) and reports index+doc counts
    with no network call; respects ai_search.enabled flag
  - search.linkage: derive_chunk_doc / derive_document_element_doc fields
  - search.push: PushResult, push_from_build_dir mock
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli.compile_search_cmd import compile_search_cmd
from fabric_kg_builder.cli.deploy_cmd import deploy_search_cmd, _read_search_env_config
from fabric_kg_builder.search.linkage import (
    derive_chunk_doc,
    derive_document_element_doc,
    build_entity_lookup,
)
from fabric_kg_builder.search.push import PushResult, push_from_build_dir


# ---------------------------------------------------------------------------
# Fixtures helpers
# ---------------------------------------------------------------------------

_ENTITY_ROW = {
    "entity_id": "ent-001",
    "entity_type": "Device",
    "display_name": "Surface Pro 11",
    "canonical_key": "surface_pro_11",
    "search_aliases": ["Surface Pro 11", "SP11"],
    "aliases": ["SP11"],
}

_CHUNK_ROW = {
    "chunk_id": "chk-001",
    "source_file_id": "src-001",
    "chunk_type": "section_text",
    "content": "The Surface Pro 11 features a USB-C port.",
    "embedding_text": "Surface Pro 11 features USB-C port.",
    "blob_url": None,
    "related_entity_ids": ["ent-001"],
    "entity_search_keys": ["Surface Pro 11", "SP11"],
    "content_hash": "abc123",
    "created_at": "2026-06-24T00:00:00+00:00",
}

_DOC_ELEMENT_ROW = {
    "document_element_id": "de-001",
    "source_file_id": "src-001",
    "element_type": "section",
    "content": "Troubleshooting connectivity issues.",
    "content_html": "<p>Troubleshooting connectivity issues.</p>",
    "blob_url": None,
    "page_number": 3,
    "section_path": "Chapter 2 > Connectivity",
    "content_hash": "def456",
    "extracted_at": "2026-06-24T00:00:00+00:00",
}


def _write_parquet(directory: Path, table_name: str, rows: list[dict]) -> None:
    """Write rows as a real Parquet file using pyarrow."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    if not rows:
        return
    directory.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows)
    pq.write_table(table, str(directory / f"{table_name}.parquet"))


def _make_parquet_input(tmp: Path) -> Path:
    """Write minimal chunks + entities Parquet fixtures."""
    parquet_dir = tmp / "build" / "parquet"
    _write_parquet(parquet_dir, "chunks", [_CHUNK_ROW])
    _write_parquet(parquet_dir, "entities", [_ENTITY_ROW])
    return parquet_dir


def _make_env_json_with_search(
    tmp: Path,
    env: str = "dev",
    enabled: bool = True,
    service_name: str = "example-search",
) -> Path:
    """Write a minimal environments/{env}.json with ai_search section."""
    envs_dir = tmp / "ontology" / "environments"
    envs_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "env": env,
        "fabric": {
            "workspace_id": "ws-test-1234",
            "lakehouse_item_id": "lh-test-5678",
        },
        "ai_search": {
            "enabled": enabled,
            "service_name": service_name,
            "endpoint": f"https://{service_name}.search.windows.net",
            "index_prefix": "kg-dev-",
            "index_chunks": "kg-chunks",
            "index_document_elements": "kg-document-elements",
        },
    }
    path = envs_dir / f"{env}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ===========================================================================
# search.linkage — unit tests
# ===========================================================================


class TestLinkageDerivation:
    """Tests for derive_chunk_doc and derive_document_element_doc."""

    def test_derive_chunk_doc_has_all_required_fields(self):
        """derive_chunk_doc returns a doc with all required AI Search fields."""
        doc = derive_chunk_doc(_CHUNK_ROW)
        for field in ("chunk_id", "content", "embedding_text", "entity_ids",
                      "entity_aliases", "canonical_key", "entity_types",
                      "graph_path", "blob_url", "source_path",
                      "last_modified", "content_type"):
            assert field in doc, f"Missing field: {field}"

    def test_derive_chunk_doc_entity_ids_populated(self):
        """entity_ids comes from related_entity_ids."""
        doc = derive_chunk_doc(_CHUNK_ROW)
        assert doc["entity_ids"] == ["ent-001"]

    def test_derive_chunk_doc_entity_aliases_populated(self):
        """entity_aliases comes from entity_search_keys."""
        doc = derive_chunk_doc(_CHUNK_ROW)
        assert "Surface Pro 11" in doc["entity_aliases"]

    def test_derive_chunk_doc_canonical_key_from_entities(self):
        """canonical_key is resolved from entities_by_id lookup."""
        lookup = build_entity_lookup([_ENTITY_ROW])
        doc = derive_chunk_doc(_CHUNK_ROW, lookup)
        assert doc["canonical_key"] == "surface_pro_11"

    def test_derive_chunk_doc_entity_types_from_entities(self):
        """entity_types populated when entities_by_id is provided."""
        lookup = build_entity_lookup([_ENTITY_ROW])
        doc = derive_chunk_doc(_CHUNK_ROW, lookup)
        assert "Device" in doc["entity_types"]

    def test_derive_chunk_doc_graph_path_none(self):
        """graph_path is None at compile time (injected at push time)."""
        doc = derive_chunk_doc(_CHUNK_ROW)
        assert doc["graph_path"] is None

    def test_derive_chunk_doc_no_entities_lookup(self):
        """derive_chunk_doc works without an entity lookup (canonical_key='')."""
        doc = derive_chunk_doc(_CHUNK_ROW)
        assert doc["canonical_key"] == ""
        assert doc["entity_types"] == []

    def test_derive_doc_element_doc_fields(self):
        """derive_document_element_doc has all required fields."""
        doc = derive_document_element_doc(_DOC_ELEMENT_ROW)
        for field in ("document_element_id", "content", "content_html",
                      "element_type", "page_number", "section_path",
                      "entity_ids", "entity_aliases", "canonical_key",
                      "graph_path", "blob_url"):
            assert field in doc, f"Missing field: {field}"

    def test_derive_doc_element_doc_page_number(self):
        """page_number is preserved correctly."""
        doc = derive_document_element_doc(_DOC_ELEMENT_ROW)
        assert doc["page_number"] == 3

    def test_derive_doc_element_doc_section_path(self):
        """section_path is preserved correctly."""
        doc = derive_document_element_doc(_DOC_ELEMENT_ROW)
        assert doc["section_path"] == "Chapter 2 > Connectivity"

    def test_build_entity_lookup_keyed_by_entity_id(self):
        """build_entity_lookup returns dict keyed by entity_id."""
        lookup = build_entity_lookup([_ENTITY_ROW])
        assert "ent-001" in lookup
        assert lookup["ent-001"]["display_name"] == "Surface Pro 11"


# ===========================================================================
# search.push — unit tests
# ===========================================================================


class TestSearchPush:
    """Tests for PushResult and push_from_build_dir in mock mode."""

    def test_push_result_mock_str(self):
        """PushResult.__str__ includes MOCK, index name, and doc count."""
        r = PushResult(index_name="kg-chunks", doc_count=42, mock=True)
        s = str(r)
        assert "MOCK" in s
        assert "kg-chunks" in s
        assert "42" in s

    def test_push_result_succeeded_default(self):
        """PushResult.succeeded defaults to True."""
        r = PushResult(index_name="kg-chunks", doc_count=0, mock=True)
        assert r.succeeded is True

    def test_push_from_build_dir_mock_returns_results(self, tmp_path):
        """push_from_build_dir mock returns (schema_result, docs_result)."""
        # Scaffold minimal build/search/kg-chunks/
        idx_dir = tmp_path / "kg-chunks"
        idx_dir.mkdir()
        (idx_dir / "index.schema.json").write_text('{"name":"kg-chunks","fields":[]}')
        (idx_dir / "docs.json").write_text('[{"chunk_id":"c1","content":"hello"}]')

        schema_r, docs_r = push_from_build_dir(tmp_path, "kg-chunks", "kg-dev-chunks", mock=True)
        assert schema_r.mock is True
        assert docs_r.mock is True
        assert docs_r.doc_count == 1

    def test_push_from_build_dir_missing_schema_raises(self, tmp_path):
        """push_from_build_dir raises FileNotFoundError when schema is missing."""
        with pytest.raises(FileNotFoundError):
            push_from_build_dir(tmp_path, "kg-chunks", "kg-dev-chunks", mock=True)

    def test_push_from_build_dir_no_docs_json(self, tmp_path):
        """push_from_build_dir works when docs.json is absent (0 docs)."""
        idx_dir = tmp_path / "kg-chunks"
        idx_dir.mkdir()
        (idx_dir / "index.schema.json").write_text('{"name":"kg-chunks","fields":[]}')

        schema_r, docs_r = push_from_build_dir(tmp_path, "kg-chunks", "kg-dev-chunks", mock=True)
        assert docs_r.doc_count == 0


# ===========================================================================
# compile_search_cmd — Sprint 2 full document generation
# ===========================================================================


class TestCompileSearchSprint2:
    """Tests for compile-search full document generation from Parquet fixtures."""

    def test_compile_search_with_parquet_writes_docs_json(self, tmp_path):
        """compile-search writes docs.json when Parquet input is present."""
        parquet_dir = _make_parquet_input(tmp_path)
        out = tmp_path / "search"
        runner = CliRunner()
        result = runner.invoke(compile_search_cmd, [
            "--input", str(parquet_dir),
            "--out", str(out),
            "--indexes", "kg-chunks",
        ])
        assert result.exit_code == 0, f"Expected 0:\n{result.output}"
        docs_path = out / "kg-chunks" / "docs.json"
        assert docs_path.exists(), "docs.json not written"

    def test_compile_search_docs_json_has_chunk_doc(self, tmp_path):
        """docs.json for kg-chunks contains a document with chunk_id."""
        parquet_dir = _make_parquet_input(tmp_path)
        out = tmp_path / "search"
        runner = CliRunner()
        runner.invoke(compile_search_cmd, [
            "--input", str(parquet_dir),
            "--out", str(out),
            "--indexes", "kg-chunks",
        ])
        docs = json.loads((out / "kg-chunks" / "docs.json").read_text())
        assert len(docs) == 1
        assert docs[0]["chunk_id"] == "chk-001"

    def test_compile_search_docs_json_entity_ids_filterable(self, tmp_path):
        """docs.json chunk doc has entity_ids populated from related_entity_ids."""
        parquet_dir = _make_parquet_input(tmp_path)
        out = tmp_path / "search"
        runner = CliRunner()
        runner.invoke(compile_search_cmd, [
            "--input", str(parquet_dir), "--out", str(out), "--indexes", "kg-chunks",
        ])
        docs = json.loads((out / "kg-chunks" / "docs.json").read_text())
        assert docs[0]["entity_ids"] == ["ent-001"]

    def test_compile_search_docs_json_entity_aliases_searchable(self, tmp_path):
        """docs.json chunk doc has entity_aliases from entity_search_keys."""
        parquet_dir = _make_parquet_input(tmp_path)
        out = tmp_path / "search"
        runner = CliRunner()
        runner.invoke(compile_search_cmd, [
            "--input", str(parquet_dir), "--out", str(out), "--indexes", "kg-chunks",
        ])
        docs = json.loads((out / "kg-chunks" / "docs.json").read_text())
        assert "Surface Pro 11" in docs[0]["entity_aliases"]

    def test_compile_search_docs_json_canonical_key_resolved(self, tmp_path):
        """canonical_key in docs.json is resolved via entities Parquet."""
        parquet_dir = _make_parquet_input(tmp_path)
        out = tmp_path / "search"
        runner = CliRunner()
        runner.invoke(compile_search_cmd, [
            "--input", str(parquet_dir), "--out", str(out), "--indexes", "kg-chunks",
        ])
        docs = json.loads((out / "kg-chunks" / "docs.json").read_text())
        assert docs[0]["canonical_key"] == "surface_pro_11"

    def test_compile_search_schema_written_even_with_parquet(self, tmp_path):
        """index.schema.json is written alongside docs.json."""
        parquet_dir = _make_parquet_input(tmp_path)
        out = tmp_path / "search"
        runner = CliRunner()
        runner.invoke(compile_search_cmd, [
            "--input", str(parquet_dir), "--out", str(out), "--indexes", "kg-chunks",
        ])
        assert (out / "kg-chunks" / "index.schema.json").exists()

    def test_compile_search_schema_1536_dims(self, tmp_path):
        """index.schema.json still has 1536-dim vector field when docs are present."""
        parquet_dir = _make_parquet_input(tmp_path)
        out = tmp_path / "search"
        runner = CliRunner()
        runner.invoke(compile_search_cmd, [
            "--input", str(parquet_dir), "--out", str(out), "--indexes", "kg-chunks",
        ])
        schema = json.loads((out / "kg-chunks" / "index.schema.json").read_text())
        vec_fields = [f for f in schema["fields"] if f.get("dimensions")]
        assert vec_fields[0]["dimensions"] == 1536

    def test_compile_search_schema_entity_ids_filterable_in_schema(self, tmp_path):
        """index.schema.json entity_ids field is filterable (not searchable)."""
        parquet_dir = _make_parquet_input(tmp_path)
        out = tmp_path / "search"
        runner = CliRunner()
        runner.invoke(compile_search_cmd, [
            "--input", str(parquet_dir), "--out", str(out), "--indexes", "kg-chunks",
        ])
        schema = json.loads((out / "kg-chunks" / "index.schema.json").read_text())
        field = next(f for f in schema["fields"] if f["name"] == "entity_ids")
        assert field["filterable"] is True
        assert field.get("searchable", True) is False

    def test_compile_search_schema_entity_aliases_searchable_in_schema(self, tmp_path):
        """index.schema.json entity_aliases field is searchable (not filterable)."""
        parquet_dir = _make_parquet_input(tmp_path)
        out = tmp_path / "search"
        runner = CliRunner()
        runner.invoke(compile_search_cmd, [
            "--input", str(parquet_dir), "--out", str(out), "--indexes", "kg-chunks",
        ])
        schema = json.loads((out / "kg-chunks" / "index.schema.json").read_text())
        field = next(f for f in schema["fields"] if f["name"] == "entity_aliases")
        assert field["searchable"] is True
        assert field.get("filterable", True) is False

    def test_compile_search_no_parquet_no_docs_json(self, tmp_path):
        """compile-search skips docs.json when Parquet tables are absent."""
        empty_input = tmp_path / "empty"
        empty_input.mkdir()
        out = tmp_path / "search"
        runner = CliRunner()
        result = runner.invoke(compile_search_cmd, [
            "--input", str(empty_input),
            "--out", str(out),
            "--indexes", "kg-chunks",
        ])
        assert result.exit_code == 0
        # Schema is always written
        assert (out / "kg-chunks" / "index.schema.json").exists()
        # docs.json skipped when no Parquet
        assert not (out / "kg-chunks" / "docs.json").exists()

    def test_compile_search_summary_line_has_doc_count(self, tmp_path):
        """SUCCESS line in output includes the document count."""
        parquet_dir = _make_parquet_input(tmp_path)
        out = tmp_path / "search"
        runner = CliRunner()
        result = runner.invoke(compile_search_cmd, [
            "--input", str(parquet_dir), "--out", str(out), "--indexes", "kg-chunks",
        ])
        assert "SUCCESS" in result.output
        assert "1" in result.output  # 1 document derived

    def test_compile_search_no_network_calls_with_parquet(self, tmp_path):
        """compile-search (even with Parquet) never makes network calls."""
        parquet_dir = _make_parquet_input(tmp_path)
        out = tmp_path / "search"
        with patch("socket.getaddrinfo", side_effect=AssertionError("NETWORK BLOCKED")):
            runner = CliRunner()
            result = runner.invoke(compile_search_cmd, [
                "--input", str(parquet_dir), "--out", str(out),
            ])
        assert result.exit_code == 0


# ===========================================================================
# deploy_search_cmd — mock mode
# ===========================================================================


class TestDeploySearchCmd:
    """Tests for fabric-kg deploy-search mock implementation."""

    def test_deploy_search_exits_0(self, tmp_path, monkeypatch):
        """deploy-search exits 0 when env JSON present."""
        monkeypatch.chdir(tmp_path)
        _make_env_json_with_search(tmp_path)
        runner = CliRunner()
        result = runner.invoke(deploy_search_cmd, ["--env", "dev", "--mock"])
        assert result.exit_code == 0, f"Expected 0:\n{result.output}"

    def test_deploy_search_reports_service_name(self, tmp_path, monkeypatch):
        """deploy-search output contains service name from env JSON."""
        monkeypatch.chdir(tmp_path)
        _make_env_json_with_search(tmp_path, service_name="example-search")
        runner = CliRunner()
        result = runner.invoke(deploy_search_cmd, ["--env", "dev", "--mock"])
        assert "example-search" in result.output

    def test_deploy_search_reports_index_names(self, tmp_path, monkeypatch):
        """deploy-search output names the deployed index (with prefix)."""
        monkeypatch.chdir(tmp_path)
        _make_env_json_with_search(tmp_path)
        runner = CliRunner()
        result = runner.invoke(deploy_search_cmd, ["--env", "dev", "--mock"])
        # kg-dev-kg-chunks is the prefixed deployed index name
        assert "kg-dev-" in result.output

    def test_deploy_search_disabled_skips_push(self, tmp_path, monkeypatch):
        """deploy-search exits 0 immediately when ai_search.enabled=false."""
        monkeypatch.chdir(tmp_path)
        _make_env_json_with_search(tmp_path, enabled=False)
        runner = CliRunner()
        result = runner.invoke(deploy_search_cmd, ["--env", "dev", "--mock"])
        assert result.exit_code == 0
        assert "enabled=false" in result.output.lower() or "enabled" in result.output

    def test_deploy_search_with_docs_reports_doc_count(self, tmp_path, monkeypatch):
        """deploy-search reports doc count from docs.json."""
        monkeypatch.chdir(tmp_path)
        _make_env_json_with_search(tmp_path)

        # Write minimal build/search/kg-chunks/
        idx_dir = tmp_path / "build" / "search" / "kg-chunks"
        idx_dir.mkdir(parents=True)
        (idx_dir / "index.schema.json").write_text('{"name":"kg-chunks","fields":[]}')
        (idx_dir / "docs.json").write_text('[{"chunk_id":"c1"},{"chunk_id":"c2"}]')

        runner = CliRunner()
        result = runner.invoke(deploy_search_cmd, ["--env", "dev", "--mock",
                                                    "--dist", str(tmp_path / "build" / "search")])
        assert "2" in result.output  # 2 docs reported

    def test_deploy_search_no_network_call(self, tmp_path, monkeypatch):
        """deploy-search mock never makes any network calls."""
        monkeypatch.chdir(tmp_path)
        _make_env_json_with_search(tmp_path)
        with patch("socket.getaddrinfo", side_effect=AssertionError("NETWORK BLOCKED")):
            runner = CliRunner()
            result = runner.invoke(deploy_search_cmd, ["--env", "dev", "--mock"])
        assert result.exit_code == 0

    def test_deploy_search_missing_env_json_exits_1(self, tmp_path, monkeypatch):
        """deploy-search exits 1 when env JSON is missing."""
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(deploy_search_cmd, ["--env", "dev"])
        assert result.exit_code == 1

    def test_deploy_search_success_message(self, tmp_path, monkeypatch):
        """deploy-search prints SUCCESS message."""
        monkeypatch.chdir(tmp_path)
        _make_env_json_with_search(tmp_path)
        runner = CliRunner()
        result = runner.invoke(deploy_search_cmd, ["--env", "dev", "--mock"])
        assert "SUCCESS" in result.output

    def test_read_search_env_config_returns_ai_search(self, tmp_path, monkeypatch):
        """_read_search_env_config returns ai_search dict from env JSON."""
        monkeypatch.chdir(tmp_path)
        _make_env_json_with_search(tmp_path, service_name="my-search")
        cfg = _read_search_env_config(
            "dev", environments_dir=tmp_path / "ontology" / "environments"
        )
        assert cfg["ai_search"]["service_name"] == "my-search"
        assert cfg["ai_search"]["enabled"] is True

    def test_deploy_search_reads_real_dev_json(self, tmp_path, monkeypatch):
        """deploy-search reads a dev.json env config and surfaces its service name.

        Uses the committed dev.json.example template copied into a temp working
        dir, so the test does not depend on the developer's real (gitignored)
        dev.json and stays CI-safe.
        """
        import shutil

        repo_root = Path(__file__).parents[2]
        env_dir = tmp_path / "ontology" / "environments"
        env_dir.mkdir(parents=True)
        shutil.copy(
            repo_root / "ontology" / "environments" / "dev.json.example",
            env_dir / "dev.json",
        )
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        # Run against the example dev.json — mock mode, no network
        with patch("socket.getaddrinfo", side_effect=AssertionError("NETWORK BLOCKED")):
            result = runner.invoke(deploy_search_cmd, ["--env", "dev", "--mock"])
        assert result.exit_code == 0
        # service_name placeholder from the example template appears in the output
        assert "<your-search-service>" in result.output
