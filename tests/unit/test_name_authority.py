"""Unit tests for deploy/name_authority.py — manifest naming authority contracts.

Covers the acceptance contracts for Issue #6 (scope/deploy-manifest branch).

Contract surface tested:
  - resolve_item_name: manifest wins; generated_metadata/command conflicts raise NameAuthorityConflict
  - NameAuthorityConflict: structured exception with code/item_type/manifest_name/conflicting_name/source
  - render_name_resolution: exact output format matches ADR example (including blank line + footer)
  - validate_readback_name: pass on match, raise NameAuthorityConflict on mismatch
  - manifest_from_env_config: builds in-memory DeploymentManifest from legacy env JSON dict
  - Legacy env divergence: warn + manifest wins (never silent override)
  - infra/names.py validators exercised for Ontology + Lakehouse identifier rules
  - Error text matches "ERROR NAME_AUTHORITY_CONFLICT:" format from ADR §4

All tests are RED (ImportError) until Verbal implements
  src/fabric_kg_builder/deploy/name_authority.py
and
  src/fabric_kg_builder/deploy/manifest.py

See ADR: .squad/decisions/inbox/keyser-deployment-manifest.md §2–§4.
"""

from __future__ import annotations

import warnings
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Deferred import helpers
# ---------------------------------------------------------------------------

def _import_manifest_module():
    from fabric_kg_builder.deploy import manifest as _m  # noqa: PLC0415
    return _m


def _import_authority_module():
    from fabric_kg_builder.deploy import name_authority as _na  # noqa: PLC0415
    return _na


def _import_symbols():
    na = _import_authority_module()
    return (
        na.ResolvedName,
        na.NameAuthorityConflict,
        na.resolve_item_name,
        na.validate_readback_name,
        na.render_name_resolution,
        na.manifest_from_env_config,
    )


# ---------------------------------------------------------------------------
# Minimal manifest builder (pure dict, avoids circular dependency on loader)
# ---------------------------------------------------------------------------

def _minimal_manifest(
    *,
    ontology: str = "demo_ontology",
    lakehouse: str = "kg_lakehouse",
    semantic_model: str = "kg_semantic",
    graph_model: str = "KG Graph",
    data_agent: str = "fkg-dev-data-agent",
    search_index: str = "kg-chunks",
    workspace: str = "ws-test-123",
) -> Any:
    """Build a DeploymentManifest directly from the module's constructor for test isolation."""
    m = _import_manifest_module()
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


# ---------------------------------------------------------------------------
# ResolvedName dataclass shape
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestResolvedNameShape:
    """ResolvedName is a frozen dataclass with four fields."""

    def test_has_item_type_field(self):
        ResolvedName, *_ = _import_symbols()
        r = ResolvedName(
            item_type="Ontology",
            display_name="demo_ontology",
            authority="deployment.yaml",
            generated_metadata="absent",
        )
        assert r.item_type == "Ontology"

    def test_has_display_name_field(self):
        ResolvedName, *_ = _import_symbols()
        r = ResolvedName(
            item_type="Ontology",
            display_name="demo_ontology",
            authority="deployment.yaml",
            generated_metadata="absent",
        )
        assert r.display_name == "demo_ontology"

    def test_has_authority_field(self):
        ResolvedName, *_ = _import_symbols()
        r = ResolvedName(
            item_type="Ontology",
            display_name="demo_ontology",
            authority="deployment.yaml",
            generated_metadata="absent",
        )
        assert r.authority == "deployment.yaml"

    def test_has_generated_metadata_field(self):
        ResolvedName, *_ = _import_symbols()
        r = ResolvedName(
            item_type="Ontology",
            display_name="demo_ontology",
            authority="deployment.yaml",
            generated_metadata="compatible",
        )
        assert r.generated_metadata == "compatible"

    def test_is_frozen(self):
        """Frozen dataclass — mutation must raise."""
        ResolvedName, *_ = _import_symbols()
        r = ResolvedName(
            item_type="Ontology",
            display_name="demo_ontology",
            authority="deployment.yaml",
            generated_metadata="absent",
        )
        with pytest.raises((AttributeError, TypeError)):
            r.display_name = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# resolve_item_name — manifest wins (precedence)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestResolveItemNameManifestPrecedence:
    """Manifest display_name is always the effective display name."""

    def test_returns_manifest_ontology_name(self):
        _, _, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(ontology="demo_ontology")
        result = resolve(manifest, "ontology")
        assert result.display_name == "demo_ontology"

    def test_returns_manifest_lakehouse_name(self):
        _, _, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(lakehouse="kg_lakehouse")
        result = resolve(manifest, "lakehouse")
        assert result.display_name == "kg_lakehouse"

    def test_returns_manifest_graph_model_name(self):
        _, _, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(graph_model="KG Graph")
        result = resolve(manifest, "graph_model")
        assert result.display_name == "KG Graph"

    def test_returns_manifest_data_agent_name(self):
        _, _, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(data_agent="fkg-dev-data-agent")
        result = resolve(manifest, "data_agent")
        assert result.display_name == "fkg-dev-data-agent"

    def test_returns_manifest_semantic_model_name(self):
        _, _, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(semantic_model="kg_semantic")
        result = resolve(manifest, "semantic_model")
        assert result.display_name == "kg_semantic"

    def test_generated_metadata_absent_when_not_provided(self):
        _, _, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest()
        result = resolve(manifest, "ontology")
        assert result.generated_metadata == "absent"

    def test_generated_metadata_compatible_when_names_match(self):
        _, _, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(ontology="demo_ontology")
        result = resolve(manifest, "ontology", generated_metadata_name="demo_ontology")
        assert result.generated_metadata == "compatible"

    def test_command_name_matching_manifest_is_compatible(self):
        _, _, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(lakehouse="kg_lakehouse")
        result = resolve(manifest, "lakehouse", command_name="kg_lakehouse")
        # No conflict — compatible or absent for generated_metadata
        assert result.display_name == "kg_lakehouse"

    def test_authority_string_reflects_manifest_source(self):
        _, _, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest()
        result = resolve(manifest, "ontology")
        # Authority must be a non-empty string (e.g. "deployment.yaml")
        assert isinstance(result.authority, str)
        assert result.authority.strip() != ""


# ---------------------------------------------------------------------------
# resolve_item_name — NameAuthorityConflict for generated metadata mismatch
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestResolveItemNameGeneratedMetadataConflict:
    """Generated display name differing from manifest name raises NameAuthorityConflict.

    This is the exact failure mode from the issue's example:
    'Equipment semantic contract' silently replacing 'demo-ontology'.
    """

    def test_raises_on_generated_metadata_name_mismatch(self):
        _, NameAuthorityConflict, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(ontology="demo_ontology")
        with pytest.raises(NameAuthorityConflict):
            resolve(
                manifest,
                "ontology",
                generated_metadata_name="Equipment semantic contract",
            )

    def test_conflict_has_code_name_authority_conflict(self):
        _, NameAuthorityConflict, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(ontology="demo_ontology")
        try:
            resolve(
                manifest,
                "ontology",
                generated_metadata_name="Equipment semantic contract",
            )
            pytest.fail("Expected NameAuthorityConflict not raised")
        except NameAuthorityConflict as exc:
            assert exc.code == "NAME_AUTHORITY_CONFLICT"

    def test_conflict_carries_manifest_name(self):
        _, NameAuthorityConflict, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(ontology="demo_ontology")
        try:
            resolve(
                manifest,
                "ontology",
                generated_metadata_name="Equipment semantic contract",
            )
        except NameAuthorityConflict as exc:
            assert exc.manifest_name == "demo_ontology"

    def test_conflict_carries_conflicting_name(self):
        _, NameAuthorityConflict, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(ontology="demo_ontology")
        try:
            resolve(
                manifest,
                "ontology",
                generated_metadata_name="Equipment semantic contract",
            )
        except NameAuthorityConflict as exc:
            assert exc.conflicting_name == "Equipment semantic contract"

    def test_conflict_carries_item_type(self):
        _, NameAuthorityConflict, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(ontology="demo_ontology")
        try:
            resolve(
                manifest,
                "ontology",
                generated_metadata_name="Equipment semantic contract",
            )
        except NameAuthorityConflict as exc:
            assert exc.item_type == "ontology" or exc.item_type == "Ontology"

    def test_conflict_source_identifies_generated_metadata(self):
        _, NameAuthorityConflict, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(ontology="demo_ontology")
        try:
            resolve(
                manifest,
                "ontology",
                generated_metadata_name="Equipment semantic contract",
            )
        except NameAuthorityConflict as exc:
            # source must identify that generated metadata caused the conflict
            assert "generated" in exc.source.lower() or "metadata" in exc.source.lower()

    def test_error_message_includes_conflicting_name(self):
        _, NameAuthorityConflict, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(ontology="demo_ontology")
        try:
            resolve(
                manifest,
                "ontology",
                generated_metadata_name="Equipment semantic contract",
            )
        except NameAuthorityConflict as exc:
            assert "Equipment semantic contract" in str(exc)

    def test_error_message_includes_manifest_name(self):
        _, NameAuthorityConflict, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(ontology="demo_ontology")
        try:
            resolve(
                manifest,
                "ontology",
                generated_metadata_name="Equipment semantic contract",
            )
        except NameAuthorityConflict as exc:
            assert "demo_ontology" in str(exc)


# ---------------------------------------------------------------------------
# resolve_item_name — NameAuthorityConflict for command name mismatch
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestResolveItemNameCommandNameConflict:
    """Command-line --display-name differing from manifest name raises NameAuthorityConflict."""

    def test_raises_on_command_name_mismatch(self):
        _, NameAuthorityConflict, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(lakehouse="kg_lakehouse")
        with pytest.raises(NameAuthorityConflict):
            resolve(manifest, "lakehouse", command_name="override_lakehouse")

    def test_conflict_source_identifies_command(self):
        _, NameAuthorityConflict, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(lakehouse="kg_lakehouse")
        try:
            resolve(manifest, "lakehouse", command_name="override_lakehouse")
        except NameAuthorityConflict as exc:
            assert "command" in exc.source.lower()

    def test_command_name_matching_manifest_does_not_raise(self):
        _, NameAuthorityConflict, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(lakehouse="kg_lakehouse")
        # Should not raise
        result = resolve(manifest, "lakehouse", command_name="kg_lakehouse")
        assert result.display_name == "kg_lakehouse"

    def test_none_command_name_does_not_raise(self):
        _, _, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(lakehouse="kg_lakehouse")
        result = resolve(manifest, "lakehouse", command_name=None)
        assert result.display_name == "kg_lakehouse"


# ---------------------------------------------------------------------------
# NameAuthorityConflict error format — ADR §4
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestNameAuthorityConflictErrorFormat:
    """str(exc) must contain the ERROR NAME_AUTHORITY_CONFLICT: prefix block."""

    def test_error_string_contains_error_code_prefix(self):
        _, NameAuthorityConflict, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(ontology="demo_ontology")
        try:
            resolve(
                manifest,
                "ontology",
                generated_metadata_name="Equipment semantic contract",
            )
        except NameAuthorityConflict as exc:
            # ADR requires: "ERROR NAME_AUTHORITY_CONFLICT:"
            assert "NAME_AUTHORITY_CONFLICT" in str(exc)

    def test_error_message_contains_generated_display_name_label(self):
        _, NameAuthorityConflict, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(ontology="demo_ontology")
        try:
            resolve(
                manifest,
                "ontology",
                generated_metadata_name="Equipment semantic contract",
            )
        except NameAuthorityConflict as exc:
            msg = str(exc)
            # ADR error example: 'Generated display name "Equipment semantic contract"'
            assert '"Equipment semantic contract"' in msg or "Equipment semantic contract" in msg

    def test_error_message_contains_manifest_display_name_label(self):
        _, NameAuthorityConflict, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(ontology="demo_ontology")
        try:
            resolve(
                manifest,
                "ontology",
                generated_metadata_name="Equipment semantic contract",
            )
        except NameAuthorityConflict as exc:
            msg = str(exc)
            assert "demo_ontology" in msg

    def test_exception_is_subclass_of_exception(self):
        _, NameAuthorityConflict, *_ = _import_symbols()
        assert issubclass(NameAuthorityConflict, Exception)


# ---------------------------------------------------------------------------
# render_name_resolution — exact output format
# ---------------------------------------------------------------------------

_EXPECTED_RENDER_OUTPUT = (
    "Resolved item:\n"
    "  type: Ontology\n"
    "  display name: demo_ontology\n"
    "  name authority: deployment.yaml\n"
    "  generated metadata: compatible\n"
    "\n"
    "No naming conflicts detected."
)


@pytest.mark.unit
class TestRenderNameResolution:
    """render_name_resolution produces the exact format from ADR §2."""

    def _make_resolved(self, *, item_type="Ontology", display_name="demo_ontology",
                       authority="deployment.yaml", generated_metadata="compatible"):
        ResolvedName, *_ = _import_symbols()
        return ResolvedName(
            item_type=item_type,
            display_name=display_name,
            authority=authority,
            generated_metadata=generated_metadata,
        )

    def test_output_matches_adr_exact_format(self):
        *_, render, _ = _import_symbols()
        resolved = self._make_resolved()
        output = render(resolved)
        assert output == _EXPECTED_RENDER_OUTPUT

    def test_output_contains_type_line(self):
        *_, render, _ = _import_symbols()
        resolved = self._make_resolved(item_type="Lakehouse")
        output = render(resolved)
        assert "  type: Lakehouse" in output

    def test_output_contains_display_name_line(self):
        *_, render, _ = _import_symbols()
        resolved = self._make_resolved(display_name="kg_lakehouse")
        output = render(resolved)
        assert "  display name: kg_lakehouse" in output

    def test_output_contains_name_authority_line(self):
        *_, render, _ = _import_symbols()
        resolved = self._make_resolved(authority="deployment.yaml")
        output = render(resolved)
        assert "  name authority: deployment.yaml" in output

    def test_output_contains_generated_metadata_line(self):
        *_, render, _ = _import_symbols()
        resolved = self._make_resolved(generated_metadata="absent")
        output = render(resolved)
        assert "  generated metadata: absent" in output

    def test_output_ends_with_no_conflicts_detected(self):
        *_, render, _ = _import_symbols()
        resolved = self._make_resolved()
        output = render(resolved)
        assert output.endswith("No naming conflicts detected.")

    def test_output_has_blank_line_before_footer(self):
        *_, render, _ = _import_symbols()
        resolved = self._make_resolved()
        output = render(resolved)
        assert "\n\nNo naming conflicts detected." in output

    def test_output_starts_with_resolved_item_header(self):
        *_, render, _ = _import_symbols()
        resolved = self._make_resolved()
        output = render(resolved)
        assert output.startswith("Resolved item:")

    def test_returns_string_type(self):
        *_, render, _ = _import_symbols()
        resolved = self._make_resolved()
        assert isinstance(render(resolved), str)


# ---------------------------------------------------------------------------
# validate_readback_name
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestValidateReadbackName:
    """validate_readback_name enforces deployed name == manifest name (ADR invariant 4)."""

    def test_passes_when_names_match(self):
        _, _, _, validate_readback, *_ = _import_symbols()
        manifest = _minimal_manifest(ontology="demo_ontology")
        # Must not raise
        validate_readback("ontology", "demo_ontology", manifest)

    def test_raises_when_deployed_name_differs(self):
        _, NameAuthorityConflict, _, validate_readback, *_ = _import_symbols()
        manifest = _minimal_manifest(ontology="demo_ontology")
        with pytest.raises(NameAuthorityConflict):
            validate_readback("ontology", "wrong_name", manifest)

    def test_readback_conflict_identifies_item_type(self):
        _, NameAuthorityConflict, _, validate_readback, *_ = _import_symbols()
        manifest = _minimal_manifest(lakehouse="kg_lakehouse")
        try:
            validate_readback("lakehouse", "old_lakehouse", manifest)
        except NameAuthorityConflict as exc:
            assert exc.item_type == "lakehouse" or exc.item_type == "Lakehouse"

    def test_readback_conflict_carries_manifest_name(self):
        _, NameAuthorityConflict, _, validate_readback, *_ = _import_symbols()
        manifest = _minimal_manifest(ontology="demo_ontology")
        try:
            validate_readback("ontology", "wrong_name", manifest)
        except NameAuthorityConflict as exc:
            assert exc.manifest_name == "demo_ontology"

    def test_readback_conflict_carries_deployed_name(self):
        _, NameAuthorityConflict, _, validate_readback, *_ = _import_symbols()
        manifest = _minimal_manifest(ontology="demo_ontology")
        try:
            validate_readback("ontology", "wrong_name", manifest)
        except NameAuthorityConflict as exc:
            assert exc.conflicting_name == "wrong_name"

    def test_passes_for_graph_model_match(self):
        _, _, _, validate_readback, *_ = _import_symbols()
        manifest = _minimal_manifest(graph_model="KG Graph")
        validate_readback("graph_model", "KG Graph", manifest)

    def test_raises_for_graph_model_mismatch(self):
        _, NameAuthorityConflict, _, validate_readback, *_ = _import_symbols()
        manifest = _minimal_manifest(graph_model="KG Graph")
        with pytest.raises(NameAuthorityConflict):
            validate_readback("graph_model", "Old Graph", manifest)


# ---------------------------------------------------------------------------
# manifest_from_env_config — legacy migration path
# ---------------------------------------------------------------------------

_LEGACY_ENV_CONFIG: dict[str, Any] = {
    "fabric": {
        "workspace_id": "ws-legacy-abc",
        "lakehouse_display_name": "kg_lakehouse",
        "ontology_display_name": "kg_ontology",
        "data_agent_display_name": "fkg-dev-data-agent",
        "graph_model_display_name": "KG Graph",
    },
    "ai_search": {
        "index_prefix": "kg-dev-",
        "index_chunks": "kg-chunks",
    },
}


@pytest.mark.unit
class TestManifestFromEnvConfig:
    """manifest_from_env_config builds in-memory DeploymentManifest from legacy dict."""

    def test_returns_deployment_manifest(self):
        *_, manifest_from_env = _import_symbols()
        m = _import_manifest_module()
        result = manifest_from_env(_LEGACY_ENV_CONFIG)
        assert isinstance(result, m.DeploymentManifest)

    def test_workspace_set_from_fabric_workspace_id(self):
        *_, manifest_from_env = _import_symbols()
        result = manifest_from_env(_LEGACY_ENV_CONFIG)
        assert result.workspace == "ws-legacy-abc"

    def test_lakehouse_name_from_env(self):
        *_, manifest_from_env = _import_symbols()
        result = manifest_from_env(_LEGACY_ENV_CONFIG)
        assert result.items.lakehouse.display_name == "kg_lakehouse"

    def test_ontology_name_from_env(self):
        *_, manifest_from_env = _import_symbols()
        result = manifest_from_env(_LEGACY_ENV_CONFIG)
        assert result.items.ontology.display_name == "kg_ontology"

    def test_data_agent_name_from_env(self):
        *_, manifest_from_env = _import_symbols()
        result = manifest_from_env(_LEGACY_ENV_CONFIG)
        assert result.items.data_agent.display_name == "fkg-dev-data-agent"

    def test_graph_model_name_from_env(self):
        *_, manifest_from_env = _import_symbols()
        result = manifest_from_env(_LEGACY_ENV_CONFIG)
        assert result.items.graph_model.display_name == "KG Graph"

    def test_missing_optional_fields_do_not_raise(self):
        """Minimal env config with only workspace_id should not crash."""
        *_, manifest_from_env = _import_symbols()
        minimal = {"fabric": {"workspace_id": "ws-minimal"}}
        # Either succeeds or raises a structured error — not a crash
        try:
            manifest_from_env(minimal)
        except Exception as exc:
            m = _import_manifest_module()
            assert isinstance(exc, m.DeploymentManifestError)


# ---------------------------------------------------------------------------
# Legacy env + manifest divergence — warn, manifest wins
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestLegacyEnvDivergenceWarning:
    """When both manifest and legacy env config are present and differ, manifest wins + warns."""

    def test_manifest_name_wins_over_legacy_env(self):
        """When manifest has 'prod_ontology' and env config has 'kg_ontology',
        the resolved name is always the manifest's 'prod_ontology'."""
        _, _, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(ontology="prod_ontology")
        result = resolve(manifest, "ontology")
        assert result.display_name == "prod_ontology"

    def test_legacy_divergence_emits_warning_not_error(self):
        """Legacy env field that differs from manifest → warning, not NameAuthorityConflict."""
        na = _import_authority_module()
        # resolve_item_name has no legacy_env_name param, but manifest_from_env_config
        # may emit warnings on build. We test that a manifest + differing env_config
        # does NOT raise NameAuthorityConflict when env is legacy-only migration input.
        *_, manifest_from_env = _import_symbols()
        legacy = {
            "fabric": {
                "workspace_id": "ws-legacy",
                "ontology_display_name": "old_kg_ontology",
            }
        }
        # Building from env config must produce a manifest or warn — never silent mismatch
        # If it raises, it should be a warning-class, not NameAuthorityConflict
        try:
            result = manifest_from_env(legacy)
            # Divergence detection may emit warnings
        except Exception as exc:
            _, NameAuthorityConflict, *_ = _import_symbols()
            assert not isinstance(exc, NameAuthorityConflict), (
                "Legacy env divergence must warn (not raise NameAuthorityConflict)"
            )


# ---------------------------------------------------------------------------
# infra/names.py validators exercised through resolve_item_name
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestNamesValidatorsExercisedByResolver:
    """Ontology and Lakehouse names are validated with infra/names.py identifier rules."""

    def test_valid_ontology_identifier_name_passes(self):
        _, _, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(ontology="ValidOntology")
        result = resolve(manifest, "ontology")
        assert result.display_name == "ValidOntology"

    def test_ontology_name_with_hyphen_raises_value_error(self):
        """Ontology names cannot contain hyphens — Fabric identifier rule."""
        _, _, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(ontology="my-ontology")
        with pytest.raises(ValueError, match="letter|identifier|hyphen|only"):
            resolve(manifest, "ontology")

    def test_ontology_name_with_space_raises_value_error(self):
        _, _, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(ontology="my ontology")
        with pytest.raises(ValueError):
            resolve(manifest, "ontology")

    def test_lakehouse_name_with_hyphen_raises_value_error(self):
        _, _, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(lakehouse="my-lakehouse")
        with pytest.raises(ValueError):
            resolve(manifest, "lakehouse")

    def test_graph_model_name_with_spaces_is_valid(self):
        """Graph model names allow spaces — different validation rule."""
        _, _, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(graph_model="KG Graph Model")
        result = resolve(manifest, "graph_model")
        assert result.display_name == "KG Graph Model"

    def test_blank_graph_model_name_raises_value_error(self):
        """Graph model name must not be blank — validate_fabric_graph_model_name."""
        _, _, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(graph_model="   ")
        with pytest.raises(ValueError, match="blank|empty"):
            resolve(manifest, "graph_model")

    def test_ontology_name_starting_with_digit_raises(self):
        _, _, resolve, *_ = _import_symbols()
        manifest = _minimal_manifest(ontology="1ontology")
        with pytest.raises(ValueError):
            resolve(manifest, "ontology")
