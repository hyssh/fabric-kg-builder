"""Tests for infra/manifest.py — YAML loader with env var interpolation."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from fabric_kg_builder.infra.manifest import (
    InfraManifestError,
    InfraManifestParseError,
    InfraManifestValidationError,
    _resolve_env_vars,
    default_manifest_path,
    load_manifest,
)
from fabric_kg_builder.infra.schema import InfraManifest


_MINIMAL_YAML = """\
environment: dev
azure:
  subscription_id: sub-abc
"""

_YAML_WITH_ENV_VARS = """\
environment: dev
azure:
  subscription_id: ${AZURE_SUBSCRIPTION_ID}
"""


class TestResolveEnvVars:
    def test_replaces_variable(self):
        os.environ["TEST_VAR_XYZ"] = "hello"
        result = _resolve_env_vars("value: ${TEST_VAR_XYZ}")
        assert "hello" in result
        del os.environ["TEST_VAR_XYZ"]

    def test_leaves_unresolved_intact(self):
        result = _resolve_env_vars("${UNRESOLVED_VAR_NOPE}")
        assert result == "${UNRESOLVED_VAR_NOPE}"

    def test_multiple_vars(self):
        os.environ["VAR_A"] = "alpha"
        os.environ["VAR_B"] = "beta"
        result = _resolve_env_vars("${VAR_A}-${VAR_B}")
        assert result == "alpha-beta"
        del os.environ["VAR_A"]
        del os.environ["VAR_B"]

    def test_no_vars_unchanged(self):
        text = "plain text without variables"
        assert _resolve_env_vars(text) == text


class TestDefaultManifestPath:
    def test_returns_path(self):
        p = default_manifest_path("dev")
        assert isinstance(p, Path)
        assert "dev.yaml" in str(p)

    def test_environment_in_path(self):
        p = default_manifest_path("production")
        assert "production" in str(p)


class TestLoadManifest:
    def test_loads_minimal_yaml(self, tmp_path):
        f = tmp_path / "dev.yaml"
        f.write_text(_MINIMAL_YAML)
        manifest = load_manifest(f)
        assert isinstance(manifest, InfraManifest)
        assert manifest.environment == "dev"
        assert manifest.azure.subscription_id == "sub-abc"

    def test_resolves_env_vars(self, tmp_path):
        os.environ["TEST_SUB_ID"] = "sub-from-env"
        f = tmp_path / "dev.yaml"
        f.write_text("""\
environment: dev
azure:
  subscription_id: ${TEST_SUB_ID}
""")
        manifest = load_manifest(f)
        assert manifest.azure.subscription_id == "sub-from-env"
        del os.environ["TEST_SUB_ID"]

    def test_raises_on_missing_file(self, tmp_path):
        with pytest.raises(InfraManifestError, match="Cannot read"):
            load_manifest(tmp_path / "nonexistent.yaml")

    def test_raises_on_invalid_yaml(self, tmp_path):
        f = tmp_path / "dev.yaml"
        f.write_text("invalid: yaml: [bad\n  structure: {")
        with pytest.raises(InfraManifestParseError, match="YAML syntax error"):
            load_manifest(f)

    def test_raises_on_empty_yaml(self, tmp_path):
        f = tmp_path / "dev.yaml"
        f.write_text("")
        with pytest.raises(InfraManifestValidationError, match="empty"):
            load_manifest(f)

    def test_raises_on_non_mapping_yaml(self, tmp_path):
        f = tmp_path / "dev.yaml"
        f.write_text("- item1\n- item2\n")
        with pytest.raises(InfraManifestValidationError, match="mapping"):
            load_manifest(f)

    def test_raises_on_missing_required_field(self, tmp_path):
        f = tmp_path / "dev.yaml"
        f.write_text("environment: dev\n")  # missing azure
        with pytest.raises(InfraManifestValidationError, match="failed validation"):
            load_manifest(f)

    def test_accepts_path_as_string(self, tmp_path):
        f = tmp_path / "dev.yaml"
        f.write_text(_MINIMAL_YAML)
        manifest = load_manifest(str(f))
        assert manifest.environment == "dev"
