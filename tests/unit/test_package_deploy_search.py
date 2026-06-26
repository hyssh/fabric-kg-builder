"""Unit tests for package, deploy-lakehouse (mock), and compile-search commands.

Sprint 1 coverage:
  - package: bundles parquet + ontology into dist with manifest.json; missing artifacts -> exit 1
  - deploy-lakehouse: mock exits 0, reports workspace/lakehouse from env JSON; no network
  - compile-search: writes valid index.schema.json with 1536-dim vector + entity-linkage fields
  - env-json: dev.json loads via config loader and lakehouse_item_id is present
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from fabric_kg_builder.cli.compile_search_cmd import (
    _VECTOR_DIMS,
    _build_chunks_schema,
    _build_document_elements_schema,
    compile_search_cmd,
)
from tests.conftest import combined_output, make_cli_runner  # noqa: E402,F401
from fabric_kg_builder.cli.deploy_cmd import (
    _read_fabric_env_config,
    deploy_lakehouse_cmd,
)
from fabric_kg_builder.cli.package_cmd import package_cmd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_build(tmp: Path, with_parquet: bool = True, with_ontology: bool = True,
                with_search: bool = False) -> Path:
    """Scaffold a minimal build directory under tmp."""
    build = tmp / "build"
    if with_parquet:
        p_dir = build / "parquet"
        p_dir.mkdir(parents=True)
        (p_dir / "entities.parquet").write_bytes(b"PAR1MOCK")
        (p_dir / "chunks.parquet").write_bytes(b"PAR1MOCK")
    if with_ontology:
        o_dir = build / "ontology"
        o_dir.mkdir(parents=True)
        (o_dir / "definition.json").write_text('{"parts":[]}', encoding="utf-8")
    if with_search:
        s_dir = build / "search" / "kg-chunks"
        s_dir.mkdir(parents=True)
        (s_dir / "index.schema.json").write_text('{"name":"kg-chunks"}', encoding="utf-8")
    return build


def _make_env_json(tmp: Path, env: str = "dev",
                   workspace_id: str = "ws-test-1234",
                   lakehouse_item_id: str = "lh-test-5678") -> Path:
    """Write a minimal environments/{env}.json."""
    envs_dir = tmp / "ontology" / "environments"
    envs_dir.mkdir(parents=True, exist_ok=True)
    env_data = {
        "env": env,
        "fabric": {
            "workspace_id": workspace_id,
            "lakehouse_item_id": lakehouse_item_id,
            "lakehouse_display_name": "kg_lakehouse",
            "onelake_tables_path": (
                f"https://onelake.dfs.fabric.microsoft.com/{workspace_id}/{lakehouse_item_id}/Tables"
            ),
        },
    }
    path = envs_dir / f"{env}.json"
    path.write_text(json.dumps(env_data), encoding="utf-8")
    return path


# ===========================================================================
# package_cmd
# ===========================================================================


class TestPackageCmd:
    """Tests for fabric-kg package command."""

    def test_package_exits_0_with_required_artifacts(self, tmp_path):
        """package succeeds when build/parquet and build/ontology are present."""
        build = _make_build(tmp_path)
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(
                package_cmd,
                ["--build-dir", str(build), "--out", str(tmp_path / "dist")],
            )
        assert result.exit_code == 0, f"Expected 0, got {result.exit_code}\n{result.output}"

    def test_package_creates_manifest_json(self, tmp_path):
        """manifest.json must be written inside dist/fabric-kg-package/."""
        build = _make_build(tmp_path)
        dist = tmp_path / "dist"
        runner = CliRunner()
        runner.invoke(package_cmd, ["--build-dir", str(build), "--out", str(dist)])
        manifest_path = dist / "fabric-kg-package" / "manifest.json"
        assert manifest_path.exists(), "manifest.json missing from dist/fabric-kg-package/"
        manifest = json.loads(manifest_path.read_text())
        assert "artifacts" in manifest
        assert "parquet" in manifest["artifacts"]
        assert "ontology" in manifest["artifacts"]

    def test_manifest_lists_bundled_files(self, tmp_path):
        """manifest.json must list individual files in each artifact section."""
        build = _make_build(tmp_path)
        dist = tmp_path / "dist"
        runner = CliRunner()
        runner.invoke(package_cmd, ["--build-dir", str(build), "--out", str(dist)])
        manifest = json.loads((dist / "fabric-kg-package" / "manifest.json").read_text())
        parquet_files = manifest["artifacts"]["parquet"]["files"]
        assert any("entities.parquet" in f for f in parquet_files)
        assert any("chunks.parquet" in f for f in parquet_files)

    def test_package_copies_parquet_and_ontology(self, tmp_path):
        """build/parquet and build/ontology are copied into the dist package."""
        build = _make_build(tmp_path)
        dist = tmp_path / "dist"
        runner = CliRunner()
        runner.invoke(package_cmd, ["--build-dir", str(build), "--out", str(dist)])
        pkg = dist / "fabric-kg-package"
        assert (pkg / "parquet" / "entities.parquet").exists()
        assert (pkg / "ontology" / "definition.json").exists()

    def test_package_missing_parquet_exits_1(self, tmp_path):
        """package exits 1 when build/parquet is missing."""
        build = _make_build(tmp_path, with_parquet=False)
        runner = CliRunner()
        result = runner.invoke(
            package_cmd,
            ["--build-dir", str(build), "--out", str(tmp_path / "dist")],
        )
        assert result.exit_code == 1, f"Expected exit 1 for missing parquet, got {result.exit_code}"

    def test_package_missing_ontology_exits_1(self, tmp_path):
        """package exits 1 when build/ontology is missing."""
        build = _make_build(tmp_path, with_ontology=False)
        runner = CliRunner()
        result = runner.invoke(
            package_cmd,
            ["--build-dir", str(build), "--out", str(tmp_path / "dist")],
        )
        assert result.exit_code == 1, f"Expected exit 1 for missing ontology, got {result.exit_code}"

    def test_package_missing_both_required_exits_1(self, tmp_path):
        """package exits 1 when both required build dirs are missing."""
        empty = tmp_path / "build"
        empty.mkdir()
        runner = CliRunner()
        result = runner.invoke(
            package_cmd,
            ["--build-dir", str(empty), "--out", str(tmp_path / "dist")],
        )
        assert result.exit_code == 1

    def test_package_include_search_bundles_search_dir(self, tmp_path):
        """--include-search copies build/search into the dist package."""
        build = _make_build(tmp_path, with_search=True)
        dist = tmp_path / "dist"
        runner = CliRunner()
        result = runner.invoke(
            package_cmd,
            ["--build-dir", str(build), "--out", str(dist), "--include-search"],
        )
        assert result.exit_code == 0
        assert (dist / "fabric-kg-package" / "search").exists()
        manifest = json.loads((dist / "fabric-kg-package" / "manifest.json").read_text())
        assert "search" in manifest["artifacts"]

    def test_package_search_dir_absent_still_succeeds_with_warning(self, tmp_path):
        """--include-search emits a warning when build/search absent, but still exits 0."""
        build = _make_build(tmp_path, with_search=False)
        dist = tmp_path / "dist"
        runner = make_cli_runner()
        result = runner.invoke(
            package_cmd,
            ["--build-dir", str(build), "--out", str(dist), "--include-search"],
        )
        assert result.exit_code == 0
        assert "WARNING" in combined_output(result)

    def test_package_no_include_search_flag_skips_search(self, tmp_path):
        """Without --include-search, search dir is NOT bundled even if present."""
        build = _make_build(tmp_path, with_search=True)
        dist = tmp_path / "dist"
        runner = CliRunner()
        runner.invoke(package_cmd, ["--build-dir", str(build), "--out", str(dist)])
        manifest = json.loads((dist / "fabric-kg-package" / "manifest.json").read_text())
        assert "search" not in manifest["artifacts"]

    def test_package_output_contains_success_message(self, tmp_path):
        """package prints a SUCCESS line on exit 0."""
        build = _make_build(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            package_cmd,
            ["--build-dir", str(build), "--out", str(tmp_path / "dist")],
        )
        assert "SUCCESS" in result.output

    def test_package_idempotent_reruns_succeed(self, tmp_path):
        """Running package twice on same build dir both exit 0 (dist is overwritten)."""
        build = _make_build(tmp_path)
        dist = tmp_path / "dist"
        runner = CliRunner()
        r1 = runner.invoke(package_cmd, ["--build-dir", str(build), "--out", str(dist)])
        r2 = runner.invoke(package_cmd, ["--build-dir", str(build), "--out", str(dist)])
        assert r1.exit_code == 0
        assert r2.exit_code == 0


# ===========================================================================
# deploy_lakehouse_cmd (mock)
# ===========================================================================


class TestDeployLakehouseCmd:
    """Tests for fabric-kg deploy-lakehouse (mock mode)."""

    def test_deploy_lakehouse_mock_exits_0(self, tmp_path, monkeypatch):
        """deploy-lakehouse exits 0 in mock mode."""
        monkeypatch.chdir(tmp_path)
        _make_env_json(tmp_path)
        runner = CliRunner()
        result = runner.invoke(deploy_lakehouse_cmd, ["--env", "dev", "--mock"])
        assert result.exit_code == 0, f"Expected 0, got {result.exit_code}\n{result.output}"

    def test_deploy_lakehouse_reports_workspace_id(self, tmp_path, monkeypatch):
        """deploy-lakehouse output contains the workspace_id from dev.json."""
        monkeypatch.chdir(tmp_path)
        _make_env_json(tmp_path, workspace_id="ws-test-1234")
        runner = CliRunner()
        result = runner.invoke(deploy_lakehouse_cmd, ["--env", "dev", "--mock"])
        assert "ws-test-1234" in result.output, (
            f"workspace_id not in output:\n{result.output}"
        )

    def test_deploy_lakehouse_reports_lakehouse_id(self, tmp_path, monkeypatch):
        """deploy-lakehouse output contains the lakehouse_item_id from dev.json."""
        monkeypatch.chdir(tmp_path)
        _make_env_json(tmp_path, lakehouse_item_id="lh-test-5678")
        runner = CliRunner()
        result = runner.invoke(deploy_lakehouse_cmd, ["--env", "dev", "--mock"])
        assert "lh-test-5678" in result.output, (
            f"lakehouse_item_id not in output:\n{result.output}"
        )

    def test_deploy_lakehouse_lists_all_default_tables(self, tmp_path, monkeypatch):
        """deploy-lakehouse mock lists 7 graph/ontology tables; chunks excluded (→ AI Search)."""
        monkeypatch.chdir(tmp_path)
        _make_env_json(tmp_path)
        runner = CliRunner()
        result = runner.invoke(deploy_lakehouse_cmd, ["--env", "dev", "--mock"])
        # 7 graph/ontology tables must appear as WOULD upload
        for table in [
            "entities", "relationships", "evidence",
            "source_files", "visual_assets", "document_elements",
        ]:
            assert table in result.output, f"Table '{table}' not in output:\n{result.output}"
        # chunks must appear in the output (as excluded notice), but NOT as WOULD upload
        assert "chunks" in result.output, "chunks should appear in lean-scope exclusion notice"
        assert "WOULD upload chunks.parquet" not in result.output, (
            "chunks must not be listed as a WOULD upload table"
        )

    def test_deploy_lakehouse_subset_tables(self, tmp_path, monkeypatch):
        """--tables limits which tables are reported; chunks appears as skipped (projection)."""
        monkeypatch.chdir(tmp_path)
        _make_env_json(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            deploy_lakehouse_cmd, ["--env", "dev", "--mock", "--tables", "entities,chunks"]
        )
        assert result.exit_code == 0
        assert "entities" in result.output
        # chunks should appear as SKIPPED (not in Lakehouse projection)
        assert "chunks" in result.output

    def test_deploy_lakehouse_mock_mode_no_network(self, tmp_path, monkeypatch):
        """deploy-lakehouse (--mock) never calls any network / Azure SDK."""
        monkeypatch.chdir(tmp_path)
        _make_env_json(tmp_path)
        # Patch socket to ensure no real network calls are made
        with patch("socket.getaddrinfo", side_effect=AssertionError("NETWORK CALL BLOCKED")):
            runner = CliRunner()
            result = runner.invoke(deploy_lakehouse_cmd, ["--env", "dev", "--mock"])
        assert result.exit_code == 0, f"Network guard fired or command failed:\n{result.output}"

    def test_deploy_lakehouse_missing_env_json_exits_1(self, tmp_path, monkeypatch):
        """deploy-lakehouse exits 1 when ontology/environments/dev.json is missing."""
        monkeypatch.chdir(tmp_path)
        runner = CliRunner()
        result = runner.invoke(deploy_lakehouse_cmd, ["--env", "dev"])
        assert result.exit_code == 1

    def test_deploy_lakehouse_force_flag_reported(self, tmp_path, monkeypatch):
        """--force flag appears in deploy-lakehouse output."""
        monkeypatch.chdir(tmp_path)
        _make_env_json(tmp_path)
        runner = CliRunner()
        result = runner.invoke(deploy_lakehouse_cmd, ["--env", "dev", "--mock", "--force"])
        assert result.exit_code == 0
        assert "True" in result.output  # force: True

    def test_read_fabric_env_config_reads_dev_json(self, tmp_path, monkeypatch):
        """_read_fabric_env_config returns workspace_id + lakehouse_item_id from JSON."""
        monkeypatch.chdir(tmp_path)
        _make_env_json(tmp_path, workspace_id="ws-abc", lakehouse_item_id="lh-xyz")
        cfg = _read_fabric_env_config("dev", environments_dir=tmp_path / "ontology" / "environments")
        assert cfg["workspace_id"] == "ws-abc"
        assert cfg["lakehouse_item_id"] == "lh-xyz"


# ===========================================================================
# compile_search_cmd (schema placeholder)
# ===========================================================================


class TestCompileSearchCmd:
    """Tests for fabric-kg compile-search Sprint 1 placeholder schema generation."""

    def test_compile_search_exits_0(self, tmp_path):
        """compile-search exits 0."""
        runner = CliRunner()
        result = runner.invoke(
            compile_search_cmd, ["--out", str(tmp_path / "search")]
        )
        assert result.exit_code == 0, f"Expected 0, got {result.exit_code}\n{result.output}"

    def test_compile_search_creates_kg_chunks_schema(self, tmp_path):
        """compile-search creates build/search/kg-chunks/index.schema.json."""
        out = tmp_path / "search"
        runner = CliRunner()
        runner.invoke(compile_search_cmd, ["--out", str(out)])
        schema_path = out / "kg-chunks" / "index.schema.json"
        assert schema_path.exists(), f"Missing {schema_path}"

    def test_compile_search_creates_kg_document_elements_schema(self, tmp_path):
        """compile-search creates build/search/kg-document-elements/index.schema.json."""
        out = tmp_path / "search"
        runner = CliRunner()
        runner.invoke(compile_search_cmd, ["--out", str(out)])
        schema_path = out / "kg-document-elements" / "index.schema.json"
        assert schema_path.exists(), f"Missing {schema_path}"

    def test_chunks_schema_is_valid_json(self, tmp_path):
        """kg-chunks/index.schema.json parses as valid JSON."""
        out = tmp_path / "search"
        runner = CliRunner()
        runner.invoke(compile_search_cmd, ["--out", str(out)])
        schema = json.loads((out / "kg-chunks" / "index.schema.json").read_text())
        assert isinstance(schema, dict)
        assert "fields" in schema

    def test_chunks_schema_has_1536_dim_vector_field(self, tmp_path):
        """kg-chunks schema has a Collection(Edm.Single) vector field with dimensions=1536."""
        out = tmp_path / "search"
        runner = CliRunner()
        runner.invoke(compile_search_cmd, ["--out", str(out)])
        schema = json.loads((out / "kg-chunks" / "index.schema.json").read_text())
        vector_fields = [
            f for f in schema["fields"]
            if f.get("type") == "Collection(Edm.Single)" and f.get("dimensions")
        ]
        assert vector_fields, "No vector field found in kg-chunks schema"
        assert vector_fields[0]["dimensions"] == 1536
        assert vector_fields[0]["dimensions"] == _VECTOR_DIMS

    def test_chunks_schema_entity_ids_filterable(self, tmp_path):
        """entity_ids field must be filterable and NOT searchable (SPEC-002 §11.4)."""
        schema = _build_chunks_schema()
        entity_ids_field = next(
            (f for f in schema["fields"] if f["name"] == "entity_ids"), None
        )
        assert entity_ids_field is not None, "entity_ids field missing"
        assert entity_ids_field["filterable"] is True
        assert entity_ids_field.get("searchable", True) is False
        assert entity_ids_field["type"] == "Collection(Edm.String)"

    def test_chunks_schema_entity_aliases_searchable(self, tmp_path):
        """entity_aliases field must be searchable and NOT filterable (SPEC-002 §11.4)."""
        schema = _build_chunks_schema()
        field = next((f for f in schema["fields"] if f["name"] == "entity_aliases"), None)
        assert field is not None, "entity_aliases field missing"
        assert field["searchable"] is True
        assert field.get("filterable", True) is False
        assert field["type"] == "Collection(Edm.String)"

    def test_chunks_schema_canonical_key_filterable(self, tmp_path):
        """canonical_key must be filterable (stable exact-match filter per SPEC-002 §11.3)."""
        schema = _build_chunks_schema()
        field = next((f for f in schema["fields"] if f["name"] == "canonical_key"), None)
        assert field is not None, "canonical_key field missing"
        assert field["filterable"] is True

    def test_chunks_schema_graph_path_present(self, tmp_path):
        """graph_path field must be present (SPEC-002 §11.3)."""
        schema = _build_chunks_schema()
        field = next((f for f in schema["fields"] if f["name"] == "graph_path"), None)
        assert field is not None, "graph_path field missing from kg-chunks schema"
        assert field.get("retrievable") is True

    def test_chunks_schema_blob_url_present(self, tmp_path):
        """blob_url field must be present with filterable + retrievable."""
        schema = _build_chunks_schema()
        field = next((f for f in schema["fields"] if f["name"] == "blob_url"), None)
        assert field is not None, "blob_url field missing from kg-chunks schema"
        assert field["filterable"] is True
        assert field["retrievable"] is True

    def test_chunks_schema_has_key_field(self, tmp_path):
        """kg-chunks schema must have exactly one key=True field (chunk_id)."""
        schema = _build_chunks_schema()
        key_fields = [f for f in schema["fields"] if f.get("key")]
        assert len(key_fields) == 1
        assert key_fields[0]["name"] == "chunk_id"

    def test_document_elements_schema_has_vector_field(self, tmp_path):
        """kg-document-elements schema has a 1536-dim vector field."""
        schema = _build_document_elements_schema()
        vector_fields = [
            f for f in schema["fields"]
            if f.get("type") == "Collection(Edm.Single)" and f.get("dimensions")
        ]
        assert vector_fields, "No vector field in kg-document-elements schema"
        assert vector_fields[0]["dimensions"] == 1536

    def test_document_elements_schema_has_entity_linkage_fields(self, tmp_path):
        """kg-document-elements schema includes entity_ids, entity_aliases, canonical_key."""
        schema = _build_document_elements_schema()
        names = {f["name"] for f in schema["fields"]}
        for required in ("entity_ids", "entity_aliases", "canonical_key", "graph_path"):
            assert required in names, f"'{required}' missing from kg-document-elements schema"

    def test_compile_search_subset_index(self, tmp_path):
        """--indexes limits which schemas are generated."""
        out = tmp_path / "search"
        runner = CliRunner()
        result = runner.invoke(
            compile_search_cmd, ["--out", str(out), "--indexes", "kg-chunks"]
        )
        assert result.exit_code == 0
        assert (out / "kg-chunks" / "index.schema.json").exists()
        assert not (out / "kg-document-elements" / "index.schema.json").exists()

    def test_compile_search_unknown_index_exits_1(self, tmp_path):
        """--indexes with unknown name exits 1."""
        runner = CliRunner()
        result = runner.invoke(
            compile_search_cmd,
            ["--out", str(tmp_path / "search"), "--indexes", "kg-bogus"],
        )
        assert result.exit_code == 1

    def test_compile_search_no_network_calls(self, tmp_path):
        """compile-search Sprint 1 never makes network calls."""
        with patch("socket.getaddrinfo", side_effect=AssertionError("NETWORK BLOCKED")):
            runner = CliRunner()
            result = runner.invoke(
                compile_search_cmd, ["--out", str(tmp_path / "search")]
            )
        assert result.exit_code == 0


# ===========================================================================
# env-json: verify dev.json loads via config loader
# ===========================================================================


class TestDevEnvJson:
    """Task 4: verify dev.json loads via config loader and lakehouse_item_id is present."""

    def test_dev_json_loads_lakehouse_item_id(self, tmp_path, monkeypatch):
        """load_config('dev') returns a Config with fabric.lakehouse_item_id set.

        Uses the committed dev.json.example template (not the developer's real,
        gitignored dev.json) so the test is self-contained and CI-safe.
        """
        import shutil

        from fabric_kg_builder.config.loader import load_config

        repo_root = Path(__file__).parents[2]
        yaml_path = repo_root / "fabric-kg.yaml"

        # Build a temp environments dir from the committed example template.
        environments_dir = tmp_path / "environments"
        environments_dir.mkdir()
        shutil.copy(
            repo_root / "ontology" / "environments" / "dev.json.example",
            environments_dir / "dev.json",
        )

        # Provide the required Foundry endpoint via env var (mock — not a real call)
        monkeypatch.setenv(
            "AZURE_AI_FOUNDRY_ENDPOINT",
            "https://mock.services.ai.azure.com",
        )
        monkeypatch.chdir(repo_root)

        cfg = load_config(
            env="dev",
            yaml_path=yaml_path,
            environments_dir=environments_dir,
        )
        assert cfg.fabric.lakehouse_item_id, (
            "fabric.lakehouse_item_id is empty — check dev.json.example"
        )
        assert cfg.fabric.workspace_id, (
            "fabric.workspace_id is empty — check dev.json.example"
        )
        # Values come from the sanitized dev.json.example template.
        assert cfg.fabric.lakehouse_item_id == "<your-lakehouse-item-id>"
        assert cfg.fabric.workspace_id == "<your-fabric-workspace-id>"
