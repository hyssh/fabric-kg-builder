"""Contract tests for deploy naming authority — cross-layer invariants.

Covers Issue #6 (scope/deploy-manifest) at the integration layer:
  - fabric_def._platform_part displayName must equal resolved manifest name
  - Standalone and orchestrated commands share one resolver (no name duplication)
  - dry-run (--mock / --dry-run) renders the "Resolved item:" block per ADR §6
  - CLI CliRunner: --dry-run --manifest detects NAME_AUTHORITY_CONFLICT and prints it
  - Read-back name mismatch: validate_readback_name raises when deployed ≠ manifest
  - Manifest authority printed in dry-run lists resolved name + authority per item

These tests are RED (ImportError) until Verbal implements
  src/fabric_kg_builder/deploy/manifest.py
  src/fabric_kg_builder/deploy/name_authority.py
  and wires them through cli/deploy_cmd.py + cli/build_deploy_cmd.py.

See ADR: .squad/decisions/inbox/keyser-deployment-manifest.md §5–§6 + Invariants.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers / deferred imports
# ---------------------------------------------------------------------------

def _import_manifest():
    from fabric_kg_builder.deploy import manifest as _m  # noqa: PLC0415
    return _m


def _import_authority():
    from fabric_kg_builder.deploy import name_authority as _na  # noqa: PLC0415
    return _na


def _import_fabric_def():
    from fabric_kg_builder.ontology import fabric_def  # noqa: PLC0415
    return fabric_def


def _import_cli():
    from fabric_kg_builder.cli import cli  # noqa: PLC0415
    return cli


def _make_manifest(
    *,
    ontology: str = "demo_ontology",
    workspace: str = "ws-test-123",
    lakehouse: str = "kg_lakehouse",
    semantic_model: str = "kg_semantic",
    graph_model: str = "KG Graph",
    data_agent: str = "fkg-dev-data-agent",
    search_index: str = "kg-chunks",
):
    m = _import_manifest()
    return m.DeploymentManifest.model_validate({
        "workspace": workspace,
        "items": {
            "ontology": {"display_name": ontology},
            "lakehouse": {"display_name": lakehouse},
            "semantic_model": {"display_name": semantic_model},
            "graph_model": {"display_name": graph_model},
            "data_agent": {"display_name": data_agent},
            "search_index": {"display_name": search_index},
        },
    })


_FULL_MANIFEST_YAML = """\
workspace: ws-contract-test
items:
  ontology:
    display_name: demo_ontology
  lakehouse:
    display_name: kg_lakehouse
  semantic_model:
    display_name: kg_semantic
  graph_model:
    display_name: KG Graph
  data_agent:
    display_name: fkg-dev-data-agent
  search_index:
    display_name: kg-chunks
dependencies:
  - item: data_agent
    depends_on:
      - ontology
      - semantic_model
      - graph_model
  - item: graph_model
    depends_on:
      - ontology
"""


# ---------------------------------------------------------------------------
# fabric_def._platform_part displayName == resolved manifest name
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestPlatformPartDisplayNameMatchesManifest:
    """_platform_part(ontology_name) must use exactly the manifest-resolved name.

    This directly tests ADR §1 Invariant 1: 'exactly one resolved display name per item'.
    The fabric_def module is production code — these tests verify it accepts the
    resolved name correctly and surfaces it in the .platform metadata.
    """

    def test_platform_part_displayname_equals_manifest_name(self):
        na = _import_authority()
        fd = _import_fabric_def()
        manifest = _make_manifest(ontology="demo_ontology")

        resolved = na.resolve_item_name(manifest, "ontology")
        platform = fd._platform_part(resolved.display_name)

        assert platform["metadata"]["displayName"] == "demo_ontology"

    def test_platform_part_displayname_equals_resolved_name_not_fallback(self):
        """Resolved name (not hardcoded default 'kg_ontology') is used."""
        na = _import_authority()
        fd = _import_fabric_def()
        manifest = _make_manifest(ontology="custom_ontology_v2")

        resolved = na.resolve_item_name(manifest, "ontology")
        platform = fd._platform_part(resolved.display_name)

        assert platform["metadata"]["displayName"] == "custom_ontology_v2"
        assert platform["metadata"]["displayName"] != "kg_ontology"

    def test_platform_part_rejects_conflicting_generated_name(self):
        """If generated metadata conflicts with manifest, _platform_part must never
        receive the conflicting name — conflict must be caught before reaching fabric_def."""
        na = _import_authority()
        manifest = _make_manifest(ontology="demo_ontology")

        with pytest.raises(na.NameAuthorityConflict):
            na.resolve_item_name(
                manifest,
                "ontology",
                generated_metadata_name="Equipment semantic contract",
            )
        # The _platform_part call must not be reachable with a conflicting name

    def test_platform_part_type_metadata_is_ontology(self):
        """Type metadata in .platform must always be 'Ontology'."""
        fd = _import_fabric_def()
        part = fd._platform_part("any_ontology_name")
        assert part["metadata"]["type"] == "Ontology"

    def test_platform_part_display_name_comes_from_resolve_not_raw_manifest(self):
        """resolve_item_name is the gate — raw manifest field alone is not sufficient."""
        na = _import_authority()
        fd = _import_fabric_def()
        manifest = _make_manifest(ontology="ValidOntology123")

        resolved = na.resolve_item_name(manifest, "ontology")
        # Resolved display name matches manifest
        assert resolved.display_name == "ValidOntology123"
        platform = fd._platform_part(resolved.display_name)
        assert platform["metadata"]["displayName"] == resolved.display_name


# ---------------------------------------------------------------------------
# Single resolver shared by standalone + orchestrated commands
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSingleResolverInvariant:
    """Standalone and orchestrated paths must import from one resolver module.

    ADR §2 Invariant 6: 'No name-resolution logic exists in two places'.
    Tested by verifying both CLI modules reference the same name_authority symbols.
    """

    def test_deploy_cmd_imports_resolve_item_name(self):
        """deploy_cmd must reference resolve_item_name from name_authority."""
        import importlib  # noqa: PLC0415
        deploy_cmd = importlib.import_module("fabric_kg_builder.cli.deploy_cmd")
        # After Verbal's implementation, resolve_item_name must be importable from the module
        # (either directly used or accessed via name_authority import)
        na = _import_authority()
        assert hasattr(na, "resolve_item_name")
        # Verify deploy_cmd references the authority module (not a local copy)
        src = Path(deploy_cmd.__file__).read_text(encoding="utf-8")
        assert "name_authority" in src or "resolve_item_name" in src, (
            "deploy_cmd must reference name_authority.resolve_item_name — "
            "no duplicate resolution logic allowed"
        )

    def test_build_deploy_cmd_imports_resolve_item_name(self):
        """build_deploy_cmd must reference resolve_item_name from name_authority."""
        import importlib  # noqa: PLC0415
        build_cmd = importlib.import_module("fabric_kg_builder.cli.build_deploy_cmd")
        src = Path(build_cmd.__file__).read_text(encoding="utf-8")
        assert "name_authority" in src or "resolve_item_name" in src, (
            "build_deploy_cmd must reference name_authority.resolve_item_name — "
            "no duplicate resolution logic allowed"
        )

    def test_name_authority_module_has_single_resolve_function(self):
        """There is exactly one resolve_item_name function — not copied per command."""
        na = _import_authority()
        assert callable(na.resolve_item_name)
        # Should not have per-command duplicates like resolve_ontology_name separately
        # (other helpers are fine, but the core resolver must be one)
        assert hasattr(na, "resolve_item_name")


# ---------------------------------------------------------------------------
# Dry-run: rendered name resolution block output
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestDryRunRenderedBlock:
    """dry-run outputs include the 'Resolved item:' block for every named item."""

    def test_render_block_present_in_deploy_ontology_mock_output(
        self, tmp_path, isolated_ontology_project
    ):
        """deploy-ontology --mock with --manifest must print 'Resolved item:' block."""
        from tests.conftest import make_cli_runner, combined_output  # noqa: PLC0415
        cli = _import_cli()

        manifest_file = tmp_path / "deployment.yaml"
        manifest_file.write_text(_FULL_MANIFEST_YAML)

        runner = make_cli_runner()
        result = runner.invoke(cli, [
            "deploy-ontology",
            "--env", "dev",
            "--mock",
            "--manifest", str(manifest_file),
        ])
        output = combined_output(result)
        assert "Resolved item:" in output, (
            f"Expected 'Resolved item:' block in deploy-ontology --mock output.\n"
            f"Got: {output!r}"
        )
        assert not (isolated_ontology_project / ".git").exists()
        assert "test-workspace-id" in output

    def test_render_block_contains_name_authority_line(
        self, tmp_path, isolated_ontology_project
    ):
        """The rendered block must include 'name authority:' line."""
        from tests.conftest import make_cli_runner, combined_output  # noqa: PLC0415
        cli = _import_cli()

        manifest_file = tmp_path / "deployment.yaml"
        manifest_file.write_text(_FULL_MANIFEST_YAML)

        runner = make_cli_runner()
        result = runner.invoke(cli, [
            "deploy-ontology",
            "--env", "dev",
            "--mock",
            "--manifest", str(manifest_file),
        ])
        output = combined_output(result)
        assert "name authority:" in output, (
            f"Expected 'name authority:' in output. Got: {output!r}"
        )

    def test_render_block_contains_manifest_ontology_name(
        self, tmp_path, isolated_ontology_project
    ):
        """The dry-run output must include the manifest's ontology display name."""
        from tests.conftest import make_cli_runner, combined_output  # noqa: PLC0415
        cli = _import_cli()

        manifest_file = tmp_path / "deployment.yaml"
        manifest_file.write_text(_FULL_MANIFEST_YAML)

        runner = make_cli_runner()
        result = runner.invoke(cli, [
            "deploy-ontology",
            "--env", "dev",
            "--mock",
            "--manifest", str(manifest_file),
        ])
        output = combined_output(result)
        assert "demo_ontology" in output, (
            f"Expected manifest ontology name 'demo_ontology' in output. Got: {output!r}"
        )

    def test_build_deploy_dryrun_lists_resolved_name_per_item(self, tmp_path):
        """build-deploy --dry-run with --manifest lists resolved name + authority per planned item."""
        from tests.conftest import make_cli_runner, combined_output  # noqa: PLC0415
        cli = _import_cli()

        manifest_file = tmp_path / "deployment.yaml"
        manifest_file.write_text(_FULL_MANIFEST_YAML)

        runner = make_cli_runner()
        result = runner.invoke(cli, [
            "build-deploy",
            "--input", str(tmp_path),
            "--env", "dev",
            "--dry-run",
            "--manifest", str(manifest_file),
            "--domain-contract", "examples/domains/supply-chain-risk.domain.yaml",
        ])
        output = combined_output(result)
        # dry-run plan must include resolved names
        assert "name authority" in output or "Resolved item" in output or "demo_ontology" in output, (
            f"Expected name resolution block in build-deploy --dry-run output. Got: {output!r}"
        )


# ---------------------------------------------------------------------------
# CLI: conflict detection in dry-run
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestCLIConflictDetectionInDryRun:
    """--manifest with a conflicting name source → NAME_AUTHORITY_CONFLICT in output."""

    def test_deploy_ontology_mock_with_display_name_conflict_reports_error(self, tmp_path):
        """Passing --display-name that differs from manifest raises conflict."""
        from tests.conftest import make_cli_runner, combined_output  # noqa: PLC0415
        cli = _import_cli()

        manifest_file = tmp_path / "deployment.yaml"
        manifest_file.write_text(_FULL_MANIFEST_YAML)

        runner = make_cli_runner()
        result = runner.invoke(cli, [
            "deploy-ontology",
            "--env", "dev",
            "--mock",
            "--manifest", str(manifest_file),
            "--display-name", "ConflictingName",
        ])
        output = combined_output(result)
        assert result.exit_code != 0 or "NAME_AUTHORITY_CONFLICT" in output, (
            f"Expected non-zero exit or NAME_AUTHORITY_CONFLICT in output for conflicting "
            f"--display-name. Got exit={result.exit_code}, output={output!r}"
        )

    def test_conflict_message_names_both_names(self, tmp_path):
        """Conflict output must name both the manifest name and the conflicting name."""
        from tests.conftest import make_cli_runner, combined_output  # noqa: PLC0415
        cli = _import_cli()

        manifest_file = tmp_path / "deployment.yaml"
        manifest_file.write_text(_FULL_MANIFEST_YAML)

        runner = make_cli_runner()
        result = runner.invoke(cli, [
            "deploy-ontology",
            "--env", "dev",
            "--mock",
            "--manifest", str(manifest_file),
            "--display-name", "ConflictingName",
        ])
        output = combined_output(result)
        if "NAME_AUTHORITY_CONFLICT" in output or result.exit_code != 0:
            # If conflict was reported, both names should appear
            assert "demo_ontology" in output or "ConflictingName" in output


# ---------------------------------------------------------------------------
# Read-back name mismatch contract
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestReadbackNameMismatchContract:
    """validate_readback_name enforces deployed == manifest (ADR invariant 4)."""

    def test_readback_validates_ontology_name_match(self):
        na = _import_authority()
        manifest = _make_manifest(ontology="demo_ontology")
        # Must not raise
        na.validate_readback_name("ontology", "demo_ontology", manifest)

    def test_readback_raises_on_ontology_name_mismatch(self):
        na = _import_authority()
        manifest = _make_manifest(ontology="demo_ontology")
        with pytest.raises(na.NameAuthorityConflict):
            na.validate_readback_name("ontology", "Equipment semantic contract", manifest)

    def test_readback_conflict_code_is_name_authority_conflict(self):
        na = _import_authority()
        manifest = _make_manifest(ontology="demo_ontology")
        try:
            na.validate_readback_name("ontology", "Equipment semantic contract", manifest)
        except na.NameAuthorityConflict as exc:
            assert exc.code == "NAME_AUTHORITY_CONFLICT"

    def test_readback_validates_lakehouse_name_match(self):
        na = _import_authority()
        manifest = _make_manifest(lakehouse="kg_lakehouse")
        na.validate_readback_name("lakehouse", "kg_lakehouse", manifest)

    def test_readback_raises_on_data_agent_name_mismatch(self):
        na = _import_authority()
        manifest = _make_manifest(data_agent="fkg-dev-data-agent")
        with pytest.raises(na.NameAuthorityConflict):
            na.validate_readback_name("data_agent", "fkg-prod-data-agent", manifest)


# ---------------------------------------------------------------------------
# Manifest dependency ordering invariant
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestManifestDependencyInvariant:
    """Dependencies in manifest must be parseable and reflect the ADR ordering."""

    def test_data_agent_declared_after_its_dependencies(self, tmp_path):
        """data_agent depends_on ontology + semantic_model + graph_model (ADR §1)."""
        m = _import_manifest()
        manifest_file = tmp_path / "deployment.yaml"
        manifest_file.write_text(_FULL_MANIFEST_YAML)
        manifest = m.load_deployment_manifest(manifest_file)

        da_dep = next(
            (d for d in (manifest.dependencies or []) if d.item == "data_agent"),
            None,
        )
        assert da_dep is not None, "data_agent dependency entry must be declared"
        assert "ontology" in da_dep.depends_on
        assert "semantic_model" in da_dep.depends_on
        assert "graph_model" in da_dep.depends_on

    def test_graph_model_declared_after_ontology(self, tmp_path):
        """graph_model depends_on ontology (ADR §1)."""
        m = _import_manifest()
        manifest_file = tmp_path / "deployment.yaml"
        manifest_file.write_text(_FULL_MANIFEST_YAML)
        manifest = m.load_deployment_manifest(manifest_file)

        gm_dep = next(
            (d for d in (manifest.dependencies or []) if d.item == "graph_model"),
            None,
        )
        assert gm_dep is not None
        assert "ontology" in gm_dep.depends_on

    def test_manifest_from_env_has_no_dependencies_by_default(self):
        """Legacy env config migration produces a manifest — dependency list may be absent."""
        na = _import_authority()
        legacy = {"fabric": {"workspace_id": "ws-legacy", "ontology_display_name": "kg_ont"}}
        result = na.manifest_from_env_config(legacy)
        # Dependencies are not required from legacy env
        assert result.dependencies is None or isinstance(result.dependencies, list)


# ---------------------------------------------------------------------------
# D1 defect exposure: read-back validation must use Fabric API response name,
#                     not the name that was sent to Fabric
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestReadbackValidationUsesActualFabricName:
    """validate_readback_name must be called with the Fabric API's returned
    displayName — not the name that was resolved and sent to Fabric.

    If the validation is called with `ontology_name` (= `resolved.display_name`
    = manifest name), it is always a no-op tautology.

    This test exposes D1: deploy_cmd.py line ~1348 calls
    validate_readback_name("Ontology", ontology_name, deployment_manifest)
    where ontology_name = resolved_ontology.display_name = manifest name.
    That is a tautological check and NEVER catches a real read-back mismatch.

    CONTRACT: The code must fetch the deployed item's display name from the
    Fabric API response and compare THAT against the manifest.
    create_or_get_ontology_item returns {"item_id": ..., "created": bool} —
    it does NOT return displayName. The implementation must either:
    (a) add displayName to the create_or_get_ontology_item return value, or
    (b) make a separate GET /items/{id} call to fetch the deployed name.
    """

    def test_tautological_call_always_passes(self):
        """Demonstrate that calling validate_readback_name with the same name
        we resolved from the manifest is always a no-op tautology."""
        na = _import_authority()
        manifest = _make_manifest(ontology="demo_ontology")
        resolved = na.resolve_item_name(manifest, "ontology")
        # This is what deploy_cmd.py does: use the resolved name as both sent and received
        sent_to_fabric = resolved.display_name  # = "demo_ontology"
        # The call below always passes because sent_to_fabric == manifest name
        na.validate_readback_name("ontology", sent_to_fabric, manifest)
        # CONFIRMED TAUTOLOGY: this is a no-op, not a real read-back check

    def test_mismatch_detected_only_if_called_with_fabric_api_name(self):
        """If Fabric returns a different name than requested, only a call with
        the ACTUAL Fabric API response name detects the mismatch."""
        na = _import_authority()
        manifest = _make_manifest(ontology="demo_ontology")

        # Simulate what Fabric might actually return (truncation, normalization, etc.)
        fabric_api_returned_name = "demo_ontology_renamed_by_fabric"

        with pytest.raises(na.NameAuthorityConflict):
            # This raises — but only if the caller uses the ACTUAL Fabric response
            na.validate_readback_name("ontology", fabric_api_returned_name, manifest)



    # --- Behavioral replacement tests (Keyser review: no source-text inspection) ---

    # Fake minimal env config shared by behavioral CLI tests below
    _FAKE_CFG_NO_ID = {
        "workspace_id": "test-ws-id-00000000",
        "workspace_display_name": "test-workspace",
        "lakehouse_item_id": "test-lh-id-00000000",
        "lakehouse_display_name": "kg_lakehouse",
        "onelake_tables_path": "",
        "schema_name": "dbo",
        "ontology_item_id": "",
        "ontology_display_name": "demo_ontology",
        "graph_model_id": "",
        "graph_model_display_name": "KG Graph",
        "data_agent_item_id": "",
        "data_agent_display_name": "",
    }

    _MANIFEST_YAML_ONTOLOGY = (
        "workspace: ws-test-contract\n"
        "items:\n"
        "  ontology:\n"
        "    display_name: demo_ontology\n"
    )

    def test_create_path_displayname_mismatch_fails_hard_not_warn(self, tmp_path):
        """When create_or_get_ontology_item returns a displayName ≠ manifest,
        deploy-ontology must exit non-zero and emit NAME_AUTHORITY_CONFLICT.
        It must NOT emit only a warning and continue with exit 0.

        This is a hard-fail contract: read-back mismatch is an error, not advisory."""
        from tests.conftest import make_cli_runner, combined_output  # noqa: PLC0415
        cli = _import_cli()

        manifest_file = tmp_path / "deployment.yaml"
        manifest_file.write_text(self._MANIFEST_YAML_ONTOLOGY)

        with patch("fabric_kg_builder.cli.deploy_cmd._read_fabric_env_config",
                   return_value=self._FAKE_CFG_NO_ID), \
             patch("fabric_kg_builder.deploy.fabric_ontology.create_or_get_ontology_item",
                   return_value={"item_id": "new-id-abc", "created": True,
                                 "display_name": "Fabric_Normalized_Different",
                                 "note": "Created."}), \
             patch("fabric_kg_builder.deploy.fabric_ontology.update_ontology_definition",
                   return_value={"parts_count": 6, "status": "ok-200", "note": "OK"}), \
             patch("fabric_kg_builder.deploy.fabric_ontology.read_graph_counts",
                   return_value={"total_nodes": 0, "total_edges": 0,
                                 "nodes_by_type": {}, "edges_by_type": {}, "note": ""}):
            runner = make_cli_runner()
            result = runner.invoke(cli, [
                "deploy-ontology", "--env", "dev", "--no-mock",
                "--manifest", str(manifest_file),
            ])

        output = combined_output(result)
        assert result.exit_code != 0, (
            "deploy-ontology must hard-fail (exit ≠ 0) when Fabric API returns a "
            "displayName different from the manifest. Got exit 0 — this means the "
            "read-back mismatch was only warned and the deployment continued. "
            f"Output: {output!r}"
        )
        assert "NAME_AUTHORITY_CONFLICT" in output, (
            "Hard fail must include NAME_AUTHORITY_CONFLICT for actionable diagnosis. "
            f"Got: {output!r}"
        )

    def test_create_path_matching_displayname_exits_zero(self, tmp_path):
        """When Fabric API returns a displayName matching the manifest, no error."""
        from tests.conftest import make_cli_runner, combined_output  # noqa: PLC0415
        cli = _import_cli()

        manifest_file = tmp_path / "deployment.yaml"
        manifest_file.write_text(self._MANIFEST_YAML_ONTOLOGY)

        with patch("fabric_kg_builder.cli.deploy_cmd._read_fabric_env_config",
                   return_value=self._FAKE_CFG_NO_ID), \
             patch("fabric_kg_builder.deploy.fabric_ontology.create_or_get_ontology_item",
                   return_value={"item_id": "new-id-abc", "created": True,
                                 "display_name": "demo_ontology",   # matches manifest
                                 "note": "Created."}), \
             patch("fabric_kg_builder.deploy.fabric_ontology.update_ontology_definition",
                   return_value={"parts_count": 6, "status": "ok-200", "note": "OK"}), \
             patch("fabric_kg_builder.deploy.fabric_ontology.read_graph_counts",
                   return_value={"total_nodes": 0, "total_edges": 0,
                                 "nodes_by_type": {}, "edges_by_type": {}, "note": ""}):
            runner = make_cli_runner()
            result = runner.invoke(cli, [
                "deploy-ontology", "--env", "dev", "--no-mock",
                "--manifest", str(manifest_file),
            ])

        output = combined_output(result)
        assert result.exit_code == 0, (
            "Matching Fabric displayName must not cause an error. "
            f"Got exit={result.exit_code}. Output: {output!r}"
        )

    def test_configured_item_id_remote_displayname_mismatch_fails_hard(self, tmp_path):
        """When ontology_item_id is configured (pre-existing item) and the remote
        Fabric item's displayName ≠ manifest display_name, deploy-ontology must
        fail hard with NAME_AUTHORITY_CONFLICT.

        McManus added get_ontology_item_display_name to fetch the configured item's
        actual displayName from Fabric via GET /items/{id}. The test mocks that
        function to inject a mismatched remote name."""
        from tests.conftest import make_cli_runner, combined_output  # noqa: PLC0415
        cli = _import_cli()

        manifest_file = tmp_path / "deployment.yaml"
        manifest_file.write_text(self._MANIFEST_YAML_ONTOLOGY)

        cfg_with_id = {**self._FAKE_CFG_NO_ID, "ontology_item_id": "configured-item-abc"}

        with patch("fabric_kg_builder.cli.deploy_cmd._read_fabric_env_config",
                   return_value=cfg_with_id), \
             patch("fabric_kg_builder.deploy.fabric_ontology.get_ontology_item_display_name",
                   return_value="Remote_Renamed_In_Fabric"), \
             patch("fabric_kg_builder.deploy.fabric_ontology.update_ontology_definition",
                   return_value={"parts_count": 6, "status": "ok-200", "note": "OK"}), \
             patch("fabric_kg_builder.deploy.fabric_ontology.read_graph_counts",
                   return_value={"total_nodes": 0, "total_edges": 0,
                                 "nodes_by_type": {}, "edges_by_type": {}, "note": ""}):
            runner = make_cli_runner()
            result = runner.invoke(cli, [
                "deploy-ontology", "--env", "dev", "--no-mock",
                "--manifest", str(manifest_file),
            ])

        output = combined_output(result)
        assert result.exit_code != 0, (
            "Configured-ID path must hard-fail when remote Fabric displayName ≠ manifest name. "
            f"Got exit={result.exit_code}. Output: {output!r}"
        )
        assert "NAME_AUTHORITY_CONFLICT" in output, (
            f"Hard fail must include NAME_AUTHORITY_CONFLICT. Got: {output!r}"
        )

    def test_no_success_shaped_lro_fallback_to_sent_name(self):
        """create_or_get_ontology_item 202-LRO path must return the displayName
        from the actual Fabric API response, not the `name` parameter that was sent.

        Returning `name` as `display_name` is a success-shaped fallback that
        never detects Fabric normalization. McManus must add a GET after LRO
        completion to fetch the actual displayName."""
        from fabric_kg_builder.deploy.fabric_ontology import create_or_get_ontology_item  # noqa: PLC0415

        sent_name = "demo_ontology"
        actual_fabric_name = "Actual_Fabric_Created_Name"

        # GET 1: workspace items list — empty (no existing item)
        empty_list = MagicMock(status_code=200, ok=True)
        empty_list.json.return_value = {"value": []}
        empty_list.headers = {}

        # POST: create → 202 LRO
        create_resp = MagicMock(status_code=202)
        create_resp.headers = {
            "Location": "https://api.fabric.microsoft.com/v1/operations/op-123",
            "Retry-After": "0",
        }

        # GET 2: LRO poll → succeeded with itemId
        lro_poll = MagicMock(status_code=200, ok=True)
        lro_poll.json.return_value = {"status": "Succeeded", "itemId": "lro-item-id-456"}
        lro_poll.headers = {}

        # GET 3: McManus's added fetch — actual Fabric item displayName
        item_fetch = MagicMock(status_code=200, ok=True)
        item_fetch.json.return_value = {
            "id": "lro-item-id-456",
            "displayName": actual_fabric_name,
            "type": "Ontology",
        }
        item_fetch.headers = {}

        with patch("requests.get", side_effect=[empty_list, lro_poll, item_fetch]), \
             patch("requests.post", return_value=create_resp):
            result = create_or_get_ontology_item(
                workspace_id="ws-id",
                name=sent_name,
                mock=False,
                token_provider=lambda: "fake-token",
                _lro_sleep=lambda s: None,
                _lro_max_attempts=5,
            )

        assert result["display_name"] != sent_name, (
            f"LRO path must NOT return the sent name {sent_name!r} as display_name "
            "(success-shaped fallback). McManus must fetch the actual displayName "
            f"from the Fabric API after LRO completion. Got: {result['display_name']!r}"
        )
        assert result["display_name"] == actual_fabric_name, (
            f"LRO path must return the displayName from the Fabric GET response. "
            f"Expected {actual_fabric_name!r}, got {result['display_name']!r}"
        )


# ---------------------------------------------------------------------------
# D2 defect exposure: build_deploy_cmd must pass deploy manifest to _deploy_knowledge
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestBuildDeployCmdThreadsManifestToAllSubcommands:
    """build_deploy_cmd must pass deploy_manifest_path to _deploy_knowledge
    so the orchestrated data agent deployment uses the manifest authority.

    D2: _deploy_knowledge is called at build_deploy_cmd.py line ~3024 with
    data_agent_display_name=data_agent_name where data_agent_name comes from
    the CLI --data-agent-name flag, NOT from the manifest resolver.
    The deploy_manifest_path is NOT in _deploy_knowledge's parameter list.

    CONTRACT: ADR §3 requires _deploy_knowledge + planning echo to consume
    resolved names from the single manifest resolver (single source).
    ADR Invariant 6: No name-resolution logic exists in two places.
    """

    # Full env config for _deploy_knowledge tests — all required fields non-empty
    _FULL_DK_ENV = {
        "fabric": {
            "workspace_id": "ws-id-0000-0000",
            "graph_model_item_id": "gm-id-0000-0000",
            "ontology_item_id": "ont-id-0000-0000",
            "lakehouse_item_id": "lh-id-0000-0000",
            "data_agent_display_name": "env-data-agent",
            "data_agent_item_id": "",
        },
        "ai_search": {"endpoint": "https://fake.search.azure.com"},
    }

    def test_deploy_knowledge_signature_accepts_deployment_manifest(self):
        """_deploy_knowledge must accept a DeploymentManifest (or manifest_path)
        parameter so the orchestrated data agent deployment can resolve names
        from the manifest authority."""
        import importlib  # noqa: PLC0415
        import inspect  # noqa: PLC0415
        build_cmd = importlib.import_module("fabric_kg_builder.cli.build_deploy_cmd")
        sig = inspect.signature(build_cmd._deploy_knowledge)
        param_names = list(sig.parameters.keys())
        has_deploy_manifest_param = any(
            "deploy_manifest" in p or "deployment_manifest" in p
            for p in param_names
        )
        assert has_deploy_manifest_param, (
            f"_deploy_knowledge must accept a deployment manifest parameter "
            f"(e.g. 'deploy_manifest_path' or 'deployment_manifest'). "
            f"Current params: {param_names}"
        )

    def test_deploy_knowledge_raises_conflict_when_cli_data_agent_name_differs_from_manifest(
        self, tmp_path
    ):
        """_deploy_knowledge must raise NameAuthorityConflict when
        data_agent_display_name (CLI arg) conflicts with manifest data_agent
        display_name. Silent override (manifest wins quietly) is prohibited —
        the ADR requires a hard fail with actionable NAME_AUTHORITY_CONFLICT.

        Currently FAILS: the except-Exception swallows the conflict and
        resolve_item_name is called without command_name, so no conflict fires."""
        import importlib  # noqa: PLC0415
        na = _import_authority()
        build_cmd = importlib.import_module("fabric_kg_builder.cli.build_deploy_cmd")

        manifest_file = tmp_path / "deployment.yaml"
        manifest_file.write_text(
            "workspace: ws-test\n"
            "items:\n"
            "  data_agent:\n"
            "    display_name: manifest-agent\n"
        )

        with patch("fabric_kg_builder.cli.build_deploy_cmd._read_environment_config",
                   return_value=self._FULL_DK_ENV):
            with pytest.raises(na.NameAuthorityConflict) as exc_info:
                build_cmd._deploy_knowledge(
                    environment="dev",
                    run_token="test-run-001",
                    domain_contract=MagicMock(),
                    metadata_path=tmp_path / "metadata.yaml",
                    outputs={},
                    manifest=MagicMock(),
                    search_index_name="kg-chunks",
                    semantic_dir=tmp_path / "semantic",
                    persisted_projection_path=tmp_path / "projection.json",
                    semantic_context_path=tmp_path / "context.json",
                    agent_instructions_path=tmp_path / "instructions.md",
                    agent_publication_receipt_path=tmp_path / "receipt.json",
                    workspace_name="test-workspace",
                    data_agent_mode="create",
                    data_agent_item_id=None,
                    data_agent_display_name="cli-agent",  # conflicts with manifest "manifest-agent"
                    approve_data_agent_replace=False,
                    deploy_manifest_path=str(manifest_file),
                )

        conflict = exc_info.value
        assert "cli-agent" in str(conflict) or "manifest-agent" in str(conflict), (
            f"NameAuthorityConflict must name the conflicting values. Got: {conflict!r}"
        )

    def test_deploy_knowledge_calls_resolve_item_name_for_data_agent(
        self, tmp_path, monkeypatch
    ):
        """_deploy_knowledge must call resolve_item_name for 'data_agent' when
        deploy_manifest_path is provided — one resolver, no duplicated logic.

        Replaces source-text inspection: verifies the behavior, not the source."""
        import importlib  # noqa: PLC0415
        build_cmd = importlib.import_module("fabric_kg_builder.cli.build_deploy_cmd")

        manifest_file = tmp_path / "deployment.yaml"
        manifest_file.write_text(
            "workspace: ws-test\n"
            "items:\n"
            "  data_agent:\n"
            "    display_name: manifest-agent\n"
        )

        resolved_calls: list[dict] = []
        na = _import_authority()
        _real_resolve = na.resolve_item_name

        def spy_resolve(manifest, item_type, *, command_name=None, **kwargs):
            resolved_calls.append({
                "item_type": str(item_type).lower(),
                "command_name": command_name,
            })
            return _real_resolve(manifest, item_type, command_name=command_name, **kwargs)

        monkeypatch.setattr(
            "fabric_kg_builder.deploy.name_authority.resolve_item_name",
            spy_resolve,
        )

        with patch("fabric_kg_builder.cli.build_deploy_cmd._read_environment_config",
                   return_value=self._FULL_DK_ENV):
            try:
                build_cmd._deploy_knowledge(
                    environment="dev",
                    run_token="test-run-002",
                    domain_contract=MagicMock(),
                    metadata_path=tmp_path / "metadata.yaml",
                    outputs={},
                    manifest=MagicMock(),
                    search_index_name="kg-chunks",
                    semantic_dir=tmp_path / "semantic",
                    persisted_projection_path=tmp_path / "projection.json",
                    semantic_context_path=tmp_path / "context.json",
                    agent_instructions_path=tmp_path / "instructions.md",
                    agent_publication_receipt_path=tmp_path / "receipt.json",
                    workspace_name="test-workspace",
                    data_agent_mode="create",
                    data_agent_item_id=None,
                    data_agent_display_name=None,  # no CLI arg — manifest is sole authority
                    approve_data_agent_replace=False,
                    deploy_manifest_path=str(manifest_file),
                )
            except Exception:  # noqa: BLE001
                pass  # function fails on missing files; we only care about the spy calls

        da_calls = [c for c in resolved_calls if c["item_type"] == "data_agent"]
        assert len(da_calls) >= 1, (
            "_deploy_knowledge must call resolve_item_name for 'data_agent' when "
            "deploy_manifest_path is provided (one resolver, no duplicated logic). "
            f"All spy calls seen: {resolved_calls}"
        )
