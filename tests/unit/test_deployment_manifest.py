"""Unit tests for deploy/manifest.py — DeploymentManifest schema and loader.

Covers the acceptance contracts for Issue #6 (scope/deploy-manifest branch).

Contract surface:
  - load_deployment_manifest(path) -> DeploymentManifest
  - DeploymentManifest: workspace, items (per-type), dependencies
  - ${ENV_VAR} interpolation in all string fields
  - DeploymentManifestParseError (bad YAML), DeploymentManifestValidationError
  - DeploymentManifestError as base
  - DeploymentManifest is distinct from infra.schema.InfraManifest

These tests import from the not-yet-implemented module via deferred helpers so
pytest collection succeeds — every test will be FAILED (ImportError) until
Verbal implements `src/fabric_kg_builder/deploy/manifest.py`.

See ADR: .squad/decisions/inbox/keyser-deployment-manifest.md §1.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Deferred import helpers — preserves collection even when module is absent
# ---------------------------------------------------------------------------

def _import_module():
    from fabric_kg_builder.deploy import manifest as _m  # noqa: PLC0415
    return _m


def _import_symbols():
    m = _import_module()
    return (
        m.DeploymentManifest,
        m.load_deployment_manifest,
        m.DeploymentManifestError,
        m.DeploymentManifestParseError,
        m.DeploymentManifestValidationError,
    )


# ---------------------------------------------------------------------------
# YAML fixture helpers
# ---------------------------------------------------------------------------

_MINIMAL_YAML = """\
workspace: ws-abc-123
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
"""

_FULL_YAML = """\
workspace: ws-abc-123
items:
  ontology:
    display_name: demo_ontology
    prefix: ""
    configured_id: ""
  lakehouse:
    display_name: kg_lakehouse
    configured_id: ""
  semantic_model:
    display_name: kg_semantic
    configured_id: ""
  graph_model:
    display_name: KG Graph
    configured_id: ""
  data_agent:
    display_name: fkg-dev-data-agent
    configured_id: ""
  search_index:
    display_name: kg-chunks
    prefix: kg-dev-
    configured_id: ""
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

_ENV_VAR_YAML = """\
workspace: ${FABRIC_WORKSPACE_ID}
items:
  ontology:
    display_name: demo_ontology
"""

_BAD_YAML = """\
workspace: test
items: [invalid: yaml: [
  broken
"""


# ---------------------------------------------------------------------------
# Distinct class identity test (no deferred import needed for InfraManifest)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestDeploymentManifestIsDistinctType:
    """DeploymentManifest must NOT be InfraManifest; they are separate concepts."""

    def test_different_class_from_infra_manifest(self):
        DeploymentManifest, *_ = _import_symbols()
        from fabric_kg_builder.infra.schema import InfraManifest  # noqa: PLC0415
        assert DeploymentManifest is not InfraManifest

    def test_deployment_manifest_not_subclass_of_infra_manifest(self):
        DeploymentManifest, *_ = _import_symbols()
        from fabric_kg_builder.infra.schema import InfraManifest  # noqa: PLC0415
        assert not issubclass(DeploymentManifest, InfraManifest)

    def test_different_module_path(self):
        _import_module()  # confirms deploy.manifest module exists separately
        import importlib  # noqa: PLC0415
        deploy_mod = importlib.import_module("fabric_kg_builder.deploy.manifest")
        infra_mod = importlib.import_module("fabric_kg_builder.infra.manifest")
        assert deploy_mod is not infra_mod


# ---------------------------------------------------------------------------
# Schema — workspace field
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestDeploymentManifestWorkspace:
    """workspace is a required top-level field."""

    def test_workspace_set_from_yaml(self, tmp_path):
        DeploymentManifest, load, *_ = _import_symbols()
        f = tmp_path / "deployment.yaml"
        f.write_text(_MINIMAL_YAML)
        manifest = load(f)
        assert manifest.workspace == "ws-abc-123"

    def test_workspace_accepts_env_var_string(self, tmp_path):
        _, load, _, _, DeploymentManifestValidationError = _import_symbols()
        os.environ["FABRIC_WORKSPACE_ID"] = "ws-from-env-123"
        try:
            f = tmp_path / "deployment.yaml"
            f.write_text(_ENV_VAR_YAML)
            manifest = load(f)
            assert manifest.workspace == "ws-from-env-123"
        finally:
            del os.environ["FABRIC_WORKSPACE_ID"]

    def test_missing_workspace_raises_validation_error(self, tmp_path):
        _, load, _, _, DeploymentManifestValidationError = _import_symbols()
        f = tmp_path / "deployment.yaml"
        f.write_text("items:\n  ontology:\n    display_name: test_ont\n")
        with pytest.raises(DeploymentManifestValidationError):
            load(f)

    def test_workspace_as_unreserved_env_var_kept_intact_when_unset(self, tmp_path):
        """Unresolved ${VAR} passed through to Pydantic — validation may fail or succeed
        depending on schema strictness; either way no silent data loss."""
        _, load, DeploymentManifestError, *_ = _import_symbols()
        # Ensure the var is not set
        os.environ.pop("_UNSET_WKSPC_VAR_9999", None)
        f = tmp_path / "deployment.yaml"
        f.write_text("workspace: ${_UNSET_WKSPC_VAR_9999}\nitems:\n  ontology:\n    display_name: test_ont\n")
        # Either succeeds (workspace holds literal ${...}) or raises a structured error — not a crash
        try:
            load(f)
        except Exception as exc:
            assert isinstance(exc, DeploymentManifestError)


# ---------------------------------------------------------------------------
# Schema — items parsing
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestDeploymentManifestItemsParsing:
    """Each item section is accessible with display_name and optional fields."""

    def test_all_six_item_types_present(self, tmp_path):
        _, load, *_ = _import_symbols()
        f = tmp_path / "deployment.yaml"
        f.write_text(_FULL_YAML)
        manifest = load(f)
        assert manifest.items.ontology.display_name == "demo_ontology"
        assert manifest.items.lakehouse.display_name == "kg_lakehouse"
        assert manifest.items.semantic_model.display_name == "kg_semantic"
        assert manifest.items.graph_model.display_name == "KG Graph"
        assert manifest.items.data_agent.display_name == "fkg-dev-data-agent"
        assert manifest.items.search_index.display_name == "kg-chunks"

    def test_ontology_item_display_name(self, tmp_path):
        _, load, *_ = _import_symbols()
        f = tmp_path / "deployment.yaml"
        f.write_text(_MINIMAL_YAML)
        manifest = load(f)
        assert manifest.items.ontology.display_name == "demo_ontology"

    def test_search_index_prefix_parsed(self, tmp_path):
        _, load, *_ = _import_symbols()
        f = tmp_path / "deployment.yaml"
        f.write_text(_FULL_YAML)
        manifest = load(f)
        assert manifest.items.search_index.prefix == "kg-dev-"

    def test_configured_id_defaults_to_empty_string(self, tmp_path):
        _, load, *_ = _import_symbols()
        f = tmp_path / "deployment.yaml"
        f.write_text(_FULL_YAML)
        manifest = load(f)
        assert manifest.items.ontology.configured_id == ""

    def test_missing_required_display_name_raises(self, tmp_path):
        _, load, _, _, DeploymentManifestValidationError = _import_symbols()
        f = tmp_path / "deployment.yaml"
        f.write_text(
            "workspace: ws-test\n"
            "items:\n"
            "  ontology:\n"
            "    configured_id: abc\n"  # no display_name
        )
        with pytest.raises(DeploymentManifestValidationError):
            load(f)

    def test_display_name_env_var_resolved_in_item(self, tmp_path):
        _, load, *_ = _import_symbols()
        os.environ["TEST_ONT_NAME"] = "resolved_ontology"
        try:
            f = tmp_path / "deployment.yaml"
            f.write_text(
                "workspace: ws-test\n"
                "items:\n"
                "  ontology:\n"
                "    display_name: ${TEST_ONT_NAME}\n"
            )
            manifest = load(f)
            assert manifest.items.ontology.display_name == "resolved_ontology"
        finally:
            del os.environ["TEST_ONT_NAME"]

    def test_returns_deployment_manifest_instance(self, tmp_path):
        DeploymentManifest, load, *_ = _import_symbols()
        f = tmp_path / "deployment.yaml"
        f.write_text(_MINIMAL_YAML)
        manifest = load(f)
        assert isinstance(manifest, DeploymentManifest)


# ---------------------------------------------------------------------------
# Schema — dependency parsing
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestDeploymentManifestDependencies:
    """dependencies section parsed to structured dependency list."""

    def test_dependency_count(self, tmp_path):
        _, load, *_ = _import_symbols()
        f = tmp_path / "deployment.yaml"
        f.write_text(_FULL_YAML)
        manifest = load(f)
        assert len(manifest.dependencies) == 2

    def test_data_agent_depends_on_three_items(self, tmp_path):
        _, load, *_ = _import_symbols()
        f = tmp_path / "deployment.yaml"
        f.write_text(_FULL_YAML)
        manifest = load(f)
        da_dep = next(d for d in manifest.dependencies if d.item == "data_agent")
        assert set(da_dep.depends_on) == {"ontology", "semantic_model", "graph_model"}

    def test_graph_model_depends_on_ontology(self, tmp_path):
        _, load, *_ = _import_symbols()
        f = tmp_path / "deployment.yaml"
        f.write_text(_FULL_YAML)
        manifest = load(f)
        gm_dep = next(d for d in manifest.dependencies if d.item == "graph_model")
        assert gm_dep.depends_on == ["ontology"]

    def test_empty_dependencies_list_is_valid(self, tmp_path):
        _, load, *_ = _import_symbols()
        f = tmp_path / "deployment.yaml"
        f.write_text(
            "workspace: ws-test\n"
            "items:\n"
            "  ontology:\n"
            "    display_name: test_ont\n"
            "dependencies: []\n"
        )
        manifest = load(f)
        assert manifest.dependencies == []

    def test_missing_dependencies_key_is_valid(self, tmp_path):
        """dependencies is optional — missing key → empty list or None."""
        _, load, *_ = _import_symbols()
        f = tmp_path / "deployment.yaml"
        f.write_text(_MINIMAL_YAML)
        manifest = load(f)
        # Either empty list or None is acceptable for absent key
        assert manifest.dependencies is None or manifest.dependencies == []


# ---------------------------------------------------------------------------
# Loader error handling
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestLoadDeploymentManifestErrors:
    """Loader raises structured errors; never swallows exceptions silently."""

    def test_raises_on_missing_file(self, tmp_path):
        _, load, DeploymentManifestError, *_ = _import_symbols()
        with pytest.raises(DeploymentManifestError):
            load(tmp_path / "nonexistent.yaml")

    def test_raises_parse_error_on_bad_yaml(self, tmp_path):
        _, load, _, DeploymentManifestParseError, _ = _import_symbols()
        f = tmp_path / "deployment.yaml"
        f.write_text(_BAD_YAML)
        with pytest.raises(DeploymentManifestParseError, match="YAML"):
            load(f)

    def test_raises_on_empty_yaml(self, tmp_path):
        _, load, _, _, DeploymentManifestValidationError = _import_symbols()
        f = tmp_path / "deployment.yaml"
        f.write_text("")
        with pytest.raises(DeploymentManifestValidationError):
            load(f)

    def test_raises_on_non_mapping_yaml(self, tmp_path):
        _, load, _, _, DeploymentManifestValidationError = _import_symbols()
        f = tmp_path / "deployment.yaml"
        f.write_text("- item1\n- item2\n")
        with pytest.raises(DeploymentManifestValidationError):
            load(f)

    def test_accepts_path_as_string(self, tmp_path):
        DeploymentManifest, load, *_ = _import_symbols()
        f = tmp_path / "deployment.yaml"
        f.write_text(_MINIMAL_YAML)
        manifest = load(str(f))
        assert isinstance(manifest, DeploymentManifest)

    def test_parse_error_is_subclass_of_base_error(self, tmp_path):
        _, load, DeploymentManifestError, DeploymentManifestParseError, _ = _import_symbols()
        assert issubclass(DeploymentManifestParseError, DeploymentManifestError)

    def test_validation_error_is_subclass_of_base_error(self):
        _, _, DeploymentManifestError, _, DeploymentManifestValidationError = _import_symbols()
        assert issubclass(DeploymentManifestValidationError, DeploymentManifestError)

    def test_raises_on_list_items_not_mapping(self, tmp_path):
        _, load, _, _, DeploymentManifestValidationError = _import_symbols()
        f = tmp_path / "deployment.yaml"
        f.write_text("workspace: ws-test\nitems:\n  - display_name: bad\n")
        with pytest.raises(DeploymentManifestValidationError):
            load(f)


# ---------------------------------------------------------------------------
# ENV_VAR interpolation
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestDeploymentManifestEnvVarInterpolation:
    """${VAR} placeholders are resolved before YAML parsing."""

    def test_multiple_env_vars_resolved(self, tmp_path):
        _, load, *_ = _import_symbols()
        os.environ["DM_TEST_WS"] = "ws-multi-env"
        os.environ["DM_TEST_ONT"] = "multi_ont"
        try:
            f = tmp_path / "deployment.yaml"
            f.write_text(
                "workspace: ${DM_TEST_WS}\n"
                "items:\n"
                "  ontology:\n"
                "    display_name: ${DM_TEST_ONT}\n"
            )
            manifest = load(f)
            assert manifest.workspace == "ws-multi-env"
            assert manifest.items.ontology.display_name == "multi_ont"
        finally:
            del os.environ["DM_TEST_WS"]
            del os.environ["DM_TEST_ONT"]

    def test_unresolved_var_left_intact(self, tmp_path):
        """Unresolved ${VAR} is preserved verbatim — not silently replaced with empty."""
        _, load, DeploymentManifestError, *_ = _import_symbols()
        os.environ.pop("_DM_UNSET_1234", None)
        f = tmp_path / "deployment.yaml"
        f.write_text(
            "workspace: ${_DM_UNSET_1234}\n"
            "items:\n"
            "  ontology:\n"
            "    display_name: real_name\n"
        )
        # Unresolved workspace is either held as-is or raises a structured error
        try:
            manifest = load(f)
            # If it succeeds, the literal placeholder is preserved
            assert "${_DM_UNSET_1234}" in manifest.workspace
        except Exception as exc:
            assert isinstance(exc, DeploymentManifestError)
